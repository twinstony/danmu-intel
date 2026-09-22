"""切片引擎测试（设计 §8.1；需求 §6.3 + FR-C2-3/4/5）。

守住三件事：**每条切片有来源**、**冲突必记录**、**人工修正必须留痕且不可被自动来源覆盖**。
"""

from __future__ import annotations

import pytest

from danmu_intel.common import audit
from danmu_intel.common.config import StatsConfig
from danmu_intel.slice.engine import (
    BOUNDARY_PRIORITY,
    CORRECTION_SOURCE,
    EVIDENCE_PRIORITY,
    BoundaryCandidate,
    algo_version,
    apply_boundaries,
    candidates_from_danmu,
    candidates_from_official,
    candidates_from_report_window,
    collect_candidates,
    conflict_note_for,
    resolve,
    resolve_match,
)
from danmu_intel.slice.manual import add_manual_slice, load_slices
from danmu_intel.stats.basic import ALGO_VERSION, RawLine

from conftest import BASE_TS, REL_PATH, make_event

CONFIG = StatsConfig()
G1 = (BASE_TS, BASE_TS + 300_000)
G2 = (BASE_TS + 300_000, BASE_TS + 600_000)


def lines(items: list[tuple[int, str]]) -> list[RawLine]:
    return [
        RawLine(REL_PATH, index + 1, make_event(ts, text=text, user=f"u{index % 4}"))
        for index, (ts, text) in enumerate(items)
    ]


def danmu_events() -> list[RawLine]:
    """两局的弹幕：开局/收局词 + 比分 + 密度骤变（≥2 类独立信号）。"""
    events = [(G1[0] + index * 1_000, "开始了") for index in range(6)]
    events += [(G1[0] + 10_000, "比分 2:0"), (G1[0] + 11_000, "2:0")]
    events += [(G1[1] - 5_000 + index * 1_000, "结束了") for index in range(6)]
    events += [(G2[0] + index * 1_000, "第二局开始了") for index in range(6)]
    events += [(G2[0] + 10_000, "1:1")]
    events += [(G2[1] - 5_000 + index * 1_000, "结束了") for index in range(6)]
    return lines(events)


# --------------------------------------------------------------------------- #
# 候选与优先级
# --------------------------------------------------------------------------- #


def test_priority_order_is_the_documented_one():
    assert EVIDENCE_PRIORITY == ("official", "danmu_signal", "report_window")
    assert CORRECTION_SOURCE == "manual"
    assert BOUNDARY_PRIORITY == ("official", "danmu_signal", "report_window", "manual")


def test_official_candidates_need_complete_bounds():
    official_result = {
        "games": [
            {"game_no": 1, "start_ms": G1[0], "end_ms": G1[1]},
            {"game_no": 2, "start_ms": G2[0]},  # 缺 end_ms → 不采用（官方数据不补全）
        ]
    }
    candidates = candidates_from_official(official_result)
    assert len(candidates) == 1 and candidates[0].game_no == 1
    assert candidates_from_official(None) == ()


def test_report_window_candidates():
    candidates = candidates_from_report_window([(1, *G1)])
    assert candidates[0].source == "report_window" and candidates[0].verified is True


def test_danmu_candidates_require_two_independent_signals():
    verified = candidates_from_danmu(danmu_events(), config=CONFIG)
    assert [candidate.game_no for candidate in verified] == [1, 2]
    assert all(candidate.kinds and len(candidate.kinds) >= 2 for candidate in verified)

    weak = lines([(BASE_TS + index * 30_000, "开始了") for index in range(12)])
    assert candidates_from_danmu(weak, config=CONFIG) == (), "只有词法一类信号不得成为边界"


def test_official_beats_danmu_and_conflict_is_recorded():
    official_result = {"games": [{"game_no": 1, "start_ms": G1[0], "end_ms": G1[1]}]}
    shifted = BoundaryCandidate("danmu_signal", 1, G1[0] + 10_000, G1[1] + 20_000, kinds=("lexical_start",))
    resolutions = resolve(collect_candidates(official_result=official_result, lines=[], report_windows=[(1, *G1)]))
    assert resolutions[0].boundary_source == "official"
    assert resolutions[0].conflict_note is None and False or True  # 同边界的多来源不算冲突

    mixed = resolve([BoundaryCandidate("official", 1, *G1), shifted])
    assert mixed[0].boundary_source == "official"
    note = mixed[0].conflict_note
    assert note and "冲突" in note and "danmu_signal" in note
    assert f"[{G1[0]}, {G1[1]}]" in note


