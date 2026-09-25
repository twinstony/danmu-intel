"""灰信号评审（后台的人工动作：`escalate` / `discard` + **必填理由**）。

需求 §6.5 的 6 条硬约束在后台这一侧的落点：

- 状态机只有三个值（`candidate` / `escalated` / `discarded`），没有「确认作弊」这种断言；
  `escalated` 只是「值得人再看一眼」，`discarded` 只是「不进报告」；
- **作废必须写理由**（第 6 条：不达标即 discarded 且留原因）—— 后台的作废按钮不给理由就拒；
- **不提供任何导出接口**：本模块只读库与改状态，不写文件、不出 CSv、不生成对外链接。

人工升级过的行不被统计重算覆盖（`pipeline.write_gray_signals` 只删非 `escalated` 的行），
因此人的判断优先于自动重算 —— 这一点由 `stats/gray.py` 与流水线共同保证。
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass

from danmu_intel.common import audit
from danmu_intel.stats.gray import (
    GRAY_CATEGORY_LABELS,
    STATUS_DISCARDED,
    STATUS_ESCALATED,
    GraySample,
)

ACTION_REVIEW = "gray.review"

ESCALATE = "escalate"
DISCARD = "discard"
REVIEW_ACTIONS: dict[str, str] = {ESCALATE: STATUS_ESCALATED, DISCARD: STATUS_DISCARDED}


@dataclass(frozen=True, slots=True)
class StoredGraySignal:
    """`gray_signals` 表的一行（含样本：需求 §6.5 第 3 条「必须附样本」）。"""

    id: int
    match_id: int
    category: str
    keyword: str
    hit_count: int
    distinct_users: int
    window_count: int
    samples: tuple[GraySample, ...]
    status: str
    reason: str | None
    created_at: int
    evaluated_at: int | None

    @property
    def category_label(self) -> str:
        return GRAY_CATEGORY_LABELS.get(self.category, self.category)


def _to_signal(row: sqlite3.Row) -> StoredGraySignal:
    samples = tuple(
        GraySample(
            ts=int(item["ts"]),
            text=str(item["text"]),
            rel_path=str(item["rel_path"]),
            line_no=int(item["line_no"]),
        )
        for item in json.loads(row["samples_json"])
    )
    return StoredGraySignal(
        id=int(row["id"]),
        match_id=int(row["match_id"]),
        category=row["category"],
        keyword=row["keyword"],
        hit_count=int(row["hit_count"]),
        distinct_users=int(row["distinct_users"]),
        window_count=int(row["window_count"]),
        samples=samples,
        status=row["status"],
        reason=row["reason"],
        created_at=int(row["created_at"]),
        evaluated_at=row["evaluated_at"],
    )


def list_signals(
    conn: sqlite3.Connection, *, match_id: int | None = None, status: str | None = None
) -> list[StoredGraySignal]:
    sql = "SELECT * FROM gray_signals"
    params: list[object] = []
    conditions: list[str] = []
    if match_id is not None:
        conditions.append("match_id=?")
        params.append(match_id)
    if status is not None:
        conditions.append("status=?")
        params.append(status)
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY match_id, id"
    return [_to_signal(row) for row in conn.execute(sql, params)]


def get_signal(conn: sqlite3.Connection, signal_id: int) -> StoredGraySignal:
    row = conn.execute("SELECT * FROM gray_signals WHERE id=?", (signal_id,)).fetchone()
    if row is None:
        raise LookupError(f"未找到灰信号 #{signal_id}")
    return _to_signal(row)


def review(
    conn: sqlite3.Connection,
    signal_id: int,
    *,
    action: str,
    reason: str,
    actor: str,
    ts: int | None = None,
) -> StoredGraySignal:
    """人工升级或作废一条灰信号（**理由必填**，动作入审计）。"""
    if action not in REVIEW_ACTIONS:
        raise ValueError(f"未知的评审动作：{action}（允许：{','.join(REVIEW_ACTIONS)}）")
    if not reason.strip():
        raise ValueError("评审灰信号必须写明理由（无理由的作废不可复核，需求 §6.5 第 6 条）")
    before = get_signal(conn, signal_id)
    moment = int(time.time() * 1000) if ts is None else ts
    conn.execute(
        "UPDATE gray_signals SET status=?, reason=?, evaluated_at=? WHERE id=?",
        (REVIEW_ACTIONS[action], reason.strip(), moment, signal_id),
    )
    conn.commit()
    after = get_signal(conn, signal_id)
    audit.record(
        conn,
        actor=actor,
        action=ACTION_REVIEW,
        target=f"{before.match_id}:{before.keyword}",
        detail={"signal_id": signal_id, "action": action, "from": before.status,
                "to": after.status, "reason": reason.strip()},
        ts=moment,
    )
    return after
