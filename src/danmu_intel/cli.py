"""命令行入口：一条条命令跑通整条链路。

    danmu-intel collect --url https://www.huya.com/660000 --seconds 300 --match-id 1
    danmu-intel supervise --match-id 1 --room … --room … --room …   # 多房间并发 + 监督
    danmu-intel health --match-id 1            # 每房间健康状态（心跳/重连/重启/严重级别）
    danmu-intel contribution --match-id 1      # 每房间贡献量（条数/跨度/去重后条数）
    danmu-intel events --match-id 1            # 采集异常事件（待 T11 投递）
    danmu-intel match add --league LPL --team-a iG --team-b LNG --state ended
    danmu-intel boundaries --match-id 1        # 切片引擎：优先级裁决 + 冲突记录
    danmu-intel slice --match-id 1 --game-no 1 --start-ms … --end-ms …   # 人工修正
    danmu-intel stats --match-id 1
    danmu-intel final --match-id 1             # 终局判定（≥3 类独立信号 + 2 分钟无反转）
    danmu-intel gray --match-id 1              # 灰信号（只作风险提示，不点名）
    danmu-intel config --set gray_min_users=3  # 统计门槛（改动留审计）
    danmu-intel report --match-id 1 --kind live_brief --completed-game 1  # 赛中快报（节点结束）
    danmu-intel report --match-id 1 --kind full     # 完整版 → site/matches/1/full.html
    danmu-intel report --match-id 1 --kind review   # 复盘版 → site/matches/1/review.html
    danmu-intel reports --match-id 1                # 已发布的报告版本（FR-C4-9）
    danmu-intel publish                             # 站点产物 → 7 项检查 → 原子发布 → 提交/部署
    danmu-intel publish --dry-run                   # 只跑检查，不发布（看哪一项会拦下）
    danmu-intel publish --no-deploy                 # 只落本地产物，不推 git、不调 Vercel
    danmu-intel releases                            # 发布批次账本（版本/指纹/部署/付费比赛）
    danmu-intel rollback                            # 秒级回滚到上一批（Vercel 即时回滚 + git revert 跟进）
    danmu-intel match set-state --match-id 1 --state ended   # 状态机写入 → 自动再发布公开版（FR-C5-10）
    danmu-intel rebuild --match-id 1                # AC-13：删统计重算，断言结果不变
    danmu-intel verify-sources --match-id 1 --kind full  # 逐项复核 文件+行范围+SHA256

`report` 的解读层：配了凭据（仓库外 `.env`，0600，键 `DEEPSEEK_API_KEY`）就走受约束的
LLM 调用 + 反幻觉校验 + 成本硬闸；没配/超时/报错/校验不过就回落规则直出，并在命令输出、
报告第 10 段与页面横幅上标注降级与原因（降级不静默）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from datetime import datetime

from danmu_intel.common import paths
from danmu_intel.common.audit import record as audit_record
from danmu_intel.common.config import load_stats_config, save_stats_config
from danmu_intel.common.db import open_db
from danmu_intel.common.matches import create_match, get_match, set_match_state
from danmu_intel.pipeline import (
    collect_facts,
    generate_and_publish,
    load_lines,
    rebuild_metrics,
    verify_sources,
    write_metrics,
)
from danmu_intel.report.forms import form_of
from danmu_intel.publish.release import (
    ReleaseContext,
    ReleaseRefused,
    current_release,
    list_releases,
    publish_site,
    rollback,
    seals,
    sync_ended,
)
from danmu_intel.publish.site import build_site
from danmu_intel.publish.vercel import VercelError
from danmu_intel.common.credentials import CredentialError
from danmu_intel.report.publish import PublishRefused, list_reports
from danmu_intel.slice import engine, signals
from danmu_intel.slice.manual import add_manual_slice
from danmu_intel.stats.gray import reportable

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
    """待投递事件（采集异常 + 解读层降级/成本闸报警），投递属 T11。"""
    from danmu_intel.common.notifications import recent

    conn = open_db()
    try:
        incidents = recent(conn, match_id=args.match_id, limit=args.limit)
    finally:
        conn.close()
    if not incidents:
        print("没有待投递事件")
        return 0
    for item in incidents:
        room = item.payload.get("platform")
        source = f"{room}/{item.payload.get('room_id')}" if room else "解读层"
        detail = {
            key: value
            for key, value in item.payload.items()
            if key not in {"platform", "room_id", "match_id"}
        }
        print(
            f"#{item.id} {_stamp(item.created_at)}｜{item.kind}（{item.severity}，{item.state}）｜{source}｜{json.dumps(detail, ensure_ascii=False)}"
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
        version = engine.algo_version(conn, args.match_id)
    finally:
        conn.close()
    print(f"已写入切片 #{slice_id}：G{args.game_no} {args.start_ms}–{args.end_ms}（边界来源：manual）")
    if args.override_by:
        print(f"  人工修正已留痕：{args.override_by}｜{args.override_reason}｜算法版本 {version}")
    return 0


def _parse_report_windows(values: list[str] | None) -> list[tuple[int, int, int]]:
    windows: list[tuple[int, int, int]] = []
    for value in values or []:
        parts = value.split(":")
        if len(parts) != 3:
            raise ValueError(f"--report-window 格式应为 game_no:start_ms:end_ms，收到：{value}")
        windows.append((int(parts[0]), int(parts[1]), int(parts[2])))
    return windows


def _cmd_boundaries(args: argparse.Namespace) -> int:
    """切片引擎：官方 > 弹幕信号 > 报告窗口，冲突必记录，人工修正优先。"""
    conn = open_db()
    try:
        match = get_match(conn, args.match_id)
        config = load_stats_config(conn)
        lines = load_lines(conn, args.match_id, data_root=paths.data_dir())
        claims = signals.review(signals.moments(lines, config=config), config=config)
        resolutions = engine.resolve_match(
            conn,
            args.match_id,
            official_result=match.official_result,
            lines=lines,
            report_windows=_parse_report_windows(args.report_window),
            config=config,
            apply=not args.dry_run,
            actor=args.actor,
        )
    finally:
        conn.close()
    for claim in claims:
        verdict = "通过复核" if claim.verified else "复核不通过"
        print(
            f"弹幕信号候选：{claim.direction} @ {_stamp(claim.at_ms)}｜{verdict}｜"
            f"信号类别 {'、'.join(claim.kinds) or '无'}｜{claim.note or '≥2 类独立信号'}"
        )
    if not resolutions:
        print("没有可用的小局边界候选（官方数据、弹幕信号、报告窗口三者皆无）")
        return 0
    for resolved in resolutions:
        action = "已写入" if not args.dry_run else "试算"
        print(
            f"{action} G{resolved.game_no}：{_stamp(resolved.start_ms)} – {_stamp(resolved.end_ms)}｜"
            f"边界来源 {resolved.boundary_source}"
        )
        if resolved.conflict_note:
            print(f"  冲突：{resolved.conflict_note}")
    return 0


def _cmd_final(args: argparse.Namespace) -> int:
    """终局判定：≥3 类相互独立信号同时成立，且 2 分钟内无反转。"""
    conn = open_db()
    try:
        facts = collect_facts(conn, args.match_id, data_root=paths.data_dir())
    finally:
        conn.close()
    judgement = facts.final_judgement
    print(f"终局判定：{judgement.verdict}｜{judgement.reason}")
    if judgement.satisfied_at_ms:
        print(f"  首次满足：{_stamp(judgement.satisfied_at_ms)}（{'、'.join(judgement.kinds)}）")
    if judgement.decided_at_ms:
        print(f"  判定时刻：{_stamp(judgement.decided_at_ms)}")
    if judgement.reversal is not None:
        print(f"  撤销：{judgement.reversal.detail}（{_stamp(judgement.reversal.at_ms)}）")
    for fact in facts.signal_facts:
        end = _stamp(fact.end_ms) if fact.end_ms else "观测结束仍成立"
        print(f"  信号 {fact.kind}：{_stamp(fact.start_ms)} → {end}｜{json.dumps(fact.evidence, ensure_ascii=False)}")
    if not facts.signal_facts:
        print("  本场没有任何一类独立信号成立")
    return 0


def _cmd_gray(args: argparse.Namespace) -> int:
    """灰信号：只作风险提示，不指控、不点名（输出里没有任何身份标识）。"""
    conn = open_db()
    try:
        facts = collect_facts(conn, args.match_id, data_root=paths.data_dir())
    finally:
        conn.close()
    config = facts.stats_config
    print(
        f"灰信号门槛（config）：命中 ≥{config.gray_min_hits} 次、独立发言者 ≥{config.gray_min_users} 人、"
        f"覆盖时段 ≥{config.gray_min_windows} 个（时段宽 {config.gray_window_ms // 1000} 秒）"
    )
    reportable_signals = reportable(facts.gray_signals)
    if not reportable_signals:
        print("没有达到门槛的灰信号")
    for signal in reportable_signals:
        print(
            f"【{signal.category_label}】{signal.keyword}：命中 {signal.hit_count} 条｜"
            f"独立发言者 {signal.distinct_users} 人｜覆盖 {signal.window_count} 个时段｜{signal.status}"
        )
        for sample in signal.samples:
            print(f"  样本：{_stamp(sample.ts)}｜{sample.text}（{sample.rel_path} 第 {sample.line_no} 行）")
    for signal in facts.gray_signals:
        if signal.status in reportable_signals:
            continue
        print(f"已作废：{signal.keyword}——{signal.reason}")
    print("纪律：只作风险提示，不出现指控性结论、不指名任何个人或队伍；不提供对外导出。")
    return 0


def _cmd_config(args: argparse.Namespace) -> int:
    """统计门槛（灰信号 N/M/K、终局信号门槛…）：改动留审计。"""
    conn = open_db()
    try:
        if args.set:
            changes = _parse_config_changes(args.set)
            config = save_stats_config(conn, actor=args.actor, changes=changes)
            print(f"已更新统计门槛（操作者 {args.actor}）：{json.dumps(changes, ensure_ascii=False)}")
        else:
            config = load_stats_config(conn)
    finally:
        conn.close()
    for key, value in sorted(config.as_dict().items()):
        print(f"{key} = {json.dumps(value, ensure_ascii=False)}")
    return 0


def _parse_config_changes(values: list[str]) -> dict[str, object]:
    changes: dict[str, object] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"--set 格式应为 key=value，收到：{value}")
        key, raw = value.split("=", 1)
        changes[key.strip()] = json.loads(raw)
    return changes


def _cmd_stats(args: argparse.Namespace) -> int:
    conn = open_db()
    try:
        facts = collect_facts(conn, args.match_id, data_root=paths.data_dir())
        count = write_metrics(conn, facts)
        for game in facts.games:
            metrics = game.metrics
            top = metrics["peak"]
            peak_note = f"峰值 {_stamp(int(top['t_start']))}（{top['count']} 条）" if top else "无显著峰值"
            print(
                f"G{game.window.game_no}：{metrics['danmu_total']['count']} 条｜"
                f"独立发言者 {metrics['distinct_users']['count']} 人｜{peak_note}｜"
                f"击杀轴 {len(metrics['kill_timeline']['events'])} 项｜边界来源 {game.window.boundary_source}"
            )
        judgement = facts.final_judgement
        print(f"终局判定：{judgement.verdict}（{judgement.reason}）")
        print(f"灰信号：{len(facts.reportable_gray_signals)} 项达门槛（作废 {len(facts.gray_signals) - len(facts.reportable_gray_signals)} 项）")
        print(f"已写入 {count} 行规则统计（算法版本 {facts.algo_version}）")
    finally:
        conn.close()
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    from danmu_intel.report.llm.interpreter import interpreter_for

    completed = tuple(sorted(set(args.completed_game))) if args.completed_game else None
    conn = open_db()
    try:
        try:
            interpreter = interpreter_for(conn)
            result = generate_and_publish(
                conn,
                args.match_id,
                kind=args.kind,
                completed_games=completed,
                trigger_game_no=args.trigger_game,
                interpreter=interpreter,
                data_root=paths.data_dir(),
            )
        except PublishRefused as exc:
            for item in exc.failures:
                print(f"  发布检查未通过：{item.label}｜{item.detail}", file=sys.stderr)
            print(f"错误：{exc}", file=sys.stderr)
            return 1
        # 状态行要读账本（`llm_calls`），所以必须在 `conn` 关掉**之前**打印：
        # 连接关掉之后再查会抛 sqlite3.ProgrammingError，命令在功能启用的当天必崩。
        form = form_of(result.kind)
        print(f"已发布{form.label} v{result.version}：{result.path}")
        visibility = "公开（比赛已结束）" if result.visibility == "public" else "会员（比赛进行中）"
        print(
            f"  段落 {len(result.content.segments)} 段｜解读层 {result.content.llm_state}｜"
            f"可见性 {visibility}"
        )
        print(f"  事实层哈希 {result.content.fact_layer_hash}")
        _print_interpretation_status(interpreter, conn, args.match_id, result.content)
        for item in result.checks:
            print(f"  检查｜{item.label}：{'通过' if item.passed else '未通过'}｜{item.detail}")
        return 0
    finally:
        conn.close()


def _print_interpretation_status(interpreter, conn, match_id: int, content) -> None:
    """解读层状态一行：降级原因 + 本次成本（NFR-C-3 的可见性；完整后台页属 T12）。"""
    from danmu_intel.report.llm import ledger
    from danmu_intel.report.llm.interpreter import LLMInterpreter

    note = str(getattr(interpreter, "note", ""))
    if isinstance(interpreter, LLMInterpreter):
        totals = ledger.spend(conn, match_id, now_ms=int(time.time() * 1000))
        print(
            f"  解读层：LLM 调用 {interpreter.calls} 次，本次 ¥{interpreter.spent_cny:.4f}｜"
            f"单场累计 ¥{totals.match_cny:.4f}（硬闸 ¥0.3）｜当日累计 ¥{totals.day_cny:.4f}（硬闸 ¥10）"
        )
    if note:
        print(f"  降级原因：{note}")


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


def _release_context(args: argparse.Namespace) -> ReleaseContext:
    """发布上下文：`--no-deploy` 走本地，否则真实提交 + 真实 Vercel（凭据只在仓库外 .env）。"""
    if getattr(args, "no_deploy", False):
        return ReleaseContext.local(actor=args.actor)
    from danmu_intel.publish.release import GitPublisher
    from danmu_intel.publish.vercel import client_from_credentials

    return ReleaseContext(
        site_root=paths.site_dir(),
        data_root=paths.data_dir(),
        publisher=GitPublisher(paths.repo_root()),
        vercel=client_from_credentials(),
        actor=args.actor,
    )


def _cmd_publish(args: argparse.Namespace) -> int:
    """站点产物 → 7 项检查 → 原子发布 → 提交/部署（检查不过就什么都不换）。"""
    from danmu_intel.publish.checks import run_checks

    conn = open_db()
    try:
        if args.dry_run:
            build = build_site(conn, data_root=paths.data_dir())
            checks = run_checks(build, data_root=paths.data_dir(), seals=seals(conn))
            for item in checks:
                flag = "通过" if item.passed else "未通过"
                print(f"  检查｜{item.label}：{flag}｜{item.detail}")
            failed = [item for item in checks if item.blocking and not item.passed]
            if failed:
                print(f"试运行：{len(failed)} 项检查未通过，不会发布", file=sys.stderr)
                return 1
            print(f"试运行：{len(build.tree.paths)} 个页面全部检查通过（未发布）")
            return 0
        try:
            ctx = _release_context(args)
        except (VercelError, CredentialError) as exc:
            print(f"错误：{exc}", file=sys.stderr)
            print("提示：只想出本地产物就用 danmu-intel publish --no-deploy", file=sys.stderr)
            return 2
        outcome = publish_site(conn, ctx=ctx, reason=args.reason)
    except ReleaseRefused as exc:
        for item in exc.failures:
            print(f"  发布检查未通过：{item.label}｜{item.detail}", file=sys.stderr)
        print(f"错误：{exc}", file=sys.stderr)
        print("站点保持上一版可用（一个条目都没有替换）", file=sys.stderr)
        return 1
    finally:
        conn.close()
    if not outcome.changed:
        print(f"没有变化：线上仍是 v{outcome.version}（{len(outcome.release.pages)} 个页面，指纹未变）")
        return 0
    print(
        f"已发布 v{outcome.version}：{len(outcome.release.pages)} 个页面｜"
        f"提交 {outcome.release.deploy_ref or '-'}｜部署 {outcome.release.deployment_id or '-'}"
    )
    paid = "、".join(f"#{item}" for item in outcome.paywalled_matches) or "无"
    print(f"  付费内容（不进静态产物）的比赛：{paid}")
    for item in outcome.checks:
        print(f"  检查｜{item.label}：{'通过' if item.passed else '未通过'}｜{item.detail}")
    return 0


def _cmd_releases(args: argparse.Namespace) -> int:
    conn = open_db()
    try:
        rows = list_releases(conn)
        live = current_release(conn)
    finally:
        conn.close()
    if not rows:
        print("还没有发布过任何批次")
        return 0
    for row in rows:
        marker = "← 线上" if live is not None and row.id == live.id else ""
        paid = "、".join(f"#{item}" for item in row.paywalled_matches) or "无"
        print(
            f"v{row.version}｜{row.state}｜{len(row.pages)} 个页面｜指纹 {row.tree_digest[:12]}…｜"
            f"提交 {row.deploy_ref or '-'}｜部署 {row.deployment_id or '-'}｜付费比赛 {paid} {marker}"
        )
    return 0


def _cmd_rollback(args: argparse.Namespace) -> int:
    """秒级回滚：① Vercel 即时回滚 ② git revert 跟进（失败只报警）。"""
    conn = open_db()
    try:
        ctx = _release_context(args)
        try:
            result = rollback(conn, ctx=ctx, to_version=args.to)
        except VercelError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 2
    finally:
        conn.close()
    print(
        f"已回滚：v{result.previous.version} → v{result.release.version}｜"
        f"线上部署 {result.deployment_id}"
    )
    print(
        "  账本对齐："
        + ("git revert 已跟进（仓库 = 线上）" if result.aligned else "未对齐（已报警，请手工 git revert）")
    )
    return 0


def _cmd_match_set_state(args: argparse.Namespace) -> int:
    """写比赛状态机。比赛转 `ended` 时**自动**再发布公开版（FR-C5-10，幂等）。"""
    conn = open_db()
    try:
        before = get_match(conn, args.match_id)
        official_result = json.loads(args.official_result) if args.official_result else None
        ended_at = args.ended_at
        if ended_at is None and args.state == "ended":
            ended_at = int(time.time() * 1000)
        match = set_match_state(
            conn,
            args.match_id,
            state=args.state,
            ended_at=ended_at,
            official_result=official_result,
        )
        audit_record(
            conn,
            actor=args.actor,
            action="match.state",
            target=str(match.id),
            detail={"from": before.state, "to": match.state},
        )
        print(f"比赛 #{match.id} 状态：{before.state} → {match.state}")
        if match.state != "ended" or before.state == "ended":
            outcomes = ()
        else:
            try:
                outcomes = sync_ended(conn, ctx=_release_context(args))
            except (VercelError, CredentialError, ReleaseRefused) as exc:
                print(f"错误：{exc}", file=sys.stderr)
                print(
                    f"比赛 #{match.id} 状态已写入 {match.state}，但自动再发布没做成："
                    "线上仍是旧版（状态机的写入是事实，不因此回退），"
                    "请修掉发布检查的问题后重跑 --no-deploy 或 --deploy",
                    file=sys.stderr,
                )
                return 1
    finally:
        conn.close()
    if not outcomes:
        print("  没有需要再发布的页面（比赛状态与线上可见性一致）")
        return 0
    for outcome in outcomes:
        print(
            f"  已自动再发布公开版 v{outcome.version}：{len(outcome.release.pages)} 个页面｜"
            f"部署 {outcome.release.deployment_id or '-'}"
        )
    return 0


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

    events = sub.add_parser("events", help="待投递事件（采集异常 / 解读层降级与成本闸报警）")
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

    set_state = match_sub.add_parser("set-state", help="写比赛状态机（转 ended 会自动再发布公开版）")
    set_state.add_argument("--match-id", type=int, required=True)
    set_state.add_argument("--state", required=True, help="scheduled|live|between_games|ended|aborted")
    set_state.add_argument("--ended-at", type=int, default=None, help="结束时刻（毫秒；转 ended 时缺省取当前时间）")
    set_state.add_argument("--official-result", default=None, help='JSON，如 {"score":"2:1"}')
    set_state.add_argument("--no-deploy", action="store_true", help="自动再发布时只落本地产物")
    set_state.add_argument("--actor", default="状态机", help="操作者（进审计）")
    set_state.set_defaults(func=_cmd_match_set_state)

    boundaries = sub.add_parser("boundaries", help="切片引擎：按优先级裁决小局边界并记录冲突")
    boundaries.add_argument("--match-id", type=int, required=True)
    boundaries.add_argument(
        "--report-window", action="append", default=None,
        metavar="GAME:START:END", help="已发布报告窗口（优先级 3），可重复",
    )
    boundaries.add_argument("--dry-run", action="store_true", help="只试算不落库")
    boundaries.add_argument("--actor", default="boundary-engine", help="落库审计的操作者")
    boundaries.set_defaults(func=_cmd_boundaries)

    slice_cmd = sub.add_parser("slice", help="人工指定/修正小局切片边界")
    slice_cmd.add_argument("--match-id", type=int, required=True)
    slice_cmd.add_argument("--game-no", type=int, required=True)
    slice_cmd.add_argument("--start-ms", type=int, required=True)
    slice_cmd.add_argument("--end-ms", type=int, required=True)
    slice_cmd.add_argument("--note", default=None, help="冲突事实备注")
    slice_cmd.add_argument("--override-by", default=None, help="人工修正操作者（覆盖已有边界时必填）")
    slice_cmd.add_argument("--override-reason", default=None, help="人工修正理由（覆盖已有边界时必填）")
    slice_cmd.set_defaults(func=_cmd_slice)

    stats = sub.add_parser("stats", help="计算并写入规则统计（统计全集）")
    stats.add_argument("--match-id", type=int, required=True)
    stats.set_defaults(func=_cmd_stats)

    final_cmd = sub.add_parser("final", help="终局判定：≥3 类独立信号 + 2 分钟无反转")
    final_cmd.add_argument("--match-id", type=int, required=True)
    final_cmd.set_defaults(func=_cmd_final)

    gray_cmd = sub.add_parser("gray", help="灰信号（只作风险提示，不指控、不点名）")
    gray_cmd.add_argument("--match-id", type=int, required=True)
    gray_cmd.set_defaults(func=_cmd_gray)

    config_cmd = sub.add_parser("config", help="查看/修改统计门槛（改动留审计）")
    config_cmd.add_argument("--set", action="append", default=None, metavar="KEY=VALUE", help="改门槛，可重复")
    config_cmd.add_argument("--actor", default="管理员", help="操作者（进审计）")
    config_cmd.set_defaults(func=_cmd_config)

    report = sub.add_parser("report", help="生成并发布一份报告（三形态）")
    report.add_argument("--match-id", type=int, required=True)
    report.add_argument(
        "--kind", required=True, help="live_brief（赛中快报）| full（完整版）| review（复盘版）"
    )
    report.add_argument(
        "--completed-game",
        type=int,
        action="append",
        metavar="N",
        help="已完成节点（小局）的局号，可重复；赛中快报必须给，赛后形态缺省即全部已登记的小局",
    )
    report.add_argument("--trigger-game", type=int, default=None, help="触发本次发布的节点（小局）局号")
    report.set_defaults(func=_cmd_report)

    reports = sub.add_parser("reports", help="已发布的报告版本（按形态与版本）")
    reports.add_argument("--match-id", type=int, required=True)
    reports.set_defaults(func=_cmd_reports)

    rebuild = sub.add_parser("rebuild", help="AC-13 自检：删统计后重算并比对")
    rebuild.add_argument("--match-id", type=int, required=True)
    rebuild.set_defaults(func=_cmd_rebuild)

    publish = sub.add_parser("publish", help="站点产物 → 7 项检查 → 原子发布（失败保留上一版）")
    publish.add_argument("--dry-run", action="store_true", help="只跑检查，不发布")
    publish.add_argument("--no-deploy", action="store_true", help="只落本地产物：不推 git、不调 Vercel")
    publish.add_argument("--reason", default="manual", help="本次发布原因（进审计）")
    publish.add_argument("--actor", default="发布器", help="操作者（进审计）")
    publish.set_defaults(func=_cmd_publish)

    releases = sub.add_parser("releases", help="发布批次账本（版本/指纹/部署/付费比赛）")
    releases.set_defaults(func=_cmd_releases)

    rollback_cmd = sub.add_parser("rollback", help="秒级回滚到上一批（Vercel 即时回滚 + git revert）")
    rollback_cmd.add_argument("--to", type=int, default=None, help="回滚到指定版本号（缺省为上一批）")
    rollback_cmd.add_argument("--no-deploy", action="store_true", help="本地模式（没有部署可回滚，会报错）")
    rollback_cmd.add_argument("--actor", default="发布器", help="操作者（进审计）")
    rollback_cmd.set_defaults(func=_cmd_rollback)

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
