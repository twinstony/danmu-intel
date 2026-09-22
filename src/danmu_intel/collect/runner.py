"""采集会话：把适配器事件流落到 append-only JSONL，并写库（设计 §5/§7）。

T1 是**单房间、前台进程**的最薄版本：一条命令跑完一段时间即退出，退出时把
本次涉及的文件封存（SHA256 + 条数 + 首末时间）写入 `danmu_segments`。
断流由 `adapter.reconnecting` 负责；进程级监督、心跳与重启上限属于 T2。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator

from danmu_intel.collect.adapter import Adapter, Probe, RoomKey
from danmu_intel.common import db as db_module
from danmu_intel.common import paths
from danmu_intel.common.events import DanmuEvent, JsonlAppender, iter_events

logger = logging.getLogger(__name__)

DISCOVERED_BY_MANUAL = "manual"  # T1 只支持人工登记（FR-C1-3 第一优先级）


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


async def run_session(
    room: RoomKey,
    *,
    adapter: Adapter | None = None,
    match_id: int | None = None,
    seconds: float | None = None,
    conn: sqlite3.Connection | None = None,
    data_root: Path | None = None,
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
        session_id = int(
            conn.execute(
                """
                INSERT INTO room_sessions(room_id, match_id, pid, started_at, state, last_msg_at)
                VALUES(?, ?, ?, ?, 'running', ?)
                """,
                (room_row_id, match_id, os.getpid(), now_ms(), None),
            ).lastrowid
        )
        conn.commit()

        touched: dict[Path, JsonlAppender] = {}
        count = 0
        last_ts: int | None = None
        state = "exited"
        try:
            async for event in _until_deadline(adapter.stream(room), seconds):
                event = event.with_match(match_id)
                path = paths.raw_path(event.platform, event.room_id, event.ts)
                appender = touched.get(path)
                if appender is None:
                    appender = JsonlAppender(path)
                    touched[path] = appender
                appender.append(event)
                count += 1
                last_ts = event.ts
        except Exception:
            state = "stalled"
            raise
        finally:
            for appender in touched.values():
                appender.close()
            segments = [seal_segment(conn, session_id, path, data_root=root) for path in touched]
            conn.execute(
                "UPDATE room_sessions SET ended_at=?, state=?, last_msg_at=? WHERE id=?",
                (now_ms(), state, last_ts, session_id),
            )
            conn.commit()
        return SessionResult(
            session_id=session_id,
            room_row_id=room_row_id,
            msg_count=count,
            segments=segments,
            probe=probe,
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
