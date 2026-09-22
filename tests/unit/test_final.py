"""终局判定测试（需求 §6.4）。

三条硬用例（issue #7 验收标准）：
- 2 类信号 + 无反转 → **不判终局**；
- 3 类信号但 60 秒内出现反转 → **不判终局**；
- 3 类 + 2 分钟无反转 → **判终局**。
"""

from __future__ import annotations

import pytest

from danmu_intel.common.config import StatsConfig
from danmu_intel.stats.basic import RawLine, WINDOW_MS
from danmu_intel.stats.final import (
    ANNOUNCE_LEXICON,
    END_LEXICON,
    SIGNAL_KINDS,
    VERDICT_FINAL,
    VERDICT_LIVE,
    VERDICT_REVOKED,
    SignalFact,
    active_kinds,
    announcement_fact,
    collect_signal_facts,
    end_burst_fact,
    judge_final,
    score_confirmed_fact,
    traffic_drop_fact,
)

from conftest import REL_PATH, make_event

BASE = 1_790_064_000_000
CONFIG = StatsConfig()


def fact(kind: str, start: int, end: int | None = None, **evidence) -> SignalFact:
    return SignalFact(kind=kind, start_ms=start, end_ms=end, evidence=evidence)


def lines(timestamps, *, text="普通弹幕") -> list[RawLine]:
    return [
        RawLine(REL_PATH, index + 1, make_event(ts, text=text))
        for index, ts in enumerate(timestamps)
    ]


# --------------------------------------------------------------------------- #
# 判定（纯逻辑）
# --------------------------------------------------------------------------- #


def test_two_signals_never_final():
    """2 类信号 + 无反转 → 不判终局。"""
    facts = [fact("end_burst", BASE), fact("score_confirmed", BASE + 1_000)]
    result = judge_final(facts, observed_until_ms=BASE + 3_600_000, config=CONFIG)
    assert result.verdict == VERDICT_LIVE
    assert result.kinds == ()
    assert "不足 3 类" in result.reason
    assert result.satisfied_at_ms is None and result.as_dict()["reversal"] is None


def test_reversal_within_window_blocks_judgement():
    """3 类信号但 60 秒内出现反转（终结类信号失效）→ 不判终局（撤销并留「曾判定」事实）。"""
    facts = [
        fact("end_burst", BASE, BASE + 70_000),
        fact("score_confirmed", BASE + 10_000),
        fact("announcement", BASE + 20_000),
    ]
    result = judge_final(facts, observed_until_ms=BASE + 3_600_000, config=CONFIG)
    assert result.verdict == VERDICT_REVOKED
    assert result.verdict != VERDICT_FINAL
    assert result.satisfied_at_ms == BASE + 20_000
    assert result.decided_at_ms is None
    assert result.reversal is not None
    assert result.reversal.kind == "end_burst"
    assert "撤销" in result.reason


def test_three_signals_without_reversal_is_final():
    """3 类 + 2 分钟无反转 → 判终局。"""
    facts = [
        fact("end_burst", BASE, BASE + 180_000),
        fact("score_confirmed", BASE + 10_000),
        fact("announcement", BASE + 20_000),
    ]
    result = judge_final(facts, observed_until_ms=BASE + 600_000, config=CONFIG)
    assert result.verdict == VERDICT_FINAL
    assert result.is_final
    assert result.satisfied_at_ms == BASE + 20_000
    assert result.decided_at_ms == BASE + 20_000 + 120_000
    assert result.kinds == ("announcement", "end_burst", "score_confirmed")


def test_observation_too_short_keeps_live():
    """3 类信号成立但观测还没覆盖 2 分钟反转窗口 → 保持 live（宁可不判）。"""
    facts = [fact(kind, BASE) for kind in ("end_burst", "score_confirmed", "announcement")]
    result = judge_final(facts, observed_until_ms=BASE + 60_000, config=CONFIG)
    assert result.verdict == VERDICT_LIVE
    assert result.satisfied_at_ms == BASE
    assert "反转窗口未走完" in result.reason


