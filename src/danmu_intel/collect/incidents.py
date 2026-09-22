"""采集异常事件（设计 §7.5；issue #5 §6）。

**异常不得静默**。进程退出、心跳僵死、重启超限、无首条消息、断流重连、磁盘不足
六类异常各写一行 `notifications(state='pending')`，把原因与当时的数据留在库里；
投递（统一 5 分钟时效闸门）由 T11 的通知器来接——本模块只负责「产生事件」。

`notifications` 是设计 §5.1 里既有的表，payload 里带 `match_id / platform / room_id`，
因此按房间/比赛都能查回来（`recent`）。去重抑制（`alerts` 表）属 T11，本模块不越界。
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass

PROCESS_EXIT = "process_exit"  # 采集进程退出（崩溃 / 被外部 kill）
PROCESS_HUNG = "process_hung"  # 心跳老化：子进程僵死，父进程杀掉重启
RESTART_EXCEEDED = "restart_exceeded"  # 30 分钟内重启超上限，停止重试
NO_STREAM = "no_stream"  # 120 秒没有首条弹幕
STALLED = "stalled"  # 60 秒无消息，触发重连
DISK_LOW = "disk_low"  # 数据盘可用空间低于下限

KINDS = (PROCESS_EXIT, PROCESS_HUNG, RESTART_EXCEEDED, NO_STREAM, STALLED, DISK_LOW)
SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}


def now_ms() -> int:
    return int(time.time() * 1000)


def worst(*severities: str) -> str:
    """取最严重的一档（`room_sessions.severity` 是「本会话见过的最坏情况」）。"""
    return max(severities, key=lambda severity: SEVERITY_ORDER[severity])


@dataclass(frozen=True, slots=True)
class Incident:
    id: int
    kind: str
    severity: str
    payload: dict[str, object]
    created_at: int
    state: str


def emit(
    conn: sqlite3.Connection,
    kind: str,
    *,
    severity: str,
    platform: str,
    room_id: str,
    match_id: int | None = None,
    detail: dict[str, object] | None = None,
    timestamp: int | None = None,
) -> int:
    """写一条待投递的采集异常事件，返回行 id。"""
    if kind not in KINDS:
        raise ValueError(f"未知的采集异常类型：{kind}（允许：{','.join(KINDS)}）")
    if severity not in SEVERITY_ORDER:
        raise ValueError(f"未知的严重级别：{severity}（允许：{','.join(SEVERITY_ORDER)}）")
    payload: dict[str, object] = {"match_id": match_id, "platform": platform, "room_id": room_id}
    payload.update(detail or {})
    cursor = conn.execute(
        """
        INSERT INTO notifications(kind, severity, payload_json, created_at, state, attempts)
        VALUES(?, ?, ?, ?, 'pending', 0)
        """,
        (kind, severity, json.dumps(payload, ensure_ascii=False, sort_keys=True), timestamp or now_ms()),
    )
    conn.commit()
    return int(cursor.lastrowid)


class SessionIncidents:
    """子进程侧的事件出口：一个会话内同一类异常只报一次。

    断流 60 秒会反复触发重连（`reconnects` 照常累计），若每次都写事件就会把库刷成
    噪声；这里按「首次发生即报」处理，次数由 `room_sessions.reconnects` 承担。
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        platform: str,
        room_id: str,
        match_id: int | None = None,
    ) -> None:
        self._conn = conn
        self._platform = platform
        self._room_id = room_id
        self._match_id = match_id
        self._seen: set[str] = set()

    def emit_once(
        self,
        kind: str,
        *,
        severity: str,
        detail: dict[str, object] | None = None,
        timestamp: int | None = None,
    ) -> bool:
        """同类异常本会话只报一次；返回是否真的写了事件。"""
        if kind in self._seen:
            return False
        self._seen.add(kind)
        emit(
            self._conn,
            kind,
            severity=severity,
            platform=self._platform,
            room_id=self._room_id,
            match_id=self._match_id,
            detail=detail,
            timestamp=timestamp,
        )
        return True


def _to_incident(row: sqlite3.Row) -> Incident:
    return Incident(
        id=int(row["id"]),
        kind=row["kind"],
        severity=row["severity"],
        payload=json.loads(row["payload_json"]),
        created_at=int(row["created_at"]),
        state=row["state"],
    )


def recent(
    conn: sqlite3.Connection,
    *,
    limit: int = 20,
    match_id: int | None = None,
    room_id: str | None = None,
) -> list[Incident]:
    """最近的采集异常事件（新→旧）。

    `notifications` 只装采集异常这一小类事件，量级很小，所以按 payload 过滤放在
    取回之后做（表结构是设计定死的，不为查询另加列）。
    """
    rows = conn.execute("SELECT * FROM notifications ORDER BY id DESC").fetchall()
    incidents = [_to_incident(row) for row in rows]
    if match_id is not None:
        incidents = [item for item in incidents if item.payload.get("match_id") == match_id]
    if room_id is not None:
        incidents = [item for item in incidents if item.payload.get("room_id") == room_id]
    return incidents[:limit]
