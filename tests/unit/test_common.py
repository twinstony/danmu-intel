"""共性基础测试：路径、事件契约、身份哈希、数据库、来源引用、比赛实体。"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import timezone, timedelta
from pathlib import Path

import pytest

from danmu_intel.common import identity, paths
from danmu_intel.common.db import DDL, connect, open_db, table_names
from danmu_intel.common.events import (
    JSONL_FIELDS,
    DanmuEvent,
    JsonlAppender,
    count_lines,
    decode_line,
    iter_events,
)
from danmu_intel.common.matches import MATCH_STATES, create_match, get_match
from danmu_intel.common.sources import (
    SourceRef,
    compute_digest,
    make_ref,
    merge_line_numbers,
    refs_for_lines,
    resolve,
    verify,
)

from conftest import BASE_TS, REL_PATH, make_event

CST = timezone(timedelta(hours=8))


def test_paths_default_and_env(monkeypatch, tmp_path):
    monkeypatch.delenv(paths.DATA_DIR_ENV, raising=False)
    assert paths.data_dir() == Path.home() / "danmu-intel-data"
    assert paths.db_path() == Path.home() / "danmu-intel-data" / "db.sqlite3"
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path / "x"))
    assert paths.data_dir() == tmp_path / "x"
    assert paths.salt_path() == tmp_path / "x" / "salt"


def test_raw_path_layout(data_root):
    path = paths.raw_path("huya", "660000", BASE_TS, tz=CST)
    assert path.parent.name == "2026-09-22"
    assert path.name == "660000-16.jsonl"
    assert path.parent.parent == paths.raw_dir("huya")


def test_raw_path_accepts_explicit_data_root(tmp_path):
    """显式数据目录（supervisor 给子进程补封文件时用）优先于环境变量。"""
    path = paths.raw_path("huya", "660000", BASE_TS, tz=CST, data_root=tmp_path / "elsewhere")
    assert path == tmp_path / "elsewhere" / "raw" / "huya" / "2026-09-22" / "660000-16.jsonl"
    assert paths.raw_dir("huya", data_root=tmp_path / "elsewhere") == tmp_path / "elsewhere" / "raw" / "huya"


def test_site_dir_and_report_page(site_root):
    assert paths.site_dir() == site_root
    assert paths.report_page_path(7, "full") == site_root / "matches" / "7" / "full.html"
    assert paths.report_page_path(7, "live_brief") == site_root / "matches" / "7" / "live_brief.html"
    assert paths.repo_root().name.startswith("danmu-intel")


def test_rel_to_data(data_root):
    target = data_root / "raw" / "huya" / "a.jsonl"
    target.parent.mkdir(parents=True)
    target.touch()
    assert paths.rel_to_data(target) == "raw/huya/a.jsonl"


def test_event_line_contract():
    event = make_event(BASE_TS, text="这波团开得太急了", user="3f9a")
    assert list(json.loads(event.to_line())) == list(JSONL_FIELDS)
    assert decode_line(event.to_line()) == event
    assert event.with_match(3).match_id == 3


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"ts": 1},
        {"ts": "1", "platform": "h", "room_id": "1", "match_id": None, "user_hash": "u", "text": "t", "extra": {}},
        {"ts": 1, "platform": "h", "room_id": "1", "match_id": "x", "user_hash": "u", "text": "t", "extra": {}},
        {"ts": 1, "platform": "h", "room_id": "1", "match_id": None, "user_hash": "u", "text": "t", "extra": []},
        {"ts": 1, "platform": 5, "room_id": "1", "match_id": None, "user_hash": "u", "text": "t", "extra": {}},
    ],
)
def test_event_from_json_rejects_bad_payload(payload):
    with pytest.raises(ValueError):
        DanmuEvent.from_json(payload)


def test_decode_line_rejects_bad_json():
    with pytest.raises(ValueError):
        decode_line("{not json")
    with pytest.raises(ValueError):
        decode_line("[1,2]")


def test_jsonl_appender_appends_only(data_root):
    path = data_root / "raw" / "huya" / "x.jsonl"
    with JsonlAppender(path) as appender:
        appender.append(make_event(BASE_TS))
        appender.append(make_event(BASE_TS + 1))
    with JsonlAppender(path) as appender:
        appender.append(make_event(BASE_TS + 2))
        appender.close()
        appender.close()  # 幂等
    assert count_lines(path) == 3
    assert [event.ts for _, event in iter_events(path)] == [BASE_TS, BASE_TS + 1, BASE_TS + 2]


def test_identity_hash_is_stable_and_salted(data_root):
    first = identity.user_hash("huya", "12345")
    assert first == identity.user_hash("huya", "12345")
    assert first != identity.user_hash("huya", "12346")
    assert first != identity.user_hash("soop", "12345")
    assert len(first) == identity.HASH_LENGTH
    assert "12345" not in first
    salt = paths.salt_path()
    assert salt.exists() and len(salt.read_bytes()) == identity.SALT_BYTES
    assert os.stat(salt).st_mode & 0o777 == 0o600


def test_identity_loads_existing_salt(tmp_path):
    target = tmp_path / "salt"
    target.write_bytes(b"a" * 32)
    assert identity.load_salt(target) == b"a" * 32
    assert identity.load_salt(target) == b"a" * 32  # 走缓存


def test_db_creates_all_tables(data_root):
    conn = open_db(paths.db_path())
    try:
        assert table_names(conn) == [
            "audit_log",
            "chain_cursors",
            "config",
            "danmu_segments",
            "gray_signals",
            "llm_calls",
            "matches",
            "member_credentials",
            "members",
            "metrics",
            "notifications",
            "order_payments",
            "orders",
            "quota_usage",
            "rate_limits",
            "releases",
            "reports",
            "room_sessions",
            "rooms",
            "slices",
            "stats_daily",
            "stats_events",
            "stats_salt",
        ]
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


def test_db_connect_is_idempotent(tmp_path):
    conn = connect(tmp_path / "nested" / "db.sqlite3")
    conn.executescript(DDL)
    conn.executescript(DDL)
    conn.close()


def test_matches_roundtrip(conn):
    match_id = create_match(
        conn,
        league="LPL",
        team_a="iG",
        team_b="LNG",
        state="live",
        stage="常规赛",
        scheduled_at=BASE_TS,
        official_result={"score": "1:0"},
    )
    match = get_match(conn, match_id)
    assert (match.league, match.team_a, match.team_b, match.state) == ("LPL", "iG", "LNG", "live")
    assert match.official_result == {"score": "1:0"}
    assert match.title == "iG vs LNG"
    assert MATCH_STATES == ("scheduled", "live", "between_games", "ended", "aborted")
    with pytest.raises(ValueError):
        create_match(conn, league="L", team_a="a", team_b="b", state="nope")
    with pytest.raises(LookupError):
        get_match(conn, 999)


def test_matches_without_official_result(conn):
    match_id = create_match(conn, league="LEC", team_a="G2", team_b="FNC")
    assert get_match(conn, match_id).official_result is None


def test_sources_digest_and_verify(data_root):
    path = data_root / REL_PATH
    path.parent.mkdir(parents=True)
    path.write_text("".join(f'{{"i":{i}}}\n' for i in range(5)), encoding="utf-8")

    ref = make_ref(REL_PATH, 2, 3, data_root=data_root)
    assert ref.line_start == 2 and ref.line_end == 3
    assert verify(ref, data_root=data_root)
    assert resolve(ref, data_root=data_root) == path

    # 篡改证据 → 校验失败
    path.write_text("".join(f'{{"i":{i}00}}\n' for i in range(5)), encoding="utf-8")
    assert not verify(ref, data_root=data_root)
    assert not verify(SourceRef(REL_PATH, 1, 99, "x"), data_root=data_root)
    assert not verify(SourceRef("missing.jsonl", 1, 1, "x"), data_root=data_root)


def test_sources_rejects_bad_range(data_root):
    path = data_root / "a.jsonl"
    path.write_text("x\n")
    with pytest.raises(ValueError):
        compute_digest(path, 0, 1)
    with pytest.raises(ValueError):
        compute_digest(path, 2, 1)
    with pytest.raises(ValueError):
        compute_digest(path, 1, 5)


def test_sources_merge_and_refs(data_root):
    path = data_root / REL_PATH
    path.parent.mkdir(parents=True)
    path.write_text("a\nb\nc\nd\n")
    assert merge_line_numbers([3, 1, 2, 2, 7]) == [(1, 3), (7, 7)]
    assert merge_line_numbers([]) == []
    refs = refs_for_lines(REL_PATH, [1, 2], data_root=data_root)
    assert len(refs) == 1 and refs[0].line_end == 2
    assert SourceRef.from_dict(refs[0].as_dict()) == refs[0]


def test_sqlite_row_factory_is_row(data_root):
    conn = open_db(paths.db_path())
    try:
        assert isinstance(conn.execute("SELECT 1 AS x").fetchone(), sqlite3.Row)
    finally:
        conn.close()
