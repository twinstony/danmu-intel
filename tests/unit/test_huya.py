"""虎牙适配器单测：房间号解析、页面解析、帧解码（用录制并脱敏的 fixture）。"""

from __future__ import annotations

import asyncio
import json

import pytest

from danmu_intel.collect.adapter import Probe, RoomKey
from danmu_intel.collect.huya import (
    HEARTBEAT_FRAME,
    HuyaAdapter,
    build_danmaku_frame,
    decode_frame,
    fetch_page,
    iter_recorded_frames,
    parse_page,
    parse_room_id,
    register_payload,
)

from conftest import HUYA_FRAMES, load_fixture

FAKE_WS_MSG_TYPE = type(
    "FakeWSMsgType", (), {"BINARY": "binary", "CLOSE": "close", "CLOSED": "closed", "ERROR": "error"}
)

PAGE = """
<html><script>
{"eLiveStatus":2,"sNick":"样例主播","sGameFullName":"英雄联盟",
 "sRoomName":"样例标题\\u0026测试","lProfileRoom":660000,"lYyid":1486578378,
 "lChannelId":1346609715,"lSubChannelId":1346609715}
{"lProfileRoom":0,"lYyid":0}
</script></html>
"""


@pytest.mark.parametrize(
    ("url", "room_id"),
    [
        ("https://www.huya.com/660000", "660000"),
        ("http://huya.com/123321/", "123321"),
        ("https://m.huya.com/323444?x=1", "323444"),
        ("890001", "890001"),
        ("https://www.huya.com/captainmo", "captainmo"),
    ],
)
def test_parse_room_id(url, room_id):
    assert parse_room_id(url) == room_id
    room = HuyaAdapter().parse_room(url)
    assert room == RoomKey(platform="huya", room_id=room_id, url=f"https://www.huya.com/{room_id}")


def test_parse_room_id_rejects_garbage():
    for bad in ("", "https://www.huya.com/", "https://www.huya.com/a b"):
        with pytest.raises(ValueError):
            parse_room_id(bad)


def test_parse_page_takes_first_occurrence():
    params, probe = parse_page(PAGE)
    assert params.room_id == "660000"
    assert params.uid == 1486578378
    assert params.channel_id == 1346609715
    assert params.sub_channel_id == 1346609715
    assert probe == Probe(is_live=True, streamer="样例主播", title="样例标题&测试", game="英雄联盟")


def test_parse_page_without_sub_channel_falls_back_to_channel():
    page = '"lProfileRoom":1,"lYyid":2,"lChannelId":3'
    params, probe = parse_page(page)
    assert params.sub_channel_id == 3
    assert probe.is_live is False
    assert probe.streamer is None and probe.title is None and probe.game is None


def test_parse_page_reports_missing_fields():
    with pytest.raises(ValueError, match="缺少字段"):
        parse_page('"sNick":"x"')


def test_parse_page_tolerates_unquoted_and_broken_strings():
    page = '"lProfileRoom":"323444","lYyid":"35184381630871","lChannelId":"1279512307519",' '"sNick":"a\\qb"'
    params, probe = parse_page(page)
    assert params.room_id == "323444"
    assert params.uid == 35184381630871
    assert probe.streamer == "a\\qb"


def test_register_payload_is_stable():
    from danmu_intel.collect.huya import ConnectParams

    payload = register_payload(ConnectParams("660000", 1, 2, 3))
    assert payload[:2] == bytes([0x00, 0x01])  # tag0 INT8 = 1（消息类型）
    assert payload[2] == 0x1D  # tag1 BYTES（注册体）
    assert registered_uid(payload) == 1
    assert register_payload(ConnectParams("660000", 1, 2, 3)) == payload


def registered_uid(payload: bytes) -> int:
    from danmu_intel.collect.tars import TarsReader

    body = TarsReader(payload).read_bytes(1)
    assert body is not None
    return int(TarsReader(body).read_int(0))


def test_heartbeat_frame_is_bytes():
    assert isinstance(HEARTBEAT_FRAME, bytes) and len(HEARTBEAT_FRAME) > 60


def test_build_and_decode_roundtrip():
    frame = build_danmaku_frame("123", "昵称", "内容 " + "长" * 300)
    assert decode_frame(frame) == [decode_frame(frame)[0]]  # 单条
    assert decode_frame(frame)[0].uid == "123"
    assert decode_frame(frame)[0].text == "内容 " + "长" * 300


