"""原始记录契约的聚合面：跨房间去重键与去重纯函数（issue #5 §4，AC-15）。"""

from __future__ import annotations

from danmu_intel.common.events import dedupe, message_key, msg_hash

from conftest import BASE_TS, make_event


def test_msg_hash_is_content_stable():
    event = make_event(BASE_TS, text="这波团开得太急了", user="u1")
    assert msg_hash(event) == msg_hash(make_event(BASE_TS, text="这波团开得太急了", user="u1"))
    assert msg_hash(event) != msg_hash(make_event(BASE_TS + 1, text="这波团开得太急了", user="u1"))
    assert msg_hash(event) != msg_hash(make_event(BASE_TS, text="这波团开得太急了", user="u2"))
    assert msg_hash(event) != msg_hash(make_event(BASE_TS, text="另一条", user="u1"))


def test_message_key_includes_platform_and_room():
    event = make_event(BASE_TS, room_id="660000")
    other_room = make_event(BASE_TS, room_id="323444")
    other_platform = make_event(BASE_TS, platform="soop")
    assert message_key(event) == ("huya", "660000", msg_hash(event))
    assert message_key(event) != message_key(other_room)
    assert message_key(event) != message_key(other_platform)


def test_dedupe_collapses_same_record_and_keeps_rooms_apart():
    """同一条记录重复落盘只算一条；不同房间的同文弹幕各算一条。"""
    first = make_event(BASE_TS, text="GG", user="u1", room_id="660000")
    duplicate = make_event(BASE_TS, text="GG", user="u1", room_id="660000")
    other_room = make_event(BASE_TS, text="GG", user="u1", room_id="323444")
    later = make_event(BASE_TS + 1_000, text="GG", user="u1", room_id="660000")

    kept = dedupe([first, duplicate, other_room, later])
    assert kept == [first, other_room, later]


def test_dedupe_preserves_order_and_ignores_match_id():
    """去重不改顺序；`match_id` 不参与键（记录归属由采集会话决定）。"""
    events = [make_event(BASE_TS + index * 1_000, text=f"弹幕 {index}") for index in range(5)]
    assert dedupe(events) == events
    assert dedupe(reversed(events)) == list(reversed(events))
    assert dedupe([]) == []
    with_match = [make_event(BASE_TS, text="同一条", match_id=7), make_event(BASE_TS, text="同一条", match_id=8)]
    assert len(dedupe(with_match)) == 1
