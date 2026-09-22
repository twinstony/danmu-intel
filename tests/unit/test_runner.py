"""采集会话单测（不连网络：用假适配器/假 transport 回放）。"""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager

import pytest

from danmu_intel.collect.adapter import Probe, RoomKey
from danmu_intel.collect.huya import HuyaAdapter
from danmu_intel.collect.runner import (
    match_segment_paths,
    read_raw_events,
    run_session,
    seal_segment,
    upsert_room,
)
from danmu_intel.common import paths
from danmu_intel.common.events import decode_line, iter_events
from danmu_intel.common.db import open_db

from conftest import BASE_TS, load_huya_fixture

ROOM = RoomKey("huya", "660000", "https://www.huya.com/660000")
HOUR = 3_600_000


class FakeAdapter:
    platform = "huya"

    def __init__(self, events, *, probe_error=None, stream_error=None):
        self.events = events
        self.probe_error = probe_error
        self.stream_error = stream_error

    def parse_room(self, url):
        return ROOM

    async def probe(self, room):
        if self.probe_error:
            raise self.probe_error
        return Probe(is_live=True, streamer="样例主播", title="标题", game="英雄联盟")

    async def stream(self, room):
        for event in self.events:
            yield event
        if self.stream_error:
            raise self.stream_error
        await asyncio.sleep(3600)  # 真实链路不会自己结束：靠 deadline 收工


class EmptyAdapter(FakeAdapter):
    async def stream(self, room):
        await asyncio.sleep(3600)
        yield  # pragma: no cover


"""连接用上下文管理器打开/关闭，避免测试自身泄漏连接。"""


@contextmanager
def session_conn():
    connection = open_db(paths.db_path())
    try:
        yield connection
    finally:
        connection.close()


def make_events(count: int, start_ms: int):
    from conftest import make_event

    return [make_event(start_ms + index * 1_000, text=f"弹幕 {index}") for index in range(count)]


def test_run_session_writes_contract_jsonl_and_indexes(data_root):
    events = make_events(5, BASE_TS)
    with session_conn() as session_db:
        result = asyncio.run(
            run_session(ROOM, adapter=FakeAdapter(events), seconds=30, match_id=7, conn=session_db)
        )
    assert result.msg_count == 5
    assert len(result.segments) == 1
    segment = result.segments[0]
    assert segment.msg_count == 5 and segment.sha256
    assert segment.rel_path.startswith("raw/huya/")

    path = data_root / segment.rel_path
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(events)
    first = json.loads(lines[0])
    assert list(first) == ["ts", "platform", "room_id", "match_id", "user_hash", "text", "extra"]
    assert first["match_id"] == 7
    assert first["room_id"] == "660000"

    conn = open_db(paths.db_path())
    try:
        room = conn.execute("SELECT * FROM rooms").fetchone()
        assert (room["platform"], room["room_id"], room["discovered_by"]) == ("huya", "660000", "manual")
        assert room["streamer"] == "样例主播" and room["is_live"] == 1
        session = conn.execute("SELECT * FROM room_sessions").fetchone()
        assert session["state"] == "exited" and session["match_id"] == 7
        assert session["ended_at"] is not None and session["last_msg_at"] == events[-1].ts
        row = conn.execute("SELECT * FROM danmu_segments").fetchone()
        assert row["msg_count"] == 5
        assert row["first_ts"] == events[0].ts and row["last_ts"] == events[-1].ts
        assert match_segment_paths(conn, 7) == [segment.rel_path]
        assert match_segment_paths(conn, 8) == []
    finally:
        conn.close()

    loaded = read_raw_events([segment.rel_path], data_root=data_root)
    assert [line_no for _, line_no, _ in loaded] == [1, 2, 3, 4, 5]
    assert [event.ts for _, _, event in loaded] == [event.ts for event in events]


def test_run_session_rolls_over_hourly_files(data_root):
    events = make_events(4, BASE_TS) + make_events(4, BASE_TS + HOUR)
    with session_conn() as session_db:
        result = asyncio.run(run_session(ROOM, adapter=FakeAdapter(events), seconds=30, conn=session_db))
    assert result.msg_count == 8
    assert len(result.segments) == 2
    assert {segment.rel_path.split("/")[-1] for segment in result.segments} == {"660000-16.jsonl", "660000-17.jsonl"}


