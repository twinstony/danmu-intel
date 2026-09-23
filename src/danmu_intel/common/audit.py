"""审计留痕（设计 §5.1 `audit_log`）。

**只增不改**：谁在什么时候改了什么，逐条追加。T4 用它记两件事：

- `slice.override`：人工修正切片边界（需求 FR-C2-5「修正结果为准且留痕」）。
- `slice.boundary`：自动边界裁决的落库结果（采用/跳过都能回答「为什么」）。
- `config.update`：统计门槛改动（设计 §9.1 灰信号第 4 条「门槛参数写在 config 表，改动留审计」）。

审计条数也是 `metrics.algo_version` 递增的依据：每一次人工修正都进一条 `slice.override`，
算法版本因此**由输入（审计记录）唯一确定**，而非由时钟或进程状态决定 —— 这样删掉
统计结果后重算，版本号仍然可复现（AC-13）。
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

SLICE_OVERRIDE = "slice.override"
SLICE_BOUNDARY = "slice.boundary"
CONFIG_UPDATE = "config.update"


@dataclass(frozen=True, slots=True)
class AuditEntry:
    id: int
    ts: int
    actor: str
    action: str
    target: str | None
    detail: dict[str, Any]


def record(
    conn: sqlite3.Connection,
    *,
    actor: str,
    action: str,
    target: str | None = None,
    detail: dict[str, Any] | None = None,
    ts: int | None = None,
) -> int:
    """追加一条审计记录，返回其 id。`actor` 必填 —— 无主体的改动不留痕等于没留痕。"""
    if not actor:
        raise ValueError("审计记录必须写明 actor（谁改的）")
    cursor = conn.execute(
        "INSERT INTO audit_log(ts, actor, action, target, detail_json) VALUES(?, ?, ?, ?, ?)",
        (
            int(time.time() * 1000) if ts is None else ts,
            actor,
            action,
            target,
            json.dumps(detail or {}, ensure_ascii=False, sort_keys=True),
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def entries(
    conn: sqlite3.Connection, *, action: str | None = None, target_prefix: str | None = None
) -> list[AuditEntry]:
    sql = "SELECT * FROM audit_log"
    conditions: list[str] = []
    params: list[object] = []
    if action is not None:
        conditions.append("action=?")
        params.append(action)
    if target_prefix is not None:
        conditions.append("substr(target, 1, ?) = ?")
        params.extend([len(target_prefix), target_prefix])
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY id"
    return [
        AuditEntry(
            id=int(row["id"]),
            ts=int(row["ts"]),
            actor=row["actor"],
            action=row["action"],
            target=row["target"],
            detail=json.loads(row["detail_json"]),
        )
        for row in conn.execute(sql, params)
    ]


def count(conn: sqlite3.Connection, *, action: str, target_prefix: str | None = None) -> int:
    return len(entries(conn, action=action, target_prefix=target_prefix))
