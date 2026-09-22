"""采集会话：把适配器事件流落到 append-only JSONL，并写库（设计 §5/§7）。

**一个房间一个进程**：本模块就是那个子进程（`danmu-intel collect`）。它做三件事：

1. 落盘（append-only JSONL）+ 封存（`danmu_segments`）；
2. 每 5 秒写心跳（`room_sessions` 行 + `runtime/heartbeat/<platform>-<room>.json`），
   状态在 `connecting → running → stalled/no_stream → exited` 之间走；
3. 异常不静默（`collect/incidents.py`）：`no_stream` / `stalled` / `disk_low`。

进程级监督（一房间一子进程、重启退避、重启上限）在 `collect/supervisor.py`；
它把「第几次重启 + 已累计重连数」用环境变量接力给本进程。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator

from danmu_intel.collect.adapter import Adapter, Probe, RoomKey, SILENCE_TIMEOUT_S
from danmu_intel.collect.heartbeat import (
    DISK_FREE_MIN_BYTES,
    HEARTBEAT_INTERVAL_S,
    Heartbeat,
    Supervision,
    disk_low,
    free_bytes,
    supervision_state,
    write_heartbeat,
)
from danmu_intel.collect.incidents import (
    DISK_LOW,
    NO_STREAM,
    STALLED,
    SessionIncidents,
    worst,
)
from danmu_intel.common import db as db_module
from danmu_intel.common import paths
from danmu_intel.common.events import DanmuEvent, JsonlAppender, iter_events

logger = logging.getLogger(__name__)

DISCOVERED_BY_MANUAL = "manual"  # T1 只支持人工登记（FR-C1-3 第一优先级）
NO_FIRST_MSG_S = 120.0  # 无首条消息即判 `no_stream`（设计 §7.3）


@dataclass(frozen=True, slots=True)
class SealedSegment:
    rel_path: str
    msg_count: int
    sha256: str
    first_ts: int | None
    last_ts: int | None


@dataclass(frozen=True, slots=True)
class SessionResult:
    session_id: int
    room_row_id: int
    msg_count: int
    segments: list[SealedSegment] = field(default_factory=list)
    probe: Probe | None = None
    state: str = "exited"
    reconnects: int = 0
    incidents: list[str] = field(default_factory=list)


def now_ms() -> int:
    return int(time.time() * 1000)


def upsert_room(conn: sqlite3.Connection, room: RoomKey, probe: Probe | None) -> int:
    conn.execute(
        """
        INSERT INTO rooms(platform, room_id, url, streamer, discovered_by, is_live, last_seen_at)
        VALUES(?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(platform, room_id) DO UPDATE SET
          url=excluded.url,
          streamer=COALESCE(excluded.streamer, rooms.streamer),
          is_live=excluded.is_live,
          last_seen_at=excluded.last_seen_at
        """,
        (
            room.platform,
            room.room_id,
            room.url,
            probe.streamer if probe else None,
            DISCOVERED_BY_MANUAL,
            1 if probe and probe.is_live else 0,
            now_ms(),
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM rooms WHERE platform=? AND room_id=?", (room.platform, room.room_id)
    ).fetchone()
    return int(row["id"])


def seal_segment(
    conn: sqlite3.Connection, session_id: int, path: Path, *, data_root: Path | None = None
) -> SealedSegment:
    """封存一个落盘文件：整文件 SHA256 + 条数 + 首末时间，写入 `danmu_segments`。"""
    root = data_root or paths.data_dir()
    rel_path = path.resolve().relative_to(root.resolve()).as_posix()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    count = 0
    first_ts: int | None = None
    last_ts: int | None = None
    for _, event in iter_events(path):
        count += 1
        first_ts = event.ts if first_ts is None else first_ts
        last_ts = event.ts
    conn.execute(
        """
        INSERT INTO danmu_segments(room_session_id, rel_path, sha256, first_ts, last_ts, msg_count, sealed_at)
        VALUES(?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(rel_path) DO UPDATE SET
          room_session_id=excluded.room_session_id, sha256=excluded.sha256,
          first_ts=excluded.first_ts, last_ts=excluded.last_ts,
          msg_count=excluded.msg_count, sealed_at=excluded.sealed_at
        """,
        (session_id, rel_path, digest, first_ts, last_ts, count, now_ms()),
    )
    conn.commit()
    return SealedSegment(rel_path, count, digest, first_ts, last_ts)


async def _until_deadline(source: AsyncIterator[DanmuEvent], seconds: float | None) -> AsyncIterator[DanmuEvent]:
    """按秒数截断无限事件流；没有新弹幕时也要能按时收工。"""
    iterator = source.__aiter__()
    deadline = None if not seconds else time.monotonic() + seconds
    try:
        while True:
            timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
            if timeout is not None and timeout <= 0:
                return
            try:
                event = await asyncio.wait_for(iterator.__anext__(), timeout)
            except (StopAsyncIteration, asyncio.TimeoutError):
                return
            yield event
    finally:
        aclose = getattr(iterator, "aclose", None)
        if aclose is not None:
            await aclose()


@dataclass
class SessionStats:
    """会话的实时状态（心跳每 5 秒把它写进库与心跳文件）。"""

    state: str = "connecting"
    msg_count: int = 0
    last_msg_at: int | None = None
    reconnects: int = 0
    severity: str = "info"


class SessionRuntime:
    """子进程侧的会话运行时：心跳发布、状态判定、异常事件出口。

    状态机（设计 §7.3）：`connecting` → 首条弹幕 → `running`；60 秒无消息由
    `adapter.reconnecting` 触发重连，回调把状态标成 `stalled`；120 秒没有首条
    消息则标成 `no_stream`（事件照报，进程不退——房间可能只是还没开播）。
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        session_id: int,
        room: RoomKey,
        match_id: int | None,
        data_root: Path,
        counts: Supervision | None = None,
        started_at: int | None = None,
        interval: float | None = None,
        incidents: SessionIncidents | None = None,
    ) -> None:
        self.conn = conn
        self.session_id = session_id
        self.room = room
        self.match_id = match_id
        self.data_root = data_root
        self.counts = counts or Supervision()
        self.started_at = started_at if started_at is not None else now_ms()
        self.interval = HEARTBEAT_INTERVAL_S if interval is None else interval
        self.stats = SessionStats(reconnects=self.counts.reconnects)
        self.incidents = incidents or SessionIncidents(
            conn, platform=room.platform, room_id=room.room_id, match_id=match_id
        )
        self.reported: list[str] = []

    # —— 流层事件的入口 ——

    def note_message(self, event: DanmuEvent) -> None:
        self.stats.msg_count += 1
        self.stats.last_msg_at = event.ts
        self.stats.state = "running"

    def note_reconnect(self, reason: str) -> None:
        """重连回调：累计 `reconnects`、标 `stalled`，静默断流立即报事件。"""
        self.stats.reconnects += 1
        self.stats.state = "stalled"
        if reason == "silence":
            self._report(STALLED, "warning", {"silence_s": SILENCE_TIMEOUT_S})
        self.publish()

    # —— 周期性检查 ——

    def check_first_message(self, timestamp: int | None = None) -> bool:
        """120 秒仍无首条消息 → `no_stream`（不静默）。"""
        moment = timestamp if timestamp is not None else now_ms()
        if self.stats.msg_count or self.stats.state == "no_stream":
            return False
        if moment - self.started_at < NO_FIRST_MSG_S * 1000:
            return False
        self.stats.state = "no_stream"
        self._report(NO_STREAM, "warning", {"wait_s": NO_FIRST_MSG_S})
        return True

    def check_disk(self) -> bool:
        """数据盘可用空间低于下限 → `disk_low`（不静默）。"""
        if not disk_low(self.data_root):
            return False
        self._report(
            DISK_LOW,
            "critical",
            {"free_bytes": free_bytes(self.data_root), "minimum_bytes": DISK_FREE_MIN_BYTES},
        )
        return True

    def _report(self, kind: str, severity: str, detail: dict[str, object]) -> None:
        self.stats.severity = worst(self.stats.severity, severity)
        if self.incidents.emit_once(kind, severity=severity, detail=detail):
            self.reported.append(kind)

    # —— 心跳发布 ——

    def publish(self) -> None:
        """写 `room_sessions` 行 + 心跳文件（心跳文件给 supervisor 判活）。"""
        self.conn.execute(
            "UPDATE room_sessions SET state=?, last_msg_at=?, reconnects=?, severity=? WHERE id=?",
            (
                self.stats.state,
                self.stats.last_msg_at,
                self.stats.reconnects,
                self.stats.severity,
                self.session_id,
            ),
        )
        self.conn.commit()
        write_heartbeat(
            Heartbeat(
                pid=os.getpid(),
                session_id=self.session_id,
                platform=self.room.platform,
                room_id=self.room.room_id,
                state=self.stats.state,
                started_at=self.started_at,
                last_msg_at=self.stats.last_msg_at,
                msg_count=self.stats.msg_count,
                reconnects=self.stats.reconnects,
                restart_count=self.counts.restart_count,
                written_at=now_ms(),
            ),
            data_root=self.data_root,
        )

    async def run(self) -> None:
        """心跳循环：每 `interval` 秒检查一次并写心跳。"""
        while True:
            await asyncio.sleep(self.interval)
            self.check_first_message()
            self.check_disk()
            self.publish()

    def finish(self, state: str) -> None:
        """收工：落下最终状态（含 `exited`）并再发一次心跳。"""
        self.stats.state = state
        self.conn.execute(
            "UPDATE room_sessions SET ended_at=?, state=?, last_msg_at=?, reconnects=?, severity=? WHERE id=?",
            (
                now_ms(),
                state,
                self.stats.last_msg_at,
                self.stats.reconnects,
                self.stats.severity,
                self.session_id,
            ),
        )
        self.conn.commit()
        self.publish()


async def run_session(
    room: RoomKey,
    *,
    adapter: Adapter | None = None,
    match_id: int | None = None,
    seconds: float | None = None,
    conn: sqlite3.Connection | None = None,
    data_root: Path | None = None,
    heartbeat_interval: float | None = None,
) -> SessionResult:
    """采集一个房间：`seconds=None` 表示一直采到进程被停止。"""
    from danmu_intel.collect import get_adapter

    adapter = adapter or get_adapter(room.platform)
    root = data_root or paths.data_dir()
    own_conn = conn is None
    conn = conn or db_module.open_db(root / "db.sqlite3")
    try:
        try:
            probe: Probe | None = await adapter.probe(room)
        except Exception as exc:  # 探测失败不影响采集（页面协议变更不得导致停采）
            logger.warning("【%s/%s】房间探测失败：%s", room.platform, room.room_id, exc)
            probe = None
        room_row_id = upsert_room(conn, room, probe)
        counts = supervision_state()
        session_id = int(
            conn.execute(
                """
                INSERT INTO room_sessions(room_id, match_id, pid, started_at, state, restart_count,
                                         reconnects, severity, last_msg_at)
                VALUES(?, ?, ?, ?, 'connecting', ?, ?, 'info', ?)
                """,
                (
                    room_row_id,
                    match_id,
                    os.getpid(),
                    now_ms(),
                    counts.restart_count,
                    counts.reconnects,
                    None,
                ),
            ).lastrowid
        )
        conn.commit()

        runtime = SessionRuntime(
            conn,
            session_id=session_id,
            room=room,
            match_id=match_id,
            data_root=root,
            counts=counts,
            interval=heartbeat_interval,
        )
        runtime.publish()  # 先亮心跳：supervisor 一启动就能看见这个房间活着
        heartbeat = asyncio.create_task(runtime.run())

        touched: dict[Path, JsonlAppender] = {}
        count = 0
        last_ts: int | None = None
        state = "exited"
        try:
            async for event in _until_deadline(
                adapter.stream(room, on_reconnect=runtime.note_reconnect), seconds
            ):
                event = event.with_match(match_id)
                path = paths.raw_path(event.platform, event.room_id, event.ts)
                appender = touched.get(path)
                if appender is None:
                    appender = JsonlAppender(path)
                    touched[path] = appender
                appender.append(event)
                count += 1
                last_ts = event.ts
                runtime.note_message(event)
        except Exception:
            state = "stalled"
            raise
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            for appender in touched.values():
                appender.close()
            segments = [seal_segment(conn, session_id, path, data_root=root) for path in touched]
            runtime.finish(state)
        return SessionResult(
            session_id=session_id,
            room_row_id=room_row_id,
            msg_count=count,
            segments=segments,
            probe=probe,
            state=state,
            reconnects=runtime.stats.reconnects,
            incidents=list(runtime.reported),
        )
    finally:
        if own_conn:
            conn.close()


def match_segment_paths(conn: sqlite3.Connection, match_id: int) -> list[str]:
    """该场比赛涉及的全部落盘文件（按采集会话顺序）。"""
    rows = conn.execute(
        """
        SELECT seg.rel_path AS rel_path
        FROM danmu_segments seg
        JOIN room_sessions s ON s.id = seg.room_session_id
        WHERE s.match_id = ?
        ORDER BY s.id, seg.rel_path
        """,
        (match_id,),
    ).fetchall()
    return [row["rel_path"] for row in rows]


def read_raw_events(
    rel_paths: list[str], *, data_root: Path | None = None
) -> list[tuple[str, int, DanmuEvent]]:
    """按落盘顺序读回原始记录，附带 `(rel_path, 行号)` 以便溯源。"""
    root = data_root or paths.data_dir()
    collected: list[tuple[str, int, DanmuEvent]] = []
    for rel_path in rel_paths:
        for line_no, event in iter_events(root / rel_path):
            collected.append((rel_path, line_no, event))
    return collected