def test_run_session_tolerates_probe_failure(data_root):
    events = make_events(2, BASE_TS)
    with session_conn() as session_db:
        result = asyncio.run(
            run_session(
                ROOM,
                adapter=FakeAdapter(events, probe_error=RuntimeError("页面变了")),
                seconds=10,
                conn=session_db,
            )
        )
    assert result.probe is None
    assert result.msg_count == 2
    conn = open_db(paths.db_path())
    try:
        assert conn.execute("SELECT is_live FROM rooms").fetchone()["is_live"] == 0
    finally:
        conn.close()


def test_run_session_marks_stalled_and_seals(data_root):
    events = make_events(3, BASE_TS)
    adapter = FakeAdapter(events, stream_error=RuntimeError("断流"))
    with session_conn() as session_db, pytest.raises(RuntimeError):
        asyncio.run(run_session(ROOM, adapter=adapter, seconds=30, conn=session_db))
    conn = open_db(paths.db_path())
    try:
        assert conn.execute("SELECT state FROM room_sessions").fetchone()["state"] == "stalled"
        row = conn.execute("SELECT * FROM danmu_segments").fetchone()
        assert row["msg_count"] == 3, "异常退出也必须封存已落盘的证据"
    finally:
        conn.close()


def test_run_session_stops_by_deadline_without_events(data_root):
    with session_conn() as session_db:
        result = asyncio.run(run_session(ROOM, adapter=EmptyAdapter([]), seconds=0.1, conn=session_db))
    assert result.msg_count == 0
    assert result.segments == []


def test_run_session_reuses_room_row(data_root):
    conn = open_db(paths.db_path())
    try:
        adapter = FakeAdapter(make_events(1, BASE_TS))
        asyncio.run(run_session(ROOM, adapter=adapter, seconds=5, conn=conn))
        asyncio.run(run_session(ROOM, adapter=adapter, seconds=5, conn=conn))
        assert conn.execute("SELECT COUNT(*) AS n FROM rooms").fetchone()["n"] == 1
        assert conn.execute("SELECT COUNT(*) AS n FROM room_sessions").fetchone()["n"] == 2
    finally:
        conn.close()


def test_upsert_room_is_idempotent(data_root):
    conn = open_db(paths.db_path())
    try:
        first = upsert_room(conn, ROOM, Probe(True, "A", "t", "g"))
        second = upsert_room(conn, ROOM, Probe(False, None, None, None))
        assert first == second
        row = conn.execute("SELECT * FROM rooms").fetchone()
        assert row["streamer"] == "A", "探测失败时不得覆盖已有的主播名"
        assert row["is_live"] == 0
    finally:
        conn.close()


def test_seal_segment_upserts_by_rel_path(data_root):
    events = make_events(3, BASE_TS)
    event = events[0]
    path = paths.raw_path(event.platform, event.room_id, event.ts)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(item.to_line() + "\n" for item in events), encoding="utf-8")

    conn = open_db(paths.db_path())
    try:
        first = seal_segment(conn, 1, path, data_root=data_root)
        second = seal_segment(conn, 2, path, data_root=data_root)
        assert first.rel_path == second.rel_path
        assert conn.execute("SELECT COUNT(*) AS n FROM danmu_segments").fetchone()["n"] == 1
        assert conn.execute("SELECT room_session_id FROM danmu_segments").fetchone()["room_session_id"] == 2
    finally:
        conn.close()


def test_run_session_with_huya_replay_adapter(data_root, monkeypatch):
    """真实适配器 + 录制帧：验证「适配器 → 落盘」这一段也是通的（仍不连网络）。"""
    from tests.contract.test_adapter_contract import ReplayTransport

    frames = [bytes.fromhex(r["frame_hex"]) for r in load_huya_fixture() if r["kind"] == "danmaku"]
    adapter = HuyaAdapter(transport=ReplayTransport([frames]))
    monkeypatch.setattr("danmu_intel.collect.adapter.RECONNECT_BACKOFF_S", (0.0,))

    async def fake_page(room_id):
        return '"lProfileRoom":660000,"lYyid":1,"lChannelId":2,"lSubChannelId":2,"eLiveStatus":2,"sNick":"样例主播"'

    monkeypatch.setattr("danmu_intel.collect.huya.fetch_page", fake_page)
    with session_conn() as session_db:
        result = asyncio.run(run_session(ROOM, adapter=adapter, seconds=2, conn=session_db))
    assert result.msg_count == len(frames)
    path = data_root / result.segments[0].rel_path
    assert sum(1 for _ in iter_events(path)) == len(frames)
    assert all(decode_line(line).user_hash for line in path.read_text(encoding="utf-8").splitlines())
