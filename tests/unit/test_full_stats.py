"""统计全集测试（设计 §9.1 / 需求 FR-C3-1）。

这些断言是 AC-13（可重算）的载体：同样的输入必然给同样的输出，且顺序无关。
"""

from __future__ import annotations

import json

import pytest

from danmu_intel.common import official
from danmu_intel.slice.manual import SliceWindow
from danmu_intel.stats.basic import STEP_MS, WINDOW_MS, RawLine
from danmu_intel.stats.full import (
    KILL_LEXICON,
    compute_game,
    kill_timeline,
    neutral,
    observed_until,
    peak_count,
    score,
    score_mentions,
    trough,
)

from conftest import BASE_TS, REL_PATH, make_event

SIDE_NAMES = {official.SIDE_TEAM_A: "iG", official.SIDE_TEAM_B: "LNG"}
WINDOW = SliceWindow(
    match_id=1, game_no=1, start_ms=BASE_TS, end_ms=BASE_TS + 300_000, boundary_source="manual"
)


def lines(items: list[tuple[int, str]], *, user: str = "u1") -> list[RawLine]:
    return [
        RawLine(REL_PATH, index + 1, make_event(ts, text=text, user=f"{user}{index % 3}"))
        for index, (ts, text) in enumerate(items)
    ]


def test_trough_picks_lowest_window():
    points = [{"t_start": index * STEP_MS, "count": value} for index, value in enumerate([5, 1, 9])]
    assert trough(points) == {"t_start": STEP_MS, "t_end": STEP_MS + WINDOW_MS, "count": 1, "method": "minimum"}
    assert trough([]) is None
    assert trough([{"t_start": 0, "count": 0}]) is None


def test_score_mentions_parses_forms():
    found = score_mentions(lines([(BASE_TS, "2:0 了"), (BASE_TS + 1, "另一局 1比1"), (BASE_TS + 2, "2：1")]))
    assert [item["score"] for item in found] == ["2:0", "1:1", "2:1"]
    assert found[0]["rel_path"] == REL_PATH and found[0]["line_no"] == 1
    assert score_mentions(lines([(BASE_TS, "没有比分"), (BASE_TS + 1, "时间戳 1790064000123")])) == []


def test_score_prefers_official_and_records_discrepancy():
    mentions = score_mentions(lines([(BASE_TS, "2:0"), (BASE_TS + 1_000, "2:0")]))
    agree = score(mentions, {"score": "2:0"}, game_no=1)
    assert agree["official"] == "2:0" and agree["official_scope"] == "match"
    assert agree["danmu_consensus"] == "2:0" and agree["consistent"] is True
    assert agree["discrepancy"] is None and agree["first_ts"] == BASE_TS
    assert agree["samples"][0]["text"] == "2:0"

    mismatch = score(score_mentions(lines([(BASE_TS, "1:1")])), {"score": "2:0"}, game_no=1)
    assert mismatch["official"] == "2:0" and mismatch["consistent"] is False
    assert "以官方为准" in mismatch["discrepancy"]

    per_game = {"games": [{"game_no": 1, "start_ms": 0, "end_ms": 1, "score": "1:0"}]}
    assert score([], per_game, game_no=1)["official_scope"] == "game"
    assert score([], per_game, game_no=1)["official"] == "1:0"
    assert score([], None, game_no=1)["official_scope"] == "missing"
    assert "仅供参考" in score(score_mentions(lines([(BASE_TS, "3:0")])), None, game_no=1)["discrepancy"]
    assert "没有可比对" in score([], {"score": "2:0"}, game_no=1)["discrepancy"]


def test_score_consensus_ties_by_earliest_mention():
    mentions = score_mentions(lines([(BASE_TS, "2:0"), (BASE_TS + 5_000, "1:0")]))
    assert score(mentions, None, game_no=1)["danmu_consensus"] == "2:0"