def test_official_revision_revokes():
    facts = [fact(kind, BASE) for kind in ("end_burst", "score_confirmed", "announcement")]
    facts.append(fact("official_revision", BASE + 30_000, detail="官方改判"))
    result = judge_final(facts, observed_until_ms=BASE + 600_000, config=CONFIG)
    assert result.verdict == VERDICT_REVOKED
    assert result.reversal is not None and result.reversal.kind == "official_revision"


def test_resumed_after_window_does_not_block():
    """反转发生在 2 分钟窗口之外 → 不影响判定。"""
    facts = [fact(kind, BASE) for kind in ("end_burst", "score_confirmed", "announcement")]
    facts.append(fact("resumed", BASE + 121_000, detail="比赛恢复"))
    result = judge_final(facts, observed_until_ms=BASE + 600_000, config=CONFIG)
    assert result.verdict == VERDICT_FINAL


def test_signals_must_be_simultaneous():
    """信号必须**同时**成立：三段互不重叠的信号不算。"""
    facts = [
        fact("end_burst", BASE, BASE + 10_000),
        fact("score_confirmed", BASE + 100_000, BASE + 110_000),
        fact("announcement", BASE + 200_000, BASE + 210_000),
    ]
    result = judge_final(facts, observed_until_ms=BASE + 3_600_000, config=CONFIG)
    assert result.verdict == VERDICT_LIVE


def test_active_kinds_boundaries():
    facts = [fact("end_burst", BASE, BASE + 10_000), fact("score_confirmed", BASE + 5_000)]
    assert active_kinds(facts, BASE) == {"end_burst"}
    assert active_kinds(facts, BASE + 5_000) == {"end_burst", "score_confirmed"}
    assert active_kinds(facts, BASE + 10_000) == {"score_confirmed"}
    assert active_kinds([fact("resumed", BASE)], BASE) == set()


def test_min_kinds_is_configurable():
    facts = [fact("end_burst", BASE), fact("score_confirmed", BASE)]
    looser = StatsConfig(min_signal_kinds=2)
    assert judge_final(facts, observed_until_ms=BASE + 600_000, config=looser).verdict == VERDICT_FINAL
    assert judge_final(facts, observed_until_ms=BASE + 600_000, config=CONFIG).verdict == VERDICT_LIVE


def test_judgement_is_deterministic():
    facts = [fact("end_burst", BASE), fact("score_confirmed", BASE), fact("traffic_drop", BASE)]
    first = judge_final(facts, observed_until_ms=BASE + 600_000, config=CONFIG)
    second = judge_final(list(reversed(facts)), observed_until_ms=BASE + 600_000, config=CONFIG)
    assert first == second


def test_signal_kinds_cover_requirement_four_categories():
    assert set(SIGNAL_KINDS) == {"end_burst", "score_confirmed", "traffic_drop", "announcement"}


# --------------------------------------------------------------------------- #
# 信号抽取（从原始记录推）
# --------------------------------------------------------------------------- #


def _final_text(counter: int) -> str:
    return f"{END_LEXICON[counter % len(END_LEXICON)]} 啦{counter}"


def test_end_burst_requires_two_minutes_of_density():
    dense = lines([BASE + index * 5_000 for index in range(37)], text="结束了")
    fact_found = end_burst_fact(dense, observed_until_ms=BASE + 300_000, config=CONFIG)
    assert fact_found is not None
    assert fact_found.kind == "end_burst"
    assert fact_found.evidence["hits"] >= CONFIG.end_burst_min_hits
    assert fact_found.end_ms is None or fact_found.end_ms - fact_found.start_ms >= CONFIG.end_burst_min_ms

    # 只持续 1 分钟的密集 → 不够 2 分钟
    short = lines([BASE + index * 5_000 for index in range(11)], text="结束了")
    assert end_burst_fact(short, observed_until_ms=BASE + 120_000, config=CONFIG) is None
    assert end_burst_fact([], observed_until_ms=BASE, config=CONFIG) is None
    # 有终结词但密度不够
    sparse = lines([BASE + index * 30_000 for index in range(6)], text="结束了")
    assert end_burst_fact(sparse, observed_until_ms=BASE + 300_000, config=CONFIG) is None