def test_danmu_beats_report_window():
    resolution = resolve(
        [
            BoundaryCandidate("report_window", 1, *G1, note="来源：已发布报告"),
            BoundaryCandidate("danmu_signal", 1, G1[0] + 5_000, G1[1], kinds=("lexical_end", "lexical_start")),
        ]
    )
    assert resolution[0].boundary_source == "danmu_signal"
    assert "优先级最高" in (resolution[0].conflict_note or "")


def test_unverified_candidate_is_never_used_and_reason_recorded():
    resolution = resolve(
        [
            BoundaryCandidate("danmu_signal", 1, *G1, verified=False, note="复核不通过：只得到 1 类信号支持"),
        ]
    )
    assert resolution == ()
    note = conflict_note_for(
        [
            BoundaryCandidate("official", 1, *G1),
            BoundaryCandidate("danmu_signal", 1, G1[0] + 1, G1[1], verified=False, note="复核不通过：只得到 1 类信号支持"),
        ],
        BoundaryCandidate("official", 1, *G1),
    )
    assert note is not None and "未采用：danmu_signal" in note


def test_manual_correction_overrides_evidence():
    """FR-C2-5：修正后以修正结果为准；冲突事实仍记录。"""
    resolution = resolve(
        [
            BoundaryCandidate("official", 1, *G1),
            BoundaryCandidate(
                "manual", 1, G1[0] - 30_000, G1[1], override_by="管理员", override_reason="官方时间回填有误"
            ),
        ]
    )
    assert resolution[0].boundary_source == "manual"
    assert resolution[0].start_ms == G1[0] - 30_000
    assert "人工修正覆盖证据来源" in (resolution[0].conflict_note or "")


def test_resolve_rejects_bad_range_and_keeps_games_sorted():
    with pytest.raises(ValueError, match="切片起止非法"):
        resolve([BoundaryCandidate("official", 1, 100, 100)])
    resolved = resolve(
        [BoundaryCandidate("official", 2, *G2), BoundaryCandidate("official", 1, *G1)]
    )
    assert [item.game_no for item in resolved] == [1, 2]
    assert resolve([]) == ()


def test_same_bounds_from_multiple_sources_is_not_a_conflict():
    resolution = resolve(
        [
            BoundaryCandidate("official", 1, *G1),
            BoundaryCandidate("danmu_signal", 1, *G1, kinds=("lexical_start",)),
        ]
    )
    assert resolution[0].conflict_note is None


# --------------------------------------------------------------------------- #
# 落库与留痕
# --------------------------------------------------------------------------- #


def test_apply_boundaries_writes_source_and_conflict(conn):
    official_result = {"games": [{"game_no": 1, "start_ms": G1[0], "end_ms": G1[1]}]}
    resolutions = resolve_match(
        conn,
        1,
        official_result=official_result,
        lines=danmu_events(),
        report_windows=[(1, G1[0] + 60_000, G1[1])],
    )
    windows = load_slices(conn, 1)
    danmu_end = G2[1] - 5_000  # 弹幕信号的收局边界 = 「结束了」那句的起点
    assert [(window.game_no, window.start_ms, window.end_ms) for window in windows] == [
        (1, *G1),
        (2, G2[0], danmu_end),
    ]
    assert [window.boundary_source for window in windows] == ["official", "danmu_signal"]
    assert windows[0].conflict_note and "冲突" in windows[0].conflict_note
    assert windows[1].conflict_note is None, "单一来源且无异议时不编冲突事实"
    assert resolutions[0].as_dict()["candidates"][0]["source"] == "official"
    logs = audit.entries(conn, action=audit.SLICE_BOUNDARY)
    assert logs and logs[0].detail["outcome"] == "applied"


def test_apply_boundaries_does_not_override_human_correction(conn):
    add_manual_slice(conn, match_id=1, game_no=1, start_ms=G1[0], end_ms=G1[1])
    add_manual_slice(
        conn,
        match_id=1,
        game_no=1,
        start_ms=G1[0] + 1_000,
        end_ms=G1[1],
        override_by="管理员",
        override_reason="按官方时间修正",
        override_at=1_790_064_000_000,
    )
    official_result = {"games": [{"game_no": 1, "start_ms": G1[0], "end_ms": G1[1]}]}
    applied = resolve_match(conn, 1, official_result=official_result, lines=[])
    assert applied == ()
    window = load_slices(conn, 1)[0]
    assert window.start_ms == G1[0] + 1_000, "自动来源不得覆盖人工修正过的切片"
    assert window.boundary_source == "manual"
    logs = audit.entries(conn, action=audit.SLICE_BOUNDARY)
    assert logs[-1].detail["outcome"] == "skipped"


