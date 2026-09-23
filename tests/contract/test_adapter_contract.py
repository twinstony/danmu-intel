"""适配器契约测试（设计 §7.1 / ADR-0008，测试缝 S1）。

**对注册表里的每个适配器跑同一套断言**：字段齐备、时间单调、非法 payload 不崩、
断流触发重连。新增平台只需加一行注册 + 一行回放工厂，这套断言自动覆盖
「新增平台不得改动已有平台逻辑」。

回放用 `tests/fixtures/<平台>/frames.jsonl`（真实录制 + 脱敏），**不连真实直播**。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import AsyncIterator, Callable

import pytest

from danmu_intel.collect import ADAPTERS, soop
from danmu_intel.collect.adapter import Adapter, RoomKey
from danmu_intel.collect.huya import HuyaAdapter
from danmu_intel.collect.soop import SoopAdapter
from danmu_intel.common.events import JSONL_FIELDS

from conftest import load_fixture

RECONNECT_BACKOFF = (0.0,)


class ReplayTransport:
    """按批次回放录制帧；批次用尽后抛错，用来模拟断流。"""

    def __init__(self, batches: list[list[bytes]]) -> None:
        self._batches = list(batches)
        self.calls = 0

    async def frames(self, room_id: str) -> AsyncIterator[bytes]:
        self.calls += 1
        if not self._batches:
            raise ConnectionError("回放批次已用尽")
        for frame in self._batches.pop(0):
            yield frame


@dataclass(frozen=True)
class Replay:
    adapter: Adapter
    transport: ReplayTransport


def _fixture_frames(platform: str, kind: str) -> list[bytes]:
    return [bytes.fromhex(record["frame_hex"]) for record in load_fixture(platform) if record["kind"] == kind]


def _huya_replay(frames: list[bytes]) -> Replay:
    transport = ReplayTransport([frames])
    return Replay(HuyaAdapter(transport=transport), transport)


def _soop_replay(frames: list[bytes]) -> Replay:
    transport = ReplayTransport([frames])
    return Replay(SoopAdapter(transport=transport), transport)


def _as_huya(transport) -> Adapter:
    return HuyaAdapter(transport=transport)


def _as_soop(transport) -> Adapter:
    return SoopAdapter(transport=transport)


# 每个平台一行：平台标识 → 用回放帧构造适配器 / 用给定 transport 构造适配器的工厂
REPLAY_FACTORIES: dict[str, Callable[[list[bytes]], Replay]] = {"huya": _huya_replay, "soop": _soop_replay}
ADAPTER_FACTORIES: dict[str, Callable[[object], Adapter]] = {"huya": _as_huya, "soop": _as_soop}

SAMPLE_URLS = {
    "huya": "https://www.huya.com/660000",
    "soop": "https://play.sooplive.com/seokwngud/297306971",
}

HUYA_PROBE_PAGE = "".join(
    [
        '"lProfileRoom":660000,"lYyid":1,"lChannelId":2,"lSubChannelId":2,',
        '"eLiveStatus":2,"sNick":"样例主播","sRoomName":"标题","sGameFullName":"英雄联盟"',
    ]
)
SOOP_PROBE_PAYLOAD = {
    "CHANNEL": {
        "BJID": "seokwngud",
        "BNO": "297306971",
        "CHATNO": "2679",
        "CHIP": "222.233.54.81",
        "CHPT": "9000",
        "BSTATUS": "BROADING",
        "BJNICK": "样例主播",
        "TITLE": "标题",
        "CATEGORY_TAGS": ["리그 오브 레전드"],
    }
}


def _stub_huya_probe(monkeypatch) -> None:
    async def fake_page(room_id: str) -> str:
        return HUYA_PROBE_PAGE

    monkeypatch.setattr("danmu_intel.collect.huya.fetch_page", fake_page)


def _stub_soop_probe(monkeypatch) -> None:
    async def fake_room_info(bj_id: str):
        return soop.parse_room_info(SOOP_PROBE_PAYLOAD)

    monkeypatch.setattr("danmu_intel.collect.soop.fetch_room_info", fake_room_info)


# 每个平台一行：平台标识 → 探测接口的桩（房间元数据来自平台公开接口）
PROBE_STUBS: dict[str, Callable[[object], None]] = {"huya": _stub_huya_probe, "soop": _stub_soop_probe}

@pytest.fixture(params=sorted(ADAPTERS))
def platform(request) -> str:
    """每个注册平台跑一遍同一套断言（新增平台自动被覆盖）。"""
    return request.param


@pytest.fixture(autouse=True)
def fast_reconnect(monkeypatch):
    monkeypatch.setattr("danmu_intel.collect.adapter.RECONNECT_BACKOFF_S", RECONNECT_BACKOFF)


def test_registry_is_first_wave_platforms():
    """首发平台 = 虎牙 + SOOP（ADR-0008）；注册表是唯一的接入点。"""
    assert sorted(ADAPTERS) == ["huya", "soop"]


def _collect(
    adapter: Adapter, room: RoomKey, stop_after: int, *, on_reconnect=None
) -> list:
    async def run() -> list:
        events = []
        async for event in adapter.stream(room, on_reconnect=on_reconnect):
            events.append(event)
            if len(events) >= stop_after:
                break
        return events

    return asyncio.run(run())


def test_registry_covers_replay_factories(platform):
    """契约测试必须覆盖注册表里的每个平台（新平台漏配回放工厂会在这里失败）。"""
    assert platform in REPLAY_FACTORIES, f"平台 {platform} 未配置回放工厂"
    assert platform in ADAPTER_FACTORIES, f"平台 {platform} 未配置适配器工厂"
    assert platform in PROBE_STUBS, f"平台 {platform} 未配置探测桩"
    assert platform in SAMPLE_URLS, f"平台 {platform} 未配置样例链接"
    assert ADAPTERS[platform].platform == platform


def test_parse_room_returns_platform_room_key(platform):
    adapter = ADAPTERS[platform]
    room = adapter.parse_room(SAMPLE_URLS[platform])
    assert isinstance(room, RoomKey)
    assert room.platform == platform
    assert room.room_id and isinstance(room.room_id, str)
    assert room.url


def test_stream_yields_complete_events_and_monotonic_ts(platform, data_root):
    frames = _fixture_frames(platform, "danmaku")
    replay = REPLAY_FACTORIES[platform](frames)
    room = replay.adapter.parse_room(SAMPLE_URLS[platform])

    events = _collect(replay.adapter, room, len(frames))
    assert len(events) == len(frames), "每条弹幕帧都应当产出一个事件"
    for event in events:
        payload = event.to_json()
        assert list(payload) == list(JSONL_FIELDS), "落盘字段必须齐备且顺序固定"
        assert isinstance(event.ts, int) and event.ts > 0
        assert event.platform == platform
        assert event.room_id == room.room_id
        assert event.text.strip()
        assert isinstance(event.extra, dict)
        assert event.match_id is None
        assert isinstance(event.user_hash, str) and len(event.user_hash) >= 16
    assert [event.ts for event in events] == sorted(event.ts for event in events), "时间必须单调不减"


def test_stream_survives_illegal_payloads(platform, data_root):
    """非法/无关 payload 不得中断事件流（不崩、不丢后续）。"""
    good = _fixture_frames(platform, "danmaku")[:3]
    garbage = _fixture_frames(platform, "garbage") + _fixture_frames(platform, "other")
    replay = REPLAY_FACTORIES[platform](garbage + good)
    room = replay.adapter.parse_room(SAMPLE_URLS[platform])

    events = _collect(replay.adapter, room, len(good))
    assert len(events) == len(good)


def test_stream_reconnects_after_disconnect(platform, data_root):
    frames = _fixture_frames(platform, "danmaku")
    half = len(frames) // 2
    transport = ReplayTransport([frames[:half], frames[half:]])
    adapter = ADAPTER_FACTORIES[platform](transport)
    room = adapter.parse_room(SAMPLE_URLS[platform])

    events = _collect(adapter, room, len(frames))
    assert len(events) == len(frames), "断流后必须自动重连并继续产出事件"
    assert transport.calls >= 2, "应当至少重连一次"


class BrokenTransport:
    """第一次连接发到一半就断流（抛错），第二次把剩下的帧发完。"""

    def __init__(self, first: list[bytes], second: list[bytes]) -> None:
        self._batches = [first, second]
        self.calls = 0

    async def frames(self, room_id: str) -> AsyncIterator[bytes]:
        self.calls += 1
        batch = self._batches.pop(0)
        for frame in batch:
            yield frame
        if self._batches:
            raise ConnectionError("断流")


def test_stream_reports_reconnect_reason(platform, data_root):
    """重连必须把原因回调给采集器（T2 靠它累计 `reconnects` 并把会话标成 `stalled`）。"""
    frames = _fixture_frames(platform, "danmaku")
    half = len(frames) // 2
    adapter = ADAPTER_FACTORIES[platform](BrokenTransport(frames[:half], frames[half:]))
    room = adapter.parse_room(SAMPLE_URLS[platform])

    reasons: list[str] = []
    events = _collect(adapter, room, len(frames), on_reconnect=reasons.append)
    assert len(events) == len(frames), "断流后必须重连并补齐剩余事件"
    assert reasons == ["error"], "抛错断流的原因必须是 error"


def test_probe_returns_probe_shape(platform, monkeypatch):
    adapter = ADAPTERS[platform]
    PROBE_STUBS[platform](monkeypatch)
    probe = asyncio.run(adapter.probe(adapter.parse_room(SAMPLE_URLS[platform])))
    assert isinstance(probe.is_live, bool)
    assert probe.streamer == "样例主播"
    assert probe.title == "标题"
