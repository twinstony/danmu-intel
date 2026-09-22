"""适配器契约测试（设计 §7.1 / ADR-0008，测试缝 S1）。

**对注册表里的每个适配器跑同一套断言**：字段齐备、时间单调、非法 payload 不崩、
断流触发重连。新增平台只需加一行注册 + 一行回放工厂，这套断言自动覆盖
「新增平台不得改动已有平台逻辑」。

回放用 `tests/fixtures/huya/frames.jsonl`（真实录制 + 脱敏），**不连真实直播**。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import AsyncIterator, Callable

import pytest

from danmu_intel.collect import ADAPTERS
from danmu_intel.collect.adapter import Adapter, RoomKey
from danmu_intel.collect.huya import HuyaAdapter
from danmu_intel.common.events import JSONL_FIELDS

from conftest import load_huya_fixture

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


def _fixture_frames(kind: str) -> list[bytes]:
    return [bytes.fromhex(record["frame_hex"]) for record in load_huya_fixture() if record["kind"] == kind]


def _huya_replay(frames: list[bytes]) -> Replay:
    transport = ReplayTransport([frames])
    return Replay(HuyaAdapter(transport=transport), transport)


# 每个平台一行：平台标识 → 用回放帧构造适配器的工厂
REPLAY_FACTORIES: dict[str, Callable[[list[bytes]], Replay]] = {"huya": _huya_replay}

SAMPLE_URLS = {"huya": "https://www.huya.com/660000"}

pytestmark = pytest.mark.parametrize("platform", sorted(ADAPTERS))


@pytest.fixture(autouse=True)
def fast_reconnect(monkeypatch):
    monkeypatch.setattr("danmu_intel.collect.adapter.RECONNECT_BACKOFF_S", RECONNECT_BACKOFF)


def _collect(adapter: Adapter, room: RoomKey, stop_after: int) -> list:
    async def run() -> list:
        events = []
        async for event in adapter.stream(room):
            events.append(event)
            if len(events) >= stop_after:
                break
        return events

    return asyncio.run(run())


def test_registry_covers_replay_factories(platform):
    """契约测试必须覆盖注册表里的每个平台（新平台漏配回放工厂会在这里失败）。"""
    assert platform in REPLAY_FACTORIES, f"平台 {platform} 未配置回放工厂"
    assert ADAPTERS[platform].platform == platform


def test_parse_room_returns_platform_room_key(platform):
    adapter = ADAPTERS[platform]
    room = adapter.parse_room(SAMPLE_URLS[platform])
    assert isinstance(room, RoomKey)
    assert room.platform == platform
    assert room.room_id and isinstance(room.room_id, str)
    assert room.url


def test_stream_yields_complete_events_and_monotonic_ts(platform, data_root):
    frames = _fixture_frames("danmaku")
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
    good = _fixture_frames("danmaku")[:3]
    garbage = _fixture_frames("garbage") + _fixture_frames("other")
    replay = REPLAY_FACTORIES[platform](garbage + good)
    room = replay.adapter.parse_room(SAMPLE_URLS[platform])

    events = _collect(replay.adapter, room, len(good))
    assert len(events) == len(good)


def test_stream_reconnects_after_disconnect(platform, data_root):
    frames = _fixture_frames("danmaku")
    half = len(frames) // 2
    transport = ReplayTransport([frames[:half], frames[half:]])
    adapter: Adapter = HuyaAdapter(transport=transport)
    assert platform in ADAPTERS  # 契约对每个注册平台都成立
    room = adapter.parse_room(SAMPLE_URLS[platform])

    events = _collect(adapter, room, len(frames))
    assert len(events) == len(frames), "断流后必须自动重连并继续产出事件"
    assert transport.calls >= 2, "应当至少重连一次"


def test_probe_returns_probe_shape(platform, monkeypatch):
    adapter = ADAPTERS[platform]
    page = "".join(
        [
            '"lProfileRoom":660000,"lYyid":1,"lChannelId":2,"lSubChannelId":2,',
            '"eLiveStatus":2,"sNick":"样例主播","sRoomName":"标题","sGameFullName":"英雄联盟"',
        ]
    )

    async def fake_page(room_id: str) -> str:
        return page

    monkeypatch.setattr("danmu_intel.collect.huya.fetch_page", fake_page)
    probe = asyncio.run(adapter.probe(adapter.parse_room(SAMPLE_URLS[platform])))
    assert isinstance(probe.is_live, bool)
    assert probe.streamer == "样例主播"