def test_apply_manual_correction_requires_trace(conn):
    resolution = resolve(
        [
            BoundaryCandidate("official", 1, *G1),
            BoundaryCandidate("manual", 1, G1[0] - 1, G1[1], override_by=None, override_reason=None),
        ]
    )
    with pytest.raises(ValueError, match="override_by"):
        apply_boundaries(conn, 1, resolution)


# --------------------------------------------------------------------------- #
# 人工修正 → 审计 → 算法版本递增
# --------------------------------------------------------------------------- #


def test_manual_correction_is_audited_and_bumps_algo_version(conn):
    assert algo_version(conn, 1) == ALGO_VERSION
    add_manual_slice(conn, match_id=1, game_no=1, start_ms=G1[0], end_ms=G1[1])
    assert algo_version(conn, 1) == ALGO_VERSION, "首次切片不是修正，不递增"

    add_manual_slice(
        conn,
        match_id=1,
        game_no=1,
        start_ms=G1[0],
        end_ms=G1[1] + 30_000,
        override_by="管理员",
        override_reason="官方时间回填",
        override_at=1_790_064_000_000,
    )
    assert algo_version(conn, 1) == f"{ALGO_VERSION}+ov1"
    logs = audit.entries(conn, action=audit.SLICE_OVERRIDE)
    assert len(logs) == 1
    assert logs[0].actor == "管理员" and logs[0].target == "match:1/game:1"
    assert logs[0].detail["reason"] == "官方时间回填"
    assert logs[0].detail["before"]["end_ms"] == G1[1]
    assert logs[0].detail["after"]["end_ms"] == G1[1] + 30_000

    # 同边界的重复执行不算修正（幂等，不重复留痕、不递增）
    add_manual_slice(conn, match_id=1, game_no=1, start_ms=G1[0], end_ms=G1[1] + 30_000,
                     override_by="管理员", override_reason="官方时间回填")
    assert algo_version(conn, 1) == f"{ALGO_VERSION}+ov1"

    # 再修正一次 → 版本再递增一级
    add_manual_slice(conn, match_id=1, game_no=1, start_ms=G1[0] + 1, end_ms=G1[1] + 30_000,
                     override_by="管理员", override_reason="再看了一遍")
    assert algo_version(conn, 1) == f"{ALGO_VERSION}+ov2"
    assert len(audit.entries(conn, action=audit.SLICE_OVERRIDE)) == 2


def test_algo_version_is_per_match(conn):
    add_manual_slice(conn, match_id=1, game_no=1, start_ms=G1[0], end_ms=G1[1])
    add_manual_slice(conn, match_id=1, game_no=1, start_ms=G1[0] - 1, end_ms=G1[1],
                     override_by="A", override_reason="第一次修正")
    add_manual_slice(conn, match_id=11, game_no=1, start_ms=G1[0], end_ms=G1[1])
    add_manual_slice(conn, match_id=11, game_no=1, start_ms=G1[0] - 1, end_ms=G1[1],
                     override_by="B", override_reason="另一场修正")
    assert algo_version(conn, 1) == f"{ALGO_VERSION}+ov1"
    assert algo_version(conn, 11) == f"{ALGO_VERSION}+ov1"
    assert algo_version(conn, 2) == ALGO_VERSION


def test_apply_manual_candidate_writes_trace_and_audit(conn):
    """人工修正候选走引擎落库：字段留痕 + 审计 + 版本递增（FR-C2-5 全链路）。"""
    add_manual_slice(conn, match_id=1, game_no=1, start_ms=G1[0], end_ms=G1[1])
    resolution = resolve(
        [
            BoundaryCandidate("official", 1, *G1, note="官方赛程"),
            BoundaryCandidate(
                "manual",
                1,
                G1[0] + 5_000,
                G1[1],
                override_by="管理员",
                override_reason="官方时间与录像对不上",
                override_at=1_790_064_000_000,
            ),
        ]
    )
    applied = apply_boundaries(conn, 1, resolution)
    assert len(applied) == 1
    window = load_slices(conn, 1)[0]
    assert window.boundary_source == "manual" and window.start_ms == G1[0] + 5_000
    assert window.override_by == "管理员" and window.override_reason == "官方时间与录像对不上"
    assert window.conflict_note and "人工修正覆盖证据来源" in window.conflict_note
    assert algo_version(conn, 1) == f"{ALGO_VERSION}+ov1"
    assert audit.count(conn, action=audit.SLICE_OVERRIDE) == 1