def test_traffic_drop_requires_ten_percent_and_five_minutes():
    events = lines([BASE + index for index in range(40)], text="热闹")
    events += lines([BASE + 1_000_000, BASE + 1_000_010], text="零星")
    found = traffic_drop_fact(events, observed_until_ms=BASE + 1_000_011, config=CONFIG)
    assert found is not None
    assert found.kind == "traffic_drop"
    assert found.evidence["peak_count"] == 40
    assert found.end_ms is None  # 一直静到观测结束

    # 流量没有下降到一成以下 → 不成立
    busy = lines([BASE + index * 1_000 for index in range(600)], text="一直热闹")
    assert traffic_drop_fact(busy, observed_until_ms=BASE + 600_000, config=CONFIG) is None
    assert traffic_drop_fact([], observed_until_ms=BASE, config=CONFIG) is None
    zero = lines([BASE + 1_000_000, BASE + 1_000_010], text="只有零星")
    assert traffic_drop_fact(zero, observed_until_ms=BASE + 1_000_011, config=CONFIG) is None


def test_score_confirmed_requires_official_match():
    consistent = {
        "official": "2:0",
        "danmu_consensus": "2:0",
        "consistent": True,
        "mentions": 5,
        "first_ts": BASE,
    }
    found = score_confirmed_fact(consistent)
    assert found is not None and found.kind == "score_confirmed" and found.start_ms == BASE
    assert score_confirmed_fact({**consistent, "consistent": False}) is None
    assert score_confirmed_fact({"official": None, "consistent": True}) is None


def test_announcement_from_official_then_danmu():
    official = announcement_fact([], official_ended_at=BASE, config=CONFIG)
    assert official is not None and official.evidence["channel"] == "official"

    single = lines([BASE], text="主播说要下播了")
    assert announcement_fact(single, official_ended_at=None, config=CONFIG) is None

    double = lines([BASE, BASE + 30_000], text="官宣了")
    found = announcement_fact(double, official_ended_at=None, config=CONFIG)
    assert found is not None and found.evidence["channel"] == "danmu"
    assert found.start_ms == BASE and found.end_ms == BASE + 30_001

    far_apart = lines([BASE, BASE + 600_000], text="官宣了")
    assert announcement_fact(far_apart, official_ended_at=None, config=CONFIG) is None
    assert ANNOUNCE_LEXICON


def test_collect_signal_facts_composes_and_sorts():
    events = lines([BASE + index * 5_000 for index in range(37)], text="结束了")
    events += lines([BASE + 1_000_000], text="结束了")
    score = {"official": "2:0", "danmu_consensus": "2:0", "consistent": True, "mentions": 1, "first_ts": BASE}
    facts = collect_signal_facts(
        events,
        score=score,
        official_ended_at=BASE + 900_000,
        observed_until_ms=BASE + 1_000_001,
        config=CONFIG,
    )
    assert [item.start_ms for item in facts] == sorted(item.start_ms for item in facts)
    assert {"end_burst", "score_confirmed", "announcement"} <= {item.kind for item in facts}
    assert all(item.as_dict()["label"] for item in facts)
    assert WINDOW_MS == 60_000


@pytest.mark.parametrize("kind", SIGNAL_KINDS)
def test_signal_facts_are_json_serializable(kind):
    import json

    payload = fact(kind, BASE, BASE + 1, hits=1).as_dict()
    assert json.loads(json.dumps(payload))["kind"] == kind
