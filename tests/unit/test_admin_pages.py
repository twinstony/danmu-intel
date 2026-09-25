"""后台 12 个页面（设计 §14.1 / 需求 FR-C8-4、C8-5）。

三条要守住的纪律，逐条可断言：

1. **清单完整**：设计 §14.1 列的 12 条页面一个不少（清单写死在测试里，改一边就红）；
2. **页面只读**：渲染过程里不许出现任何 `INSERT/UPDATE/DELETE`（写操作都在 `actions.py`）；
3. **默认转义**：库里带 `<script>` 的字段渲染出来必须是被转义过的（浏览器不会执行它）。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Mapping

import pytest

from danmu_intel.admin import pages
from danmu_intel.billing import members, orders, pricing
from danmu_intel.collect.incidents import PROCESS_EXIT, emit as emit_incident
from danmu_intel.common import paths, rooms
from danmu_intel.common.matches import create_match
from danmu_intel.common.notifications import emit
from danmu_intel.slice.manual import add_manual_slice
from tests.unit.test_billing_xpub import ACCOUNT_XPUB

BASE = 1_790_064_000_000

#: 设计 §14.1 的页面清单（**顺序与名称照抄设计文档**）。
DESIGN_PAGES = [
    ("/admin", "概览"),
    ("/admin/matches", "比赛管理"),
    ("/admin/rooms", "房间与数据源"),
    ("/admin/slices", "切片复核"),
    ("/admin/gray", "灰信号评审"),
    ("/admin/reports", "报告"),
    ("/admin/releases", "发布与回滚"),
    ("/admin/members", "会员与订单"),
    ("/admin/notifications", "通知与告警"),
    ("/admin/config", "配置"),
    ("/admin/audit", "审计日志"),
    ("/admin/cost", "成本与额度"),
]


@pytest.fixture
def ctx(conn, data_root) -> pages.AdminContext:
    return pages.AdminContext(conn=conn, data_root=data_root, clock=lambda: BASE)


def render(key: str, ctx: pages.AdminContext, query: Mapping[str, str] | None = None) -> str:
    return str(pages.page_of(key).render(ctx, dict(query or {})))


def seed(conn, data_root) -> int:
    """一场有采集、切片、灰信号、报告、发布、会员与订单的比赛（页面才有的看）。"""
    match_id = create_match(
        conn, league="LPL", team_a="iG", team_b="LNG", state="live", official_result={"score": "1:0"}
    )
    pricing.save_billing_config(conn, actor="admin", changes={"polygon_xpub": ACCOUNT_XPUB})
    room = rooms.add_room(
        conn, platform="huya", room_id="660000", url="https://www.huya.com/660000",
        streamer="主播甲", actor="admin", ts=BASE,
    )
    cursor = conn.execute(
        "INSERT INTO room_sessions(room_id, match_id, pid, started_at, state, restart_count,"
        " reconnects, severity, last_msg_at) VALUES(?, ?, 4242, ?, 'running', 0, 2, 'info', ?)",
        (room.id, match_id, BASE, BASE + 1000),
    )
    session_id = int(cursor.lastrowid)
    conn.execute(
        "INSERT INTO danmu_segments(room_session_id, rel_path, sha256, first_ts, last_ts,"
        " msg_count, sealed_at) VALUES(?, 'raw/huya/2026-09-22/660000-16.jsonl', 'a' * 64, ?, ?, 3, ?)",
        (session_id, BASE, BASE + 2000, BASE + 3000),
    )
    add_manual_slice(conn, match_id=match_id, game_no=1, start_ms=BASE, end_ms=BASE + 60_000)
    add_manual_slice(
        conn, match_id=match_id, game_no=2, start_ms=BASE + 60_000, end_ms=BASE + 120_000,
        override_by="admin", override_reason="对齐官方开赛时间",
    )
    conn.execute(
        "INSERT INTO gray_signals(match_id, category, keyword, hit_count, distinct_users,"
        " window_count, samples_json, status, reason, created_at, evaluated_at)"
        " VALUES(?, 'betting', '盘口', 9, 4, 3, ?, 'candidate', NULL, ?, NULL)",
        (match_id, json.dumps([{"ts": BASE, "text": "这盘口有问题", "rel_path": "raw/x.jsonl", "line_no": 3}]), BASE),
    )
    conn.execute(
        "INSERT INTO reports(match_id, game_no, kind, version, generated_at, state, content_json,"
        " fact_layer_hash, llm_state, path, timing_json) VALUES(?, NULL, 'full', 1, ?, 'published',"
        " '{}', 'f' || printf('%063d', 0), 'llm', 'matches/1/full.html', '{}')",
        (match_id, BASE),
    )
    conn.execute(
        "INSERT INTO releases(version, tree_digest, state, deployment_id, deploy_ref,"
        " paywalled_matches, pages_json, checks_json, created_at)"
        " VALUES(1, 'deadbeef' * 8, 'live', 'dpl_1', 'commit1', ?, ?, ?, ?)",
        (
            json.dumps([match_id]),
            json.dumps(["index.html"]),
            json.dumps([{"key": "k", "label": "来源可达", "passed": True, "detail": "全部命中", "blocking": True}]),
            BASE,
        ),
    )
    conn.execute(
        "INSERT INTO llm_calls(match_id, segment_no, model, prompt_version, prompt_tokens,"
        " completion_tokens, cost_cny, latency_ms, outcome, created_at)"
        " VALUES(?, 1, 'deepseek-v4-flash', 'v1', 100, 50, 0.0123, 800, 'ok', ?)",
        (match_id, BASE),
    )
    conn.execute(
        "INSERT INTO quota_usage(provider, day, calls, credits, updated_at) VALUES('polygonscan', '2026-09-22', 1234, 0, ?)",
        (BASE,),
    )
    emit_incident(conn, PROCESS_EXIT, severity="warning", platform="huya", room_id="660000",
                  match_id=match_id, detail={"exit_code": 1})
    emit(conn, "llm.cost_gate", severity="warning", payload={"match_id": match_id})
    member = members.get_or_create_member(conn, platform="telegram", username="tester", tier="standard")
    orders.create_order(conn, platform="telegram", username="tester", tier="standard", network="polygon",
                        now=BASE, config=pricing.load_billing_config(conn))
    members.grant(conn, member_id=member.id, tier="standard", tx_ref="0xdead", now=BASE, actor="chain")
    rooms.add_room(conn, platform="soop", room_id="afchall", url="https://play.sooplive.com/afchall",
                   actor="admin")
    return match_id


def test_page_registry_matches_design_14_1():
    """设计 §14.1 的清单逐条对上：路径、标题、渲染函数一个不少。"""
    assert [(page.path, page.title) for page in pages.PAGES] == DESIGN_PAGES
    for page in pages.PAGES:
        assert callable(page.render) and page.key


def test_nav_lists_every_page(conn, ctx):
    rendered = render("dashboard", ctx)
    for path, title in DESIGN_PAGES:
        assert f'href="{path}"' in rendered, f"{title} 没有出现在导航里"


def test_every_page_renders_even_with_an_empty_db(conn, ctx):
    """空库也要能打开每一页（页面上写「暂无数据」，而不是抛异常）。"""
    for page in pages.PAGES:
        rendered = str(page.render(ctx, {}))
        assert rendered.startswith("<!doctype html>")
        assert "弹幕情报库后台" in rendered
        assert f"<title>{page.title}｜" in rendered


def test_dashboard_shows_counts_health_and_todos(conn, ctx, data_root):
    seed(conn, data_root)
    rendered = render("dashboard", ctx)
    assert "采集健康" in rendered
    assert "huya/660000" in rendered and "主播甲" in rendered
    assert "待投递事件" in rendered
    # 页面上显示的版本号必须就是库里的那个：seed 改过收款配置、也登记过房间 → 早已超过 1 版
    from danmu_intel.common import config_store

    shown = config_store.version(conn)
    assert shown >= 2, "改配置与登记房间都要递增版本号"
    assert f"配置版本：v{shown}" in rendered
    assert "线上发布：v1" in rendered


def test_matches_page_offers_crud_and_state_machine(conn, ctx):
    match_id = create_match(conn, league="LPL", team_a="iG", team_b="LNG", state="live")
    rendered = render("matches", ctx)
    assert "iG vs LNG" in rendered
    assert 'action="/admin/matches/state"' in rendered
    assert 'action="/admin/matches/add"' in rendered
    assert 'action="/admin/matches/delete"' in rendered
    assert 'value="between_games"' in rendered, "状态机的每个状态都要能选"
    assert str(match_id) in rendered


def test_rooms_page_shows_health_and_forms(conn, ctx, data_root):
    seed(conn, data_root)
    rendered = render("rooms", ctx)
    assert "huya/660000" in rendered and "play.sooplive.com" in rendered
    assert 'action="/admin/rooms/add"' in rendered
    assert 'action="/admin/rooms/update"' in rendered
    assert 'action="/admin/rooms/delete"' in rendered
    assert "no_stream" in rendered, "数据源可用性要在页面上说清有哪些状态"


def test_slices_page_shows_boundaries_and_requires_reason(conn, ctx):
    seed(conn, ctx.conn)
    rendered = render("slices", ctx)
    assert "对齐官方开赛时间" in rendered
    assert 'action="/admin/slices/override"' in rendered
    assert "修正理由（必填）" in rendered


def test_gray_page_shows_samples_without_identity(conn, ctx):
    seed(conn, ctx.conn)
    rendered = render("gray", ctx)
    assert "这盘口有问题" in rendered
    assert "raw/x.jsonl 第 3 行" in rendered
    assert 'action="/admin/gray/review"' in rendered
    assert "不指控、不点名" in rendered


def test_reports_page_links_preview_and_generation(conn, ctx):
    match_id = seed(conn, ctx.conn)
    rendered = render("reports", ctx)
    # 链接里的 `&` 会被转义（`&amp;`）—— 这正是「默认转义」在起作用
    assert f"/admin/reports/preview?match_id={match_id}&amp;kind=full&amp;version=1" in rendered
    assert 'action="/admin/reports/generate"' in rendered
    assert "v1" in rendered


def test_report_preview_reports_a_missing_version(conn, ctx):
    """预览读的是 `reports` 账本；版本不存在就如实报「未找到」，不编一个空页。"""
    match_id = seed(conn, ctx.conn)
    with pytest.raises(LookupError):
        pages.render_report_preview(ctx, {"match_id": str(match_id), "kind": "full", "version": 9})


def test_releases_page_shows_checks_and_actions(conn, ctx):
    seed(conn, ctx.conn)
    rendered = render("releases", ctx)
    assert "来源可达" in rendered and "全部命中" in rendered
    assert "线上版本：v1" in rendered
    assert 'action="/admin/releases/publish"' in rendered
    assert 'action="/admin/releases/rollback"' in rendered


def test_members_page_shows_members_orders_and_actions(conn, ctx):
    seed(conn, ctx.conn)
    rendered = render("members", ctx)
    assert "tester" in rendered and "DM" in rendered
    assert 'action="/admin/members/grant"' in rendered
    assert 'action="/admin/members/revoke"' in rendered
    assert 'action="/admin/members/sweep"' in rendered


def test_notifications_page_shows_queue(conn, ctx):
    seed(conn, ctx.conn)
    rendered = render("notifications", ctx)
    assert "process_exit" in rendered and "llm.cost_gate" in rendered
    assert "属 T11" in rendered, "投递属 T11 这件事必须在页面上说清"


def test_config_page_shows_every_section(conn, ctx):
    conn_ = ctx.conn
    from danmu_intel.common.config import save_stats_config

    save_stats_config(conn_, actor="admin", changes={"gray_min_hits": 6}, ts=BASE)
    rendered = render("config", ctx)
    assert "配置版本 v1" in rendered
    assert "gray_keywords" in rendered and "gray_min_hits" in rendered
    assert 'action="/admin/config/stats"' in rendered
    assert 'action="/admin/config/billing"' in rendered
    assert "时效预算" in rendered and "提醒档位" in rendered and "成本闸" in rendered


def test_audit_page_lists_changes_and_filters(conn, ctx):
    seed(conn, ctx.conn)
    rendered = render("audit", ctx)
    assert "room.add" in rendered and "admin" in rendered
    filtered = render("audit", ctx, {"action": "room.add", "limit": "5"})
    assert "room.add" in filtered
    assert 'action="/admin/audit"' in rendered


def test_cost_page_shows_llm_and_quota(conn, ctx):
    seed(conn, ctx.conn)
    rendered = render("cost", ctx)
    assert "deepseek-v4-flash" in rendered
    assert "polygonscan" in rendered and "1234" in rendered
    assert "硬闸" in rendered


# —— 三条纪律 ——


class WriteGuard(sqlite3.Connection):
    """会拦下写语句的连接：页面渲染不许写库（渲染路径必须是只读的）。"""

    def execute(self, sql, parameters=(), /):  # type: ignore[override]
        head = sql.strip().split(maxsplit=1)[0].upper() if sql.strip() else ""
        if head in {"INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER", "REPLACE"}:
            raise AssertionError(f"后台页面渲染时写库了：{sql[:60]}")
        return super().execute(sql, parameters)


def test_pages_never_write_to_the_database(conn, data_root):
    """写操作一律走 POST + `actions.py`；只读页面在写语句上会当场炸。"""
    seed(conn, data_root)
    guarding = sqlite3.connect(paths.db_path(), factory=WriteGuard)
    guarding.row_factory = sqlite3.Row
    try:
        ctx = pages.AdminContext(conn=guarding, data_root=data_root, clock=lambda: BASE)
        for page in pages.PAGES:
            assert str(page.render(ctx, {})).startswith("<!doctype html>")
    finally:
        guarding.close()


def test_values_from_the_database_are_escaped(conn, ctx):
    create_match(conn, league="LPL", team_a="<script>alert(1)</script>", team_b="LNG", state="live")
    rendered = render("matches", ctx)
    assert "<script>alert(1)</script>" not in rendered
    assert "&lt;script&gt;" in rendered


def test_unknown_page_key_is_a_lookup_error():
    with pytest.raises(LookupError):
        pages.page_of("不存在")
