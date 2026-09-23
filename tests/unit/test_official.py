"""官方数据契约测试（`matches.official_result`）。

铁律：官方数据**缺哪项就报缺**，不做补全、不做猜测（需求 §6.3 第 1 优先级 + NFR-Q-1）。
"""

from __future__ import annotations

from danmu_intel.common import official

FULL = {
    "score": "2:0",
    "games": [
        {"game_no": 2, "start_ms": 200, "end_ms": 300, "score": "1:1"},
        {"game_no": 1, "start_ms": 0, "end_ms": 100, "score": "1:0"},
        {"game_no": 3, "start_ms": 400},  # 缺 end_ms → 跳过
        "垃圾数据",
    ],
    "kills": [
        {"ts": 20, "side": "team_b", "note": "反打"},
        {"ts": 10, "side": "team_a"},
        {"note": "缺 ts"},  # 跳过
    ],
}


def test_games_are_sorted_and_incomplete_entries_dropped():
    assert [entry["game_no"] for entry in official.games(FULL)] == [1, 2]
    assert official.games(None) == ()
    assert official.games({"games": "坏形状"}) == ()
    assert official.game(FULL, 2)["score"] == "1:1"
    assert official.game(FULL, 9) is None


def test_score_for_game_scopes():
    assert official.score_for_game(FULL, 1) == ("1:0", "game")
    assert official.score_for_game({"score": "2:0"}, 1) == ("2:0", "match")
    assert official.score_for_game({}, 1) == (None, "missing")
    assert official.score_for_game(None, 1) == (None, "missing")
    assert official.match_score(FULL) == "2:0"
    assert official.match_score({}) is None


def test_kills_are_sorted_with_default_side_and_note():
    events = official.kills(FULL)
    assert [event["ts"] for event in events] == [10, 20]
    assert events[0] == {"ts": 10, "side": "team_a", "note": ""}
    assert events[1]["note"] == "反打"
    assert official.kills(None) == ()
    assert official.SIDES == ("team_a", "team_b", "neutral")
