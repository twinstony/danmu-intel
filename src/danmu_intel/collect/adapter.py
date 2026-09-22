"""适配器契约与共性采集逻辑（设计 §7.1，ADR-0008）。

**新增平台 = 新增一个模块 + 注册表加一行，已有平台代码零改动。**
适配器只负责「平台原始 payload → `DanmuEvent`」；重连、落盘、建库都是共性逻辑，
放在这里与 `runner.py`。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import AsyncIterator, Callable, Protocol

from danmu_intel.common.events import DanmuEvent

logger = logging.getLogger(__name__)

# 断流重连退避（设计 §7.4：1s → 2s → 4s → … → 60s 上限）
RECONNECT_BACKOFF_S = (1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0)
# 无消息多久算断流（设计 §7.3：60 秒静默→ stalled 并重连）。
SILENCE_TIMEOUT_S = 60.0


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
    """平台适配器契约。契约测试见 `tests/contract/test_adapter_contract.py`。

    `stream` 的 `on_reconnect(原因)` 是采集器用来累计 `reconnects` 并把会话标成
    `stalled` 的接线口（原因：`silence` / `error` / `ended`）——适配器只需把它
    转交给 `reconnecting`，不必知道上层拿它做什么。
    """

    platform: str

    def parse_room(self, url: str) -> RoomKey: ...

    async def probe(self, room: RoomKey) -> Probe: ...

    def stream(
        self, room: RoomKey, *, on_reconnect: Callable[[str], None] | None = None
    ) -> AsyncIterator[DanmuEvent]: ...


def _backoff_delay(attempt: int, backoff: tuple[float, ...]) -> float:
    return backoff[min(attempt, len(backoff) - 1)]


async def _aclose(iterator: AsyncIterator[DanmuEvent]) -> None:
    """收掉一条已放弃的连接（不把清理期的异常当业务异常）。"""
    aclose = getattr(iterator, "aclose", None)
    if aclose is None:
        return
    with contextlib.suppress(Exception):
        await aclose()


async def reconnecting(
    connect: Callable[[RoomKey], AsyncIterator[DanmuEvent]],
    room: RoomKey,
    *,
    backoff: tuple[float, ...] | None = None,
    silence_timeout: float | None = None,
    on_reconnect: Callable[[str], None] | None = None,
) -> AsyncIterator[DanmuEvent]:
    """把「一次连接」包装成「断流自动重连、不静默停止」的事件流。

    - 连接报错或正常结束 → 退避后重连；
    - **连续 `SILENCE_TIMEOUT_S`（预设 60 秒）没有消息** → 主动关掉这条连接再重连
      （平台不报错、不说话的静默断流也逃不掉）；
    - 每次重连都回调 `on_reconnect(原因)` —— `silence` / `error` / `ended`，让上层
      累计 `reconnects` 并把状态标成 `stalled`。进程级监督（重启退避、重启上限）
      在 `supervisor.py`。
    """
    ladder = backoff if backoff is not None else RECONNECT_BACKOFF_S
    silence = silence_timeout if silence_timeout is not None else SILENCE_TIMEOUT_S
    attempt = 0
    while True:
        iterator = connect(room).__aiter__()
        reason = "ended"
        try:
            while True:
                try:
                    event = await asyncio.wait_for(iterator.__anext__(), silence)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    reason = "silence"
                    logger.warning(
                        "【%s/%s】%.0f 秒没有消息，判定断流并重连",
                        room.platform,
                        room.room_id,
                        silence,
                    )
                    break
                attempt = 0
                yield event
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 断流/解码异常：重连并留痕
            reason = "error"
            logger.warning(
                "【%s/%s】弹幕流中断（第 %d 次）：%s",
                room.platform,
                room.room_id,
                attempt + 1,
                exc,
            )
        finally:
            await _aclose(iterator)
        delay = _backoff_delay(attempt, ladder)
        attempt += 1
        if on_reconnect is not None:
            on_reconnect(reason)
        if reason == "ended":
            logger.info("【%s/%s】弹幕流结束，%.1fs 后重连", room.platform, room.room_id, delay)
        await asyncio.sleep(delay)
