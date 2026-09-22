#!/usr/bin/env python3
"""录制虎牙原始帧 → 生成脱敏 fixture（契约测试回放用）。

两步（第一步需要外网，第二步完全离线）：

    # 1) 录制：连真实直播间，把原始帧逐帧写成 hex
    python3 tools/record_fixtures.py record --url https://www.huya.com/660000 \\
        --seconds 60 --dump .frames-dump/660000.jsonl

    # 2) 脱敏：把录制帧里的**身份与文本**替换成样例值，产出仓库内 fixture
    python3 tools/record_fixtures.py sanitize --dump .frames-dump/660000.jsonl \\
        --out tests/fixtures/huya/frames.jsonl

脱敏原则：只保留「帧的线格式结构」，uid / 昵称 / 弹幕原文一律替换，
因此 fixture 里不含任何真实用户身份或用户原话。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from danmu_intel.collect.huya import (  # noqa: E402
    LiveTransport,
    build_danmaku_frame,
    decode_frame,
    iter_recorded_frames,
    parse_room_id,
)

SAMPLE_TEXTS = (
    "样例弹幕：这波团开得太急了",
    "样例弹幕：上单换血有点亏",
    "样例弹幕：大龙快刷了",
    "样例弹幕：这局稳了",
    "样例弹幕：🎉🎉🎉",
    "样例弹幕：" + "长文本占用 STRING4 分支。" * 20,
)
SAMPLE_NICKS = ("样例用户甲", "样例用户乙", "样例用户丙")


def _record(args: argparse.Namespace) -> int:
    room_id = parse_room_id(args.url)
    transport = LiveTransport(dump_frames=Path(args.dump))

    async def run() -> int:
        count = 0
        deadline = None if not args.seconds else time.monotonic() + args.seconds
        try:
            async for frame in transport.frames(room_id):
                count += 1
                if args.max_frames and count >= args.max_frames:
                    break
                if deadline and time.monotonic() >= deadline:
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 录制是开发工具：网络中断即停，已录帧仍可用
            print(f"录制中断：{exc}", file=sys.stderr)
        return count

    frames = asyncio.run(run())
    print(f"已录制 {frames} 帧 → {args.dump}")
    return 0


def _sanitize(args: argparse.Namespace) -> int:
    dump = Path(args.dump)
    uid_map: dict[str, str] = {}
    danmaku_count = 0
    other_count = 0
    records: list[dict[str, str]] = []
    for frame in iter_recorded_frames(dump):
        decoded = decode_frame(frame)
        if not decoded:
            # 非弹幕帧（心跳应答、礼物等）原样保留：它们验证「非法/无关 payload 不崩」
            records.append({"kind": "other", "frame_hex": frame.hex()})
            other_count += 1
            continue
        raw = decoded[0]
        index = uid_map.setdefault(raw.uid, str(1000000 + len(uid_map) + 1))
        sample_no = danmaku_count
        text = SAMPLE_TEXTS[sample_no % len(SAMPLE_TEXTS)]
        nick = SAMPLE_NICKS[sample_no % len(SAMPLE_NICKS)]
        frame = build_danmaku_frame(index, nick, text)
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
    parser = argparse.ArgumentParser(description="虎牙原始帧录制与 fixture 脱敏")
    sub = parser.add_subparsers(dest="command", required=True)

    record = sub.add_parser("record", help="连真实直播间录制原始帧（需要外网）")
    record.add_argument("--url", required=True)
    record.add_argument("--seconds", type=float, default=60)
    record.add_argument("--max-frames", type=int, default=0)
    record.add_argument("--dump", required=True, help="原始帧落盘路径（hex，一行一帧）")
    record.set_defaults(func=_record)

    sanitize = sub.add_parser("sanitize", help="把录制帧脱敏成仓库内 fixture（离线）")
    sanitize.add_argument("--dump", required=True)
    sanitize.add_argument("--out", required=True)
    sanitize.set_defaults(func=_sanitize)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
