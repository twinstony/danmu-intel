"""LLM 调用账本（`llm_calls`）：写一行、按场/按日累计、从账本推导"是否长期不可用"。

账本**只增不改**：每次调用（含被闸掉没调的 `gated`）一行，成本闸与降级判定都只读它。
"连续 N 次失败即全局降级"因此不需要另存状态，也从账本里推出来——这是"状态可重算"
（设计 §9 的铁律）在解读层的同一条思路：少一份要同步的状态，就少一处会漂移的真值。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import tzinfo

from danmu_intel.report.llm.cost import Spend, day_bounds

OUTCOME_OK = "ok"  # 调用成功且通过反幻觉校验
OUTCOME_TIMEOUT = "timeout"  # 单次调用超时（按失败处理）
OUTCOME_ERROR = "error"  # 断网 / API 报错 / 响应不可解析
OUTCOME_REJECTED = "rejected"  # 调通了，但输出不合契约或含事实层之外的新事实
OUTCOME_GATED = "gated"  # 没调：成本闸或全局降级

#: 算"失败"的结局（连续 N 次失败 → 全局降级）。`gated` 不算失败：它本身是结果而非原因。
FAILURE_OUTCOMES: tuple[str, ...] = (OUTCOME_TIMEOUT, OUTCOME_ERROR, OUTCOME_REJECTED)

#: 连续失败到这个次数就认为 LLM 长期不可用（ADR-0003「长时间不可用」）。
DEGRADE_AFTER_FAILURES = 3


@dataclass(frozen=True, slots=True)
class CallRecord:
    id: int
    match_id: int | None
    segment_no: int | None
    model: str
    prompt_version: str
    prompt_tokens: int
    completion_tokens: int
    cache_hit_tokens: int
    cost_cny: float
    latency_ms: int
    outcome: str
    reason: str | None
    created_at: int


def record(
    conn: sqlite3.Connection,
    *,
    match_id: int | None,
    segment_no: int | None,
    model: str,
    prompt_version: str,
    outcome: str,
    created_at: int,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cache_hit_tokens: int = 0,
    cost_cny: float = 0.0,
    latency_ms: int = 0,
    reason: str | None = None,
) -> int:
    """记一次调用（成功/失败/被闸都要记）。返回行 id。"""
    cursor = conn.execute(
        """
        INSERT INTO llm_calls(match_id, segment_no, model, prompt_version, prompt_tokens,
                              completion_tokens, cache_hit_tokens, cost_cny, latency_ms,
                              outcome, reason, created_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            match_id,
            segment_no,
            model,
            prompt_version,
            prompt_tokens,
            completion_tokens,
            cache_hit_tokens,
            cost_cny,
            latency_ms,
            outcome,
            reason,
            created_at,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def spend(conn: sqlite3.Connection, match_id: int, *, now_ms: int, tz: tzinfo | None = None) -> Spend:
    """累计花费：本场 + 当日（本地日历日）。"""
    match_row = conn.execute(
        "SELECT COALESCE(SUM(cost_cny), 0) AS total FROM llm_calls WHERE match_id=?", (match_id,)
    ).fetchone()
    start, end = day_bounds(now_ms, tz=tz)
    day_row = conn.execute(
        "SELECT COALESCE(SUM(cost_cny), 0) AS total FROM llm_calls WHERE created_at>=? AND created_at<?",
        (start, end),
    ).fetchone()
    return Spend(match_cny=float(match_row["total"]), day_cny=float(day_row["total"]))


def recent_outcomes(conn: sqlite3.Connection, *, limit: int = 12) -> list[str]:
    """最近的调用结局（新 → 旧）。"""
    rows = conn.execute("SELECT outcome FROM llm_calls ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [row["outcome"] for row in rows]


def consecutive_failures(outcomes: list[str]) -> int:
    """从最新往回数连续失败的次数（纯函数）。"""
    count = 0
    for outcome in outcomes:
        if outcome not in FAILURE_OUTCOMES:
            break
        count += 1
    return count


def is_degraded(outcomes: list[str], *, threshold: int = DEGRADE_AFTER_FAILURES) -> bool:
    """最近是否连续失败到阈值（纯函数）——出现一次成功调用即自动恢复。"""
    return consecutive_failures(outcomes) >= threshold


def calls_for_match(conn: sqlite3.Connection, match_id: int) -> list[CallRecord]:
    """该场全部调用记录（按时间序），供对账/后台"成本与额度"页（NFR-C-3）。"""
    rows = conn.execute(
        "SELECT * FROM llm_calls WHERE match_id=? ORDER BY id", (match_id,)
    ).fetchall()
    return [
        CallRecord(
            id=int(row["id"]),
            match_id=row["match_id"],
            segment_no=row["segment_no"],
            model=row["model"],
            prompt_version=row["prompt_version"],
            prompt_tokens=int(row["prompt_tokens"]),
            completion_tokens=int(row["completion_tokens"]),
            cache_hit_tokens=int(row["cache_hit_tokens"]),
            cost_cny=float(row["cost_cny"]),
            latency_ms=int(row["latency_ms"]),
            outcome=row["outcome"],
            reason=row["reason"],
            created_at=int(row["created_at"]),
        )
        for row in rows
    ]