def test_decode_frame_rejects_non_danmaku():
    # 其它消息类型（字段 0 != 7）
    from danmu_intel.collect.tars import TarsWriter

    other = TarsWriter()
    other.write_int(0, 2)
    assert decode_frame(other.getvalue()) == []

    # URI 不是 1400
    inner = TarsWriter()
    inner.write_int(0, 3)
    inner.write_int(1, 1999)
    frame = TarsWriter()
    frame.write_int(0, 7)
    frame.write_bytes(1, inner.getvalue())
    assert decode_frame(frame.getvalue()) == []


@pytest.mark.parametrize("broken", [b"", b"\x00", b"\x1d", b"\x1d\x00", b"\xff" * 8, b"\x00\x07\x1d"])
def test_decode_frame_never_crashes_on_garbage(broken):
    assert decode_frame(broken) == []


def test_decode_frame_skips_blank_text():
    assert decode_frame(build_danmaku_frame("1", "n", "   ")) == []


def test_recorded_fixture_is_sanitized_and_replayable():
    records = load_fixture("huya")
    kinds = {record["kind"] for record in records}
    assert kinds == {"danmaku", "other", "garbage"}
    danmaku = [record for record in records if record["kind"] == "danmaku"]
    assert len(danmaku) >= 20  # 真实录制的弹幕帧足够多

    for record in records:
        frame = bytes.fromhex(record["frame_hex"])
        decoded = decode_frame(frame)
        if record["kind"] == "danmaku":
            assert decoded == [type(decoded[0])(uid=record["uid"], text=record["text"])]
        else:
            assert decoded == []


def _nickname_of(frame: bytes) -> str:
    """从帧里取出用户昵称（只用于校验脱敏，生产代码不读取昵称）。"""
    from danmu_intel.collect.tars import TarsReader

    broadcast = TarsReader(frame).read_bytes(1)
    payload_bytes = TarsReader(broadcast).read_bytes(2)
    user = TarsReader(payload_bytes).read_struct(0)
    return str(user.read_string(2))


def test_fixture_is_sanitized():
    """脱敏纪律：只保留帧结构，真实 uid / 昵称 / 用户原话都不进仓库。"""
    records = load_fixture("huya")
    danmaku = [record for record in records if record["kind"] == "danmaku"]
    assert all(int(record["uid"]) >= 1_000_001 for record in danmaku)
    assert all(record["text"].startswith("样例弹幕") for record in danmaku)
    for record in danmaku:
        assert _nickname_of(bytes.fromhex(record["frame_hex"])).startswith("样例用户")


def test_iter_recorded_frames(tmp_path):
    path = tmp_path / "frames.jsonl"
    path.write_text("00ff\n\n0a0b\n", encoding="utf-8")
    assert list(iter_recorded_frames(path)) == [bytes([0x00, 0xFF]), bytes([0x0A, 0x0B])]


def test_fetch_page_uses_mobile_page(monkeypatch):
    captured = {}

    class FakeResponse:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def raise_for_status(self):
            return None

        async def text(self):
            return PAGE

    class FakeSession:
        def __init__(self, **kwargs):
            captured["headers"] = kwargs.get("headers")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def get(self, url):
            captured["url"] = url
            return FakeResponse()

    monkeypatch.setattr("danmu_intel.collect.huya.aiohttp.ClientSession", FakeSession)
    page = asyncio.run(fetch_page("660000"))
    assert page == PAGE
    assert captured["url"].endswith("m.huya.com/660000")
    assert "Mobile" in captured["headers"]["User-Agent"]


def test_adapter_probe_uses_fetch_page(monkeypatch):
    async def fake_page(room_id):
        assert room_id == "660000"
        return PAGE

    monkeypatch.setattr("danmu_intel.collect.huya.fetch_page", fake_page)
    probe = asyncio.run(HuyaAdapter().probe(RoomKey("huya", "660000", "u")))
    assert probe.is_live and probe.streamer == "样例主播"


class FakeTransport:
    """把录制帧当作「一次连接的产出」，可在中途抛错以验证重连。"""

    def __init__(self, batches: list[list[bytes]]) -> None:
        self.batches = list(batches)
        self.calls = 0

    async def frames(self, room_id: str):
        self.calls += 1
        if not self.batches:
            raise ConnectionError("录制帧已用尽（模拟断流）")
        batch = self.batches.pop(0)
        for frame in batch:
            yield frame


