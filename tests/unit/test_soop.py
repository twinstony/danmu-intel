"""SOOP 适配器单测：主播 ID 解析、房间信息解析、聊天包解码（用录制并脱敏的 fixture）。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from danmu_intel.collect import soop
from danmu_intel.collect.adapter import Probe, RoomKey
from danmu_intel.collect.soop import (
    CHAT_MIN_FIELDS,
    HEAD_PREFIX,
    HEADER_LEN,
    SEP,
    SVC_CHATMESG,
    SVC_JOINCH,
    SVC_KEEPALIVE,
    SVC_LOGIN,
    LiveTransport,
    RoomInfo,
    SoopAdapter,
    build_chat_packet,
    build_log_string,
    chat_url,
    decode_message,
    fetch_room_info,
    iter_recorded_packets,
    join_body,
    keepalive_body,
    login_body,
    make_packet,
    packet_header,
    parse_bj_id,
    parse_room_info,
)

from conftest import SOOP_FRAMES, load_fixture

FAKE_WS_MSG_TYPE = type(
    "FakeWSMsgType", (), {"BINARY": "binary", "CLOSE": "close", "CLOSED": "closed", "ERROR": "error"}
)

LIVE_PAYLOAD = {
    "CHANNEL": {
        "BJID": "seokwngud",
        "BNO": "297306971",
        "CHATNO": "2679",
        "CHIP": "222.233.54.81",
        "CHPT": "9000",
        "BSTATUS": "BROADING",
        "BJNICK": " 样例主播 ",
        "TITLE": "标题",
        "CATEGORY_TAGS": ["리그 오브 레전드"],
    }
}


@pytest.mark.parametrize(
    ("url", "bj_id"),
    [
        ("https://play.sooplive.com/seokwngud/297306971", "seokwngud"),
        ("https://play.sooplive.com/seokwngud", "seokwngud"),
        ("https://www.sooplive.co.kr/afchall/", "afchall"),
        ("https://play.sooplive.com/seokwngud/297306971?x=1#y", "seokwngud"),
        ("afchall", "afchall"),
    ],
)
def test_parse_bj_id(url, bj_id):
    assert parse_bj_id(url) == bj_id


def test_parse_room_uses_stable_channel_identity():
    """房间标识取主播频道（`broad_no` 每场直播都变，不能当房间主键）。"""
    assert SoopAdapter().parse_room("https://play.sooplive.com/seokwngud/297306971") == RoomKey(
        platform="soop", room_id="seokwngud", url="https://play.sooplive.com/seokwngud"
    )


def test_parse_bj_id_rejects_garbage():
    for bad in ("", "   ", "https://play.sooplive.com/", "https://play.sooplive.com/a b", "/"):
        with pytest.raises(ValueError):
            parse_bj_id(bad)


def test_packet_layout_is_50_byte_header_plus_body():
    packet = make_packet(SVC_LOGIN, login_body())
    assert packet.startswith(HEAD_PREFIX + b"0001")
    assert packet[6:12] == str(len(login_body())).zfill(6).encode()  # 长度字段（6 位 ASCII）
    assert packet[12:14] == b"00"
    assert packet_header(packet) == (SVC_LOGIN, 0)
    assert len(packet) == HEADER_LEN + len(login_body())


def test_packet_header_rejects_short_packet():
    for short in (b"", b"\x1d", b"\x1d" * (HEADER_LEN - 1)):
        with pytest.raises(ValueError):
            packet_header(short)


def test_login_packet_is_guest_anonymous():
    body = login_body()
    assert body.startswith(SEP) and body.endswith(SEP)
    assert body[1:-1].split(SEP) == [b"", b"", b"16"]


def test_join_body_carries_channel_and_log():
    body = join_body(2679, log=build_log_string())
    fields = body[1:-1].split(SEP)
    assert fields[0] == b"2679"
    assert fields[4].decode().startswith("log")
    assert keepalive_body() == SEP


def test_build_and_decode_roundtrip():
    packet = build_chat_packet("123", "이건 뭐지 这波团开得太急了")
    decoded = decode_message(packet)
    assert len(decoded) == 1
    assert (decoded[0].uid, decoded[0].text) == ("123", "이건 뭐지 这波团开得太急了")


def test_decode_message_rejects_non_chat_services():
    for service in (SVC_KEEPALIVE, SVC_LOGIN, SVC_JOINCH, 127):
        assert decode_message(make_packet(service, SEP + b"x" + SEP)) == []


def test_decode_message_rejects_too_few_fields():
    body = SEP + SEP.join(b"x" for _ in range(CHAT_MIN_FIELDS - 1)) + SEP
    assert decode_message(make_packet(SVC_CHATMESG, body)) == []


def test_decode_message_rejects_body_without_separators():
    assert decode_message(make_packet(SVC_CHATMESG, b"no separators")) == []


@pytest.mark.parametrize(
    "broken",
    [b"", b"\x00", b"\x1d", b"\x1d\t", b"\xff" * 8, HEAD_PREFIX + b"0005" + b"\xff" * 60, b"\x1d\t0005" + b"a" * 60],
)
def test_decode_message_never_crashes_on_garbage(broken):
    assert decode_message(broken) == []


def test_decode_message_tolerates_invalid_utf8():
    """韩文包偶有非 UTF-8 字节：按 latin-1 兜底解码，不整包丢弃。"""
    body = SEP + b"\xff\xfe" + SEP + b"1" + SEP + b"\x0c" + b"\x0c" + b"\x0c" + SEP
    decoded = decode_message(make_packet(SVC_CHATMESG, body))
    assert len(decoded) == 1 and decoded[0].uid == "1"


def test_decode_message_skips_blank_text():
    assert decode_message(build_chat_packet("1", "   ")) == []
    assert decode_message(build_chat_packet("1", "\r\n")) == []


def test_parse_room_info_reads_live_metadata():
    info = parse_room_info(LIVE_PAYLOAD)
    assert info == RoomInfo(
        bj_id="seokwngud",
        broad_no="297306971",
        chat_no=2679,
        chip="222.233.54.81",
        chpt=9000,
        is_live=True,
        streamer="样例主播",
        title="标题",
        game="리그 오브 레전드",
    )
    assert chat_url(info) == "wss://chat-DEE93651.sooplive.com:9001/Websocket/seokwngud"


def test_parse_room_info_reads_offline_room():
    info = parse_room_info({"CHANNEL": {"RESULT": 0, "GDPR": False}})
    assert info.is_live is False
    assert info.streamer is None and info.title is None and info.game is None
    assert info.chat_no == 0
    with pytest.raises(ValueError, match="CHAT|聊天服务器"):
        chat_url(info)


def test_parse_room_info_reports_missing_channel():
    with pytest.raises(ValueError, match="CHANNEL"):
        parse_room_info({"RESULT": 1})


def test_chat_url_rejects_bad_ip():
    info = RoomInfo("bj", "1", 1, "1.2.3", 9000, True, None, None, None)
    with pytest.raises(ValueError, match="聊天服务器地址非法"):
        chat_url(info)


def test_recorded_fixture_is_sanitized_and_replayable():
    records = load_fixture("soop")
    kinds = {record["kind"] for record in records}
    assert kinds == {"danmaku", "other", "garbage"}
    danmaku = [record for record in records if record["kind"] == "danmaku"]
    assert len(danmaku) >= 20  # 真实录制的弹幕帧足够多

    for record in records:
        packet = bytes.fromhex(record["frame_hex"])
        decoded = decode_message(packet)
        if record["kind"] == "danmaku":
            assert decoded == [type(decoded[0])(uid=record["uid"], text=record["text"])]
        else:
            assert decoded == []


def test_fixture_is_sanitized():
    """脱敏纪律：只保留包结构，真实 uid / 昵称 / 用户原话都不进仓库。"""
    danmaku = [record for record in load_fixture("soop") if record["kind"] == "danmaku"]
    assert all(int(record["uid"]) >= 1_000_001 for record in danmaku)
    assert all(record["text"].startswith("样例弹幕") for record in danmaku)
    # 昵称字段只出现样例值（真实昵称不进仓库）
    for record in danmaku:
        fields = soop._split_fields(bytes.fromhex(record["frame_hex"])[HEADER_LEN:])
        assert fields[5].startswith("样例用户")


def test_sanitized_fixture_is_committed_for_soop():
    assert SOOP_FRAMES.exists()
    assert {record["kind"] for record in load_fixture("soop")} == {"danmaku", "other", "garbage"}


def test_iter_recorded_packets(tmp_path):
    path = tmp_path / "frames.jsonl"
    path.write_text("00ff\n\n0a0b\n", encoding="utf-8")
    assert list(iter_recorded_packets(path)) == [bytes([0x00, 0xFF]), bytes([0x0A, 0x0B])]


def test_fetch_room_info_posts_current_broadcast(monkeypatch):
    captured = {}

    class FakeResponse:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def raise_for_status(self):
            captured["raised"] = True

        async def json(self, content_type=None):
            return LIVE_PAYLOAD

    class FakeSession:
        def __init__(self, **kwargs):
            captured["headers"] = kwargs.get("headers")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def post(self, url, data):
            captured["url"] = url
            captured["data"] = data
            return FakeResponse()

    monkeypatch.setattr("danmu_intel.collect.soop.aiohttp.ClientSession", FakeSession)
    info = asyncio.run(fetch_room_info("seokwngud"))
    assert info.chat_no == 2679
    assert captured["url"].endswith("player_live_api.php?bjid=seokwngud")
    assert captured["data"]["bno"] == "0" and captured["data"]["type"] == "LIVE"
    assert "play.sooplive.com/seokwngud" in captured["headers"]["Referer"]


def test_adapter_probe_uses_fetch_room_info(monkeypatch):
    async def fake_room_info(bj_id):
        assert bj_id == "seokwngud"
        return parse_room_info(LIVE_PAYLOAD)

    monkeypatch.setattr("danmu_intel.collect.soop.fetch_room_info", fake_room_info)
    probe = asyncio.run(SoopAdapter().probe(RoomKey("soop", "seokwngud", "u")))
    assert probe == Probe(is_live=True, streamer="样例主播", title="标题", game="리그 오브 레전드")


class FakeTransport:
    """把录制包当作「一次连接的产出」，可在中途抛错以验证重连。"""

    def __init__(self, batches: list[list[bytes]]) -> None:
        self.batches = list(batches)
        self.calls = 0

    async def frames(self, room_id: str):
        self.calls += 1
        if not self.batches:
            raise ConnectionError("录制包已用尽（模拟断流）")
        batch = self.batches.pop(0)
        for frame in batch:
            yield frame


def _danmaku_packets(limit: int = 4) -> list[bytes]:
    records = [r for r in load_fixture("soop") if r["kind"] == "danmaku"][:limit]
    return [bytes.fromhex(r["frame_hex"]) for r in records]


def test_adapter_stream_maps_packets_to_events(data_root):
    packets = _danmaku_packets()
    adapter = SoopAdapter(transport=FakeTransport([packets]))
    room = RoomKey("soop", "seokwngud", "https://play.sooplive.com/seokwngud")

    async def collect():
        events = []
        async for event in adapter.stream(room):
            events.append(event)
            if len(events) == len(packets):
                break
        return events

    events = asyncio.run(collect())
    assert len(events) == len(packets)
    assert {event.platform for event in events} == {"soop"}
    assert {event.room_id for event in events} == {"seokwngud"}
    assert all(event.text for event in events)
    assert all(event.extra == {} for event in events)
    assert [event.ts for event in events] == sorted(event.ts for event in events)


class FakeWS:
    """假聊天连接：按脚本吐包，记录发出去的包。"""

    def __init__(self, incoming: list[bytes]) -> None:
        self.sent: list[bytes] = []
        self._queue = list(incoming)

    async def send_bytes(self, payload: bytes) -> None:
        self.sent.append(payload)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._queue:
            raise StopAsyncIteration
        return type("Msg", (), {"type": FAKE_WS_MSG_TYPE.BINARY, "data": self._queue.pop(0)})()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _fake_sessions(monkeypatch, ws_factory):
    sessions = []

    class FakeSession:
        def __init__(self, **kwargs):
            self.ws = ws_factory()
            sessions.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def ws_connect(self, url, **kwargs):
            self.url = url
            self.kwargs = kwargs
            return self.ws

    monkeypatch.setattr("danmu_intel.collect.soop.aiohttp.ClientSession", FakeSession)
    monkeypatch.setattr("danmu_intel.collect.soop.aiohttp.WSMsgType", FAKE_WS_MSG_TYPE)
    return sessions


def test_live_transport_logs_in_joins_and_dumps(monkeypatch, tmp_path):
    """真实链路用假 aiohttp 会话跑通：登录 → 进频道 → 收包 → 落 dump。"""
    dump = tmp_path / "dump.jsonl"
    login_ack = make_packet(SVC_LOGIN, SEP + SEP)
    join_ack = make_packet(SVC_JOINCH, SEP + b"2679" + SEP)
    chat = build_chat_packet("1", "样例弹幕：这局稳了")
    # 登录应答前先来一个截断包：不得被误判成登录成功（不崩、不乱进频道）
    sessions = _fake_sessions(monkeypatch, lambda: FakeWS([b"\x1d\t", login_ack, join_ack, chat]))

    async def fake_room_info(bj_id):
        return parse_room_info(LIVE_PAYLOAD)

    monkeypatch.setattr("danmu_intel.collect.soop.fetch_room_info", fake_room_info)

    async def run():
        collected = []
        async for packet in LiveTransport(dump_packets=dump).frames("seokwngud"):
            collected.append(packet)
        return collected

    collected = asyncio.run(run())
    assert collected == [b"\x1d\t", login_ack, join_ack, chat]
    sent = sessions[0].ws.sent
    assert len(sent) == 2, "登录包 + 进频道包"
    assert packet_header(sent[0])[0] == SVC_LOGIN
    assert packet_header(sent[1])[0] == SVC_JOINCH
    assert b"2679" in sent[1]
    assert sessions[0].url == "wss://chat-DEE93651.sooplive.com:9001/Websocket/seokwngud"
    assert sessions[0].kwargs["protocols"] == ("chat",)
    assert dump.read_text(encoding="utf-8").count("\n") == 4


def test_live_transport_returns_on_close_message(monkeypatch):
    class ClosingWS(FakeWS):
        def __init__(self):
            super().__init__([])

        async def __anext__(self):
            return type("Msg", (), {"type": FAKE_WS_MSG_TYPE.CLOSE, "data": b""})()

    _fake_sessions(monkeypatch, ClosingWS)

    async def fake_room_info(bj_id):
        return parse_room_info(LIVE_PAYLOAD)

    monkeypatch.setattr("danmu_intel.collect.soop.fetch_room_info", fake_room_info)

    async def run():
        return [packet async for packet in LiveTransport().frames("seokwngud")]

    assert asyncio.run(run()) == []


def test_live_transport_skips_unknown_message_types(monkeypatch):
    class PingingWS(FakeWS):
        """先来一条非二进制消息（ping），再来一条弹幕包。"""

        def __init__(self):
            super().__init__([build_chat_packet("1", "x")])
            self._pinged = False

        async def __anext__(self):
            if not self._pinged:
                self._pinged = True
                return type("Msg", (), {"type": "ping", "data": b""})()
            return await super().__anext__()

    _fake_sessions(monkeypatch, PingingWS)

    async def fake_room_info(bj_id):
        return parse_room_info(LIVE_PAYLOAD)

    monkeypatch.setattr("danmu_intel.collect.soop.fetch_room_info", fake_room_info)

    async def run():
        return [packet async for packet in LiveTransport().frames("seokwngud")]

    assert len(asyncio.run(run())) == 1


def test_live_transport_sends_keepalive(monkeypatch):
    monkeypatch.setattr("danmu_intel.collect.soop.KEEPALIVE_INTERVAL_S", 0.0)
    ws = FakeWS([])

    async def run() -> list[bytes]:
        task = asyncio.create_task(LiveTransport()._keepalive(ws))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return ws.sent

    sent = asyncio.run(run())
    assert sent and all(packet_header(packet)[0] == SVC_KEEPALIVE for packet in sent)


def test_keepalive_survives_send_failure(monkeypatch):
    """保活发送失败不致命（连接已断时靠重连路径接手），不抛到流层。"""
    monkeypatch.setattr("danmu_intel.collect.soop.KEEPALIVE_INTERVAL_S", 0.0)

    class BrokenWS:
        async def send_bytes(self, payload: bytes) -> None:
            raise ConnectionResetError("连接已断")

    async def run() -> None:
        task = asyncio.create_task(LiveTransport()._keepalive(BrokenWS()))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())


def test_run_session_records_soop_room_metadata(data_root, conn, monkeypatch):
    """采集会话把 SOOP 房间元数据（房间标识/主播名/开播状态）写进 `rooms`，
    原始记录落在 `raw/soop/...`（AC：SOOP 房间元数据入库 + 真实落盘路径同构）。"""
    from danmu_intel.collect.runner import run_session

    async def fake_room_info(bj_id):
        return parse_room_info(LIVE_PAYLOAD)

    monkeypatch.setattr("danmu_intel.collect.soop.fetch_room_info", fake_room_info)
    adapter = SoopAdapter(transport=FakeTransport([_danmaku_packets(6)]))
    room = adapter.parse_room("https://play.sooplive.com/seokwngud")

    result = asyncio.run(
        run_session(room, adapter=adapter, match_id=None, seconds=5, conn=conn, data_root=data_root)
    )

    assert result.msg_count == 6
    row = conn.execute("SELECT * FROM rooms WHERE platform='soop'").fetchone()
    assert (row["room_id"], row["streamer"], row["is_live"]) == ("seokwngud", "样例主播", 1)
    assert row["url"] == "https://play.sooplive.com/seokwngud"
    segment = Path(data_root) / result.segments[0].rel_path
    assert result.segments[0].rel_path.startswith("raw/soop/")
    assert len([line for line in segment.read_text(encoding="utf-8").splitlines() if line]) == 6
    assert json.loads(segment.read_text(encoding="utf-8").splitlines()[0])["platform"] == "soop"
