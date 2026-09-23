#!/usr/bin/env python3
"""录制平台原始帧 → 生成脱敏 fixture（契约测试回放用）。

两步（第一步需要外网，第二步完全离线）：

    # 1) 录制：连真实直播间，把原始帧逐帧写成 hex
    python3 tools/record_fixtures.py record --platform huya --url https://www.huya.com/660000 \\
        --seconds 60 --dump .frames-dump/660000.jsonl
    python3 tools/record_fixtures.py record --platform soop \\
        --url https://play.sooplive.com/afchall --seconds 60 --dump .frames-dump/afchall.jsonl

    # 2) 脱敏：把录制帧里的**身份与文本**替换成样例值，产出仓库内 fixture
    python3 tools/record_fixtures.py sanitize --platform huya --dump .frames-dump/660000.jsonl \\
        --out tests/fixtures/huya/frames.jsonl
    python3 tools/record_fixtures.py sanitize --platform soop --dump .frames-dump/afchall.jsonl \\
        --out tests/fixtures/soop/frames.jsonl

脱敏原则：只保留「帧的线格式结构」，uid / 昵称 / 弹幕原文一律替换，
因此 fixture 里不含任何真实用户身份或用户原话。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from danmu_intel.collect import huya, soop  # noqa: E402

SAMPLE_TEXTS = (
    "样例弹幕：这波团开得太急了",
    "样例弹幕：上单换血有点亏",
    "样例弹幕：大龙快刷了",
    "样例弹幕：这局稳了",
    "样例弹幕：🎉🎉🎉",
    "样例弹幕：" + "长文本占用 STRING4 分支。" * 20,
)
SAMPLE_NICKS = ("样例用户甲", "样例用户乙", "样例用户丙")


@dataclass(frozen=True)
class Platform:
    """一个平台的录制/脱敏接线（工具只用这些：解析房间、取帧、解码、重建帧）。"""

    parse_room_id: Callable[[str], str]
    transport: Callable[[Path], object]
    decode: Callable[[bytes], list]
    rebuild: Callable[[str, str, str], bytes]
    iter_recorded: Callable[[Path], Iterator[bytes]]
    other_key: Callable[[bytes], str]  # 非弹幕帧的「形状」：同一形状只留一帧


PLATFORMS: dict[str, Platform] = {
    "huya": Platform(
        parse_room_id=huya.parse_room_id,
        transport=lambda dump: huya.LiveTransport(dump_frames=dump),
        decode=huya.decode_frame,
        rebuild=lambda uid, nick, text: huya.build_danmaku_frame(uid, nick, text),
        iter_recorded=huya.iter_recorded_frames,
        other_key=lambda frame: frame[:1].hex(),  # Tars 消息类型
    ),
    "soop": Platform(
        parse_room_id=soop.parse_bj_id,
        transport=lambda dump: soop.LiveTransport(dump_packets=dump),
        decode=soop.decode_message,
        rebuild=lambda uid, nick, text: soop.build_chat_packet(uid, text, nickname=nick),
        iter_recorded=soop.iter_recorded_packets,
        other_key=lambda frame: str(soop.packet_header(frame)[0]),  # 服务码
    ),
}


def _record(args: argparse.Namespace) -> int:
    platform = PLATFORMS[args.platform]
    room_id = platform.parse_room_id(args.url)
    transport = platform.transport(Path(args.dump))

    async def run() -> int:
        count = 0
        deadline = None if not args.seconds else time.monotonic() + args.seconds
        attempts = 0
        while attempts < args.retries:
            attempts += 1
            try:
                async for _ in transport.frames(room_id):
                    count += 1
                    if args.max_frames and count >= args.max_frames:
                        return count
                    if deadline and time.monotonic() >= deadline:
                        return count
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 网络抖动（TLS 重置/超时）不该让整段录制白跑
                print(f"录制中断（第 {attempts} 次）：{type(exc).__name__}: {exc}", file=sys.stderr)
                await asyncio.sleep(args.retry_delay)
            if deadline and time.monotonic() >= deadline:
                return count
            if attempts >= args.retries:
                return count
        return count

    frames = asyncio.run(run())
    print(f"已录制 {frames} 帧 → {args.dump}")
    return 0


def _sanitize(args: argparse.Namespace) -> int:
    platform = PLATFORMS[args.platform]
    dump = Path(args.dump)
    uid_map: dict[str, str] = {}
    danmaku_count = 0
    other_count = 0
    other_keys: set[str] = set()
    records: list[dict[str, str]] = []
    for frame in platform.iter_recorded(dump):
        decoded = platform.decode(frame)
        if not decoded:
            # 非弹幕帧（心跳应答、礼物、进频道应答等）任一形状留一帧即可：
            # 它们验证「非法/无关 payload 不崩」，同形状录多少帧都不增加覆盖
            key = platform.other_key(frame)
            if key in other_keys:
                continue
            other_keys.add(key)
            records.append({"kind": "other", "frame_hex": frame.hex()})
            other_count += 1
            continue
        raw = decoded[0]
        index = uid_map.setdefault(raw.uid, str(1000000 + len(uid_map) + 1))
        sample_no = danmaku_count
        text = SAMPLE_TEXTS[sample_no % len(SAMPLE_TEXTS)]
        nick = SAMPLE_NICKS[sample_no % len(SAMPLE_NICKS)]
        frame = platform.rebuild(index, nick, text)
        records.append({"kind": "danmaku", "frame_hex": frame.hex(), "uid": index, "text": text})
        danmaku_count += 1

    # 补足线格式分支：非法/截断帧必须不崩
    truncated = records[0]["frame_hex"][:20] if records else "00071d"
    for broken in ("", "00", "ffffffff", truncated, "0a", "0a0a0a"):
        records.append({"kind": "garbage", "frame_hex": broken})

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"已生成 fixture：{out}（弹幕帧 {danmaku_count}、其它帧 {other_count}、非法帧 6）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="平台原始帧录制与 fixture 脱敏")
    sub = parser.add_subparsers(dest="command", required=True)

    record = sub.add_parser("record", help="连真实直播间录制原始帧（需要外网）")
    record.add_argument("--platform", choices=sorted(PLATFORMS), required=True)
    record.add_argument("--url", required=True)
    record.add_argument("--seconds", type=float, default=60)
    record.add_argument("--retries", type=int, default=5, help="连接失败/断流时的重试次数")
    record.add_argument("--retry-delay", type=float, default=3.0, help="重试间隔（秒）")
    record.add_argument("--max-frames", type=int, default=0)
    record.add_argument("--dump", required=True, help="原始帧落盘路径（hex，一行一帧）")
    record.set_defaults(func=_record)

    sanitize = sub.add_parser("sanitize", help="把录制帧脱敏成仓库内 fixture（离线）")
    sanitize.add_argument("--platform", choices=sorted(PLATFORMS), required=True)
    sanitize.add_argument("--dump", required=True)
    sanitize.add_argument("--out", required=True)
    sanitize.set_defaults(func=_sanitize)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
