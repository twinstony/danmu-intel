"""后台的写入口（存储层）：直播间增删改查、灰信号评审、比赛删除。

三件事逐条对着需求验：FR-C8-1（页面上增删改查，不碰文件）、FR-C8-4（改动留痕）、
需求 §6.5 第 6 条（灰信号作废必须留原因）、以及「原始记录是账本」那条数据纪律。
"""

from __future__ import annotations

import json

import pytest

from danmu_intel.common import audit, gray_review, rooms
from danmu_intel.common.matches import create_match, delete_match, get_match

BASE = 1_790_064_000_000


def test_add_room_creates_and_audits(conn):
    room = rooms.add_room(
        conn, platform="huya", room_id="660000", url="https://www.huya.com/660000",
        streamer="主播甲", actor="admin", ts=BASE,
    )
    assert room.label == "huya/660000" and room.is_live is False and room.last_seen_at is None
    entries = audit.entries(conn, action=rooms.ACTION_ADD)
    assert len(entries) == 1 and entries[0].actor == "admin" and entries[0].ts == BASE
    assert entries[0].target == "huya/660000" and entries[0].detail["created"] is True


def test_add_room_twice_updates_instead_of_duplicating(conn):
    rooms.add_room(conn, platform="huya", room_id="660000", url="https://a", actor="admin")
    again = rooms.add_room(
        conn, platform="huya", room_id="660000", url="https://b", streamer="主播乙", actor="admin"
    )
    assert again.url == "https://b" and again.streamer == "主播乙"
    assert conn.execute("SELECT COUNT(*) AS n FROM rooms").fetchone()["n"] == 1
    assert audit.entries(conn, action=rooms.ACTION_ADD)[-1].detail["created"] is False


@pytest.mark.parametrize(
    "kwargs",
    [
        {"platform": "", "room_id": "1", "url": "u"},
        {"platform": "huya", "room_id": "", "url": "u"},
        {"platform": "huya", "room_id": "1", "url": ""},
        {"platform": "huya", "room_id": "1", "url": "u", "discovered_by": "猜的"},
    ],
)
def test_add_room_validates_input(conn, kwargs):
    with pytest.raises(ValueError):
        rooms.add_room(conn, actor="admin", **kwargs)


def test_update_room_records_before_and_after(conn):
    room = rooms.add_room(conn, platform="huya", room_id="660000", url="https://a", actor="admin")
    updated = rooms.update_room(conn, room.id, url="https://b", actor="admin", ts=BASE + 5)
    assert updated.url == "https://b" and updated.streamer is None
    entry = audit.entries(conn, action=rooms.ACTION_UPDATE)[-1]
    assert entry.detail == {"before": {"url": "https://a", "streamer": None},
                            "after": {"url": "https://b", "streamer": None}}


def test_update_room_needs_a_change(conn):
    room = rooms.add_room(conn, platform="huya", room_id="1", url="u", actor="admin")
    with pytest.raises(ValueError):
        rooms.update_room(conn, room.id, actor="admin")
    with pytest.raises(LookupError):
        rooms.update_room(conn, 999, url="u", actor="admin")


def test_delete_room_refuses_when_it_has_sessions(conn):
    """采集过的房间不许删：原始 JSONL 是账本，房间行是它的出处。"""
    room = rooms.add_room(conn, platform="huya", room_id="660000", url="u", actor="admin")
    conn.execute(
        "INSERT INTO room_sessions(room_id, pid, started_at, state) VALUES(?, 1, ?, 'exited')",
        (room.id, BASE),
    )
    conn.commit()
    with pytest.raises(ValueError) as excinfo:
        rooms.delete_room(conn, room.id, actor="admin")
    assert "采集会话" in str(excinfo.value)
    assert rooms.list_rooms(conn) != []


def test_delete_room_without_data_is_audited(conn):
    room = rooms.add_room(conn, platform="huya", room_id="660000", url="u", actor="admin")
    deleted = rooms.delete_room(conn, room.id, actor="admin", ts=BASE + 7)
    assert deleted.label == "huya/660000"
    assert rooms.list_rooms(conn) == []
    assert audit.entries(conn, action=rooms.ACTION_DELETE)[-1].ts == BASE + 7


