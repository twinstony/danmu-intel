"""采集健康状态与每房间贡献量（FR-C1-7 可见性；AC-15 贡献量）。

两件事都在**同一个只读入口**里：`room_health` 回答「现在采得怎么样」（进程/状态/
最后一条消息/重连/重启/严重级别/最近异常），`room_contribution` 回答「每个直播间
给这场比赛贡献了多少」（条数 / 时间跨度 / 去重后条数）。

健康看两处：`room_sessions` 行是**durable 事实**（进程死了也还在），心跳文件是
**活体事实**（只有 pid 还活着才算数，否则会把上一轮残留的进程号当成现在）。
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from danmu_intel.collect.incidents import Incident, recent
from danmu_intel.common import paths
from danmu_intel.common.events import dedupe, iter_events

NO_SESSION = "none"


@dataclass(frozen=True, slots=True)
class RoomHealth:
    """一个直播间在当前比赛里的采集健康状态。"""

    platform: str
    room_id: str
    url: str
    streamer: str | None
    is_live: bool
    session_id: int | None
    pid: int | None
    state: str
    started_at: int | None
    ended_at: int | None
    last_msg_at: int | None
    msg_count: int
    reconnects: int
    restart_count: int
    severity: str
    heartbeat_age_ms: int | None
    last_incident: Incident | None


@dataclass(frozen=True, slots=True)
class RoomContribution:
    """一个直播间对某场比赛的贡献量（`platform + room_id` 是去重键的一部分）。"""

    platform: str
    room_id: str
    msg_count: int
    deduped_count: int
    duplicate_count: int
    first_ts: int | None
    last_ts: int | None
    session_count: int


def pid_alive(pid: int | None) -> bool:
    """进程是否还活着（`kill -0` 语义；权限不足也算活着）。"""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _latest_sessions(conn: sqlite3.Connection, match_id: int) -> list[sqlite3.Row]:
    # 显式列名：`room_sessions.room_id` 是外键整数，不能和 `rooms.room_id`（房间号）撞名
    return conn.execute(
        """
        SELECT s.id AS session_id, s.pid AS pid, s.started_at AS started_at, s.ended_at AS ended_at,
               s.state AS state, s.restart_count AS restart_count, s.reconnects AS reconnects,
               s.severity AS severity, s.last_msg_at AS last_msg_at,
               r.platform AS platform, r.room_id AS room_key, r.url AS url,
               r.streamer AS streamer, r.is_live AS is_live
        FROM room_sessions s
        JOIN rooms r ON r.id = s.room_id
        WHERE s.match_id = ? AND s.id = (
              SELECT MAX(inner_s.id) FROM room_sessions inner_s WHERE inner_s.room_id = s.room_id
        )
        ORDER BY r.platform, r.room_id
        """,
        (match_id,),
    ).fetchall()


def _session_msg_count(conn: sqlite3.Connection, session_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(msg_count), 0) AS n FROM danmu_segments WHERE room_session_id=?",
        (session_id,),
    ).fetchone()
    return int(row["n"])


def room_health(
    conn: sqlite3.Connection,
    match_id: int,
    *,
    data_root: Path | None = None,
    now: int | None = None,
) -> list[RoomHealth]:
    """本场比赛每个直播间的最新健康状态（新→旧无关，按平台/房间排序）。"""
    from danmu_intel.collect.heartbeat import read_room_heartbeat

    root = data_root or paths.data_dir()
    moment = now if now is not None else int(time.time() * 1000)
    health: list[RoomHealth] = []
    for row in _latest_sessions(conn, match_id):
        session_id = int(row["session_id"])
        from danmu_intel.collect.adapter import RoomKey

        room = RoomKey(platform=row["platform"], room_id=row["room_key"], url=row["url"])
        beat = read_room_heartbeat(room, data_root=root)
        live = (
            beat is not None
            and beat.session_id == session_id
            and pid_alive(beat.pid)
        )
        incidents = recent(conn, room_id=row["room_key"], limit=1)
        health.append(
            RoomHealth(
                platform=row["platform"],
                room_id=row["room_key"],
                url=row["url"],
                streamer=row["streamer"],
                is_live=bool(row["is_live"]),
                session_id=session_id,
                pid=(beat.pid if live and beat is not None else row["pid"]),
                state=beat.state if live and beat is not None else row["state"],
                started_at=row["started_at"],
                ended_at=None if live else row["ended_at"],
                last_msg_at=(beat.last_msg_at if live and beat is not None else row["last_msg_at"]),
                msg_count=(beat.msg_count if live and beat is not None else _session_msg_count(conn, session_id)),
                reconnects=(beat.reconnects if live and beat is not None else int(row["reconnects"])),
                restart_count=int(row["restart_count"]),
                severity=row["severity"],
                heartbeat_age_ms=None if beat is None else moment - beat.written_at,
                last_incident=incidents[0] if incidents else None,
            )
        )
    return health


def room_contribution(
    conn: sqlite3.Connection, match_id: int, *, data_root: Path | None = None
) -> list[RoomContribution]:
    """按直播间统计本场的落盘条数、时间跨度与**去重后**条数（AC-15）。

    去重键是 `(platform, room_id, msg_hash)`：同一房间内重复落盘的记录只算一条，
    不同房间的同文弹幕各算一条（它们是两份独立证据）。
    """
    root = data_root or paths.data_dir()
    rows = conn.execute(
        """
        SELECT r.platform AS platform, r.room_id AS room_id,
               seg.rel_path AS rel_path, s.id AS session_id
        FROM danmu_segments seg
        JOIN room_sessions s ON s.id = seg.room_session_id
        JOIN rooms r ON r.id = s.room_id
        WHERE s.match_id = ?
        ORDER BY r.platform, r.room_id, seg.rel_path
        """,
        (match_id,),
    ).fetchall()

    order: list[tuple[str, str]] = []
    events: dict[tuple[str, str], list] = {}
    sessions: dict[tuple[str, str], set[int]] = {}
    for row in rows:
        key = (row["platform"], row["room_id"])
        if key not in events:
            order.append(key)
            events[key] = []
            sessions[key] = set()
        sessions[key].add(int(row["session_id"]))
        for _, event in iter_events(root / row["rel_path"]):
            events[key].append(event)

    contributions: list[RoomContribution] = []
    for key in order:
        collected = events[key]
        kept = dedupe(collected)
        timestamps = [event.ts for event in collected]
        contributions.append(
            RoomContribution(
                platform=key[0],
                room_id=key[1],
                msg_count=len(collected),
                deduped_count=len(kept),
                duplicate_count=len(collected) - len(kept),
                first_ts=min(timestamps) if timestamps else None,
                last_ts=max(timestamps) if timestamps else None,
                session_count=len(sessions[key]),
            )
        )
    return contributions
