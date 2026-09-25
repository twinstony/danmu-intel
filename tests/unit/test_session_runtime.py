"""子进程侧会话运行时：心跳、状态机、异常事件（不连网络）。"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager

import pytest

from danmu_intel.collect import adapter as adapter_module
from danmu_intel.collect import heartbeat as heartbeat_module
from danmu_intel.collect import runner
from danmu_intel.collect.adapter import Probe, RoomKey
from danmu_intel.collect.heartbeat import Supervision, read_heartbeat, supervision_env
from danmu_intel.collect.incidents import DISK_LOW, DROP_RATE_HIGH, NO_STREAM, STALLED, recent
from danmu_intel.collect.runner import run_session
from danmu_intel.common import paths
from danmu_intel.common.db import open_db

from conftest import BASE_TS, make_event

ROOM = RoomKey("huya", "660000", "https://www.huya.com/660000")


class ScriptedAdapter:
    """按脚本产出事件；`None` 表示静默挂住（不发消息也不报错）。"""

    platform = "huya"

    def __init__(self, script: list[object]) -> None:
        self._script = script
        self.on_reconnect = None

    def parse_room(self, url: str) -> RoomKey:
        return ROOM

    async def probe(self, room: RoomKey) -> Probe:
        return Probe(is_live=True, streamer="样例主播", title="标题", game="英雄联盟")

    async def stream(self, room: RoomKey, *, on_reconnect=None):
        self.on_reconnect = on_reconnect
        from danmu_intel.collect.adapter import SILENCE_TIMEOUT_S, reconnecting

        async def once(room_key: RoomKey):
            for item in self._script:
                if item is None:
                    await asyncio.sleep(3600)
                else:
                    yield item

        async for event in reconnecting(
            lambda room_key: once(room_key), room, silence_timeout=SILENCE_TIMEOUT_S, on_reconnect=on_reconnect
        ):
            yield event


@contextmanager
def session_db():
    connection = open_db(paths.db_path())
    try:
        yield connection
    finally:
        connection.close()


def events(count: int, *, start_ms: int = BASE_TS):
    return [make_event(start_ms + index * 1_000, text=f"弹幕 {index}") for index in range(count)]


def test_session_publishes_heartbeat_and_final_state(data_root):
    adapter = ScriptedAdapter(events(3))
    with session_db() as conn:
        result = asyncio.run(
            run_session(ROOM, adapter=adapter, match_id=5, seconds=0.3, conn=conn, heartbeat_interval=0.02)
        )
    # 脚本发完即流结束 → 流层按「结束」重连一次（真实链路里对端关连接也是这条路）
    assert (result.msg_count, result.state, result.reconnects) == (3, "exited", 1)

    beat = read_heartbeat("huya", "660000", data_root=data_root)
    assert beat is not None
    assert beat.pid and beat.session_id == result.session_id
    assert beat.state == "exited" and beat.msg_count == 3
    assert beat.last_msg_at == BASE_TS + 2_000
    assert beat.restart_count == 0

    conn = open_db(paths.db_path())
    try:
        row = conn.execute("SELECT * FROM room_sessions").fetchone()
        assert row["state"] == "exited" and row["ended_at"] is not None
        assert row["severity"] == "info" and row["reconnects"] == 1
        assert row["restart_count"] == 0 and row["last_msg_at"] == BASE_TS + 2_000
        assert recent(conn) == []
    finally:
        conn.close()


def test_supervision_counts_are_carried_into_the_session_row(data_root, monkeypatch):
    """supervisor 给的第几次重启 / 累计重连数写进本会话行（接力棒不丢）。"""
    monkeypatch.setenv(
        heartbeat_module.SUPERVISION_ENV,
        supervision_env(Supervision(restart_count=4, reconnects=7))[heartbeat_module.SUPERVISION_ENV],
    )
    with session_db() as conn:
        result = asyncio.run(
            run_session(ROOM, adapter=ScriptedAdapter(events(1)), seconds=0.2, conn=conn, heartbeat_interval=0.02)
        )
    conn = open_db(paths.db_path())
    try:
        row = conn.execute("SELECT * FROM room_sessions").fetchone()
        assert row["restart_count"] == 4, "第几次重启原样入行"
        assert row["reconnects"] == 8, "接力棒 7 次 + 本会话流结束重连 1 次"
    finally:
        conn.close()
    assert result.reconnects == 8


def test_silence_marks_stalled_and_counts_reconnect(data_root, monkeypatch):
    """60 秒（测试压到 0.05s）无消息 → 重连一次、状态 stalled、报事件。"""
    monkeypatch.setattr(adapter_module, "SILENCE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(adapter_module, "RECONNECT_BACKOFF_S", (0.05,))
    with session_db() as conn:
        result = asyncio.run(
            run_session(
                ROOM,
                adapter=ScriptedAdapter(events(2) + [None]),
                match_id=9,
                seconds=0.4,
                conn=conn,
                heartbeat_interval=0.02,
            )
        )
    assert result.reconnects >= 1
    assert STALLED in result.incidents

    conn = open_db(paths.db_path())
    try:
        row = conn.execute("SELECT * FROM room_sessions").fetchone()
        assert row["reconnects"] >= 1 and row["severity"] == "warning"
        assert row["state"] in {"exited", "stalled"}
        incidents = recent(conn, match_id=9)
        assert [item.kind for item in incidents] == [STALLED]
        assert incidents[0].payload["silence_s"] == 0.05
        assert incidents[0].severity == "warning"
    finally:
        conn.close()


def test_no_stream_after_120s_without_first_message(data_root, monkeypatch):
    monkeypatch.setattr(runner, "NO_FIRST_MSG_S", 0.05)
    with session_db() as conn:
        result = asyncio.run(
            run_session(
                ROOM,
                adapter=ScriptedAdapter([None]),
                match_id=11,
                seconds=0.3,
                conn=conn,
                heartbeat_interval=0.02,
            )
        )
    assert result.incidents == [NO_STREAM]
    conn = open_db(paths.db_path())
    try:
        row = conn.execute("SELECT * FROM room_sessions").fetchone()
        assert row["severity"] == "warning"
        incidents = recent(conn, match_id=11)
        assert [item.kind for item in incidents] == [NO_STREAM]
        assert incidents[0].payload["wait_s"] == 0.05
    finally:
        conn.close()


def test_no_stream_is_not_reported_when_messages_arrive(data_root, monkeypatch):
    monkeypatch.setattr(runner, "NO_FIRST_MSG_S", 0.0)
    with session_db() as conn:
        result = asyncio.run(
            run_session(
                ROOM, adapter=ScriptedAdapter(events(2)), seconds=0.2, conn=conn, heartbeat_interval=0.02
            )
        )
    assert result.incidents == []
    assert read_heartbeat("huya", "660000", data_root=data_root).msg_count == 2


def test_disk_low_reports_once_and_raises_severity(data_root, monkeypatch):
    monkeypatch.setattr(heartbeat_module, "free_bytes", lambda path: 1)
    with session_db() as conn:
        result = asyncio.run(
            run_session(
                ROOM,
                adapter=ScriptedAdapter(events(1) + [None]),
                match_id=13,
                seconds=0.25,
                conn=conn,
                heartbeat_interval=0.02,
            )
        )
    assert result.incidents == [DISK_LOW]
    conn = open_db(paths.db_path())
    try:
        row = conn.execute("SELECT * FROM room_sessions").fetchone()
        assert row["severity"] == "critical"
        assert [item.kind for item in recent(conn, match_id=13)] == [DISK_LOW], "同类异常只报一次"
        assert recent(conn)[0].payload["minimum_bytes"] > 0
    finally:
        conn.close()


def test_session_still_seals_evidence_when_stream_raises(data_root):
    class Exploding(ScriptedAdapter):
        async def stream(self, room: RoomKey, *, on_reconnect=None):
            for event in events(2):
                yield event
            raise RuntimeError("适配器炸了")

    with session_db() as conn, pytest.raises(RuntimeError):
        asyncio.run(run_session(ROOM, adapter=Exploding([]), seconds=1, conn=conn, heartbeat_interval=0.02))
    conn = open_db(paths.db_path())
    try:
        row = conn.execute("SELECT * FROM room_sessions").fetchone()
        assert row["state"] == "stalled" and row["ended_at"] is not None
        assert conn.execute("SELECT msg_count FROM danmu_segments").fetchone()["msg_count"] == 2
    finally:
        conn.close()


def test_seal_pending_files_uses_the_given_data_root(tmp_path, data_root):
    """子进程被 kill 时由主进程补封：文件路径按显式数据目录算，不读环境变量。"""
    from danmu_intel.collect.runner import seal_pending_files

    elsewhere = tmp_path / "elsewhere"
    moment = BASE_TS + 1_000
    path = paths.raw_path("huya", "660000", moment, data_root=elsewhere)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(make_event(moment).to_line() + "\n", encoding="utf-8")

    with session_db() as conn:
        assert seal_pending_files(
            conn, 12, ROOM, data_root=elsewhere, moments=[moment, moment + 3_600_000]
        )
        row = conn.execute("SELECT * FROM danmu_segments").fetchone()
    assert row["rel_path"].endswith("660000-16.jsonl")
    assert row["msg_count"] == 1 and row["room_session_id"] == 12


def test_session_writes_under_the_given_data_root_not_the_env(tmp_path, monkeypatch):
    """显式数据目录优先：supervisor 给子进程的是它自己的目录（父子必须写同一处）。"""
    elsewhere = tmp_path / "elsewhere"
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path / "环境变量指的别处"))
    with session_db() as conn:
        result = asyncio.run(
            run_session(
                ROOM,
                adapter=ScriptedAdapter(events(2)),
                seconds=0.2,
                conn=conn,
                data_root=elsewhere,
                heartbeat_interval=0.02,
            )
        )
    assert result.segments[0].rel_path.startswith("raw/huya/")
    assert (elsewhere / result.segments[0].rel_path).exists()


def _flaky_appender(drops: int):
    """真的写盘，但前 `drops` 次谎报"没写进去"（模拟短写 / 写失败）。"""
    original = runner.JsonlAppender.append
    remaining = {"drops": drops}

    def append(self, event):
        written = original(self, event)
        if remaining["drops"] > 0:
            remaining["drops"] -= 1
            return False
        return written

    return append


def test_drop_rate_over_threshold_reports_once(data_root, monkeypatch):
    """落盘丢包率 > 2% → `drop_rate_high`（设计 §15 #3），分母是收到的条数。"""
    monkeypatch.setattr(runner.JsonlAppender, "append", _flaky_appender(3))
    with session_db() as conn:
        result = asyncio.run(
            run_session(
                ROOM,
                adapter=ScriptedAdapter(events(100)),
                match_id=21,
                seconds=0.5,
                conn=conn,
                heartbeat_interval=0.02,
            )
        )
    assert result.msg_count == 100
    assert result.incidents == [DROP_RATE_HIGH]

    conn = open_db(paths.db_path())
    try:
        [item] = recent(conn, match_id=21)
        assert item.severity == "warning"
        assert item.payload["drop_count"] == 3 and item.payload["msg_count"] == 100
        assert item.payload["ratio"] == 0.03 and item.payload["threshold"] == 0.02
        assert item.payload["platform"] == "huya" and item.payload["room_id"] == "660000"
    finally:
        conn.close()


def test_drop_rate_at_or_below_threshold_stays_quiet(data_root, monkeypatch):
    monkeypatch.setattr(runner.JsonlAppender, "append", _flaky_appender(2))  # 2/100 = 2%
    with session_db() as conn:
        result = asyncio.run(
            run_session(
                ROOM,
                adapter=ScriptedAdapter(events(100)),
                match_id=22,
                seconds=0.5,
                conn=conn,
                heartbeat_interval=0.02,
            )
        )
    assert result.incidents == []
    conn = open_db(paths.db_path())
    try:
        assert recent(conn, match_id=22) == []
    finally:
        conn.close()