def seed_signal(conn, *, status: str = "candidate", keyword: str = "盘口") -> int:
    match_id = create_match(conn, league="LPL", team_a="iG", team_b="LNG", state="live")
    cursor = conn.execute(
        "INSERT INTO gray_signals(match_id, category, keyword, hit_count, distinct_users,"
        " window_count, samples_json, status, reason, created_at, evaluated_at)"
        " VALUES(?, 'betting', ?, 9, 4, 3, ?, ?, NULL, ?, ?)",
        (
            match_id,
            keyword,
            json.dumps([{"ts": BASE, "text": "这盘口有问题", "rel_path": "raw/huya/1.jsonl", "line_no": 3}]),
            status,
            BASE,
            BASE,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def test_list_and_get_signals(conn):
    signal_id = seed_signal(conn)
    signals = gray_review.list_signals(conn)
    assert len(signals) == 1
    signal = signals[0]
    assert (signal.id, signal.status, signal.hit_count) == (signal_id, "candidate", 9)
    assert signal.category_label == "盘口讨论聚集"
    assert signal.samples[0].text == "这盘口有问题" and signal.samples[0].line_no == 3
    assert gray_review.list_signals(conn, status="discarded") == []
    assert gray_review.list_signals(conn, match_id=signal.match_id) == signals
    assert gray_review.get_signal(conn, signal_id) == signal
    with pytest.raises(LookupError):
        gray_review.get_signal(conn, 999)


def test_review_escalates_with_reason_and_audit(conn):
    signal_id = seed_signal(conn)
    after = gray_review.review(
        conn, signal_id, action=gray_review.ESCALATE, reason="样本集中且多时段", actor="admin", ts=BASE + 3
    )
    assert after.status == "escalated" and after.evaluated_at == BASE + 3
    entry = audit.entries(conn, action=gray_review.ACTION_REVIEW)[-1]
    assert entry.detail["from"] == "candidate" and entry.detail["to"] == "escalated"
    assert entry.detail["reason"] == "样本集中且多时段"


def test_review_discards_with_reason(conn):
    signal_id = seed_signal(conn)
    after = gray_review.review(
        conn, signal_id, action=gray_review.DISCARD, reason="同一人反复刷", actor="admin"
    )
    assert after.status == "discarded" and after.reason == "同一人反复刷"


def test_review_requires_reason_and_known_action(conn):
    signal_id = seed_signal(conn)
    with pytest.raises(ValueError) as excinfo:
        gray_review.review(conn, signal_id, action=gray_review.DISCARD, reason="   ", actor="admin")
    assert "理由" in str(excinfo.value)
    with pytest.raises(ValueError):
        gray_review.review(conn, signal_id, action="确认作弊", reason="有理由", actor="admin")
    with pytest.raises(LookupError):
        gray_review.review(conn, 999, action=gray_review.DISCARD, reason="有理由", actor="admin")


def test_escalated_signal_survives_recompute(conn, data_root):
    """人工升级过的行不被统计重算覆盖（T4 的流水线与 T12 的后台在这一条上对齐）。"""
    from danmu_intel.pipeline import write_gray_signals
    from danmu_intel.report.facts import MatchFacts

    signal_id = seed_signal(conn, keyword="内幕")
    gray_review.review(conn, signal_id, action=gray_review.ESCALATE, reason="需人工复核", actor="admin")

    facts = MatchFacts(
        match=get_match(conn, gray_review.get_signal(conn, signal_id).match_id),
        games=(),
        all_lines=(),
        segments=(),
        algo_version="1.0.0",
        data_root=data_root,
        generated_at=BASE,
        stats_config=None,
        final_judgement=_fake_judgement(),
        signal_facts=(),
        gray_signals=(),
    )
    write_gray_signals(conn, facts, now=BASE)
    assert conn.execute("SELECT COUNT(*) AS n FROM gray_signals").fetchone()["n"] == 1
    assert gray_review.get_signal(conn, signal_id).status == "escalated"


def _fake_judgement():
    from danmu_intel.stats.final import FinalJudgement

    return FinalJudgement(verdict="live", reason="测试用", satisfied_at_ms=None, kinds=(), decided_at_ms=None)


def test_delete_match_refuses_when_data_exists(conn):
    match_id = create_match(conn, league="LPL", team_a="iG", team_b="LNG", state="ended")
    conn.execute("INSERT INTO slices(match_id, game_no, start_ms, end_ms, boundary_source) VALUES(?, 1, 1, 2, 'manual')", (match_id,))
    conn.commit()
    with pytest.raises(ValueError) as excinfo:
        delete_match(conn, match_id, actor="admin")
    assert "slices" in str(excinfo.value)


def test_delete_match_without_data_is_audited(conn):
    match_id = create_match(conn, league="LPL", team_a="iG", team_b="LNG", state="ended")
    deleted = delete_match(conn, match_id, actor="admin", ts=BASE + 1)
    assert deleted.id == match_id
    with pytest.raises(LookupError):
        get_match(conn, match_id)
    entry = audit.entries(conn, action="match.delete")[-1]
    assert entry.detail["league"] == "LPL" and entry.target == str(match_id)


def test_room_changes_bump_the_config_version(conn):
    """数据源改动落在同一个版本号上：监督进程据此在 1 分钟内增/停子进程（FR-C8-2）。"""
    from danmu_intel.common import config_store

    before = config_store.version(conn)
    room = rooms.add_room(conn, platform="huya", room_id="660000", url="https://a", actor="admin")
    assert config_store.version(conn) == before + 1

    rooms.update_room(conn, room.id, url="https://b", actor="admin")
    assert config_store.version(conn) == before + 2

    rooms.delete_room(conn, room.id, actor="admin")
    assert config_store.version(conn) == before + 3
    latest = config_store.latest(conn)
    assert latest is not None and latest.keys == ("rooms",) and latest.updated_by == "admin"
