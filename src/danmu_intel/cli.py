"""命令行入口：一条条命令跑通整条链路。

    danmu-intel collect --url https://www.huya.com/660000 --seconds 300 --match-id 1
    danmu-intel match add --league LPL --team-a iG --team-b LNG --state ended
    danmu-intel slice --match-id 1 --game-no 1 --start-ms … --end-ms …
    danmu-intel stats --match-id 1
    danmu-intel render --match-id 1          # → site/matches/1.html
    danmu-intel rebuild --match-id 1         # AC-13：删统计重算，断言结果不变
    danmu-intel verify-sources --match-id 1  # 逐项复核 文件+行范围+SHA256
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from danmu_intel.common import paths
from danmu_intel.common.db import open_db
from danmu_intel.common.matches import create_match
from danmu_intel.pipeline import (
    collect_facts,
    rebuild_metrics,
    render_match_page,
    verify_sources,
    write_metrics,
)
from danmu_intel.slice.manual import add_manual_slice

logger = logging.getLogger("danmu_intel")


def _cmd_collect(args: argparse.Namespace) -> int:
    from danmu_intel.collect import get_adapter
    from danmu_intel.collect.runner import run_session

    adapter = get_adapter(args.platform)
    room = adapter.parse_room(args.url)
    result = asyncio.run(
        run_session(
            room,
            adapter=adapter,
            match_id=args.match_id,
            seconds=args.seconds,
            conn=open_db(),
        )
    )
    probe = result.probe
    if probe:
        print(f"房间：{room.platform}/{room.room_id}｜{probe.streamer or '未知主播'}｜开播={probe.is_live}")
    print(f"采集会话 #{result.session_id}：共 {result.msg_count} 条弹幕")
    for segment in result.segments:
        print(f"  落盘：{paths.data_dir() / segment.rel_path}｜{segment.msg_count} 条｜SHA256 {segment.sha256}")
    return 0


def _cmd_match_add(args: argparse.Namespace) -> int:
    conn = open_db()
    try:
        match_id = create_match(
            conn,
            league=args.league,
            team_a=args.team_a,
            team_b=args.team_b,
            state=args.state,
            stage=args.stage,
            scheduled_at=args.scheduled_at,
            started_at=args.started_at,
            ended_at=args.ended_at,
            official_result=json.loads(args.official_result) if args.official_result else None,
        )
    finally:
        conn.close()
    print(f"已登记比赛 #{match_id}：{args.team_a} vs {args.team_b}（{args.state}）")
    return 0


def _cmd_slice(args: argparse.Namespace) -> int:
    conn = open_db()
    try:
        slice_id = add_manual_slice(
            conn,
            match_id=args.match_id,
            game_no=args.game_no,
            start_ms=args.start_ms,
            end_ms=args.end_ms,
            note=args.note,
            override_by=args.override_by,
            override_reason=args.override_reason,
        )
    finally:
        conn.close()
    print(f"已写入切片 #{slice_id}：G{args.game_no} {args.start_ms}–{args.end_ms}（边界来源：manual）")
    return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    conn = open_db()
    try:
        facts = collect_facts(conn, args.match_id, data_root=paths.data_dir())
        count = write_metrics(conn, facts)
        for game in facts.games:
            print(
                f"G{game.window.game_no}：{game.metrics['danmu_total']['count']} 条｜"
                f"独立发言者 {game.metrics['distinct_users']['count']} 人"
            )
        print(f"已写入 {count} 行规则统计（算法版本 {facts.algo_version}）")
    finally:
        conn.close()
    return 0


def _cmd_render(args: argparse.Namespace) -> int:
    conn = open_db()
    try:
        path = render_match_page(conn, args.match_id, data_root=paths.data_dir())
    finally:
        conn.close()
    print(f"已生成静态页：{path}")
    return 0


def _cmd_rebuild(args: argparse.Namespace) -> int:
    conn = open_db()
    try:
        same = rebuild_metrics(conn, args.match_id, data_root=paths.data_dir())
    finally:
        conn.close()
    if same:
        print("AC-13 通过：删除统计结果后，仅凭原始记录 + 切片重算的结果逐字节相同")
        return 0
    print("AC-13 失败：重算结果与删除前不一致（统计不可重建）", file=sys.stderr)
    return 1


def _cmd_verify_sources(args: argparse.Namespace) -> int:
    conn = open_db()
    try:
        failed = verify_sources(conn, args.match_id, data_root=paths.data_dir())
    finally:
        conn.close()
    if not failed:
        print("全部来源校验通过（文件 + 行范围 + SHA256）")
        return 0
    for ref in failed:
        print(f"来源校验失败：{ref.rel_path} 第 {ref.line_start}–{ref.line_end} 行", file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="danmu-intel", description="弹幕情报库（T1：采集→静态页最薄闭环）")
    parser.add_argument("--verbose", action="store_true", help="打印重连等运行日志")
    sub = parser.add_subparsers(dest="command", required=True)

    collect = sub.add_parser("collect", help="采集一个直播间的弹幕")
    collect.add_argument("--url", required=True, help="直播间链接或房间号")
    collect.add_argument("--platform", default="huya", help="平台标识（默认 huya）")
    collect.add_argument("--seconds", type=float, default=None, help="采集时长（秒），缺省则持续采集")
    collect.add_argument("--match-id", type=int, default=None, help="关联的比赛 id")
    collect.set_defaults(func=_cmd_collect)

    match = sub.add_parser("match", help="比赛实体")
    match_sub = match.add_subparsers(dest="match_command", required=True)
    add = match_sub.add_parser("add", help="登记一场比赛")
    add.add_argument("--league", required=True)
    add.add_argument("--team-a", required=True)
    add.add_argument("--team-b", required=True)
    add.add_argument("--state", default="ended")
    add.add_argument("--stage", default=None)
    add.add_argument("--scheduled-at", type=int, default=None)
    add.add_argument("--started-at", type=int, default=None)
    add.add_argument("--ended-at", type=int, default=None)
    add.add_argument("--official-result", default=None, help='JSON，如 {"score":"2:1"}')
    add.set_defaults(func=_cmd_match_add)

    slice_cmd = sub.add_parser("slice", help="人工指定小局切片边界")
    slice_cmd.add_argument("--match-id", type=int, required=True)
    slice_cmd.add_argument("--game-no", type=int, required=True)
    slice_cmd.add_argument("--start-ms", type=int, required=True)
    slice_cmd.add_argument("--end-ms", type=int, required=True)
    slice_cmd.add_argument("--note", default=None, help="冲突事实备注")
    slice_cmd.add_argument("--override-by", default=None, help="人工修正操作者")
    slice_cmd.add_argument("--override-reason", default=None, help="人工修正理由")
    slice_cmd.set_defaults(func=_cmd_slice)

    stats = sub.add_parser("stats", help="计算并写入规则统计")
    stats.add_argument("--match-id", type=int, required=True)
    stats.set_defaults(func=_cmd_stats)

    render = sub.add_parser("render", help="生成十一段静态页")
    render.add_argument("--match-id", type=int, required=True)
    render.set_defaults(func=_cmd_render)

    rebuild = sub.add_parser("rebuild", help="AC-13 自检：删统计后重算并比对")
    rebuild.add_argument("--match-id", type=int, required=True)
    rebuild.set_defaults(func=_cmd_rebuild)

    verify = sub.add_parser("verify-sources", help="复核页面全部来源的 SHA256")
    verify.add_argument("--match-id", type=int, required=True)
    verify.set_defaults(func=_cmd_verify_sources)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        return int(args.func(args))
    except (ValueError, LookupError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
