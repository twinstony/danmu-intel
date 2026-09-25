"""待投递事件出口（`notifications` 表，设计 §5.1/§7.5/§15）。

**异常不得静默**：任何"出事了、要有人知道"的事实都写一行 `notifications(state='pending')`
留在这里；投递（统一 5 分钟时效闸门）由 `notify/` 接手，本模块只管这张表的
**结构与状态流转**（写一行、按比赛/房间/类型查回来、改一个状态）。

两条产出方：

- 采集层（`collect/incidents.py`）：进程退出、心跳僵死、重启超限、无首条消息、断流、磁盘不足、落盘丢包；
- 解读层（`report/llm/alerts.py`）：成本硬闸触及、连续失败导致解读能力降级；
- 链上（`chain/alerts.py`）、发布（`publish/release.py`）、付费开通（`billing/settle.py`）、
  会员批处理（`billing/members.py`）同样走这张表。

`kind` 由各自的模块定义并校验（本模块只管结构：kind/severity/payload/state），
`payload` 里统一带 `match_id`，因此按比赛能一次查回所有事件。

状态机（只前进，不补发）：

| 状态 | 含义 |
|---|---|
| `pending` | 已入库、等投递 |
| `delivered` | 送达（`channel` 记下走通的通道） |
| `suppressed` | 同一 `alert_key` 在冷却期内，**只留痕不发** |
| `dropped_expired` | 超过时效闸门仍未送出 → 销毁 |
| `failed` | 重试次数用尽仍未送出 → 销毁 |
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass

SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}

PENDING = "pending"
DELIVERED = "delivered"
SUPPRESSED = "suppressed"
DROPPED_EXPIRED = "dropped_expired"
FAILED = "failed"
#: 终态（不会再被投递）—— 闸门与重试判定用。
TERMINAL_STATES = (DELIVERED, SUPPRESSED, DROPPED_EXPIRED, FAILED)


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True, slots=True)
class Notification:
    """`notifications` 表的一行（待投递/已投递/已销毁的事件）。"""

    id: int
    kind: str
    severity: str
    payload: dict[str, object]
    created_at: int
    state: str
    channel: str | None = None
    delivered_at: int | None = None
    attempts: int = 0


def emit(
    conn: sqlite3.Connection,
    kind: str,
    *,
    severity: str,
    payload: dict[str, object],
    timestamp: int | None = None,
) -> int:
    """写一条待投递事件，返回行 id。"""
    if severity not in SEVERITY_ORDER:
        raise ValueError(f"未知的严重级别：{severity}（允许：{','.join(SEVERITY_ORDER)}）")
    cursor = conn.execute(
        """
        INSERT INTO notifications(kind, severity, payload_json, created_at, state, attempts)
        VALUES(?, ?, ?, ?, 'pending', 0)
        """,
        (kind, severity, json.dumps(payload, ensure_ascii=False, sort_keys=True), timestamp or now_ms()),
    )
    conn.commit()
    return int(cursor.lastrowid)


def _to_notification(row: sqlite3.Row) -> Notification:
    return Notification(
        id=int(row["id"]),
        kind=row["kind"],
        severity=row["severity"],
        payload=json.loads(row["payload_json"]),
        created_at=int(row["created_at"]),
        state=row["state"],
        channel=row["channel"],
        delivered_at=int(row["delivered_at"]) if row["delivered_at"] is not None else None,
        attempts=int(row["attempts"]),
    )


def recent(
    conn: sqlite3.Connection,
    *,
    limit: int = 20,
    match_id: int | None = None,
    room_id: str | None = None,
    kind: str | None = None,
) -> list[Notification]:
    """最近的事件（新→旧）。

    `notifications` 只装待投递事件这一小类，量级很小，所以按 payload 过滤放在
    取回之后做（表结构是设计定死的，不为查询另加列）。
    """
    rows = conn.execute("SELECT * FROM notifications ORDER BY id DESC").fetchall()
    events = [_to_notification(row) for row in rows]
    if kind is not None:
        events = [item for item in events if item.kind == kind]
    if match_id is not None:
        events = [item for item in events if item.payload.get("match_id") == match_id]
    if room_id is not None:
        events = [item for item in events if item.payload.get("room_id") == room_id]
    return events[:limit]


def get(conn: sqlite3.Connection, notification_id: int) -> Notification | None:
    row = conn.execute("SELECT * FROM notifications WHERE id=?", (notification_id,)).fetchone()
    return None if row is None else _to_notification(row)


def pending(conn: sqlite3.Connection, *, limit: int | None = None) -> list[Notification]:
    """待投递事件（旧→新：先来的先处理，闸门才不会被后来的抢前面）。"""
    sql = "SELECT * FROM notifications WHERE state='pending' ORDER BY id"
    params: tuple[object, ...] = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)
    return [_to_notification(row) for row in conn.execute(sql, params).fetchall()]


def record_attempt(conn: sqlite3.Connection, notification_id: int) -> int:
    """记一次投递尝试，返回累计尝试次数（首次 + 重试次数）。"""
    conn.execute("UPDATE notifications SET attempts = attempts + 1 WHERE id=?", (notification_id,))
    conn.commit()
    row = conn.execute("SELECT attempts FROM notifications WHERE id=?", (notification_id,)).fetchone()
    return int(row["attempts"])


def mark_delivered(
    conn: sqlite3.Connection, notification_id: int, *, channel: str, at: int | None = None
) -> Notification:
    conn.execute(
        "UPDATE notifications SET state=?, channel=?, delivered_at=? WHERE id=?",
        (DELIVERED, channel, now_ms() if at is None else at, notification_id),
    )
    conn.commit()
    return _require(conn, notification_id)


def mark_suppressed(conn: sqlite3.Connection, notification_id: int) -> Notification:
    """同一 `alert_key` 冷却期内的后来者：**留痕但不发**。"""
    return _set_state(conn, notification_id, SUPPRESSED)


def mark_dropped_expired(conn: sqlite3.Connection, notification_id: int) -> Notification:
    """时效闸门销毁（超 5 分钟未送达，不补发）。"""
    return _set_state(conn, notification_id, DROPPED_EXPIRED)


def mark_failed(conn: sqlite3.Connection, notification_id: int) -> Notification:
    """重试次数用尽（通道全挂）→ 销毁；原因另在 `audit_log` 里。"""
    return _set_state(conn, notification_id, FAILED)


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    """各状态条数（`alerts` 命令与控制台看的口径）。"""
    rows = conn.execute("SELECT state, COUNT(*) AS n FROM notifications GROUP BY state").fetchall()
    return {row["state"]: int(row["n"]) for row in rows}


def _set_state(conn: sqlite3.Connection, notification_id: int, state: str) -> Notification:
    conn.execute("UPDATE notifications SET state=? WHERE id=?", (state, notification_id))
    conn.commit()
    return _require(conn, notification_id)


def _require(conn: sqlite3.Connection, notification_id: int) -> Notification:
    item = get(conn, notification_id)
    if item is None:
        raise LookupError(f"通知不存在：#{notification_id}")
    return item
