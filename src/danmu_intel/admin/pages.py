"""后台的 12 个页面（设计 §14.1 的清单，逐条对应；需求 FR-C8-4/C8-5）。

| 页面 | 路径 | 回答什么问题 |
|---|---|---|
| 概览 | `/admin` | 现在有几场比赛在跑、采集健不健康、有什么待办 |
| 比赛管理 | `/admin/matches` | 比赛的增删改查与状态机操作 |
| 房间与数据源 | `/admin/rooms` | 直播间登记与采集健康（数据源可用性） |
| 切片复核 | `/admin/slices` | 小局边界对不对、人工修正（必填理由） |
| 灰信号评审 | `/admin/gray` | 哪些聚集现象值得看一眼（升级/作废 + 理由） |
| 报告 | `/admin/reports` | 三形态报告的版本、预览、重生成 |
| 发布与回滚 | `/admin/releases` | 线上是哪一版、7 项检查结果、回滚 |
| 会员与订单 | `/admin/members` | 谁开通了、订单到哪一步、人工补开通/撤权 |
| 通知与告警 | `/admin/notifications` | 待投递事件队列（投递属 T11） |
| 配置 | `/admin/config` | 关键词表、门槛、价格、时效预算、提醒档位 |
| 审计日志 | `/admin/audit` | 谁在什么时候改了什么（只增不改） |
| 成本与额度 | `/admin/cost` | LLM 花了多少、链上额度用了多少 |

三条纪律：

1. **页面只读**：所有写操作在 `admin/actions.py`，本模块一个 `INSERT/UPDATE/DELETE` 都没有
   （写操作必须走 POST 且入审计，见 `actions.py`）。
2. **事实来自账本**：数字直接从库里查（`metrics` / `reports` / `releases` / `orders` / …），
   页面不自己另攒一份可以漂移的状态。
3. **灰信号零身份**：样本只有「时间 + 原文 + 取证坐标」（需求 §6.5 第 2 条），页面照此渲染。
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, fields as dataclass_fields
from pathlib import Path
from typing import Callable, Mapping, Sequence

from danmu_intel.admin import html
from danmu_intel.admin.html import Field, Html
from danmu_intel.billing import members as members_module
from danmu_intel.billing import orders as orders_module
from danmu_intel.billing import pricing
from danmu_intel.chain import cursor as chain_cursor
from danmu_intel.chain.quota import PROVIDERS, QuotaLedger
from danmu_intel.collect import health as health_module
from danmu_intel.common import audit, config_store, gray_review, paywall, rooms
from danmu_intel.common.config import StatsConfig, load_stats_config
from danmu_intel.common.matches import MATCH_STATES, list_matches
from danmu_intel.common.notifications import recent as recent_notifications
from danmu_intel.notify import suppression
from danmu_intel.notify.suppression import list_alerts
from danmu_intel.publish import release as release_module
from danmu_intel.report import forms as report_forms
from danmu_intel.report.llm import cost as llm_cost
from danmu_intel.report.llm import ledger as llm_ledger
from danmu_intel.report.publish import list_reports
from danmu_intel.slice.manual import load_slices


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True, slots=True)
class AdminContext:
    """一次页面渲染要用的东西：库、数据目录、时钟（测试注入假时钟）。"""

    conn: sqlite3.Connection
    data_root: Path
    clock: Callable[[], int] = now_ms

    def now(self) -> int:
        return self.clock()


@dataclass(frozen=True, slots=True)
class AdminPage:
    """一个后台页面：键（路由与导航用）、路径、标题、渲染函数。"""

    key: str
    path: str
    title: str
    render: Callable[[AdminContext, Mapping[str, str]], Html]


# —— 通用零件 ——


def nav_items() -> tuple[tuple[str, str], ...]:
    """固定导航：`(路径, 标签)`，顺序就是设计 §14.1 的清单顺序。"""
    return tuple((page.path, page.title) for page in PAGES)


def page_of(key: str) -> AdminPage:
    for page in PAGES:
        if page.key == key:
            return page
    raise LookupError(f"未注册的后台页面：{key}")


def _count(conn: sqlite3.Connection, sql: str, params: Sequence[object] = ()) -> int:
    return int(conn.execute(sql, tuple(params)).fetchone()["n"])


def _latest_match_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT MAX(id) AS v FROM matches").fetchone()
    return None if row["v"] is None else int(row["v"])


def _health_match_id(conn: sqlite3.Connection) -> int | None:
    """采集健康按「最近有采集会话的比赛」看（没有会话就看最新一场）。"""
    row = conn.execute("SELECT MAX(match_id) AS v FROM room_sessions WHERE match_id IS NOT NULL").fetchone()
    if row["v"] is not None:
        return int(row["v"])
    return _latest_match_id(conn)


def selected_match_id(conn: sqlite3.Connection, query: Mapping[str, str]) -> int | None:
    """页面上的「比赛」筛选：`?match_id=` 优先，缺省取最新的那一场。"""
    raw = (query.get("match_id") or "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            return None
    return _latest_match_id(conn)


def _match_selector(conn: sqlite3.Connection, current: int | None, *, action: str) -> Html:
    matches = list_matches(conn)
    if not matches:
        return html.paragraphs(["还没有登记任何比赛。"])
    options = tuple(
        (str(match.id), f"#{match.id} {match.team_a} vs {match.team_b}（{match.state}）")
        for match in matches
    )
    return html.form(
        action,
        [
            Field("match_id", "比赛（切换后点「查看」）", value="" if current is None else current,
                  kind="select", options=options),
        ],
        submit="查看",
        method="get",
    )


def _match_label(conn: sqlite3.Connection, match_id: int) -> str:
    try:
        row = conn.execute("SELECT team_a, team_b FROM matches WHERE id=?", (match_id,)).fetchone()
    except sqlite3.Error:  # pragma: no cover - 库结构损坏时不该把页面打崩
        return f"#{match_id}"
    if row is None:
        return f"#{match_id}（已删除）"
    return f"#{match_id} {row['team_a']} vs {row['team_b']}"


# —— ① 概览 ——


def render_dashboard(ctx: AdminContext, query: Mapping[str, str]) -> Html:
    conn, now = ctx.conn, ctx.now()
    by_state = {
        state: _count(conn, "SELECT COUNT(*) AS n FROM matches WHERE state=?", (state,))
        for state in MATCH_STATES
    }
    health_match = _health_match_id(conn)
    rows = health_module.room_health(conn, health_match, data_root=ctx.data_root, now=now) if health_match else []
    pending_events = _count(conn, "SELECT COUNT(*) AS n FROM notifications WHERE state='pending'")
    candidate_gray = _count(conn, "SELECT COUNT(*) AS n FROM gray_signals WHERE status='candidate'")
    open_orders = _count(conn, "SELECT COUNT(*) AS n FROM orders WHERE status IN ('pending','short')")
    active_members = _count(conn, "SELECT COUNT(*) AS n FROM members WHERE status IN ('active','grace')")
    live = release_module.current_release(conn)
    version = config_store.latest(conn)

    body = [
        html.stats(
            [
                ("比赛（进行中）", by_state["live"] + by_state["between_games"]),
                ("比赛（已结束）", by_state["ended"]),
                ("直播间", _count(conn, "SELECT COUNT(*) AS n FROM rooms")),
                ("待投递事件", pending_events),
                ("待评审灰信号", candidate_gray),
                ("待付款订单", open_orders),
                ("有效会员", active_members),
            ]
        ),
        html.card(
            f"采集健康（比赛 {html.esc(health_match) if health_match else '未登记'}）",
            _health_table(rows),
            note="状态与严重级别来自 `room_sessions` + 心跳文件：进程死了也留得下证据（FR-C8-5）。",
        ),
        html.card(
            "待办",
            html.paragraphs(
                [
                    f"待投递事件 {html.esc(pending_events)} 条（投递与 5 分钟闸门属 T11，见「通知与告警」页）",
                    f"待评审灰信号 {html.esc(candidate_gray)} 条（见「灰信号评审」页）",
                    f"待付款/待补款订单 {html.esc(open_orders)} 条（见「会员与订单」页）",
                ]
            ),
        ),
        html.card(
            "线上与配置",
            html.paragraphs(
                [
                    f"线上发布：{'v' + str(live.version) + '（' + live.state + '）' if live else '还没有发布过'}",
                    f"配置版本：v{version.version}（{version.updated_by} @ {html.stamp(version.updated_at)}）"
                    if version
                    else "配置版本：v0（还没有改过任何配置）",
                    "配置生效：本进程保存即刻生效；别的进程 ≤60 秒（TTL 缓存）；采集子进程按版本号重起。",
                ]
            ),
        ),
        html.card(
            "最近改动",
            _audit_table(conn, limit=8),
            note="全部写操作都进 `audit_log`（只增不改）：谁、何时、改了什么（FR-C8-4）。",
        ),
    ]
    return html.layout("概览", html.raw("".join(body)), nav=nav_items(), active="/admin")


def _health_table(rows: Sequence[health_module.RoomHealth]) -> Html:
    table = html.Table(("平台/房间", "主播", "状态", "级别", "pid", "已收(条)", "重启", "重连", "最后一条消息"))
    for item in rows:
        table.add(
            f"{item.platform}/{item.room_id}",
            item.streamer or "-",
            html.tag(item.state, "ok" if item.state == "running" else "warning"),
            html.severity_tag(item.severity),
            item.pid if item.pid else "-",
            item.msg_count,
            item.restart_count,
            item.reconnects,
            html.stamp(item.last_msg_at),
        )
    return table.render(empty="这个比赛还没有采集会话")


# —— ② 比赛管理 ——


def render_matches(ctx: AdminContext, query: Mapping[str, str]) -> Html:
    conn = ctx.conn
    rows = html.Table(("#", "联赛", "对阵", "状态", "阶段", "开赛", "结束", "官方结果", "数据", "操作", "删除"))
    for match in list_matches(conn):
        slices = _count(conn, "SELECT COUNT(*) AS n FROM slices WHERE match_id=?", (match.id,))
        reports = _count(conn, "SELECT COUNT(*) AS n FROM reports WHERE match_id=?", (match.id,))
        rows.add(
            match.id,
            match.league,
            f"{match.team_a} vs {match.team_b}",
            html.tag(match.state, "ok" if match.state == "ended" else "warning"),
            match.stage or "-",
            html.stamp(match.scheduled_at),
            html.stamp(match.ended_at),
            html.json_block(match.official_result, limit=80) if match.official_result else "-",
            f"切片 {slices}｜报告 {reports}",
            html.form(
                "/admin/matches/state",
                [
                    Field("match_id", value=match.id, kind="hidden"),
                    Field(
                        "state",
                        "状态",
                        value=match.state,
                        kind="select",
                        options=tuple((state, _state_label(state)) for state in MATCH_STATES),
                    ),
                    Field("official_result", "官方结果 JSON", placeholder='{"score":"2:1"}'),
                ],
                submit="改状态",
            ),
            html.form(
                "/admin/matches/delete",
                [Field("match_id", value=match.id, kind="hidden")],
                submit="删除",
                danger=True,
            ),
        )
    add_form = html.form(
        "/admin/matches/add",
        [
            Field("league", "联赛", placeholder="LPL"),
            Field("team_a", "队伍 A", placeholder="iG"),
            Field("team_b", "队伍 B", placeholder="LNG"),
            Field(
                "state",
                "状态",
                value="scheduled",
                kind="select",
                options=tuple((state, _state_label(state)) for state in MATCH_STATES),
            ),
            Field("stage", "阶段", placeholder="季后赛"),
            Field("scheduled_at", "计划开赛（毫秒时间戳，可空）", kind="number"),
            Field("official_result", "官方结果 JSON", placeholder='{"score":"2:1"}'),
        ],
        submit="登记比赛",
    )
    return html.layout(
        "比赛管理",
        html.raw(
            html.card("全部比赛", rows.render(empty="还没有登记任何比赛"))
            + html.card("登记新比赛", add_form, note="登记后采集与报告都挂在它上面（FR-C8-1：页面上就能增删改查）。")
        ),
        nav=nav_items(),
        active="/admin/matches",
        notice=_parse_notice(query),
    )


def _state_label(state: str) -> str:
    return {
        "scheduled": "未开始",
        "live": "进行中",
        "between_games": "局间",
        "ended": "已结束",
        "aborted": "中止",
    }[state]


# —— ③ 房间与数据源 ——


def render_rooms(ctx: AdminContext, query: Mapping[str, str]) -> Html:
    conn = ctx.conn
    health_match = _health_match_id(conn)
    health_rows = (
        health_module.room_health(conn, health_match, data_root=ctx.data_root, now=ctx.now())
        if health_match
        else []
    )
    table = html.Table(("平台/房间", "地址", "主播", "来源", "开播", "会话数", "改动", "删除"))
    for room in rooms.list_rooms(conn):
        sessions = _count(conn, "SELECT COUNT(*) AS n FROM room_sessions WHERE room_id=?", (room.id,))
        table.add(
            room.label,
            room.url,
            room.streamer or "-",
            room.discovered_by,
            html.tag("在播", "ok") if room.is_live else "-",
            sessions,
            html.form(
                "/admin/rooms/update",
                [
                    Field("room_row_id", value=room.id, kind="hidden"),
                    Field("url", "地址", value=room.url),
                    Field("streamer", "主播名", value=room.streamer or ""),
                ],
                submit="保存",
            ),
            html.form(
                "/admin/rooms/delete",
                [Field("room_row_id", value=room.id, kind="hidden")],
                submit="删除",
                danger=True,
            ),
        )
    add_form = html.form(
        "/admin/rooms/add",
        [
            Field("platform", "平台", value="huya"),
            Field("room_id", "房间标识", placeholder="660000"),
            Field("url", "直播间地址", placeholder="https://www.huya.com/660000"),
            Field("streamer", "主播名（可空）"),
        ],
        submit="登记直播间",
    )
    return html.layout(
        "房间与数据源",
        html.raw(
            html.card("直播间", table.render(empty="还没有登记任何直播间"))
            + html.card("人工登记直播间", add_form, note="登记后采集监督按它拉起子进程（FR-C8-2：1 分钟内生效）。")
            + html.card(
                f"采集健康（比赛 {health_match if health_match else '未登记'}）",
                _health_table(health_rows),
                note="数据源可用性：状态 `no_stream` = 平台没有流，`stalled` = 有流但没消息（FR-C8-5）。",
            )
        ),
        nav=nav_items(),
        active="/admin/rooms",
        notice=_parse_notice(query),
    )


# —— ④ 切片复核 ——


def render_slices(ctx: AdminContext, query: Mapping[str, str]) -> Html:
    conn = ctx.conn
    match_id = selected_match_id(conn, query)
    windows = load_slices(conn, match_id) if match_id else []
    table = html.Table(("小局", "起", "止", "边界来源", "冲突事实", "人工修正", "修正理由"))
    for window in windows:
        table.add(
            f"G{window.game_no}",
            html.stamp(window.start_ms),
            html.stamp(window.end_ms),
            window.boundary_source,
            window.conflict_note or "-",
            f"{window.override_by or '-'}{'（' + html.stamp(window.override_at) + '）' if window.override_at else ''}",
            window.override_reason or "-",
        )
    fix_form = html.form(
        "/admin/slices/override",
        [
            Field("match_id", value=match_id or "", kind="hidden"),
            Field("game_no", "小局局号", kind="number", required=True),
            Field("start_ms", "起（毫秒时间戳）", kind="number", required=True),
            Field("end_ms", "止（毫秒时间戳）", kind="number", required=True),
            Field("reason", "修正理由（必填）", placeholder="对齐官方开赛时间"),
        ],
        submit="写入人工修正",
    )
    return html.layout(
        "切片复核",
        html.raw(
            html.card("选择比赛", _match_selector(conn, match_id, action="/admin/slices"))
            + html.card(
                "已定的小局边界",
                table.render(empty="这场比赛还没有切片"),
                note="优先级：官方 > 弹幕信号 > 报告窗口 > 人工修正；多来源冲突照样记录（需求 FR-C2-3）。",
            )
            + html.card(
                "人工修正（覆盖已有边界必须给理由）",
                fix_form,
                note="修正即落审计并让该场算法版本递增（`1.0.0+ov1`），统计重算结果可复现。",
            )
        ),
        nav=nav_items(),
        active="/admin/slices",
        notice=_parse_notice(query),
    )


# —— ⑤ 灰信号评审 ——


def render_gray(ctx: AdminContext, query: Mapping[str, str]) -> Html:
    conn = ctx.conn
    match_id = selected_match_id(conn, query)
    signals = gray_review.list_signals(conn, match_id=match_id) if match_id else []
    table = html.Table(("类别", "关键词", "命中", "独立发言者", "时段", "状态", "理由", "样本", "评审"))
    for signal in signals:
        samples = html.raw(
            "<br>".join(
                f"{html.esc(html.stamp(sample.ts))}｜{html.esc(sample.text)}"
                f"｜{html.esc(sample.rel_path)} 第 {html.esc(sample.line_no)} 行"
                for sample in signal.samples
            )
        )
        table.add(
            signal.category_label,
            signal.keyword,
            signal.hit_count,
            signal.distinct_users,
            signal.window_count,
            html.tag(signal.status, "warning" if signal.status == "candidate" else "ok"),
            signal.reason or "-",
            samples,
            html.form(
                "/admin/gray/review",
                [
                    Field("signal_id", value=signal.id, kind="hidden"),
                    Field("reason", "理由（必填）"),
                    Field(
                        "action",
                        "动作",
                        value=gray_review.ESCALATE,
                        kind="select",
                        options=(("escalate", "升级（值得人再看）"), ("discard", "作废（不进报告）")),
                    ),
                ],
                submit="评审",
            ),
        )
    return html.layout(
        "灰信号评审",
        html.raw(
            html.card("选择比赛", _match_selector(conn, match_id, action="/admin/gray"))
            + html.card(
                "灰信号",
                table.render(empty="这场比赛没有灰信号"),
                note="只作风险提示、不指控、不点名；样本里没有身份字段，页面也不提供任何导出（需求 §6.5）。",
            )
        ),
        nav=nav_items(),
        active="/admin/gray",
        notice=_parse_notice(query),
    )


# —— ⑥ 报告 ——


def render_reports(ctx: AdminContext, query: Mapping[str, str]) -> Html:
    conn = ctx.conn
    match_id = selected_match_id(conn, query)
    reports = list_reports(conn, match_id) if match_id else []
    table = html.Table(("形态", "版本", "状态", "生成时间", "解读层", "事实层哈希", "文件", "操作"))
    for report in reports:
        form = report_forms.form_of(report.kind)
        preview = html.link(
            f"/admin/reports/preview?match_id={report.match_id}&kind={report.kind}&version={report.version}",
            "预览",
        )
        table.add(
            f"{form.label}（{report.kind}）",
            f"v{report.version}",
            report.state,
            html.stamp(report.generated_at),
            html.tag(report.llm_state, "ok" if report.llm_state == "llm" else "warning"),
            report.fact_layer_hash[:12] + "…",
            report.path or "-",
            preview,
        )
    generate = html.form(
        "/admin/reports/generate",
        [
            Field("match_id", value=match_id or "", kind="hidden"),
            Field(
                "kind",
                "形态",
                value="full",
                kind="select",
                options=tuple((form.kind, form.label) for form in report_forms.FORMS),
            ),
            Field("trigger_game", "触发节点（小局局号，可空）", kind="number"),
            Field("completed_game", "已完成节点（局号，逗号分隔，可空）", placeholder="1,2"),
        ],
        submit="生成并发布",
    )
    return html.layout(
        "报告",
        html.raw(
            html.card("选择比赛", _match_selector(conn, match_id, action="/admin/reports"))
            + html.card(
                "已发布的报告版本",
                table.render(empty="这场比赛还没有报告"),
                note="同场同形态换版即新增版本，旧版可溯（NFR-Q-2 / FR-C4-9）。",
            )
            + html.card(
                "生成并发布",
                generate,
                note="解读层降级、成本闸、时效与来源检查都会如实写在页面上；检查不过就不上线（AC-16）。",
            )
        ),
        nav=nav_items(),
        active="/admin/reports",
        notice=_parse_notice(query),
    )


def render_report_preview(ctx: AdminContext, query: Mapping[str, str]) -> str:
    """预览一份报告：返回**报告页自己的 HTML**（后台鉴权通过才给，含付费段正文）。

    付费正文只经凭据 API 给会员；后台是运营者视角，必须看得到全文，否则「解读有没有引新事实」
    这类抽检无法进行（NFR-Q-4）。因此这里渲染的是管理员可见的完整版本，路由本身在 tailnet + 口令之后。
    """
    from danmu_intel.common.matches import get_match
    from danmu_intel.report.html import render_report_html
    from danmu_intel.report.publish import load_content

    match_id = int(query.get("match_id") or 0)
    kind = str(query.get("kind") or "")
    version = int(query.get("version") or 0)
    content = load_content(ctx.conn, match_id, kind, version)
    visibility = paywall.visibility(get_match(ctx.conn, match_id).state)
    return render_report_html(content, visibility=visibility)


# —— ⑦ 发布与回滚 ——


def render_releases(ctx: AdminContext, query: Mapping[str, str]) -> Html:
    conn = ctx.conn
    releases = release_module.list_releases(conn, limit=20)
    live = release_module.current_release(conn)
    table = html.Table(("版本", "状态", "页面数", "树指纹", "提交", "付费比赛", "时间"))
    for item in releases:
        paid = "、".join(f"#{match_id}" for match_id in item.paywalled_matches) or "无"
        table.add(
            f"v{item.version}" + ("（线上）" if live is not None and item.id == live.id else ""),
            item.state,
            len(item.pages),
            item.tree_digest[:12] + "…",
            item.deploy_ref or "-",
            paid,
            html.stamp(item.created_at),
        )
    checks = live.checks if live is not None else ()
    check_table = html.Table(("检查项", "结果", "说明"))
    for check in checks:
        item = check if isinstance(check, dict) else check.as_dict()  # 账本里存的就是字典
        check_table.add(
            item.get("label", item.get("key", "-")),
            html.tag("通过", "ok") if item.get("passed") else html.tag("未通过", "critical"),
            item.get("detail", ""),
        )
    targets = tuple(
        (str(item.version), f"v{item.version}（{item.state}）") for item in releases if item.version != (live.version if live else -1)
    )
    publish_form = html.form(
        "/admin/releases/publish",
        [Field("reason", "发布原因", value="后台手动发布")],
        submit="发布站点",
    )
    rollback_form = html.form(
        "/admin/releases/rollback",
        [Field("to", "回滚到", value=targets[0][0] if targets else "", kind="select", options=targets)],
        submit="回滚",
        danger=True,
    )
    return html.layout(
        "发布与回滚",
        html.raw(
            html.card(
                f"线上版本：{('v' + str(live.version)) if live else '还没有发布过'}",
                check_table.render(empty="还没有发布批次（没有检查结果）"),
                note="7 项检查：段完整、解读标注、来源可达、结构稳定、付费墙、跨页引用、无历史模板（设计 §11.3）。",
            )
            + html.card("发布批次账本", table.render(empty="还没有发布过任何批次"))
            + html.card("手动发布 / 秒级回滚", html.raw(f"{publish_form}{rollback_form}"),
                        note="发布走与 CLI 同一套闭环（检查不过一个条目都不换）；回滚是 Vercel 即时回滚 + git revert 跟进。")
        ),
        nav=nav_items(),
        active="/admin/releases",
        notice=_parse_notice(query),
    )


# —— ⑧ 会员与订单 ——


def render_members(ctx: AdminContext, query: Mapping[str, str]) -> Html:
    conn = ctx.conn
    member_list = members_module.list_members(conn, limit=100)
    member_rows = html.Table(("会员", "联系账号", "档位", "状态", "到期", "操作"))
    for member in member_list:
        member_rows.add(
            f"#{member.id}",
            member.contact,
            member.tier,
            html.tag(member.status, "ok" if member.status in ("active", "grace") else "warning"),
            html.stamp(member.expires_at),
            html.form(
                "/admin/members/revoke",
                [
                    Field("member_id", value=member.id, kind="hidden"),
                    Field("reason", "撤权理由（必填）"),
                ],
                submit="撤权",
                danger=True,
            ),
        )
    order_rows = html.Table(("订单", "状态", "档位", "网络", "应付", "差额", "会员", "到期"))
    order_list = orders_module.list_orders(conn, limit=100)
    members_by_id = {member.id: member for member in member_list}
    for order in order_list:
        member = members_by_id.get(order.member_id)
        order_rows.add(
            order.public_ref,
            html.tag(order.status, "ok" if order.status == "paid" else "warning"),
            order.tier,
            order.network,
            order.amount_display,
            order.shortage_display if order.status == "short" else "-",
            member.contact if member is not None else f"#{order.member_id}",
            html.stamp(order.expires_at),
        )
    grant_form = html.form(
        "/admin/members/grant",
        [
            Field("order_ref", "订单引用", placeholder="DM…", required=True),
            Field("tx_ref", "交易凭证", placeholder="0x… / 签名", required=True),
            Field("units", "入账金额（最小单位，可空 = 足额）", kind="number"),
            Field("reason", "补开通理由（必填）", required=True),
        ],
        submit="人工补开通",
    )
    return html.layout(
        "会员与订单",
        html.raw(
            html.card(
                "会员",
                member_rows.render(empty="还没有会员"),
                note="状态机：pending → active → grace（宽限）→ expired；revoked = 撤权（凭据随即失效）。",
            )
            + html.card("订单", order_rows.render(empty="还没有订单"))
            + html.card("人工补开通（凭交易凭证 + 必填理由）", grant_form,
                        note="补开通先把入账记进 `order_payments`（幂等键 = 交易凭证），再走与自动开通同一条路径。")
            + html.card(
                "到期降级",
                html.form("/admin/members/sweep", [], submit="执行一次到期降级"),
                note="active → grace → expired；不跑也不影响数字（宽限期内照旧可访问）。",
            )
        ),
        nav=nav_items(),
        active="/admin/members",
        notice=_parse_notice(query),
    )


# —— ⑨ 通知与告警 ——


def render_notifications(ctx: AdminContext, query: Mapping[str, str]) -> Html:
    conn = ctx.conn
    limit = _limit(query, default=50)
    events = recent_notifications(conn, limit=limit)
    by_state: dict[str, int] = {}
    for event in events:
        by_state[event.state] = by_state.get(event.state, 0) + 1
    rows = html.Table(("#", "类型", "级别", "状态", "创建", "内容"))
    for event in events:
        rows.add(
            event.id,
            event.kind,
            html.severity_tag(event.severity),
            html.tag(event.state, "warning" if event.state == "pending" else "ok"),
            html.stamp(event.created_at),
            html.json_block(event.payload),
        )
    kinds = html.Table(("类型", "待投递条数"))
    for kind, count in sorted(
        (kind, _count(conn, "SELECT COUNT(*) AS n FROM notifications WHERE kind=? AND state='pending'", (kind,)))
        for kind in _distinct(conn, "SELECT DISTINCT kind FROM notifications")
    ):
        kinds.add(kind, count)
    alert_rows = html.Table(("告警", "最近发生", "次数", "状态", "最后送达", "恢复"))
    for alert in list_alerts(conn, limit=limit):
        alert_rows.add(
            alert.kind,
            html.stamp(alert.last_seen),
            alert.count,
            html.tag("告警中", "critical")
            if alert.state == suppression.FIRING
            else html.tag("已恢复", "ok"),
            html.stamp(alert.last_sent_at),
            html.stamp(alert.resolved_at),
        )
    return html.layout(
        "通知与告警",
        html.raw(
            html.card(
                "队列概况",
                html.stats(
                    [
                        ("待投递", by_state.get("pending", 0)),
                        ("已送达", by_state.get("delivered", 0)),
                        ("被冷却压住", by_state.get("suppressed", 0)),
                        ("过期销毁", by_state.get("dropped_expired", 0)),
                        ("重试用尽", by_state.get("failed", 0)),
                    ]
                ),
                note="投递由 `danmu-intel notify` 负责（5 分钟闸门：超时销毁不补发）；本页只读队列"
                "与告警台账，不改投递状态。",
            )
            + html.card("按类型", kinds.render(empty="队列是空的"))
            + html.card(
                "告警台账",
                alert_rows.render(empty="还没有发生过任何告警"),
                note="同一件事（同 kind + 同一场比赛/房间/供应商/订单）在冷却期内只发一次；"
                "条件消失时发一条恢复通知（ADR-0019）。",
            )
            + html.card("最近事件", rows.render(empty="还没有任何事件"))
        ),
        nav=nav_items(),
        active="/admin/notifications",
    )


def _distinct(conn: sqlite3.Connection, sql: str) -> list[str]:
    return [row[0] for row in conn.execute(sql).fetchall()]


def _limit(query: Mapping[str, str], *, default: int, maximum: int = 500) -> int:
    try:
        value = int(query.get("limit") or default)
    except ValueError:
        return default
    return max(1, min(maximum, value))


# —— ⑩ 配置 ——


def render_config(ctx: AdminContext, query: Mapping[str, str]) -> Html:
    conn = ctx.conn
    stats = load_stats_config(conn)
    billing = pricing.load_billing_config(conn)
    version = config_store.latest(conn)

    stats_fields = _stats_fields(stats)
    billing_form = html.form(
        "/admin/config/billing",
        [
            Field("tiers", "档位（JSON 数组）", kind="textarea", rows=6,
                  placeholder='[{"key":"standard","label":"标准档","amount_units":5000000,"days":30}]'),
            Field("order_ttl_minutes", "订单时效（分钟）", value=billing.order_ttl_ms / 60000, kind="number"),
            Field("grace_hours", "宽限期（小时）", value=billing.grace_ms / 3_600_000, kind="number"),
            Field("polygon_xpub", "Polygon xpub（watch-only）", value=billing.polygon_xpub),
            Field("solana_address", "Solana 收款地址", value=billing.solana_address),
            Field("api_base", "订阅页 API 基址", value=billing.api_base),
        ],
        submit="保存价格与收款配置",
    )
    budgets = html.Table(("形态", "触发", "时限", "段数"))
    for form in report_forms.FORMS:
        budgets.add(form.label, form.trigger, html.duration(form.deadline_ms), len(form.segments))
    gates = html.Table(("闸门", "阈值", "越界后果"))
    gates.add("单场解读成本", f"¥{llm_cost.MATCH_LIMIT_CNY}", "立即降级为规则直出 + 报警")
    gates.add("当日解读成本", f"¥{llm_cost.DAILY_LIMIT_CNY}", "当日不再调 LLM + 报警")
    pending_kinds = html.Table(("提醒类型", "待投递条数"))
    for kind, count in sorted(
        (kind, _count(conn, "SELECT COUNT(*) AS n FROM notifications WHERE kind=? AND state='pending'", (kind,)))
        for kind in _distinct(conn, "SELECT DISTINCT kind FROM notifications")
    ):
        pending_kinds.add(kind, count)
    return html.layout(
        "配置",
        html.raw(
            html.card(
                f"配置版本 v{version.version if version else 0}",
                html.paragraphs(
                    [
                        f"最近改动：{version.updated_by} @ {html.stamp(version.updated_at)}（改了 {', '.join(version.keys)}）"
                        if version
                        else "还没有改过任何配置（当前全是需求默认值）",
                        "保存后：本进程立刻生效、别的进程 ≤60 秒（60 秒 TTL 缓存）、采集子进程按版本号重起。",
                    ]
                ),
            )
            + html.card("关键词表与门槛（统计）", html.form("/admin/config/stats", stats_fields, submit="保存统计配置"),
                        note="关键词表只作聚集现象的检索起点，不构成任何指控（需求 §6.5）。")
            + html.card("价格与收款配置", billing_form,
                        note="金额一律最小单位整数（USDT 6 位小数）；改价格不影响已生效会员的到期日。")
            + html.card("时效预算（需求 NFR-T 定死，页面只展示）", budgets.render(),
                        note="快报 2 分钟 / 完整 10 分钟 / 复盘 15 分钟是验收时限，不开放修改 —— 要改的是需求，不是这一页。")
            + html.card("LLM 成本闸（需求定死，页面只展示）", gates.render())
            + html.card("提醒档位", pending_kinds.render(empty="队列是空的"),
                        note="冷却 15 分钟、5 分钟时效闸门与通道选择属 T11；本页只列队列里的类型。")
        ),
        nav=nav_items(),
        active="/admin/config",
        notice=_parse_notice(query),
    )


def _stats_fields(stats: StatsConfig) -> list[Field]:
    """统计配置的编辑表单：关键词表用多行文本，其余数值项按当前值逐个给输入框。"""
    items = [
        Field(
            "gray_keywords",
            "关键词表（每行 `关键词=类别`；类别：cheat_suspicion | betting）",
            kind="textarea",
            rows=8,
            placeholder="假赛=cheat_suspicion\n盘口=betting",
        )
    ]
    for item in dataclass_fields(StatsConfig):
        if item.name == "gray_keywords":
            continue
        value = getattr(stats, item.name)
        items.append(
            Field(
                item.name,
                item.name,
                value=value,
                kind="number",
                step="0.01" if isinstance(value, float) else "1",
            )
        )
    return items


# —— ⑪ 审计日志 ——


def render_audit(ctx: AdminContext, query: Mapping[str, str]) -> Html:
    conn = ctx.conn
    action = (query.get("action") or "").strip() or None
    return html.layout(
        "审计日志",
        html.raw(
            html.card(
                "筛选",
                html.form(
                    "/admin/audit",
                    [
                        Field("action", "动作", value=action or "", kind="select",
                              options=(("", "全部"),) + tuple((item, item) for item in _distinct(conn, "SELECT DISTINCT action FROM audit_log"))),
                        Field("limit", "条数", value=_limit(query, default=100), kind="number"),
                    ],
                    submit="查看",
                    method="get",
                ),
            )
            + html.card(
                "全部改动（只增不改）",
                _audit_table(conn, limit=_limit(query, default=100), action=action),
                note="写操作的 actor、动作、对象与前后值都在这里；资金相关写操作同样留痕（NFR-S-5）。",
            )
        ),
        nav=nav_items(),
        active="/admin/audit",
    )


def _audit_table(conn: sqlite3.Connection, *, limit: int, action: str | None = None) -> Html:
    entries = audit.entries(conn, action=action)[-limit:]
    rows = html.Table(("#", "时间", "操作者", "动作", "对象", "详情"))
    for entry in reversed(entries):
        rows.add(entry.id, html.stamp(entry.ts), entry.actor, entry.action, entry.target or "-",
                 html.json_block(entry.detail))
    return rows.render(empty="还没有任何改动")


# —— ⑫ 成本与额度 ——


def render_cost(ctx: AdminContext, query: Mapping[str, str]) -> Html:
    conn, now = ctx.conn, ctx.now()
    spend = llm_ledger.spend(conn, _latest_match_id(conn) or 0, now_ms=now)
    outcomes = llm_ledger.recent_outcomes(conn, limit=20)
    calls = html.Table(("比赛", "段", "模型", "提示词", "成本 ¥", "耗时", "结果", "原因", "时间"))
    for call in llm_ledger.calls_for_match(conn, _latest_match_id(conn) or 0)[-30:]:
        calls.add(
            call.match_id if call.match_id is not None else "-",
            call.segment_no if call.segment_no is not None else "-",
            call.model,
            call.prompt_version,
            f"{call.cost_cny:.4f}",
            f"{call.latency_ms} ms",
            html.tag(call.outcome, "ok" if call.outcome == llm_ledger.OUTCOME_OK else "warning"),
            call.reason or "-",
            html.stamp(call.created_at),
        )
    quota = html.Table(("供应商", "当日", "当月", "上限", "阈值", "最近失败"))
    for provider in PROVIDERS:
        usage = QuotaLedger(conn, provider).usage()
        limit = usage.limit
        quota.add(
            provider,
            f"{usage.day_used} {limit.unit_label}",
            f"{usage.month_used} {limit.unit_label}",
            f"{limit.cap} {limit.unit_label}（按{limit.window_label}）",
            html.tag("已越 80% 阈值", "critical") if usage.over_threshold else "正常",
            usage.last_error or "-",
        )
    marks = html.Table(("网络", "范围", "游标", "更新时间"))
    for mark in chain_cursor.rows(conn):
        marks.add(mark.network, mark.scope, mark.cursor, html.stamp(mark.updated_at))
    return html.layout(
        "成本与额度",
        html.raw(
            html.card(
                "解读层成本",
                html.stats(
                    [
                        ("本场累计 ¥", f"{spend.match_cny:.4f}"),
                        ("今日累计 ¥", f"{spend.day_cny:.4f}"),
                        ("硬闸（单场/当日）", f"¥{llm_cost.MATCH_LIMIT_CNY} / ¥{llm_cost.DAILY_LIMIT_CNY}"),
                        ("连续失败", llm_ledger.consecutive_failures(outcomes)),
                    ]
                ),
                note="连续失败达阈值即全局降级为规则直出（降级不静默，报告页与命令输出都会写明原因）。",
            )
            + html.card("最近的 LLM 调用", calls.render(empty="这场比赛还没有 LLM 调用记账"))
            + html.card("链上供应商额度（免费额度内）", quota.render(), note=">80% 或撞限速一律写 critical 报警（FR-C6-11）。")
            + html.card("监听游标（扫到哪了）", marks.render(empty="还没有扫过任何地址"))
        ),
        nav=nav_items(),
        active="/admin/cost",
    )


#: **12 个页面**（设计 §14.1 清单逐条对应：概览 / 比赛管理 / 房间与数据源 / 切片复核 /
#: 灰信号评审 / 报告 / 发布与回滚 / 会员与订单 / 通知与告警 / 配置 / 审计日志 / 成本与额度）。
PAGES: tuple[AdminPage, ...] = (
    AdminPage("dashboard", "/admin", "概览", render_dashboard),
    AdminPage("matches", "/admin/matches", "比赛管理", render_matches),
    AdminPage("rooms", "/admin/rooms", "房间与数据源", render_rooms),
    AdminPage("slices", "/admin/slices", "切片复核", render_slices),
    AdminPage("gray", "/admin/gray", "灰信号评审", render_gray),
    AdminPage("reports", "/admin/reports", "报告", render_reports),
    AdminPage("releases", "/admin/releases", "发布与回滚", render_releases),
    AdminPage("members", "/admin/members", "会员与订单", render_members),
    AdminPage("notifications", "/admin/notifications", "通知与告警", render_notifications),
    AdminPage("config", "/admin/config", "配置", render_config),
    AdminPage("audit", "/admin/audit", "审计日志", render_audit),
    AdminPage("cost", "/admin/cost", "成本与额度", render_cost),
)


def _parse_notice(query: Mapping[str, str]) -> tuple[str, str] | None:
    """写操作后 303 回来带的提示（`?ok=` / `?err=`）：成功绿、失败红。"""
    ok = (query.get("ok") or "").strip()
    if ok:
        return ("ok", ok)
    err = (query.get("err") or "").strip()
    if err:
        return ("err", err)
    return None
