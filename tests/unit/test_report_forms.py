"""报告三形态的定义与时效预算（issue #8 范围第 1、3 条）。

三形态的段集固定、时限固定；赛中快报的段集是完整十一段的真子集，其余两形态一致。
"""

from __future__ import annotations

import pytest

from danmu_intel.report.forms import (
    FORMS,
    FULL_DEADLINE_MS,
    KIND_FULL,
    KIND_LIVE_BRIEF,
    KIND_REVIEW,
    LIVE_BRIEF_STAGE_BUDGET_MS,
    REVIEW_DEADLINE_MS,
    ReportScope,
    Timing,
    form_of,
)
from danmu_intel.report.segments import ALL_SEGMENT_NOS, INTERPRETATION_SEGMENTS, SEGMENTS


def test_three_forms_match_requirements():
    """NFR-T-1/2/3：快报 2 分钟、完整版 10 分钟、复盘版 15 分钟。"""
    assert [form.kind for form in FORMS] == [KIND_LIVE_BRIEF, KIND_FULL, KIND_REVIEW]
    assert form_of(KIND_LIVE_BRIEF).deadline_ms == 120_000
    assert form_of(KIND_FULL).deadline_ms == FULL_DEADLINE_MS == 600_000
    assert form_of(KIND_REVIEW).deadline_ms == REVIEW_DEADLINE_MS == 900_000
    assert form_of(KIND_LIVE_BRIEF).trigger == "node_end"
    assert form_of(KIND_FULL).trigger == "match_end"
    assert form_of(KIND_REVIEW).trigger == "match_end"
    assert [form_of(kind).label for kind in (KIND_LIVE_BRIEF, KIND_FULL, KIND_REVIEW)] == [
        "赛中快报",
        "完整版",
        "复盘版",
    ]


def test_live_brief_segments_are_a_strict_subset():
    """赛中快报只发布段落子集：需要终局对照的段（7 预测验证）不进快报。"""
    brief = set(form_of(KIND_LIVE_BRIEF).segments)
    assert brief == set(ALL_SEGMENT_NOS) - {7}
    assert brief < set(ALL_SEGMENT_NOS), "快报必须是完整十一段的真子集"
    assert form_of(KIND_FULL).segments == ALL_SEGMENT_NOS
    assert form_of(KIND_REVIEW).segments == ALL_SEGMENT_NOS


def test_every_form_publishes_interpretation_segments():
    """需求 §6.9/§6.10：解读不可省略 —— 每个形态都必须含解读段（否则 AC-16 无法满足）。"""
    for form in FORMS:
        published = set(form.segments)
        assert published & set(INTERPRETATION_SEGMENTS), f"{form.kind} 没有任何解读段"
    assert set(form_of(KIND_LIVE_BRIEF).segments) >= set(INTERPRETATION_SEGMENTS) - {7}


def test_form_segments_are_known_segment_numbers():
    known = {spec.no for spec in SEGMENTS}
    for form in FORMS:
        assert set(form.segments) <= known
        assert tuple(sorted(form.segments)) == form.segments


def test_unknown_form_is_rejected():
    with pytest.raises(ValueError, match="未注册的报告形态：hourly"):
        form_of("hourly")


def test_live_brief_budget_table_fits_the_deadline():
    """设计 §10.2 的逐阶段预算必须落在 2 分钟之内（否则「预算表进代码」没有意义）。"""
    total = sum(cost for _, cost in LIVE_BRIEF_STAGE_BUDGET_MS)
    assert total <= 120_000, "赛中快报的阶段预算之和不得超过 120s"
    assert total == 115_000
    stages = [stage for stage, _ in LIVE_BRIEF_STAGE_BUDGET_MS]
    assert stages == [
        "stats_ready",
        "fact_assembly",
        "interpretation",
        "validation",
        "render",
        "publish_checks",
        "deploy",
    ]


def test_timing_measures_stages_and_flags_over_budget():
    now = [0.0]
    timing = Timing(clock=lambda: now[0])
    now[0] = 1.5
    timing.mark("stats_ready")
    now[0] = 30.0
    timing.mark("interpretation")
    form = form_of(KIND_LIVE_BRIEF)

    assert timing.stages == {"stats_ready": 1500, "interpretation": 28_500}
    assert timing.elapsed_ms == 30_000
    assert timing.over_budget(form) == ["interpretation"]
    report = timing.as_dict(form)
    assert report["within_deadline"] is True
    assert report["over_budget_stages"] == ["interpretation"]
    assert report["deadline_ms"] == 120_000


def test_timing_within_deadline_flag():
    now = [0.0]
    timing = Timing(clock=lambda: now[0])
    now[0] = 200.0
    timing.mark("render")
    report = timing.as_dict(form_of(KIND_REVIEW))
    assert report["elapsed_ms"] == 200_000
    assert report["within_deadline"] is True
    assert timing.as_dict(form_of(KIND_LIVE_BRIEF))["within_deadline"] is False


def test_report_scope():
    assert ReportScope().covers(3) is True
    assert ReportScope(completed_games=(1, 2)).covers(3) is False
    assert ReportScope(completed_games=(1, 2), trigger_game_no=2).trigger_game_no == 2
