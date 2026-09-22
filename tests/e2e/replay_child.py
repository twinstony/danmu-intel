"""子进程回放采集器（e2e 用）：真实的 `run_session` + 录制帧，不连网络。

`tests/e2e/test_supervisor_processes.py` 用注入的 spawn 启动本脚本，于是被测到的是
**真进程 + 真心跳 + 真 SQLite + 真 JSONL**，只把网络那一段换成录制帧回放
（NFR-GA-4：断网可跑）。环境变量（`DANMU_INTEL_DATA` / `DANMU_INTEL_SUPERVISION`）
由 supervisor 通过 `child_invocation` 递过来，与生产子进程完全一致。

回放用一台**固定时钟**：重启后重放的同一批帧拿到同一个 `ts`，于是重复记录有相同的
`msg_hash`——「不重不漏」里的「不重」才有可断言的对象（真实链路里平台不会重发旧弹幕，
重启重放是测试独有的现象）。
"""

from __future__ import annotations

import asyncio
import argparse
import json
import sys
from pathlib import Path
from typing import AsyncIterator

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from danmu_intel.collect import huya  # noqa: E402
from danmu_intel.collect.huya import HuyaAdapter  # noqa: E402
from danmu_intel.collect.runner import run_session  # noqa: E402

BASE_MS = 1_790_064_000_000  # 2026-09-22 16:00 本地时（与 T1 的样例落盘同目录）
PAGE = (
    '"lProfileRoom":660000,"lYyid":1,"lChannelId":2,"lSubChannelId":2,'
    '"eLiveStatus":2,"sNick":"样例主播","sRoomName":"标题","sGameFullName":"英雄联盟"'
)


class ReplayClock:
    """每次连接从同一基准重新计时的假时钟（`huya` 模块只用到 `time.time()`）。"""

    def __init__(self, base_ms: int) -> None:
        self.base_ms = base_ms
        self.offset_ms = 0

    def reset(self) -> None:
        self.offset_ms = 0

    def time(self) -> float:
        value = (self.base_ms + self.offset_ms) / 1000
        self.offset_ms += 1
        return value


class PacedReplay:
    """一条连接里按固定节奏发完录制帧，然后静默挂住（等下一批弹幕）。"""

    def __init__(self, frames: list[bytes], *, interval_s: float, clock: ReplayClock) -> None:
        self._frames = frames
        self._interval_s = interval_s
        self._clock = clock

    async def frames(self, room_id: str) -> AsyncIterator[bytes]:
        self._clock.reset()
        for frame in self._frames:
            yield frame
            await asyncio.sleep(self._interval_s)
        await asyncio.sleep(3600)  # 像真实直播间一样安静地等（不自己结束）


def load_frames(path: Path) -> list[bytes]:
    return [
        bytes.fromhex(payload["frame_hex"])
        for payload in (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
        if payload["kind"] == "danmaku"
    ]


async def main() -> int:
    parser = argparse.ArgumentParser(prog="replay_child")
    parser.add_argument("--room-id", required=True)
    parser.add_argument("--match-id", type=int, required=True)
    parser.add_argument("--frames", required=True)
    parser.add_argument("--interval", type=float, default=0.02)
    args = parser.parse_args()

    async def fake_page(room_id: str) -> str:  # 只换掉 HTTP 探活那一段
        return PAGE

    huya.fetch_page = fake_page
    huya.time = ReplayClock(BASE_MS)  # type: ignore[assignment]
    adapter = HuyaAdapter(
        transport=PacedReplay(load_frames(Path(args.frames)), interval_s=args.interval, clock=huya.time)
    )
    room = adapter.parse_room(f"https://www.huya.com/{args.room_id}")
    result = await run_session(room, adapter=adapter, match_id=args.match_id)
    print(f"回放采集收工：{result.msg_count} 条（{result.state}）", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
