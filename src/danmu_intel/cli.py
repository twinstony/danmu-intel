"""命令行入口：一条条命令跑通整条链路。

    danmu-intel collect --url https://www.huya.com/660000 --seconds 300 --match-id 1
    danmu-intel supervise --match-id 1 --room … --room … --room …   # 多房间并发 + 监督
    danmu-intel supervise --match-id 1 --from-registry    # 房间集合取后台登记表（增删 1 分钟生效）
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
    danmu-intel chain-watch --polygon-address 0x… --solana-address …   # 链上监听（60s 轮询 + 启动补扫）
    danmu-intel chain-watch --polygon-address 0x… --once              # 只跑一轮增量（按游标，cron 友好）
    danmu-intel chain-usage                            # 供应商额度：当日/当月用量、上限、游标
    danmu-intel billing                                 # 档位/价格/宽限期/收款配置（改价格留审计）
    danmu-intel subscribe --platform telegram --username @name --tier standard --network polygon
    danmu-intel orders                                  # 订单列表（状态/金额/差额/到期）
    danmu-intel members                                 # 会员列表；--sweep 执行到期降级
    danmu-intel grant --order-ref DM… --tx-ref 0x… --reason "…"   # 人工补开通（AC-5）
    danmu-intel serve                                   # HTTP 面：下单 / 领取 / 校验 / 付费正文 / 统计上报
    danmu-intel admin-passwd                            # 设后台口令（只把 PBKDF2 哈希写进仓库外 .env）
    danmu-intel admin --host 100.64.0.1 --port 8090     # 后台（独立进程；只监听 tailnet，12 个页面）
    danmu-intel chain-watch --orders                    # 从待付订单派生监听目标并对账开通（AC-3）
    danmu-intel site-stats --day 2026-09-22             # 那天多少人来过付费页（AC-9：答不了是谁）
    danmu-intel site-stats --prune                      # 站点统计：90 天明细先汇总入 stats_daily 再删
    danmu-intel notify                                  # 投递待投递事件（统一 5 分钟时效闸门，一轮）
    danmu-intel notify --loop                           # 常驻 notifier：每 30 秒扫一遍（systemd 拉起的那个）
    danmu-intel alerts                                  # 告警台账：同一 alert_key 发生几次、恢复了没
    danmu-intel notify-config --set cooldown_ms=600000  # 通知门槛（闸门/冷却期/扫描间隔/尝试次数）

`notify` 的通道凭据（`QQ_BOT_APP_ID` / `QQ_BOT_APP_SECRET` / `QQ_BOT_OPENID` 或
`QQ_BOT_GROUP_OPENID`；备通道 `TG_BOT_TOKEN` / `TG_CHAT_ID`）同样只放仓库外 `.env`（0600）。
一个通道都没配时 `notify` 直接非零退出：不假装送达。超 5 分钟未送达的通知被销毁
（`state='dropped_expired'`，不补发），同 `alert_key` 冷却期内只发一次，恢复时发一条恢复通知。

`chain-watch` 的凭据（`POLYGONSCAN_API_KEY` / `HELIUS_API_KEY`）只放仓库外 `.env`（0600）；
一次调用记一次 `quota_usage`，用量 >80% 或撞限速都会写一条待投递报警（投递属 T11）。

后台（`admin`）与公开面是两个进程：公开面走 Funnel 暴露，后台只绑 tailnet 地址、每个请求再判一次
对端 IP，非 tailnet 一律 403（NFR-S-3 / AC-7）。后台的口令是 PBKDF2 哈希（仓库外 `.env`，0600），
登录态是 12 小时的签名 cookie；12 个页面只读，16 个写操作全部落 `audit_log`。

`report` 的解读层：配了凭据（仓库外 `.env`，0600，键 `DEEPSEEK_API_KEY`）就走受约束的
LLM 调用 + 反幻觉校验 + 成本硬闸；没配/超时/报错/校验不过就回落规则直出，并在命令输出、
报告第 10 段与页面横幅上标注降级与原因（降级不静默）。
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import sys
import time
from datetime import date, datetime
from pathlib import Path

from danmu_intel.common import paths
from danmu_intel.common.audit import record as audit_record
from danmu_intel.chain.transfer import Transfer
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
    """多房间并发采集：一房间一子进程，退出/僵死自动拉起，超限停下来报事。

    `--from-registry` 时房间集合来自库里的登记表（后台「房间与数据源」页登记的），
    并在监督过程中按版本号增/停子进程 —— 于是后台改动 1 分钟内对采集生效（FR-C8-2）。
    """
    from danmu_intel.collect import get_adapter
    from danmu_intel.collect.adapter import RoomKey
    from danmu_intel.collect.supervisor import Supervisor
    from danmu_intel.common import rooms as rooms_module

    conn = open_db()
    try:
        get_match(conn, args.match_id)
        adapter = get_adapter(args.platform)
        registry = None
        if getattr(args, "from_registry", False):

            def registry() -> list[RoomKey]:
                """每次读一遍登记表：后台增/删的直播间都要看得见（FR-C8-2）。"""
                return [
                    RoomKey(room.platform, room.room_id, room.url)
                    for room in rooms_module.list_rooms(conn)
                ]

            rooms = registry()
            if not rooms:
                raise ValueError(
                    "登记表里还没有任何直播间：先跑 danmu-intel supervise --room …，"
                    "或在后台「房间与数据源」页登记"
                )
        else:
            rooms = [adapter.parse_room(url) for url in args.room or []]
            if not rooms:
                raise ValueError("至少要给一个 --room，或用 --from-registry 从登记表读房间")
        seen: set[tuple[str, str]] = set()
        for room in rooms:
            key = (room.platform, room.room_id)
            if key in seen:
                raise ValueError(f"重复的直播间：{room.platform}/{room.room_id}（--room 不能重复）")
            seen.add(key)

        supervisor = Supervisor(
            conn, rooms, match_id=args.match_id, data_root=paths.data_dir(), registry=registry
        )
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
            if run.config_restarts:
                line += f"｜按新配置重起 {run.config_restarts} 次"
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


def _cmd_notify_config(args: argparse.Namespace) -> int:
    """通知门槛（闸门 / 冷却期 / 扫描间隔 / 尝试次数）：改动留审计。"""
    from danmu_intel.notify.config import load_notify_config, save_notify_config

    conn = open_db()
    try:
        if args.set:
            changes = _parse_config_changes(args.set)
            config = save_notify_config(conn, actor=args.actor, changes=changes)
            print(f"已更新通知门槛（操作者 {args.actor}）：{json.dumps(changes, ensure_ascii=False)}")
        else:
            config = load_notify_config(conn)
    finally:
        conn.close()
    for key, value in sorted(config.as_dict().items()):
        print(f"{key} = {json.dumps(value, ensure_ascii=False)}")
    return 0


def _cmd_notify(args: argparse.Namespace) -> int:
    """投递待投递事件：统一 5 分钟时效闸门（ADR-0010）。

    缺省只跑一轮（cron 友好）；`--loop` 才是常驻的 notifier（systemd 拉起的那个）。
    一个通道都没配直接非零退出 —— 宁可报错也不假装送达。
    """
    from danmu_intel.common.notifications import counts
    from danmu_intel.notify import (
        ChannelNotConfigured,
        channels_from_credentials,
        deliver_once,
        load_notify_config,
        run_loop,
    )

    conn = open_db()
    try:
        config = load_notify_config(conn)
        try:
            channels = channels_from_credentials()
        except (ChannelNotConfigured, CredentialError) as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 2
        if args.loop:

            def report(result) -> None:
                if not result.outcomes:  # 空扫一轮不进日志：journal 里只留真事
                    return
                print(result.summary())
                for outcome in result.outcomes:
                    print(_outcome_line(outcome))

            passes = run_loop(
                conn,
                channels=channels,
                config=config,
                seconds=args.seconds,
                interval=args.interval,
                on_pass=report,
            )
            print(f"投递循环结束：{passes} 轮（间隔 {args.interval or config.scan_interval_s:.0f} 秒）")
        else:
            result = deliver_once(conn, channels=channels, config=config)
            for outcome in result.outcomes:
                print(_outcome_line(outcome))
            print(result.summary())
        print(_queue_line(counts(conn)))
    finally:
        conn.close()
    return 0


def _outcome_line(outcome) -> str:
    channel = f"［{outcome.channel}］" if outcome.channel else ""
    detail = f"｜{outcome.detail}" if outcome.detail else ""
    return f"#{outcome.id} {outcome.kind}（{outcome.severity}）→ {outcome.label}{channel}{detail}"


def _queue_line(counts: dict[str, int]) -> str:
    order = ("pending", "delivered", "suppressed", "dropped_expired", "failed")
    parts = [f"{state} {counts.get(state, 0)}" for state in order]
    return "通知队列：" + "｜".join(parts)


def _cmd_alerts(args: argparse.Namespace) -> int:
    """告警台账：同一 `alert_key` 发生几次、上次何时发的、恢复了没有。"""
    from danmu_intel.common.notifications import counts
    from danmu_intel.notify import list_alerts

    conn = open_db()
    try:
        alerts = list_alerts(conn, state=args.state, limit=args.limit)
        queue = counts(conn)
    finally:
        conn.close()
    print(_queue_line(queue))
    if not alerts:
        print("没有告警台账记录（同一 alert_key 的抑制与恢复都记在这里）")
        return 0
    for alert in alerts:
        print(
            f"{alert.alert_key}｜{alert.state}｜发生 {alert.count} 次"
            f"｜首见 {_stamp(alert.first_seen)}｜最近 {_stamp(alert.last_seen)}"
            f"｜最后送达 {_stamp(alert.last_sent_at)}｜恢复 {_stamp(alert.resolved_at)}"
        )
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


def _chain_targets(args: argparse.Namespace, conn=None) -> list:
    """监听目标：`--orders` 时从待付订单派生（T9），否则按显式地址（T8 的运维用法）。"""
    from danmu_intel.chain.watcher import WatchTarget

    if getattr(args, "orders", False):
        from danmu_intel.billing import settle

        targets = settle.watch_targets(conn)
        if not targets:
            raise ValueError(
                "当前没有待付款的订单：--orders 从订单派生监听目标（过期订单不再轮询，"
                "补扫请用 --polygon-address/--solana-address）"
            )
        return targets
    targets = [WatchTarget(network="polygon", address=item) for item in args.polygon_address or []]
    targets += [WatchTarget(network="solana", address=item) for item in args.solana_address or []]
    if not targets:
        raise ValueError("至少要给一个监听地址：--polygon-address 或 --solana-address（或 --orders）")
    return targets


def _chain_watcher(conn, targets: list):
    """按目标涉及的网络造客户端与监听器（缺凭据直接报错，不静默降级）。"""
    from danmu_intel.chain import helius, polygonscan
    from danmu_intel.chain.watcher import Watcher

    clients: dict[str, object] = {}
    wanted = {target.network for target in targets}
    if "polygon" in wanted:
        clients["polygon"] = polygonscan.client_from_credentials(conn=conn)
    if "solana" in wanted:
        clients["solana"] = helius.client_from_credentials(conn=conn)
    return Watcher(conn, targets, polygon=clients.get("polygon"), helius=clients.get("solana"))


def _print_transfer(transfer: Transfer) -> None:
    memo = f"｜memo {transfer.memo}" if transfer.memo else ""
    where = f"区块 {transfer.block}" if transfer.block is not None else "-"
    print(
        f"入账｜{transfer.network}/{transfer.address}｜{transfer.asset}｜{transfer.units}｜"
        f"{where}｜{_stamp(transfer.at_ms)}｜{transfer.tx_ref}{memo}"
    )


def _cmd_chain_watch(args: argparse.Namespace) -> int:
    """链上监听：启动补扫（不看游标）→ 每 60 秒按游标增量轮询（ADR-0005 双重路径）。

    `--orders` 时顺手把看见的入账对到订单上（AC-3：入账 → 自动开通，全程无人工）。
    """
    from danmu_intel.billing import settle

    conn = open_db()
    settle_failures: list[str] = []

    def handle(transfer: Transfer) -> None:
        _print_transfer(transfer)
        if not args.orders:
            return
        outcome = settle.settle(conn, [transfer])
        for payment in outcome.payments:
            print(f"  对账｜{payment.public_ref}｜{payment.label}｜{payment.units}")
        for member_id in outcome.granted:
            print(f"  已开通会员 #{member_id}（幂等：同一笔交易只开通一次）")
        settle_failures.extend(outcome.failures)
        for failure in outcome.failures:
            print(f"  开通失败｜{failure}", file=sys.stderr)

    try:
        try:
            targets = _chain_targets(args, conn)
        except ValueError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 2
        try:
            watcher = _chain_watcher(conn, targets)
        except CredentialError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 2
        if args.rescan:
            print(f"补扫 {len(targets)} 个地址（按地址查全历史，不依赖游标）")
            observation = watcher.rescan()
            for transfer in observation.transfers:
                handle(transfer)
        elif args.once:
            observation = watcher.poll()
            for transfer in observation.transfers:
                handle(transfer)
        else:
            print(
                f"开始监听 {len(targets)} 个地址：启动补扫 → 每 {args.interval:.0f} 秒增量轮询"
                + ("，持续运行" if args.seconds is None else f"，共 {args.seconds:.0f} 秒")
            )
            observation = watcher.run(
                seconds=args.seconds, interval=args.interval, on_transfer=handle
            )
        crossed = watcher.check_quota()
        for summary in crossed:
            print(f"额度告警｜{summary}")
        for failure in observation.failures:
            print(f"扫描失败｜{failure}", file=sys.stderr)
    finally:
        conn.close()
    print(
        f"本轮共看见 {len(observation.transfers)} 笔入账"
        f"（扫描失败 {len(observation.failures)} 个目标）"
    )
    return 1 if observation.failures or settle_failures else 0


def _cmd_billing(args: argparse.Namespace) -> int:
    """档位、价格、宽限期与收款配置（`config` 表的 `billing` 键；改动留审计）。"""
    from danmu_intel.billing import pricing

    conn = open_db()
    try:
        if args.set:
            changes = _parse_billing_changes(args.set)
            config = pricing.save_billing_config(conn, actor=args.actor, changes=changes)
            print(f"已更新收款配置（操作者 {args.actor}）：{json.dumps(changes, ensure_ascii=False)}")
        else:
            config = pricing.load_billing_config(conn)
    finally:
        conn.close()
    for tier in config.tiers:
        print(
            f"档位 {tier.key}（{tier.label}）：{pricing.format_units(tier.amount_units)} "
            f"{pricing.ASSET_SYMBOL} / {tier.days} 天"
        )
    print(
        f"订单时效 {config.order_ttl_ms / 60000:.0f} 分钟｜宽限期 {config.grace_ms / 3600000:.0f} 小时"
    )
    print(f"Polygon xpub（watch-only）：{_mask(config.polygon_xpub) or '未配置'}")
    print(f"Solana 收款地址：{config.solana_address or '未配置'}")
    print(f"订阅页 API 基址：{config.api_base or '未配置'}")
    print("收款资产：Polygon USDT 合约 / Solana USDT mint（公开常量，不是凭据）")
    return 0


def _parse_billing_changes(values: list[str]) -> dict[str, object]:
    """`key=value`；值是 JSON 就按 JSON 解析，否则当字符串（xpub、地址都是裸串）。"""
    changes: dict[str, object] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"--set 格式应为 key=value，收到：{value}")
        key, raw = value.split("=", 1)
        try:
            changes[key.strip()] = json.loads(raw)
        except json.JSONDecodeError:
            changes[key.strip()] = raw
    return changes


def _mask(secret_like: str) -> str:
    """展示用的截断（xpub 是公开信息，但不整串刷屏）。"""
    if not secret_like:
        return ""
    return secret_like if len(secret_like) <= 24 else f"{secret_like[:16]}…{secret_like[-6:]}"


def _cmd_subscribe(args: argparse.Namespace) -> int:
    """下单：生成收款要求（命令行等价物；用户走订阅页 → `POST /api/orders`）。"""
    from danmu_intel.billing import orders, pricing

    conn = open_db()
    try:
        order, claim_token = orders.create_order(
            conn,
            platform=args.platform,
            username=args.username,
            tier=args.tier,
            network=args.network,
        )
    finally:
        conn.close()
    if args.json:
        print(json.dumps({**order.as_dict(), "claim_token": claim_token}, ensure_ascii=False, indent=2))
        return 0
    memo = f"｜memo {order.memo}" if order.memo else ""
    print(f"订单 {order.public_ref}（{order.tier}｜{order.network}）")
    print(f"  应付 {order.amount_display} {pricing.ASSET_SYMBOL}（已含唯一尾数，请精确转账）")
    print(f"  收款地址 {order.address}{memo}")
    print(f"  有效期至 {_stamp(order.expires_at)}（超时未付即过期，已展示的地址不再复用）")
    if claim_token:
        print(f"  领取令牌 {claim_token}（**只显示这一次**，付款后凭它领凭据）")
    else:
        print("  该订单已存在（复用）：领取令牌已轮换，请用页面里那一枚")
    print(
        "  付款到账会自动开通（无需人工）；随后 POST /api/claim "
        f'{{"platform":"{args.platform}","username":"{args.username}",'
        f'"order_ref":"{order.public_ref}","claim_token":"…"}} 领取凭据'
    )
    return 0


def _cmd_orders(args: argparse.Namespace) -> int:
    from danmu_intel.billing import members, orders

    conn = open_db()
    try:
        rows = orders.list_orders(conn, status=args.status, network=args.network, limit=args.limit)
        contacts = {row.member_id: members.get_member(conn, row.member_id).contact for row in rows}
    finally:
        conn.close()
    if not rows:
        print("没有订单")
        return 0
    for row in rows:
        extra = f"｜还差 {row.shortage_display}" if row.status == "short" else ""
        memo = f"｜memo {row.memo}" if row.memo else ""
        print(
            f"{row.public_ref}｜{row.status}｜{row.tier}｜{row.network}｜{row.amount_display}"
            f"{extra}｜{row.address}{memo}｜{contacts[row.member_id]}｜有效期至 {_stamp(row.expires_at)}"
        )
    return 0


def _cmd_members(args: argparse.Namespace) -> int:
    """会员列表；`--sweep` 先做一次到期降级（cron 友好：active → grace → expired）。"""
    from danmu_intel.billing import members

    conn = open_db()
    try:
        changed = members.sweep(conn) if args.sweep else []
        rows = members.list_members(conn, status=args.status, limit=args.limit)
    finally:
        conn.close()
    for member in changed:
        print(f"到期降级：会员 #{member.id} → {member.status}（{_stamp(member.expires_at)}）")
    if not rows:
        print("没有会员")
        return 0
    for member in rows:
        print(
            f"会员 #{member.id}｜{member.contact}｜{member.tier}｜{member.status}｜"
            f"到期 {_stamp(member.expires_at)}"
        )
    return 0


def _cmd_grant(args: argparse.Namespace) -> int:
    """人工补开通（AC-5 后半段）：凭交易凭证补记一笔入账，必填理由，操作留痕。"""
    from danmu_intel.billing import settle

    conn = open_db()
    try:
        order, opened = settle.manual_payment(
            conn,
            order_ref=args.order_ref,
            tx_ref=args.tx_ref,
            units=args.units,
            actor=args.actor,
            reason=args.reason,
        )
    finally:
        conn.close()
    if opened:
        print(f"已人工补开通：订单 {order.public_ref} → {order.status}（交易 {args.tx_ref}，操作者 {args.actor}）")
        return 0
    if order.status == "paid":
        print(f"订单 {order.public_ref} 已是 paid（幂等：没有重复开通）")
        return 0
    print(f"订单 {order.public_ref} 仍差 {order.shortage_display}（记了这笔入账，未达应收）")
    return 1


def _cmd_serve(args: argparse.Namespace) -> int:
    """起 HTTP 面：下单 / 领取 / 校验 / 付费正文 / 统计上报（Funnel 转发到本进程）。"""
    from danmu_intel import api

    conn = open_db()
    print(
        f"对外 API 监听 {args.host}:{args.port}（接口：/api/orders、/api/claim、/api/verify、"
        "/api/report/…、/api/stats/beacon、/api/stats/daily）"
    )
    try:
        api.run(conn, host=args.host, port=args.port)
    except KeyboardInterrupt:
        print("收到中断，已停止")
    finally:
        conn.close()
    return 0


def _rate(numerator: int, denominator: int) -> str:
    """转化率：分母为 0（那天没人来过付费页）时如实说「—」，不编一个数字。"""
    if denominator <= 0:
        return "—"
    return f"{numerator / denominator * 100:.1f}%"


def _cmd_archive(args: argparse.Namespace) -> int:
    """原始弹幕归档：到期文件压缩迁归档根（NAS 挂载点）；`--verify` 复核；`--retrieve` 取回。

    长期数据（切片/统计/报告/订单/会员/审计）不归本命令管 —— 归档只处理原始弹幕
    （需求 §7.10 / AC-17）；统计明细 90 天 → `stats_daily` 走 `site-stats --prune`。
    """
    from danmu_intel import archive as archive_module

    conn = open_db()
    try:
        if args.retrieve:
            return _archive_retrieve(conn, args, archive_module)
        if args.verify:
            return _archive_verify(conn, archive_module)
        cutoff = (
            date.fromisoformat(args.cutoff)
            if args.cutoff
            else archive_module.cutoff_date(months=args.months)
        )
        if args.dry_run:
            plan = archive_module.plan(conn, cutoff=cutoff, data_root=paths.data_dir())
            print(
                f"试运行：归档根 {paths.archive_dir()}｜保留期截止 {cutoff.isoformat()}"
                f"（在线保留 {args.months} 个月）：到期 {len(plan.due)} 个文件（未压缩、未移动）"
            )
            _print_archive_paths((segment.rel_path for segment in plan.due), limit=args.limit)
            _print_archive_anomalies(plan.anomalies)
            return 1 if plan.anomalies else 0
        result = archive_module.run(
            conn,
            actor=args.actor,
            cutoff=cutoff,
            data_root=paths.data_dir(),
            allow_same_disk=args.allow_same_disk,
        )
        print(result.summary())
        if result.archived and not result.anomalies:
            print(
                f"  索引行已改指向归档件（`rel_path` + `archived_at` {result.archived_at}）；"
                "操作者与范围进了 audit_log 的 archive.run"
            )
        _print_archive_anomalies(result.anomalies)
        return 1 if result.anomalies else 0
    finally:
        conn.close()


def _archive_verify(conn, archive_module) -> int:
    problems = archive_module.verify(conn, data_root=paths.data_dir())
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM danmu_segments WHERE archived_at IS NOT NULL"
    ).fetchone()["n"]
    print(f"归档件复核：{total} 个｜归档根 {paths.archive_dir()}")
    if problems:
        _print_archive_anomalies(problems)
        return 1
    print("  全部通过：文件在、归档件自身摘要一致、解压后内容与封存摘要一致（可核验）")
    return 0


def _archive_retrieve(conn, args: argparse.Namespace, archive_module) -> int:
    try:
        path, content = archive_module.retrieve(conn, args.retrieve, data_root=paths.data_dir())
    except LookupError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    if not args.out:
        sys.stdout.write(content.decode("utf-8"))
        print(f"（取回自 {path}，{len(content)} 字节，内容摘要与封存值一致）", file=sys.stderr)
        return 0
    target = Path(args.out).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    print(f"已取回 {args.retrieve} → {target}（{len(content)} 字节，内容摘要与封存值一致）")
    return 0


def _print_archive_paths(rel_paths, *, limit: int) -> None:
    listed = list(rel_paths)
    for rel_path in listed[:limit]:
        print(f"  到期｜{rel_path}")
    if len(listed) > limit:
        print(f"  ……还有 {len(listed) - limit} 个（--limit 调大看全）")


def _print_archive_anomalies(anomalies) -> None:
    for item in anomalies:
        print(f"异常｜{item}", file=sys.stderr)
    if anomalies:
        print(
            f"{len(anomalies)} 项需要人看一眼（未索引的超期文件 / 缺失 / 摘要对不上）："
            "归档不静默处理它们，先补齐索引或人工处置",
            file=sys.stderr,
        )


def _cmd_site_stats(args: argparse.Namespace) -> int:
    """站点统计：某天的访问量 / 付费页人数 / 下单转化 / 留资数（AC-9）；--prune 做 90 天保留。"""
    from danmu_intel.site_stats import daily

    conn = open_db()
    try:
        if args.prune:
            print(daily.prune(conn, retention_days=args.retention_days).summary())
        summary = daily.summary(conn, args.day or daily.today())
        recent = daily.recent_days(conn, limit=args.limit)
    finally:
        conn.close()
    print(
        f"{summary.day}｜页面访问 {summary.page_views} 次｜会话 {summary.sessions}｜"
        f"独立访客 {summary.unique_visitors}"
    )
    print(
        f"  付费页 {summary.paid_page_views} 次 / {summary.paid_unique_visitors} 人｜"
        f"留资 {summary.leads}｜下单 {summary.orders}"
        f"（转化 {_rate(summary.orders, summary.paid_unique_visitors)}）｜"
        f"付费 {summary.paid_orders}（转化 {_rate(summary.paid_orders, summary.paid_unique_visitors)}）"
    )
    print(f"  最近有数据的日期（{len(recent)} 天）：{'、'.join(recent) or '还没有任何访问'}")
    print("  口径：独立访客 = sha256(每日盐 + IP + UA)，每日换盐；统计答不了「具体是谁」（AC-9）")
    return 0


def _cmd_chain_usage(args: argparse.Namespace) -> int:
    """供应商额度记账：当日/当月用量、上限、报警阈值、游标（NFR-C-3；后台页属 T12）。"""
    from danmu_intel.chain import cursor as chain_cursor
    from danmu_intel.chain.quota import PROVIDERS, QuotaLedger

    conn = open_db()
    try:
        usages = [(provider, QuotaLedger(conn, provider).usage()) for provider in PROVIDERS]
        marks = chain_cursor.rows(conn)
    finally:
        conn.close()
    for provider, usage in usages:
        limit = usage.limit
        print(
            f"{provider}（{limit.unit_label}，按{limit.window_label}看上限 {limit.cap}，"
            f"限速 {limit.rate_per_s:.1f}/秒）"
        )
        print(f"  当日 {usage.day_used} {limit.unit_label}｜当月 {usage.month_used} {limit.unit_label}")
        print(f"  {usage.summary()}｜成本 ¥0.00（免费额度内：超额是限速/拒绝，不会自动计费）")
        if usage.over_threshold:
            print("  ⚠ 已越报警阈值（已写待投递报警；投递属 T11）")
        if usage.last_error:
            print(f"  最近一次失败：{usage.last_error}")
    print("监听游标（扫到哪了）：")
    if not marks:
        print("  还没有扫过任何地址")
    for mark in marks:
        print(f"  {mark.network}/{mark.scope}：{mark.cursor}｜{_stamp(mark.updated_at)}")
    return 0


def _cmd_admin_passwd(args: argparse.Namespace) -> int:
    """设/改后台口令：只把 **PBKDF2 哈希**写进仓库外 `.env`（0600），明文不落盘。"""
    from danmu_intel.admin import auth

    target = paths.env_path()
    first = getpass.getpass(f"新后台口令（写入 {target}）：")
    second = getpass.getpass("再输一次：")
    if first != second:
        print("错误：两次输入不一致", file=sys.stderr)
        return 2
    try:
        auth.save_password(target, first)
    except (auth.AuthError, CredentialError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    print(f"已写入后台口令哈希：{target}（0600；明文没有落盘）")
    print("起后台：danmu-intel admin --host <tailscale ip -4> --port 8090")
    return 0


def _cmd_admin(args: argparse.Namespace) -> int:
    """后台（独立进程 + 仅 tailnet 可达 + 单管理员）：12 个页面 + 全部写操作留痕。"""
    from danmu_intel.admin import auth, server

    try:
        auth.require_tailnet_host(args.host)  # 先验地址：非 tailnet 直接拒绝启动
        auth.load_secrets()
    except (auth.AuthError, CredentialError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    conn = open_db()

    def release() -> ReleaseContext:
        """按需构造：只有点发布/回滚才用（构造会去碰 git 与 Vercel 配置）。"""
        return _release_context(args)

    print(
        f"后台监听 {args.host}:{args.port}（只接受 tailnet 对端；12 个页面；"
        f"{'发布/回滚只落本地产物' if args.no_deploy else '发布/回滚走真实 git + Vercel'}）"
    )
    try:
        server.run(
            conn=conn,
            host=args.host,
            port=args.port,
            data_root=paths.data_dir(),
            release=release,
            secure_cookie=args.secure_cookie,
        )
    except KeyboardInterrupt:
        print("收到中断，已停止后台")
    except auth.AuthError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()
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
    supervise.add_argument("--room", action="append", default=None, metavar="URL", help="直播间链接，可重复")
    supervise.add_argument(
        "--from-registry", action="store_true",
        help="房间集合从登记表读（后台登记/删除的房间 1 分钟内生效，FR-C8-2）",
    )
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

    notify = sub.add_parser("notify", help="投递待投递事件（QQ Bot 主 + TG 备，统一 5 分钟时效闸门）")
    notify.add_argument("--loop", action="store_true", help="常驻：每 30 秒扫一遍（缺省只跑一轮，cron 友好）")
    notify.add_argument("--seconds", type=float, default=None, help="常驻模式的总运行时长（秒），缺省一直跑")
    notify.add_argument("--interval", type=float, default=None, help="扫描间隔（秒，缺省读配置：30）")
    notify.set_defaults(func=_cmd_notify)

    alerts_cmd = sub.add_parser("alerts", help="告警台账（同 alert_key 的次数/抑制/恢复）+ 队列状态")
    alerts_cmd.add_argument("--state", default=None, help="firing | resolved")
    alerts_cmd.add_argument("--limit", type=int, default=20)
    alerts_cmd.set_defaults(func=_cmd_alerts)

    notify_config = sub.add_parser("notify-config", help="通知门槛（闸门/冷却期/扫描间隔/尝试次数，改动留审计）")
    notify_config.add_argument("--set", action="append", default=None, metavar="KEY=VALUE", help="改门槛，可重复")
    notify_config.add_argument("--actor", default="管理员", help="操作者（进审计）")
    notify_config.set_defaults(func=_cmd_notify_config)

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

    chain_watch = sub.add_parser("chain-watch", help="链上监听：启动补扫 + 每 60 秒增量轮询")
    chain_watch.add_argument(
        "--polygon-address", action="append", default=None, metavar="ADDR",
        help="Polygon 收款地址，可重复（派生地址由 T9 的订单流程给出）",
    )
    chain_watch.add_argument(
        "--solana-address", action="append", default=None, metavar="ADDR",
        help="Solana 收款地址，可重复（单地址 + 每单唯一 memo）",
    )
    chain_watch.add_argument(
        "--once", action="store_true",
        help="只跑一轮增量扫描（按游标续扫；不跑全历史补扫，cron 友好）",
    )
    chain_watch.add_argument("--rescan", action="store_true", help="只做一次补扫（按地址查全历史，不依赖游标）")
    chain_watch.add_argument(
        "--orders", action="store_true",
        help="监听目标从待付款订单派生，并把入账对账开通（T9 自助闭环；与显式地址二选一）",
    )
    chain_watch.add_argument("--interval", type=float, default=60.0, help="轮询间隔（秒，默认 60）")
    chain_watch.add_argument("--seconds", type=float, default=None, help="总运行时长（秒），缺省持续运行")
    chain_watch.set_defaults(func=_cmd_chain_watch)

    billing_cmd = sub.add_parser("billing", help="档位/价格/宽限期/收款配置（改动留审计）")
    billing_cmd.add_argument(
        "--set", action="append", default=None, metavar="KEY=VALUE",
        help="改配置，可重复（如 tiers='[{…}]'、polygon_xpub=xpub…、grace_ms=86400000）",
    )
    billing_cmd.add_argument("--actor", default="管理员", help="操作者（进审计）")
    billing_cmd.set_defaults(func=_cmd_billing)

    subscribe = sub.add_parser("subscribe", help="下单：生成收款要求（地址/金额/memo/到期）")
    subscribe.add_argument("--platform", required=True, help="通讯平台：telegram | qq")
    subscribe.add_argument("--username", required=True, help="通讯账号标识（Telegram @name 或 QQ 号）")
    subscribe.add_argument("--tier", required=True, help="档位：standard | trial")
    subscribe.add_argument("--network", required=True, help="收款网络：polygon | solana")
    subscribe.add_argument("--json", action="store_true", help="按 JSON 输出（含领取令牌）")
    subscribe.set_defaults(func=_cmd_subscribe)

    orders_cmd = sub.add_parser("orders", help="订单列表（状态/金额/差额/地址/到期）")
    orders_cmd.add_argument("--status", default=None, help="pending|short|paid|expired")
    orders_cmd.add_argument("--network", default=None, help="polygon|solana")
    orders_cmd.add_argument("--limit", type=int, default=20)
    orders_cmd.set_defaults(func=_cmd_orders)

    members_cmd = sub.add_parser("members", help="会员列表；--sweep 执行到期降级（active→grace→expired）")
    members_cmd.add_argument("--status", default=None, help="pending|active|grace|expired|revoked")
    members_cmd.add_argument("--limit", type=int, default=50)
    members_cmd.add_argument("--sweep", action="store_true", help="先做一次到期降级")
    members_cmd.set_defaults(func=_cmd_members)

    grant_cmd = sub.add_parser("grant", help="人工补开通（凭交易凭证 + 必填理由，操作留痕）")
    grant_cmd.add_argument("--order-ref", required=True, help="订单引用（页面上的 DM… 短引用）")
    grant_cmd.add_argument("--tx-ref", required=True, help="链上交易凭证（交易哈希 / 签名）")
    grant_cmd.add_argument("--units", type=int, default=None, help="入账金额（最小单位；缺省视为足额）")
    grant_cmd.add_argument("--reason", required=True, help="补开通理由（必填）")
    grant_cmd.add_argument("--actor", default="管理员", help="操作者（进审计）")
    grant_cmd.set_defaults(func=_cmd_grant)

    serve = sub.add_parser("serve", help="HTTP 面：下单 / 领取 / 校验 / 付费正文 / 统计上报")
    serve.add_argument("--host", default="127.0.0.1", help="监听地址（默认只监听本机，公网靠 Funnel）")
    serve.add_argument("--port", type=int, default=8080)
    serve.set_defaults(func=_cmd_serve)

    admin_passwd = sub.add_parser("admin-passwd", help="设/改后台口令（只写哈希进仓库外 .env）")
    admin_passwd.set_defaults(func=_cmd_admin_passwd)

    admin = sub.add_parser("admin", help="后台进程（仅 tailnet 可达）：12 个页面 + 写操作留痕")
    admin.add_argument("--host", required=True, help="监听地址，必须是 tailnet 地址（`tailscale ip -4`）")
    admin.add_argument("--port", type=int, default=8090)
    admin.add_argument("--no-deploy", action="store_true", help="发布/回滚只落本地产物：不推 git、不调 Vercel")
    admin.add_argument("--actor", default="admin", help="后台写操作的操作者（进审计）")
    admin.add_argument(
        "--secure-cookie", action="store_true",
        help="经 https 反代访问时给会话 cookie 加 Secure（直连 tailnet http 时不要加）",
    )
    admin.set_defaults(func=_cmd_admin)

    site_stats = sub.add_parser(
        "site-stats", help="站点统计：某天的访问量/付费页人数/下单转化/留资（--prune 做 90 天保留）"
    )
    site_stats.add_argument("--day", default=None, help="日期 YYYY-MM-DD（缺省今天）")
    site_stats.add_argument(
        "--prune", action="store_true", help="把超过保留期的明细汇总入 stats_daily 后删除（cron 友好）"
    )
    site_stats.add_argument("--retention-days", type=int, default=90, help="明细保留天数（默认 90）")
    site_stats.add_argument("--limit", type=int, default=30, help="列出的「最近有数据的日期」条数")
    site_stats.set_defaults(func=_cmd_site_stats)

    archive_cmd = sub.add_parser(
        "archive",
        help="原始弹幕归档：6 个月 → 压缩 .jsonl.zst 迁归档根（NAS 挂载点），索引行改指向归档件",
    )
    archive_cmd.add_argument("--dry-run", action="store_true", help="只列出到期文件，不压缩、不移动")
    archive_cmd.add_argument(
        "--verify", action="store_true",
        help="复核全部归档件：文件在 + 归档件自身摘要一致 + 解压后内容与封存摘要一致",
    )
    archive_cmd.add_argument(
        "--retrieve", default=None, metavar="REL_PATH",
        help="取回一份证据（在线地址或归档地址都认），校验封存摘要后输出未压缩内容",
    )
    archive_cmd.add_argument("--out", default=None, help="取回时写到文件（缺省写标准输出）")
    archive_cmd.add_argument("--months", type=int, default=6, help="在线保留月数（缺省 6，自采集之日算起）")
    archive_cmd.add_argument("--cutoff", default=None, help="保留期截止日 YYYY-MM-DD（缺省 = 今天回推 --months 个月）")
    archive_cmd.add_argument(
        "--allow-same-disk", action="store_true",
        help="归档根与数据根同一磁盘时也继续（本机演练/测试；真实归档要求 NAS 挂载点）",
    )
    archive_cmd.add_argument("--limit", type=int, default=20, help="试运行时列出的到期文件条数")
    archive_cmd.add_argument("--actor", default="归档器", help="操作者（进审计）")
    archive_cmd.set_defaults(func=_cmd_archive)

    chain_usage = sub.add_parser("chain-usage", help="供应商额度：当日/当月用量、上限、游标")
    chain_usage.set_defaults(func=_cmd_chain_usage)

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