def _danmaku_frames(limit: int = 4) -> list[bytes]:
    records = [r for r in load_fixture("huya") if r["kind"] == "danmaku"][:limit]
    return [bytes.fromhex(r["frame_hex"]) for r in records]


def test_adapter_stream_maps_frames_to_events(data_root):
    frames = _danmaku_frames()
    adapter = HuyaAdapter(transport=FakeTransport([frames]))
    room = RoomKey("huya", "660000", "https://www.huya.com/660000")

    async def collect():
        events = []
        async for event in adapter.stream(room):
            events.append(event)
            if len(events) == len(frames):
                break
        return events

    events = asyncio.run(collect())
    assert len(events) == len(frames)
    assert {event.platform for event in events} == {"huya"}
    assert {event.room_id for event in events} == {"660000"}
    assert all(event.text for event in events)
    assert all(event.extra == {} for event in events)
    assert [event.ts for event in events] == sorted(event.ts for event in events)


def test_adapter_stream_reconnects_after_failure(data_root):
    frames = _danmaku_frames()
    transport = FakeTransport([frames[:2], frames[2:]])
    adapter = HuyaAdapter(transport=transport)
    room = RoomKey("huya", "660000", "https://www.huya.com/660000")

    async def collect():
        events = []
        async for event in adapter.stream(room):
            events.append(event)
            if len(events) == 4:
                break
        return events

    import danmu_intel.collect.adapter as adapter_module

    original = adapter_module.RECONNECT_BACKOFF_S
    adapter_module.RECONNECT_BACKOFF_S = (0.0,)
    try:
        events = asyncio.run(collect())
    finally:
        adapter_module.RECONNECT_BACKOFF_S = original
    assert len(events) == 4
    assert transport.calls == 2  # 断流后确实重连了一次


def test_live_transport_dump_and_frames(monkeypatch, tmp_path):
    """LiveTransport 用假 aiohttp 会话跑通：注册 → 收帧 → 落 dump。"""
    from danmu_intel.collect.huya import LiveTransport

    frames = _danmaku_frames(2)
    dump = tmp_path / "dump.jsonl"

    class FakeWS:
        def __init__(self):
            self.sent: list[bytes] = []
            self._queue = list(frames)

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

    class FakeSession:
        def __init__(self, **kwargs):
            self.ws = FakeWS()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def ws_connect(self, url, **kwargs):
            self.url = url
            return self.ws

    sessions: list[FakeSession] = []

    def session_factory(**kwargs):
        session = FakeSession(**kwargs)
        sessions.append(session)
        return session

    async def fake_page(room_id):
        return PAGE

    monkeypatch.setattr("danmu_intel.collect.huya.fetch_page", fake_page)
    monkeypatch.setattr("danmu_intel.collect.huya.aiohttp.ClientSession", session_factory)
    monkeypatch.setattr("danmu_intel.collect.huya.aiohttp.WSMsgType", FAKE_WS_MSG_TYPE)

    async def run():
        collected = []
        async for frame in LiveTransport(dump_frames=dump).frames("660000"):
            collected.append(frame)
        return collected

    collected = asyncio.run(run())
    assert len(collected) == 2
    assert len(sessions[0].ws.sent) == 1  # 注册帧
    assert dump.read_text(encoding="utf-8").count("\n") == 2
    assert json.loads(json.dumps({"frames": len(collected)}))


def test_live_transport_returns_on_close_message(monkeypatch):
    from danmu_intel.collect.huya import LiveTransport

    class Msg:
        type = FAKE_WS_MSG_TYPE.CLOSE
        data = b""

    class FakeWS:
        async def send_bytes(self, payload):
            return None

        def __aiter__(self):
            return self

        async def __anext__(self):
            return Msg()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class FakeSession:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def ws_connect(self, url, **kwargs):
            return FakeWS()

    async def fake_page(room_id):
        return PAGE

    monkeypatch.setattr("danmu_intel.collect.huya.fetch_page", fake_page)
    monkeypatch.setattr("danmu_intel.collect.huya.aiohttp.ClientSession", lambda **kw: FakeSession(**kw))
    monkeypatch.setattr("danmu_intel.collect.huya.aiohttp.WSMsgType", FAKE_WS_MSG_TYPE)

    async def run():
        return [frame async for frame in LiveTransport().frames("660000")]

    assert asyncio.run(run()) == []
