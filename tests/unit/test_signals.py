"""弹幕信号复核测试（设计 §8.1：候选边界需 ≥2 类独立信号支持）。"""

from __future__ import annotations

import json

from danmu_intel.common.config import StatsConfig
from danmu_intel.slice.signals import (
    BOUNDARY_SIGNAL_KINDS,
    DIRECTION_END,
    DIRECTION_START,
    DanmuWindow,
    density_moments,
    detect_danmu_windows,
    lexical_moments,
    moments,
    review,
    score_moments,
)
from danmu_intel.stats.basic import RawLine

from conftest import REL_PATH, make_event

BASE = 1_790_064_000_000
CONFIG = StatsConfig()


def lines(items: list[tuple[int, str]], *, user: str = "u") -> list[RawLine]:
    return [
        RawLine(REL_PATH, index + 1, make_event(ts, text=text, user=f"{user}{index % 4}"))
        for index, (ts, text) in enumerate(items)
    ]


def burst(start: int, count: int, text: str, *, step: int = 1_000) -> list[tuple[int, str]]:
    return [(start + index * step, text) for index in range(count)]


def test_single_kind_is_not_enough():
    """只有词法一类信号 → 复核不通过（这是设计 §8.1 要拦的那类「凭印象」）。"""
    lonely = lines([(BASE + index * 30_000, "开始了") for index in range(12)])
    claims = review(moments(lonely, config=CONFIG), config=CONFIG)
    assert len(claims) == 1
    assert claims[0].verified is False
    assert claims[0].kinds == ("lexical_start",)
    assert "复核不通过" in claims[0].note and "≥2 类独立信号" in claims[0].note
    assert detect_danmu_windows(lonely, config=CONFIG) == ()


def test_two_kinds_verify_and_pair_into_games():
    """词法 + 密度（≥2 类）→ 复核通过；起点终点成对 → 得出小局窗口。"""
    events = lines(
        burst(BASE, 6, "开始了")
        + [(BASE + 5_000, "2:0"), (BASE + 6_000, "比分 2:0")]
        + burst(BASE + 300_000, 6, "结束了")
    )
    windows = detect_danmu_windows(events, config=CONFIG)
    assert len(windows) == 1
    window = windows[0]
    assert (window.game_no, window.start_ms, window.end_ms) == (1, BASE, BASE + 300_000)
    assert set(window.kinds) >= {"lexical_end", "lexical_start"}
    assert window.evidence["start"]["verified"] is True
    assert json.loads(json.dumps(window.as_dict()))["game_no"] == 1


def test_game_numbers_follow_time_order():
    events = lines(
        burst(BASE, 6, "开始了")
        + burst(BASE + 100_000, 6, "结束了")
        + burst(BASE + 200_000, 6, "第三局开局了")
        + [(BASE + 210_000, "1:1")]
        + burst(BASE + 400_000, 6, "结束了")
    )
    windows = detect_danmu_windows(events, config=CONFIG)
    assert [window.game_no for window in windows] == [1, 2]
    assert windows[1].start_ms == BASE + 200_000


def test_end_without_start_and_start_without_end_are_dropped():
    orphan_end = lines(burst(BASE, 6, "结束了") + [(BASE + 1_000, "2:0")])
    assert detect_danmu_windows(orphan_end, config=CONFIG) == ()
    orphan_start = lines(burst(BASE, 6, "开始了") + [(BASE + 1_000, "2:0")])
    assert detect_danmu_windows(orphan_start, config=CONFIG) == ()


def test_density_and_score_moments():
    dense = lines(burst(BASE, 40, "普通弹幕") + [(BASE + 600_000, "零星")])
    density = density_moments(dense)
    assert density and all(moment.kind == "density_shift" for moment in density)
    assert density[0].evidence["baseline_median"] < density[0].evidence["count"]
    assert density_moments([]) == []
    # 密度均匀 → 没有骤变
    even = lines([(BASE + index * 30_000, "普通弹幕") for index in range(12)])
    assert density_moments(even) == []

    scored = lines([(BASE, "2:0"), (BASE + 1_000, "没有比分")])
    assert [moment.evidence["score"] for moment in score_moments(scored)] == ["2:0"]
    assert score_moments([]) == []
    assert lexical_moments([]) == []


def test_repeated_claims_cluster_by_cluster_ms():
    """同一句收局话在 2 分钟内被刷多次 → 只算一个候选边界。"""
    events = lines(
        burst(BASE, 3, "结束了", step=30_000)
        + burst(BASE + 90_000, 3, "开始了", step=5_000)
        + [(BASE + 95_000, "2:0")]
    )
    claims = review(moments(events, config=CONFIG), config=CONFIG)
    ends = [claim for claim in claims if claim.direction == DIRECTION_END]
    starts = [claim for claim in claims if claim.direction == DIRECTION_START]
    assert len(ends) == 1 and len(starts) == 1
    assert ends[0].hits == 3
    assert all(claim.at_ms >= BASE for claim in claims)


def test_verify_min_kinds_is_configurable():
    lonely = lines([(BASE + index * 30_000, "开始了") for index in range(12)])
    looser = StatsConfig(verify_min_kinds=1)
    claims = review(moments(lonely, config=looser), config=looser)
    assert claims[0].verified is True and claims[0].note is None
    assert detect_danmu_windows(lonely, config=looser) == (), "只有起点没有终点仍然成不了局"


def test_signal_kinds_are_declared():
    assert set(BOUNDARY_SIGNAL_KINDS) == {
        "lexical_start",
        "lexical_end",
        "density_shift",
        "score_mention",
    }
    assert DanmuWindow(game_no=1, start_ms=0, end_ms=1, kinds=(), evidence={}).as_dict()["kinds"] == []
