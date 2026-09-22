"""切片层单测：人工切片 + 覆盖留痕（设计 §8.1）。"""

from __future__ import annotations

import pytest

from danmu_intel.slice.manual import BOUNDARY_SOURCES, MANUAL, add_manual_slice, load_slices

from conftest import BASE_TS


def test_add_manual_slice_writes_row(conn):
    slice_id = add_manual_slice(conn, match_id=1, game_no=1, start_ms=0, end_ms=60_000)
    assert slice_id > 0
    windows = load_slices(conn, 1)
    assert len(windows) == 1
    window = windows[0]
    assert (window.game_no, window.start_ms, window.end_ms) == (1, 0, 60_000)
    assert window.boundary_source == MANUAL
    assert window.override_by is None and window.override_reason is None
    assert BOUNDARY_SOURCES == ("official", "danmu_signal", "report_window", "manual")


def test_add_slice_rejects_bad_range(conn):
    with pytest.raises(ValueError):
        add_manual_slice(conn, match_id=1, game_no=1, start_ms=100, end_ms=100)
    with pytest.raises(ValueError):
        add_manual_slice(conn, match_id=1, game_no=1, start_ms=200, end_ms=100)


def test_same_bounds_is_idempotent(conn):
    first = add_manual_slice(conn, match_id=1, game_no=1, start_ms=0, end_ms=60_000, note="首切")
    second = add_manual_slice(conn, match_id=1, game_no=1, start_ms=0, end_ms=60_000)
    assert first == second
    assert load_slices(conn, 1)[0].conflict_note == "首切"


def test_changing_bounds_requires_override_trace(conn):
    add_manual_slice(conn, match_id=1, game_no=1, start_ms=0, end_ms=60_000)
    with pytest.raises(ValueError, match="override_by"):
        add_manual_slice(conn, match_id=1, game_no=1, start_ms=0, end_ms=90_000)


def test_changing_bounds_records_override(conn):
    add_manual_slice(conn, match_id=1, game_no=1, start_ms=0, end_ms=60_000)
    add_manual_slice(
        conn,
        match_id=1,
        game_no=1,
        start_ms=0,
        end_ms=90_000,
        override_by="管理员",
        override_reason="官方时间回填",
    )
    window = load_slices(conn, 1)[0]
    assert window.end_ms == 90_000
    assert window.override_by == "管理员"
    assert window.override_reason == "官方时间回填"
    row = conn.execute("SELECT override_at FROM slices").fetchone()
    assert row["override_at"] is not None

    # 再改一次但保留原操作者与理由
    add_manual_slice(conn, match_id=1, game_no=1, start_ms=10, end_ms=90_000, override_by="管理员", override_reason="再修")
    assert load_slices(conn, 1)[0].start_ms == 10


def test_slices_are_ordered_by_game_no(conn):
    add_manual_slice(conn, match_id=1, game_no=2, start_ms=60_000, end_ms=120_000)
    add_manual_slice(conn, match_id=1, game_no=1, start_ms=0, end_ms=60_000)
    assert [window.game_no for window in load_slices(conn, 1)] == [1, 2]
    assert load_slices(conn, 999) == []


def test_slices_are_per_match(conn):
    add_manual_slice(conn, match_id=1, game_no=1, start_ms=BASE_TS, end_ms=BASE_TS + 6_000)
    add_manual_slice(conn, match_id=2, game_no=1, start_ms=BASE_TS, end_ms=BASE_TS + 6_000)
    assert len(load_slices(conn, 1)) == 1
    assert len(load_slices(conn, 2)) == 1
