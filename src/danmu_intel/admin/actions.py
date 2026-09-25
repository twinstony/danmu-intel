"""后台的写操作：每一条都走「领域函数 + 审计 + 303 回页面」（FR-C8-4）。

三条纪律：

1. **只从 POST 进来**：页面（GET）永远只读；写操作集中在 `ACTIONS` 里，一个动作一个函数。
2. **全部留痕**：动作本身调用已经带审计的领域函数（切片修正、会员开通/撤权、配置保存、
   发布/回滚），或自己补一条 `audit_log`（比赛/房间的增删改、报告发布、登录）。审计里必有
   `actor`（后台只有管理员一类角色，就是 `admin`）。
3. **失败要说人话**：领域函数抛出的异常统一转成 `ActionError`，页面用红条显示原因；
   宁可不做，也不静默吞掉（NFR-S-5：资金操作必须可审计）。

`ActionContext.release()` 给发布/回滚提供上下文（真实 git + Vercel，或测试注入的本地模式），
因此「点按钮发布」和 `danmu-intel publish` 是同一条闭环。
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, fields as dataclass_fields
from pathlib import Path
from typing import Any, Callable, Mapping

from danmu_intel.billing import members as members_module
from danmu_intel.billing import pricing, settle
from danmu_intel.common import audit, config_store, gray_review, rooms
from danmu_intel.common.config import GRAY_CATEGORY_LABELS, StatsConfig, load_stats_config, save_stats_config
from danmu_intel.common.matches import create_match, delete_match, get_match, set_match_state
from danmu_intel.publish import release as release_module
from danmu_intel.publish.release import ReleaseRefused
from danmu_intel.publish.vercel import VercelError
from danmu_intel.report.forms import FORM_KINDS
from danmu_intel.report.publish import PublishRefused
from danmu_intel.slice.manual import add_manual_slice

#: 后台只有管理员一类角色（需求 Q-8），因此审计里的操作者就是这一个名字。
ADMIN_ACTOR = "admin"

ACTION_MATCH_ADD = "match.add"
ACTION_MATCH_STATE = "match.state"
ACTION_REPORT_PUBLISH = "report.publish"
ACTION_ADMIN_LOGIN = "admin.login"
ACTION_ADMIN_LOGIN_FAILED = "admin.login_failed"
ACTION_ADMIN_LOGOUT = "admin.logout"


class ActionError(RuntimeError):
    """一个写操作没做成（消息直接显示在页面上，必须说清原因）。"""

    def __init__(self, message: str, *, redirect: str = "/admin") -> None:
        super().__init__(message)
        self.redirect = redirect


@dataclass(frozen=True, slots=True)
class ActionContext:
    """一次写操作要用的东西：库、数据目录、发布上下文（测试注入本地模式）。"""

    conn: sqlite3.Connection
    data_root: Path
    release: Callable[[], release_module.ReleaseContext]
    clock: Callable[[], int] = lambda: int(time.time() * 1000)


@dataclass(frozen=True, slots=True)
class Action:
    """一个写操作：`key` 就是 POST 的路径（`/admin/<key>`），`page` 是回哪一页。"""

    key: str
    page: str
    run: Callable[[ActionContext, Mapping[str, str]], str]


# —— 表单取值 ——


def _text(form: Mapping[str, str], name: str, *, required: bool = True, default: str = "") -> str:
    value = (form.get(name) or default).strip()
    if required and not value:
        raise ActionError(f"{name} 不能为空")
    return value


def _int(form: Mapping[str, str], name: str, *, required: bool = True) -> int | None:
    raw = (form.get(name) or "").strip()
    if not raw:
        if required:
            raise ActionError(f"{name} 不能为空")
        return None
    try:
        return int(float(raw))
    except ValueError:
        raise ActionError(f"{name} 应该是数字，收到：{raw}") from None


def _float(form: Mapping[str, str], name: str) -> float | None:
    raw = (form.get(name) or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        raise ActionError(f"{name} 应该是数字，收到：{raw}") from None


def _json_field(form: Mapping[str, str], name: str, *, required: bool = False) -> Any:
    raw = (form.get(name) or "").strip()
    if not raw:
        if required:
            raise ActionError(f"{name} 不能为空")
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ActionError(f"{name} 不是合法 JSON：{exc.msg}") from None


def _match_id(form: Mapping[str, str]) -> int:
    value = _int(form, "match_id")
    assert value is not None
    return value


# —— 比赛 ——


def match_add(ctx: ActionContext, form: Mapping[str, str]) -> str:
    match_id = create_match(
        ctx.conn,
        league=_text(form, "league"),
        team_a=_text(form, "team_a"),
        team_b=_text(form, "team_b"),
        state=_text(form, "state", default="scheduled"),
        stage=_text(form, "stage", required=False) or None,
        scheduled_at=_int(form, "scheduled_at", required=False),
        official_result=_json_field(form, "official_result"),
    )
    audit.record(
        ctx.conn,
        actor=ADMIN_ACTOR,
        action=ACTION_MATCH_ADD,
        target=str(match_id),
        detail={"league": form.get("league"), "team_a": form.get("team_a"), "team_b": form.get("team_b")},
        ts=ctx.clock(),
    )
    return f"已登记比赛 #{match_id}"


def match_state(ctx: ActionContext, form: Mapping[str, str]) -> str:
    conn = ctx.conn
    match_id = _match_id(form)
    before = get_match(conn, match_id)
    state = _text(form, "state")
    official_result = _json_field(form, "official_result")
    ended_at = ctx.clock() if state == "ended" and before.state != "ended" else None
    match = set_match_state(
        conn, match_id, state=state, ended_at=ended_at, official_result=official_result
    )
    audit.record(
        conn,
        actor=ADMIN_ACTOR,
        action=ACTION_MATCH_STATE,
        target=str(match_id),
        detail={"from": before.state, "to": match.state},
        ts=ctx.clock(),
    )
    message = f"比赛 #{match_id} 状态：{before.state} → {match.state}"
    if match.state == "ended" and before.state != "ended":
        message += "；" + _republish_for_state_machine(ctx, match_id)
    return message


def _republish_for_state_machine(ctx: ActionContext, match_id: int) -> str:
    """比赛转 `ended` → 自动再发布公开版（FR-C5-10，与 CLI 走同一条闭环）。"""
    try:
        outcomes = release_module.sync_ended(ctx.conn, ctx=ctx.release())
    except Exception as exc:  # 发布要碰 git 与 Vercel（外部世界）：失败只说清原因，不回退状态写入
        return f"自动再发布没做成（线上仍是旧版，状态写入是事实不因此回退）：{exc}"
    if not outcomes:
        return "没有需要再发布的页面"
    return "；".join(
        f"已自动再发布公开版 v{outcome.version}（{len(outcome.release.pages)} 个页面）"
        for outcome in outcomes
    )


def match_delete(ctx: ActionContext, form: Mapping[str, str]) -> str:
    deleted = delete_match(ctx.conn, _match_id(form), actor=ADMIN_ACTOR, ts=ctx.clock())
    return f"已删除比赛 #{deleted.id}（{deleted.team_a} vs {deleted.team_b}）"


# —— 房间 ——


def room_add(ctx: ActionContext, form: Mapping[str, str]) -> str:
    room = rooms.add_room(
        ctx.conn,
        platform=_text(form, "platform"),
        room_id=_text(form, "room_id"),
        url=_text(form, "url"),
        streamer=_text(form, "streamer", required=False) or None,
        actor=ADMIN_ACTOR,
        ts=ctx.clock(),
    )
    return f"已登记直播间 {room.label}"


def room_update(ctx: ActionContext, form: Mapping[str, str]) -> str:
    row_id = _int(form, "room_row_id")
    assert row_id is not None
    room = rooms.update_room(
        ctx.conn,
        row_id,
        url=_text(form, "url", required=False) or None,
        streamer=_text(form, "streamer", required=False) or None,
        actor=ADMIN_ACTOR,
        ts=ctx.clock(),
    )
    return f"已更新直播间 {room.label}"


def room_delete(ctx: ActionContext, form: Mapping[str, str]) -> str:
    row_id = _int(form, "room_row_id")
    assert row_id is not None
    room = rooms.delete_room(ctx.conn, row_id, actor=ADMIN_ACTOR, ts=ctx.clock())
    return f"已删除直播间 {room.label}"


# —— 切片 ——


def slice_override(ctx: ActionContext, form: Mapping[str, str]) -> str:
    match_id = _match_id(form)
    game_no = _int(form, "game_no")
    start_ms = _int(form, "start_ms")
    end_ms = _int(form, "end_ms")
    assert game_no is not None and start_ms is not None and end_ms is not None
    reason = _text(form, "reason")
    existed = (
        ctx.conn.execute(
            "SELECT 1 FROM slices WHERE match_id=? AND game_no=?", (match_id, game_no)
        ).fetchone()
        is not None
    )
    add_manual_slice(
        ctx.conn,
        match_id=match_id,
        game_no=game_no,
        start_ms=start_ms,
        end_ms=end_ms,
        override_by=ADMIN_ACTOR,
        override_reason=reason,
        override_at=ctx.clock(),
    )
    if not existed:
        # 新建没有「前值」可比（`add_manual_slice` 只在覆盖时留痕），这里补一条新建的痕
        audit.record(
            ctx.conn,
            actor=ADMIN_ACTOR,
            action=audit.SLICE_MANUAL,
            target=f"match:{match_id}/game:{game_no}",
            detail={"start_ms": start_ms, "end_ms": end_ms, "boundary_source": "manual", "reason": reason},
            ts=ctx.clock(),
        )
        return f"已新建切片：比赛 #{match_id} G{game_no}（操作者与理由已留痕）"
    return f"已写入人工修正：比赛 #{match_id} G{game_no}（理由已留痕）"


# —— 灰信号 ——


def review_gray_signal(ctx: ActionContext, form: Mapping[str, str]) -> str:
    """升级或作废一条灰信号（模块名也是 `gray_review`，因此函数换个更明确的名字）。"""
    signal_id = _int(form, "signal_id")
    assert signal_id is not None
    signal = gray_review.review(
        ctx.conn,
        signal_id,
        action=_text(form, "action"),
        reason=_text(form, "reason"),
        actor=ADMIN_ACTOR,
        ts=ctx.clock(),
    )
    return f"灰信号「{signal.keyword}」已{_review_label(signal.status)}"


def _review_label(status: str) -> str:
    return {"escalated": "升级（进人工复核）", "discarded": "作废（不进报告）"}.get(status, status)


# —— 报告 ——


def report_generate(ctx: ActionContext, form: Mapping[str, str]) -> str:
    from danmu_intel.pipeline import generate_and_publish
    from danmu_intel.report.llm.interpreter import interpreter_for

    match_id = _match_id(form)
    kind = _text(form, "kind")
    if kind not in FORM_KINDS:
        raise ActionError(f"未知的报告形态：{kind}（允许：{','.join(FORM_KINDS)}）")
    completed_raw = _text(form, "completed_game", required=False)
    completed = tuple(sorted({int(item) for item in completed_raw.replace("，", ",").split(",") if item.strip()})) or None
    try:
        result = generate_and_publish(
            ctx.conn,
            match_id,
            kind=kind,
            completed_games=completed,
            trigger_game_no=_int(form, "trigger_game", required=False),
            interpreter=interpreter_for(ctx.conn),
            data_root=ctx.data_root,
        )
    except PublishRefused as exc:
        detail = "；".join(f"{item.label}：{item.detail}" for item in exc.failures)
        raise ActionError(f"报告没通过发布检查，没有上线：{detail}") from None
    audit.record(
        ctx.conn,
        actor=ADMIN_ACTOR,
        action=ACTION_REPORT_PUBLISH,
        target=f"{match_id}:{kind}:v{result.version}",
        detail={"path": result.path, "llm_state": result.content.llm_state, "visibility": result.visibility},
        ts=ctx.clock(),
    )
    return f"已发布{kind} v{result.version}（{result.path}）"


# —— 发布与回滚 ——


def release_publish(ctx: ActionContext, form: Mapping[str, str]) -> str:
    reason = _text(form, "reason", default="后台手动发布")
    try:
        outcome = release_module.publish_site(ctx.conn, ctx=ctx.release(), reason=reason)
    except ReleaseRefused as exc:
        detail = "；".join(f"{item.label}：{item.detail}" for item in exc.failures)
        raise ActionError(f"发布检查未通过，一个条目都没换（线上保持上一版）：{detail}") from None
    except VercelError as exc:
        raise ActionError(f"发布没做成：{exc}") from None
    if not outcome.changed:
        return f"没有变化：线上仍是 v{outcome.version}"
    return f"已发布 v{outcome.version}（{len(outcome.release.pages)} 个页面）"


def release_rollback(ctx: ActionContext, form: Mapping[str, str]) -> str:
    to = _int(form, "to", required=False)
    try:
        result = release_module.rollback(ctx.conn, ctx=ctx.release(), to_version=to)
    except VercelError as exc:
        raise ActionError(f"回滚没做成：{exc}") from None
    aligned = "，账本已用 git revert 对齐" if result.aligned else "；账本未对齐（已报警，请手工 git revert）"
    return f"已回滚：v{result.previous.version} → v{result.release.version}{aligned}"


# —— 会员与订单 ——


def member_grant(ctx: ActionContext, form: Mapping[str, str]) -> str:
    order, opened = settle.manual_payment(
        ctx.conn,
        order_ref=_text(form, "order_ref"),
        tx_ref=_text(form, "tx_ref"),
        units=_int(form, "units", required=False),
        actor=ADMIN_ACTOR,
        reason=_text(form, "reason"),
        now=ctx.clock(),
    )
    if order.status == "paid":
        return (
            f"订单 {order.public_ref} 已开通（{order.tier}）"
            if opened
            else f"订单 {order.public_ref} 早已开通（幂等：没有重复开通）"
        )
    return f"订单 {order.public_ref} 仍差 {order.shortage_display}（这笔入账已记账）"


def member_revoke(ctx: ActionContext, form: Mapping[str, str]) -> str:
    member_id = _int(form, "member_id")
    assert member_id is not None
    member = members_module.revoke(
        ctx.conn, member_id=member_id, actor=ADMIN_ACTOR, reason=_text(form, "reason"), now=ctx.clock()
    )
    return f"会员 #{member.id} 已撤权（凭据随即失效）"


def member_sweep(ctx: ActionContext, form: Mapping[str, str]) -> str:
    changed = members_module.sweep(ctx.conn, now=ctx.clock())
    if not changed:
        return "没有需要降级的会员"
    return "；".join(f"会员 #{item.id} → {item.status}" for item in changed)


# —— 配置 ——


def config_stats(ctx: ActionContext, form: Mapping[str, str]) -> str:
    current = load_stats_config(ctx.conn)
    changes: dict[str, Any] = {}
    for item in dataclass_fields(StatsConfig):
        if item.name == "gray_keywords":
            raw = form.get("gray_keywords")
            if raw is not None:
                changes[item.name] = _parse_keywords(raw)
            continue
        raw = form.get(item.name)
        if raw is None or raw.strip() == "":
            continue
        value = getattr(current, item.name)
        if isinstance(value, float):
            changes[item.name] = _float(form, item.name)
        else:
            changes[item.name] = _int(form, item.name)
    if not changes:
        raise ActionError("没有要保存的统计配置项")
    updated = save_stats_config(ctx.conn, actor=ADMIN_ACTOR, changes=changes, ts=ctx.clock())
    version = _config_version(ctx.conn)
    return f"统计配置已保存（版本 v{version}）：命中门槛 ≥{updated.gray_min_hits} 次"


def _parse_keywords(raw: str) -> tuple[tuple[str, str], ...]:
    """关键词表：每行 `关键词=类别`（也接受空格分隔）；类别必须是已登记的类别。"""
    items: list[tuple[str, str]] = []
    for line in raw.splitlines():
        text = line.strip()
        if not text:
            continue
        keyword, _, category = text.partition("=")
        if not category:
            parts = text.split()
            if len(parts) != 2:
                raise ActionError(f"关键词行格式应为 `关键词=类别`：{text}")
            keyword, category = parts
        keyword, category = keyword.strip(), category.strip()
        if category not in GRAY_CATEGORY_LABELS:
            raise ActionError(
                f"未知的灰信号类别：{category}（允许：{','.join(GRAY_CATEGORY_LABELS)}）"
            )
        items.append((keyword, category))
    return tuple(items)


def config_billing(ctx: ActionContext, form: Mapping[str, str]) -> str:
    changes: dict[str, Any] = {}
    tiers = _json_field(form, "tiers")
    if tiers is not None:
        if not isinstance(tiers, list) or not all(isinstance(item, dict) for item in tiers):
            raise ActionError("档位应该是 JSON 数组，元素形如 {\"key\":\"standard\",\"label\":\"标准档\",\"amount_units\":5000000,\"days\":30}")
        changes["tiers"] = tiers
    minutes = _float(form, "order_ttl_minutes")
    if minutes is not None:
        changes["order_ttl_ms"] = int(minutes * 60_000)
    hours = _float(form, "grace_hours")
    if hours is not None:
        changes["grace_ms"] = int(hours * 3_600_000)
    for name in ("polygon_xpub", "solana_address", "api_base"):
        if name in form:
            changes[name] = (form.get(name) or "").strip()
    if not changes:
        raise ActionError("没有要保存的收款配置项")
    updated = pricing.save_billing_config(ctx.conn, actor=ADMIN_ACTOR, changes=changes, ts=ctx.clock())
    version = _config_version(ctx.conn)
    tiers_label = "、".join(f"{item.label}（{pricing.format_units(item.amount_units)} USDT / {item.days} 天）" for item in updated.tiers)
    return f"收款配置已保存（版本 v{version}）：{tiers_label or '未配置档位'}"


def _config_version(conn: sqlite3.Connection) -> int:
    return config_store.version(conn)


# —— 登录（鉴权事件也留痕：谁在什么时候尝试进后台）——


def log_login(ctx: ActionContext, *, ip: str, ok: bool) -> None:
    audit.record(
        ctx.conn,
        actor=ADMIN_ACTOR if ok else f"unknown@{ip}",
        action=ACTION_ADMIN_LOGIN if ok else ACTION_ADMIN_LOGIN_FAILED,
        target=ip,
        detail={"ip": ip},
        ts=ctx.clock(),
    )


def log_logout(ctx: ActionContext, *, ip: str) -> None:
    audit.record(
        ctx.conn,
        actor=ADMIN_ACTOR,
        action=ACTION_ADMIN_LOGOUT,
        target=ip,
        detail={"ip": ip},
        ts=ctx.clock(),
    )


# —— 动作清单（POST 路由用它）——


ACTIONS: tuple[Action, ...] = (
    Action("matches/add", "matches", match_add),
    Action("matches/state", "matches", match_state),
    Action("matches/delete", "matches", match_delete),
    Action("rooms/add", "rooms", room_add),
    Action("rooms/update", "rooms", room_update),
    Action("rooms/delete", "rooms", room_delete),
    Action("slices/override", "slices", slice_override),
    Action("gray/review", "gray", review_gray_signal),
    Action("reports/generate", "reports", report_generate),
    Action("releases/publish", "releases", release_publish),
    Action("releases/rollback", "releases", release_rollback),
    Action("members/grant", "members", member_grant),
    Action("members/revoke", "members", member_revoke),
    Action("members/sweep", "members", member_sweep),
    Action("config/stats", "config", config_stats),
    Action("config/billing", "config", config_billing),
)


def action_of(key: str) -> Action:
    for action in ACTIONS:
        if action.key == key:
            return action
    raise LookupError(f"未注册的后台动作：{key}")
