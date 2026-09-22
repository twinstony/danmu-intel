"""命令行入口：一条条命令跑通整条链路。

    danmu-intel collect --url https://www.huya.com/660000 --seconds 300 --match-id 1
    danmu-intel supervise --match-id 1 --room … --room … --room …   # 多房间并发 + 监督
    danmu-intel health --match-id 1            # 每房间健康状态（心跳/重连/重启/严重级别）
    danmu-intel contribution --match-id 1      # 每房间贡献量（条数/跨度/去重后条数）
    danmu-intel events --match-id 1            # 采集异常事件（待 T11 投递）
    danmu-intel match add --league LPL --team-a iG --team-b LNG --state ended
    danmu-intel slice --match-id 1 --game-no 1 --start-ms … --end-ms …
    danmu-intel stats --match-id 1
    danmu-intel report --match-id 1 --kind live_brief --completed-game 1  # 赛中快报（节点结束）
    danmu-intel report --match-id 1 --kind full     # 完整版 → site/matches/1/full.html
    danmu-intel report --match-id 1 --kind review   # 复盘版 → site/matches/1/review.html
    danmu-intel reports --match-id 1                # 已发布的报告版本（FR-C4-9）
    danmu-intel rebuild --match-id 1                # AC-13：删统计重算，断言结果不变
    danmu-intel verify-sources --match-id 1 --kind full  # 逐项复核 文件+行范围+SHA256
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime

from danmu_intel.common import paths
from danmu_intel.common.db import open_db
from danmu_intel.common.matches import create_match, get_match
from danmu_intel.pipeline import (
    collect_facts,
    generate_and_publish,
    rebuild_metrics,
    verify_sources,
    write_metrics,
)
from danmu_intel.report.forms import form_of
from danmu_intel.report.publish import PublishRefused, list_reports
from danmu_intel.slice.manual import add_manual_slice

logger = logging.getLogger("danmu_intel")


def _stamp(ms: int | None) -> str:
    return "-" if ms is None else datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


def _ago(ms: int | None, now: int) -> str:
    return "-" if ms is None else f"{(now - ms) / 1000:.0f} 秒前"


def _cmd_collect(args: argparse.Namespace) -> int:
    from danmu_intel.collect import get_adapter
    from danmu_intel.collect.runner import run_session

    adapter = get_adapter(args.platform)
    room = adapter.parse_room(args.url)
    conn = open_db()
    try:
        get_match(conn, args.match_id)  # 先确认比赛存在：采集必须挂在某场比赛上
        result = asyncio.run(
            run_session(
                room,
                adapter=adapter,
                match_id=args.match_id,
                seconds=args.seconds,
                conn=conn,
            )
        )
    finally:
        conn.close()
    probe = result.probe
    if probe:
        print(f"房间：{room.platform}/{room.room_id}｜{probe.streamer or '未知主播'}｜开播={probe.is_live}")
    print(f"采集会话 #{result.session_id}：共 {result.msg_count} 条弹幕（状态 {result.state}，累计重连 {result.reconnects} 次）")
    for kind in result.incidents:
        print(f"  异常：{kind}")
    for segment in result.segments:
        print(f"  落盘：{paths.data_dir() / segment.rel_path}｜{segment.msg_count} 条｜SHA256 {segment.sha256}")
    return 0


def _cmd_supervise(args: argparse.Namespace) -> int:
    """多房间并发采集：一房间一子进程，退出/僵死自动拉起，超限停下来报事。"""
    from danmu_intel.collect import get_adapter
    from danmu_intel.collect.supervisor import Supervisor

    adapter = get_adapter(args.platform)
    rooms = [adapter.parse_room(url) for url in args.room]
    seen: set[tuple[str, str]] = set()
    for room in rooms:
        key = (room.platform, room.room_id)
        if key in seen:
            raise ValueError(f"重复的直播间：{room.platform}/{room.room_id}（--room 不能重复）")
        seen.add(key)

    conn = open_db()
    try:
        get_match(conn, args.match_id)
        supervisor = Supervisor(conn, rooms, match_id=args.match_id, data_root=paths.data_dir())
        print(f"开始监督 {len(rooms)} 个直播间（比赛 #{args.match_id}）：" + "、".join(f"{r.platform}/{r.room_id}" for r in rooms))
        try:
            supervisor.run(seconds=args.seconds)
        except KeyboardInterrupt:
            print("收到中断，已停止子进程", file=sys.stderr)
        for run in supervisor.runs:
            line = (
                f"房间 {run.room.platform}/{run.room.room_id}：{run.state}｜"
                f"重启 {run.restarts} 次｜重连 {run.reconnects} 次"
            )
            print(line + (f"｜停止原因：{run.reason}" if run.reason else ""))
        if supervisor.stopped_rooms():
            return 1
    finally:
        conn.close()
    return 0


def _cmd_health(args: argparse.Namespace) -> int:
    from danmu_intel.collect.health import room_health

    now = int(datetime.now().timestamp() * 1000)
    conn = open_db()
    try:
        rows = room_health(conn, args.match_id, data_root=paths.data_dir(), now=now)
    finally:
        conn.close()
    if not rows:
        print(f"比赛 #{args.match_id} 还没有采集会话")
        return 0
    for item in rows:
        print(
            f"{item.platform}/{item.room_id}（{item.streamer or '未知主播'}，开播={item.is_live}）"
            f"｜状态 {item.state}｜严重级别 {item.severity}"
        )
        print(
            f"  pid {item.pid}｜会话 #{item.session_id}｜重启 {item.restart_count} 次｜"
            f"重连 {item.reconnects} 次｜已收 {item.msg_count} 条"
        )
        print(
            f"  起于 {_stamp(item.started_at)}｜止于 {_stamp(item.ended_at)}｜"
            f"最后一条消息 {_stamp(item.last_msg_at)}（{_ago(item.last_msg_at, now)}）"
        )
        if item.last_incident is not None:
            print(
                f"  最近异常：{item.last_incident.kind}（{item.last_incident.severity}）"
                f"@ {_stamp(item.last_incident.created_at)}"
            )
    return 0


def _cmd_contribution(args: argparse.Namespace) -> int:
    from danmu_intel.collect.health import room_contribution

    conn = open_db()
    try:
        rows = room_contribution(conn, args.match_id, data_root=paths.data_dir())
    finally:
        conn.close()
    if not rows:
        print(f"比赛 #{args.match_id} 还没有落盘记录")
        return 0
    for item in rows:
        span = "-" if item.first_ts is None or item.last_ts is None else f"{(item.last_ts - item.first_ts) / 1000:.0f} 秒"
        print(
            f"{item.platform}/{item.room_id}：{item.msg_count} 条｜去重后 {item.deduped_count} 条"
            f"（重复 {item.duplicate_count} 条）｜时间跨度 {span}"
            f"（{_stamp(item.first_ts)} → {_stamp(item.last_ts)}）｜{item.session_count} 个采集会话"
        )
    print(
        f"合计：{sum(item.msg_count for item in rows)} 条｜去重后 {sum(item.deduped_count for item in rows)} 条"
        f"（{len(rows)} 个直播间）"
    )
    return 0


def _cmd_events(args: argparse.Namespace) -> int:
    from danmu_intel.collect.incidents import recent

    conn = open_db()
    try:
        incidents = recent(conn, match_id=args.match_id, limit=args.limit)
    finally:
        conn.close()
    if not incidents:
        print("没有采集异常事件")
        return 0
    for item in incidents:
        room = f"{item.payload.get('platform')}/{item.payload.get('room_id')}"
        detail = {key: value for key, value in item.payload.items() if key not in {"platform", "room_id", "match_id"}}
        print(
            f"#{item.id} {_stamp(item.created_at)}｜{item.kind}（{item.severity}，{item.state}）｜{room}｜{json.dumps(detail, ensure_ascii=False)}"
        )
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


def _cmd_report(args: argparse.Namespace) -> int:
    completed = tuple(sorted(set(args.completed_game))) if args.completed_game else None
    conn = open_db()
    try:
        try:
            result = generate_and_publish(
                conn,
                args.match_id,
                kind=args.kind,
                completed_games=completed,
                trigger_game_no=args.trigger_game,
                data_root=paths.data_dir(),
            )
        except PublishRefused as exc:
            for item in exc.failures:
                print(f"  发布检查未通过：{item.label}｜{item.detail}", file=sys.stderr)
            print(f"错误：{exc}", file=sys.stderr)
            return 1
    finally:
        conn.close()
    form = form_of(result.kind)
    print(f"已发布{form.label} v{result.version}：{result.path}")
    print(
        f"  段落 {len(result.content.segments)} 段｜解读层 {result.content.llm_state}｜"
        f"事实层哈希 {result.content.fact_layer_hash}"
    )
    for item in result.checks:
        print(f"  检查｜{item.label}：{'通过' if item.passed else '未通过'}｜{item.detail}")
    return 0


def _cmd_reports(args: argparse.Namespace) -> int:
    conn = open_db()
    try:
        rows = list_reports(conn, args.match_id)
    finally:
        conn.close()
    if not rows:
        print(f"比赛 #{args.match_id} 还没有发布过报告")
        return 0
    for row in rows:
        node = f"G{row.game_no}" if row.game_no is not None else "-"
        print(
            f"{row.kind} v{row.version}｜{row.state}｜节点 {node}｜"
            f"解读层 {row.llm_state}｜事实层 {row.fact_layer_hash[:12]}…｜{row.path or '-'}"
        )
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
    try:
        failed = verify_sources(args.match_id, kind=args.kind, data_root=paths.data_dir())
    except LookupError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    if not failed:
        print("全部来源校验通过（文件 + 行范围 + SHA256）")
        return 0
    for ref in failed:
        print(f"来源校验失败：{ref.rel_path} 第 {ref.line_start}–{ref.line_end} 行", file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="danmu-intel", description="弹幕情报库（采集→监督→切片→统计→报告三形态）")
    parser.add_argument("--verbose", action="store_true", help="打印重连等运行日志")
    sub = parser.add_subparsers(dest="command", required=True)

    collect = sub.add_parser("collect", help="采集一个直播间的弹幕")
    collect.add_argument("--url", required=True, help="直播间链接或房间号")
    collect.add_argument("--platform", default="huya", help="平台标识（默认 huya）")
    collect.add_argument("--seconds", type=float, default=None, help="采集时长（秒），缺省则持续采集")
    collect.add_argument("--match-id", type=int, required=True, help="关联的比赛 id（先 match add）")
    collect.set_defaults(func=_cmd_collect)

    supervise = sub.add_parser("supervise", help="多房间并发采集与监督（一房间一子进程）")
    supervise.add_argument("--room", action="append", required=True, metavar="URL", help="直播间链接，可重复")
    supervise.add_argument("--platform", default="huya", help="平台标识（默认 huya）")
    supervise.add_argument("--match-id", type=int, required=True)
    supervise.add_argument("--seconds", type=float, default=None, help="监督时长（秒），缺省则跑到所有房间停下")
    supervise.set_defaults(func=_cmd_supervise)

    health = sub.add_parser("health", help="采集健康状态（每房间：心跳/状态/重连/重启/严重级别）")
    health.add_argument("--match-id", type=int, required=True)
    health.set_defaults(func=_cmd_health)

    contribution = sub.add_parser("contribution", help="每房间贡献量（条数/时间跨度/去重后条数）")
    contribution.add_argument("--match-id", type=int, required=True)
    contribution.set_defaults(func=_cmd_contribution)

    events = sub.add_parser("events", help="采集异常事件（待投递）")
    events.add_argument("--match-id", type=int, default=None)
    events.add_argument("--limit", type=int, default=20)
    events.set_defaults(func=_cmd_events)

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

    report = sub.add_parser("report", help="生成并发布一份报告（三形态）")
    report.add_argument("--match-id", type=int, required=True)
    report.add_argument("--kind", required=True, help="live_brief（赛中快报）| full（完整版）| review（复盘版）")
    report.add_argument(
        "--completed-game",
        type=int,
        action="append",
        metavar="N",
        help="已完成节点（小局）的局号，可重复；缺省即全部已登记的小局（赛中快报只发布已完成节点）",
    )
    report.add_argument("--trigger-game", type=int, default=None, help="触发本次发布的节点（小局）局号")
    report.set_defaults(func=_cmd_report)

    reports = sub.add_parser("reports", help="已发布的报告版本（按形态与版本）")
    reports.add_argument("--match-id", type=int, required=True)
    reports.set_defaults(func=_cmd_reports)

    rebuild = sub.add_parser("rebuild", help="AC-13 自检：删统计后重算并比对")
    rebuild.add_argument("--match-id", type=int, required=True)
    rebuild.set_defaults(func=_cmd_rebuild)

    verify = sub.add_parser("verify-sources", help="复核已发布页面全部来源的 SHA256")
    verify.add_argument("--match-id", type=int, required=True)
    verify.add_argument("--kind", required=True, help="报告形态：live_brief | full | review")
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