def test_kill_timeline_prefers_official_events():
    official_result = {"kills": [{"ts": BASE_TS + 10_000, "side": "team_a", "note": "一血"}]}
    result = kill_timeline(lines([(BASE_TS, "单杀了")]), official_result, side_names=SIDE_NAMES)
    assert result["source"] == "official"
    assert result["events"] == [
        {"ts": BASE_TS + 10_000, "side": "team_a", "note": "一血", "source": "official"}
    ]


def test_kill_timeline_extracts_from_danmu_with_side_marker():
    result = kill_timeline(
        lines([(BASE_TS, "iG 单杀了"), (BASE_TS + 1_000, "LNG 团灭"), (BASE_TS + 2_000, "普通弹幕")]),
        None,
        side_names=SIDE_NAMES,
    )
    assert result["source"] == "danmu_signal"
    assert [event["side"] for event in result["events"]] == ["team_a", "team_b"]
    assert all(event["source"] == "danmu_signal" for event in result["events"])
    assert result["events"][0]["line_no"] == 1
    assert KILL_LEXICON
    assert kill_timeline(lines([(BASE_TS, "无关弹幕")]), None, side_names=SIDE_NAMES) == {
        "source": "none",
        "events": [],
    }


def test_neutral_counts_without_judgement():
    result = neutral(
        lines([(BASE_TS, "iG 打得不错"), (BASE_TS + 1, "LNG 也还行"), (BASE_TS + 2, "这局精彩")]),
        side_names=SIDE_NAMES,
    )
    assert result["side_mentions"]["team_a"] == {"name": "iG", "count": 1, "distinct_users": 1}
    assert result["side_mentions"]["team_b"]["count"] == 1
    assert result["side_mentions"]["neutral"]["count"] == 1
    assert result["danmu_total"] == 3
    assert result["coverage"] == {"first_ts": BASE_TS, "last_ts": BASE_TS + 2}
    assert neutral([], side_names={})["coverage"] == {"first_ts": None, "last_ts": None}


def test_compute_game_returns_full_metric_set_and_is_pure():
    items = [(BASE_TS + index * 3_000, "普通弹幕") for index in range(10)]
    items += [(BASE_TS + 60_000 + index * 1_000, f"iG 击杀 {index}") for index in range(45)]
    items += [(BASE_TS + 120_000, "比分 1:1")]
    scoped = lines(items)
    first = compute_game(scoped, WINDOW, official_result={"score": "1:1"}, side_names=SIDE_NAMES)
    second = compute_game(list(reversed(scoped)), WINDOW, official_result={"score": "1:1"}, side_names=SIDE_NAMES)
    assert json.dumps(first, sort_keys=True, ensure_ascii=False) == json.dumps(
        second, sort_keys=True, ensure_ascii=False
    )
    assert set(first) == {
        "danmu_total",
        "distinct_users",
        "density_curve",
        "peak",
        "trough",
        "score",
        "kill_timeline",
        "neutral",
    }
    assert first["danmu_total"] == {"count": 56}
    assert first["score"]["consistent"] is True
    assert first["kill_timeline"]["source"] == "danmu_signal"
    assert first["neutral"]["side_mentions"]["team_a"]["count"] == 45
    assert peak_count(first) == first["peak"]["count"] > 0


def test_compute_game_without_data_is_still_complete():
    empty = compute_game([], WINDOW, official_result=None, side_names=None)
    assert empty["peak"] == {} and empty["trough"] == {}
    assert empty["kill_timeline"] == {"source": "none", "events": []}
    assert empty["score"]["official"] is None and empty["score"]["danmu_consensus"] is None
    assert peak_count(empty) == 0
    assert observed_until([]) is None
    assert observed_until(lines([(BASE_TS, "a"), (BASE_TS + 5, "b")])) == BASE_TS + 6


@pytest.mark.parametrize("raw", ["", "不是比分", "第 3 局"])
def test_score_mentions_ignores_non_scores(raw):
    assert score_mentions(lines([(BASE_TS, raw)])) == []
