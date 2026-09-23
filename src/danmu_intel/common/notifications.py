"""待投递事件出口（`notifications` 表，设计 §5.1/§7.5）。

**异常不得静默**：任何"出事了、要有人知道"的事实都写一行 `notifications(state='pending')`
留在这里，投递（统一 5 分钟时效闸门）由 T11 的通知器接手。本模块只做两件事：
写一条待投递事件（`emit`）、按比赛/房间/类型查回来（`recent`）。

两条产出方：

- 采集层（`collect/incidents.py`）：进程退出、心跳僵死、重启超限、无首条消息、断流、磁盘不足；
- 解读层（`report/llm/alerts.py`）：成本硬闸触及、连续失败导致解读能力降级。

`kind` 由各自的模块定义并校验（本模块只管结构：kind/severity/payload/state），
`payload` 里统一带 `match_id`，因此按比赛能一次查回所有事件。
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass

SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True, slots=True)
class Notification:
    """`notifications` 表的一行（待投递/已投递的事件）。"""

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
