"""流层重连：静默看门狗与重连回调（issue #5 §2：无消息 60 秒触发重连）。"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from danmu_intel.collect import adapter
from danmu_intel.collect.adapter import RoomKey, reconnecting

from conftest import BASE_TS, make_event

ROOM = RoomKey("huya", "660000", "https://www.huya.com/660000")


class ScriptedStream:
    """按脚本产出事件：`None` 表示「静默等待」（不发消息也不报错），末尾自然结束。"""

    def __init__(self, script: list[object], *, calls: list[str] | None = None, tag: str = "") -> None:
        self._script = script
        self._calls = calls
        self._tag = tag
        self.closed = False

    async def stream(self) -> AsyncIterator:
        if self._calls is not None:
            self._calls.append(self._tag)
        try:
            for item in self._script:
                if item is None:
                    await asyncio.sleep(3600)  # 静默：只能被看门狗或取消打断
                else:
                    yield item
        finally:
            self.closed = True


def run_until(agen: AsyncIterator, count: int) -> tuple[list, list[str]]:
    """取够 `count` 个事件就收工（不连网络、用极短退避）。"""
    reasons: list[str] = []

    async def collect() -> list:
        events = []
        async for event in agen:
            events.append(event)
            if len(events) >= count:
                break
        return events

    return asyncio.run(collect()), reasons


def test_silence_triggers_reconnect_and_reports_reason(monkeypatch):
    """60 秒（测试里压到 0.05s）没有消息 → 放弃这条连接、重连、回调 silence。"""
    silent = ScriptedStream([None])
    talker = ScriptedStream([make_event(BASE_TS, text="回来了")])
    streams = [silent, talker]
    reasons: list[str] = []

    def connect(room: RoomKey) -> AsyncIterator:
        return streams.pop(0).stream()

    monkeypatch.setattr(adapter, "RECONNECT_BACKOFF_S", (0.0,))

    async def collect() -> list:
        events = []
        async for event in reconnecting(
            connect, ROOM, silence_timeout=0.05, on_reconnect=reasons.append
        ):
            events.append(event)
            break
        return events

    events = asyncio.run(collect())
    assert [event.text for event in events] == ["回来了"]
    assert reasons == ["silence"]
    assert silent.closed is True, "静默的那条连接必须被关掉，不能泄漏"


def test_stream_error_and_end_report_reasons(monkeypatch):
    reasons: list[str] = []
    monkeypatch.setattr(adapter, "RECONNECT_BACKOFF_S", (0.0,))
    monkeypatch.setattr(adapter, "SILENCE_TIMEOUT_S", 0.01)

    class Boom:
        async def stream(self) -> AsyncIterator:
            raise ConnectionError("断流")
            yield  # pragma: no cover

    class Ends:
        async def stream(self) -> AsyncIterator:
            if False:  # pragma: no cover
                yield
            return

    class Talker:
        async def stream(self) -> AsyncIterator:
            yield make_event(BASE_TS, text="ok")

    streams = [Boom(), Ends(), Talker()]

    def connect(room: RoomKey) -> AsyncIterator:
        return streams.pop(0).stream()

    async def collect() -> list:
        events = []
        async for event in reconnecting(connect, ROOM, on_reconnect=reasons.append):
            events.append(event)
            break
        return events

    events = asyncio.run(collect())
    assert [event.text for event in events] == ["ok"]
    assert reasons == ["error", "ended"]


def test_reconnecting_uses_module_backoff_ladder(monkeypatch):
    """退避阶梯读模块常量（契约测试靠 monkeypatch 把它压到 0 才不拖时间）。"""
    monkeypatch.setattr(adapter, "RECONNECT_BACKOFF_S", (0.0, 0.0))
    monkeypatch.setattr(adapter, "SILENCE_TIMEOUT_S", 0.01)
    calls: list[str] = []
    streams = [
        ScriptedStream([], calls=calls, tag="a"),
        ScriptedStream([make_event(BASE_TS)], calls=calls, tag="b"),
    ]

    def connect(room: RoomKey) -> AsyncIterator:
        return streams.pop(0).stream()

    events, _ = run_until(reconnecting(connect, ROOM), 1)
    assert len(events) == 1 and calls == ["a", "b"]


def test_reconnecting_cancellation_closes_current_connection(monkeypatch):
    """被上层收工时，当前连接也要关掉（不留悬挂 socket）。"""
    monkeypatch.setattr(adapter, "RECONNECT_BACKOFF_S", (0.0,))
    monkeypatch.setattr(adapter, "SILENCE_TIMEOUT_S", 3600.0)
    silent = ScriptedStream([None])

    async def go() -> None:
        agen = reconnecting(lambda room: silent.stream(), ROOM)
        task = asyncio.ensure_future(agen.__anext__())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
