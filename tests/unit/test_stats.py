"""规则统计纯函数测试（设计 §9 的铁律：不读时钟、不读网络、不读全局状态）。

这些断言是 AC-13（可重算）的主要载体：同样的输入必然给同样的输出。
"""

from __future__ import annotations

import json

import pytest

from danmu_intel.slice.manual import SliceWindow
from danmu_intel.stats.basic import (
    ALGO_VERSION,
    PEAK_ABSOLUTE,
    STEP_MS,
    WINDOW_MS,
    RawLine,
    compute,
    coverage_span,
    density_curve,
    distinct_users,
    in_window,
    peak,
    peak_sample,
    select,
    total,
)

from conftest import BASE_TS, REL_PATH, make_event

WINDOW = SliceWindow(match_id=1, game_no=1, start_ms=0, end_ms=300_000, boundary_source="manual")


def lines(timestamps: list[int]) -> list[RawLine]:
    return [
        RawLine(REL_PATH, index + 1, make_event(ts, text=f"弹幕 {index}"))
        for index, ts in enumerate(timestamps)
    ]


def test_constants_are_documented_values():
    assert (WINDOW_MS, STEP_MS, PEAK_ABSOLUTE) == (60_000, 30_000, 40)
    assert ALGO_VERSION == "1.0.0"


def test_density_curve_sliding_windows():
    points = density_curve([0, 1_000, 61_000], start_ms=0, end_ms=120_000)
    assert [point["t_start"] for point in points] == [0, 30_000, 60_000, 90_000]
    assert [point["count"] for point in points] == [2, 1, 1, 0]
    assert density_curve([0], start_ms=100, end_ms=100) == []
    assert density_curve([], start_ms=0, end_ms=WINDOW_MS)[0]["count"] == 0


def test_density_curve_is_independent_of_input_order():
    shuffled = [61_000, 0, 1_000]
    assert density_curve(shuffled, 0, 120_000) == density_curve(sorted(shuffled), 0, 120_000)


def test_peak_requires_significance():
    flat = [{"t_start": index * STEP_MS, "count": 3} for index in range(4)]
    assert peak(flat) is None
    assert peak([]) is None
    assert peak([{"t_start": 0, "count": 0}]) is None


def test_peak_uses_mean_plus_three_sigma():
    points = [{"t_start": index * STEP_MS, "count": 0} for index in range(20)]
    points.append({"t_start": 20 * STEP_MS, "count": 30})
    result = peak(points)
    assert result is not None
    assert result["count"] == 30
    assert result["method"] == "mean+3sigma"
    assert result["t_end"] == result["t_start"] + WINDOW_MS
    assert result["threshold"] == pytest.approx(20.595, abs=0.001)


def test_peak_falls_back_to_absolute_threshold():
    points = [{"t_start": index * STEP_MS, "count": PEAK_ABSOLUTE + 1} for index in range(5)]
    result = peak(points)
    assert result is not None
    assert result["method"] == "absolute"
    assert result["count"] == PEAK_ABSOLUTE + 1


def test_peak_picks_earliest_window_on_tie():
    points = [{"t_start": 0, "count": 50}, {"t_start": 30_000, "count": 50}]
    assert peak(points)["t_start"] == 0


def test_peak_sample_is_traceable():
    scoped = lines([BASE_TS, BASE_TS + 1_000, BASE_TS + 90_000])
    top = {"t_start": BASE_TS, "t_end": BASE_TS + WINDOW_MS}
    sample = peak_sample(scoped, top)
    assert [item["line_no"] for item in sample] == [1, 2]
    assert sample[0]["rel_path"] == REL_PATH


def test_select_and_window_boundaries():
    scoped = lines([0, 59_999, 60_000])
    window = SliceWindow(match_id=1, game_no=1, start_ms=0, end_ms=60_000, boundary_source="manual")
    assert [line.event.ts for line in select(scoped, window)] == [0, 59_999]
    assert in_window(scoped[2], window) is False


def test_total_and_distinct_users():
    scoped = [
        RawLine(REL_PATH, 1, make_event(0, user="a")),
        RawLine(REL_PATH, 2, make_event(1, user="a")),
        RawLine(REL_PATH, 3, make_event(2, user="b")),
    ]
    assert total(scoped) == {"count": 3}
    assert distinct_users(scoped) == {"count": 2}
    assert total([]) == {"count": 0}


def test_coverage_span():
    assert coverage_span(lines([5, 1, 9])) == {"first_ts": 1, "last_ts": 9}
    assert coverage_span([]) == {"first_ts": None, "last_ts": None}


def test_compute_is_pure_and_json_serializable():
    scoped = lines([BASE_TS + index * 3_000 for index in range(10)])
    scoped += lines([BASE_TS + 60_000 + index * 1_000 for index in range(45)])
    window = SliceWindow(
        match_id=1, game_no=1, start_ms=BASE_TS, end_ms=BASE_TS + 300_000, boundary_source="manual"
    )
    first = compute(scoped, window)
    second = compute(list(reversed(scoped)), window)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert set(first) == {"danmu_total", "distinct_users", "density_curve", "peak"}
    assert first["danmu_total"] == {"count": 55}
    assert first["density_curve"]["window_ms"] == WINDOW_MS
    assert first["peak"]["method"] == "absolute"
    assert first["peak"]["count"] == 45


def test_compute_without_significant_peak():
    window = SliceWindow(
        match_id=1, game_no=1, start_ms=BASE_TS, end_ms=BASE_TS + 300_000, boundary_source="manual"
    )
    result = compute(lines([BASE_TS, BASE_TS + 1_000]), window)
    assert result["peak"] == {}
