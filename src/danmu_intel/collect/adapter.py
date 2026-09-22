"""适配器契约与共性采集逻辑（设计 §7.1，ADR-0008）。

**新增平台 = 新增一个模块 + 注册表加一行，已有平台代码零改动。**
适配器只负责「平台原始 payload → `DanmuEvent`」；重连、落盘、建库都是共性逻辑，
放在这里与 `runner.py`。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import AsyncIterator, Callable, Protocol

from danmu_intel.common.events import DanmuEvent

logger = logging.getLogger(__name__)

# 断流重连退避（设计 §7.4：1s → 2s → 4s → … → 60s 上限）
RECONNECT_BACKOFF_S = (1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0)


@dataclass(frozen=True, slots=True)
class RoomKey:
    """一个采集目标：平台 + 房间号。"""

    platform: str
    room_id: str
    url: str


@dataclass(frozen=True, slots=True)
class Probe:
    """房间探测结果（是否在播 / 主播 / 标题 / 分区游戏）。"""

    is_live: bool
    streamer: str | None
    title: str | None
    game: str | None


class Adapter(Protocol):
    """平台适配器契约。契约测试见 `tests/contract/test_adapter_contract.py`。"""

    platform: str

    def parse_room(self, url: str) -> RoomKey: ...

    async def probe(self, room: RoomKey) -> Probe: ...

    def stream(self, room: RoomKey) -> AsyncIterator[DanmuEvent]: ...


def _backoff_delay(attempt: int, backoff: tuple[float, ...]) -> float:
    return backoff[min(attempt, len(backoff) - 1)]


async def reconnecting(
    connect: Callable[[RoomKey], AsyncIterator[DanmuEvent]],
    room: RoomKey,
    *,
    backoff: tuple[float, ...] = RECONNECT_BACKOFF_S,
) -> AsyncIterator[DanmuEvent]:
    """把「一次连接」包装成「断流自动重连、不静默停止」的事件流。

    重连上限、报警与进程级监督属于 T2（多房间采集与监督）；T1 只保证不断流停止。
    """
    attempt = 0
    while True:
        try:
            async for event in connect(room):
                attempt = 0
                yield event
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 断流/解码异常：重连并留痕
            delay = _backoff_delay(attempt, backoff)
            attempt += 1
            logger.warning(
                "【%s/%s】弹幕流中断（第 %d 次），%.1fs 后重连：%s",
                room.platform,
                room.room_id,
                attempt,
                delay,
                exc,
            )
            await asyncio.sleep(delay)
            continue
        delay = _backoff_delay(attempt, backoff)
        attempt += 1
        logger.info("【%s/%s】弹幕流结束，%.1fs 后重连", room.platform, room.room_id, delay)
        await asyncio.sleep(delay)
