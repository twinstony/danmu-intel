"""成本口径与账本测试：价格纯函数、本地日历日边界、硬闸判定、连续失败推导。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from danmu_intel.report.llm import cost, ledger
from danmu_intel.report.llm.cost import (
    DAILY_LIMIT_CNY,
    MATCH_LIMIT_CNY,
    ModelPrice,
    Spend,
    day_bounds,
    estimate_cost_cny,
    gate,
    price_for,
)

UTC = timezone.utc


def test_price_table_covers_the_default_model():
    assert price_for(cost.DEFAULT_MODEL) == cost.MODEL_PRICES[cost.DEFAULT_MODEL]
    assert cost.MODEL_PRICES[cost.DEFAULT_MODEL].cache_hit_input < cost.MODEL_PRICES[
        cost.DEFAULT_MODEL
    ].cache_miss_input


def test_unknown_model_is_refused_not_free():
    """未登记价格的模型必须报错：静默按 0 元记账等于成本闸失效。"""
    with pytest.raises(LookupError, match="未登记价格"):
        price_for("gpt-hallucination-9000")


def test_estimate_cost_uses_cache_hit_and_miss_prices(monkeypatch):
    monkeypatch.setitem(cost.MODEL_PRICES, "test-model", ModelPrice(0.5, 2.0, 8.0))
    # 100 万 prompt（其中 40 万命中）+ 50 万输出
    assert estimate_cost_cny(
        "test-model", prompt_tokens=1_000_000, completion_tokens=500_000, cache_hit_tokens=400_000
    ) == pytest.approx(0.4 * 0.5 + 0.6 * 2.0 + 0.5 * 8.0)


def test_estimate_cost_defaults_to_cache_miss_and_clamps_hits(monkeypatch):
    monkeypatch.setitem(cost.MODEL_PRICES, "test-model", ModelPrice(0.5, 2.0, 8.0))
    assert estimate_cost_cny("test-model", prompt_tokens=1000, completion_tokens=0) == pytest.approx(
        1000 * 2.0 / 1_000_000
    )
    # 命中数比 prompt 还大 / 为负：不信 API 的怪数字，夹紧
    assert estimate_cost_cny(
        "test-model", prompt_tokens=1000, completion_tokens=0, cache_hit_tokens=9999
    ) == pytest.approx(1000 * 0.5 / 1_000_000)
    assert estimate_cost_cny(
        "test-model", prompt_tokens=1000, completion_tokens=0, cache_hit_tokens=-5
    ) == pytest.approx(1000 * 2.0 / 1_000_000)


def test_real_cost_of_a_whole_report_is_far_below_the_match_limit():
    """七段解读 ≈ 每段 4k 输入 + 400 输出：单场应远低于 ¥0.3（否则硬闸会把正常报告闸掉）。"""
    per_call = estimate_cost_cny(
        cost.DEFAULT_MODEL, prompt_tokens=4000, completion_tokens=400, cache_hit_tokens=3000
    )
    assert per_call * 7 < MATCH_LIMIT_CNY / 3


def test_day_bounds_are_local_calendar_days():
    moment = int(datetime(2026, 9, 22, 23, 30, tzinfo=UTC).timestamp() * 1000)
    start, end = day_bounds(moment, tz=UTC)
    assert datetime.fromtimestamp(start / 1000, tz=UTC) == datetime(2026, 9, 22, tzinfo=UTC)
    assert datetime.fromtimestamp(end / 1000, tz=UTC) == datetime(2026, 9, 23, tzinfo=UTC)
    assert end - start == 24 * 3600 * 1000

    # 跨时区：同一毫秒在不同时区落在不同的"当日"里
    tz_plus_8 = timezone(timedelta(hours=8))
    assert day_bounds(moment, tz=tz_plus_8)[0] != start


def test_match_limit_is_inclusive():
    """「达 ¥0.3」就要闸（验收标准原文），不是"超过才闸"。"""
    assert gate(Spend(match_cny=MATCH_LIMIT_CNY, day_cny=0)).blocked
    decision = gate(Spend(match_cny=MATCH_LIMIT_CNY, day_cny=0))
    assert decision.limit_kind == "match" and "单场" in decision.reason
    assert gate(Spend(match_cny=MATCH_LIMIT_CNY - 0.0001, day_cny=0)).allowed


def test_daily_limit_is_inclusive_and_checked_after_the_match_limit():
    assert gate(Spend(match_cny=0.0, day_cny=DAILY_LIMIT_CNY)).limit_kind == "day"
    decision = gate(Spend(match_cny=MATCH_LIMIT_CNY, day_cny=DAILY_LIMIT_CNY))
    assert decision.limit_kind == "match"  # 单场先判：更具体的先报
    assert gate(Spend(match_cny=0.1, day_cny=9.99)).allowed


def test_limits_can_be_lowered_for_tests():
    assert gate(Spend(match_cny=0.01, day_cny=0.01), match_limit=0.01).blocked


def test_gate_decision_blocked_is_the_negation_of_allowed():
    assert not gate(Spend(0, 0)).blocked
    assert gate(Spend(0, 0)).reason == "" and gate(Spend(0, 0)).limit_kind is None


# —— 账本 ——


def record(conn, *, match_id=1, segment_no=3, outcome=ledger.OUTCOME_OK, cost_cny=0.05, created_at=1000, **kwargs):
    return ledger.record(
        conn,
        match_id=match_id,
        segment_no=segment_no,
        model=cost.DEFAULT_MODEL,
        prompt_version="v1",
        outcome=outcome,
        created_at=created_at,
        cost_cny=cost_cny,
        **kwargs,
    )


def test_record_writes_every_field(conn):
    record_id = record(
        conn,
        prompt_tokens=4000,
        completion_tokens=400,
        cache_hit_tokens=3000,
        latency_ms=1234,
        reason="ok",
    )
    row = conn.execute("SELECT * FROM llm_calls WHERE id=?", (record_id,)).fetchone()
    assert row["match_id"] == 1 and row["segment_no"] == 3
    assert row["model"] == cost.DEFAULT_MODEL and row["prompt_version"] == "v1"
    assert row["prompt_tokens"] == 4000 and row["completion_tokens"] == 400
    assert row["cache_hit_tokens"] == 3000 and row["latency_ms"] == 1234
    assert row["outcome"] == ledger.OUTCOME_OK and row["cost_cny"] == pytest.approx(0.05)


def test_spend_sums_per_match_and_per_day(conn):
    record(conn, match_id=1, cost_cny=0.2, created_at=1000)
    record(conn, match_id=1, cost_cny=0.05, created_at=2000)
    record(conn, match_id=2, cost_cny=9.0, created_at=2000)

    totals = ledger.spend(conn, 1, now_ms=2000)
    assert totals.match_cny == pytest.approx(0.25)
    assert totals.day_cny == pytest.approx(9.25)

    other_day = ledger.spend(conn, 1, now_ms=1000 + 3 * 24 * 3600 * 1000)
    assert other_day.day_cny == 0.0
    assert other_day.match_cny == pytest.approx(0.25)


def test_spend_without_any_calls_is_zero(conn):
    totals = ledger.spend(conn, 42, now_ms=2000)
    assert totals == Spend(match_cny=0.0, day_cny=0.0)


def test_recent_outcomes_are_newest_first(conn):
    record(conn, outcome=ledger.OUTCOME_OK)
    record(conn, outcome=ledger.OUTCOME_TIMEOUT)
    assert ledger.recent_outcomes(conn, limit=1) == [ledger.OUTCOME_TIMEOUT]
    assert ledger.recent_outcomes(conn) == [ledger.OUTCOME_TIMEOUT, ledger.OUTCOME_OK]


def test_consecutive_failures_counts_only_the_leading_streak():
    assert ledger.consecutive_failures([]) == 0
    assert ledger.consecutive_failures([ledger.OUTCOME_OK]) == 0
    assert ledger.consecutive_failures([ledger.OUTCOME_TIMEOUT, ledger.OUTCOME_TIMEOUT]) == 2
    assert ledger.consecutive_failures(
        [ledger.OUTCOME_ERROR, ledger.OUTCOME_REJECTED, ledger.OUTCOME_OK, ledger.OUTCOME_TIMEOUT]
    ) == 2


def test_gated_is_not_counted_as_a_failure():
    """被闸住是结果不是原因：它不能把连续失败数往上推，否则会自锁。"""
    assert ledger.OUTCOME_GATED not in ledger.FAILURE_OUTCOMES
    assert ledger.consecutive_failures([ledger.OUTCOME_GATED, ledger.OUTCOME_TIMEOUT]) == 0


def test_is_degraded_recovers_after_a_success(conn):
    for _ in range(ledger.DEGRADE_AFTER_FAILURES):
        record(conn, outcome=ledger.OUTCOME_ERROR)
    assert ledger.is_degraded(ledger.recent_outcomes(conn))

    record(conn, outcome=ledger.OUTCOME_OK)
    assert not ledger.is_degraded(ledger.recent_outcomes(conn))


def test_calls_for_match_returns_records_in_order(conn):
    record(conn, segment_no=2)
    record(conn, match_id=2, segment_no=3)
    record(conn, segment_no=4, outcome=ledger.OUTCOME_TIMEOUT, reason="超时")

    calls = ledger.calls_for_match(conn, 1)
    assert [call.segment_no for call in calls] == [2, 4]
    assert calls[-1].outcome == ledger.OUTCOME_TIMEOUT and calls[-1].reason == "超时"
    assert calls[0].created_at == 1000 and calls[0].id > 0
