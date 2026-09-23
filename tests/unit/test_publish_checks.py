"""发布前检查：需求 §6.8 的 6 项各有一组「注入缺陷即拦截」的红绿用例（+ 1 项加固检查）。

每项检查都是纯函数（`SiteBuild → CheckResult`），所以缺陷注入就是把一棵**好产物**改坏：
改一处导航、塞一句旧模板残留、把付费页换成公开、删一个段、错一个联赛、塞一个速览卡。
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from html import escape

import pytest

from danmu_intel.common import paywall
from danmu_intel.pipeline import generate_and_publish
from danmu_intel.publish import checks
from danmu_intel.publish.site import (
    NAV_ITEMS,
    build_site,
    league_page_path,
    report_page_path,
    resolve_href,
)

GENERATED_AT = 1_790_064_400_000

CHECK_KEYS = (
    "navigation_unique",
    "no_legacy_template",
    "paywall_correct",
    "report_segments_complete",
    "cross_references",
    "no_quick_card",
    "report_sources_resolvable",
)


@pytest.fixture
def build(three_game_ledger):
    """一棵健康产物：G1/G2 已完成（快报付费）、比赛进行中。"""
    ledger = three_game_ledger
    generate_and_publish(
        ledger.conn,
        ledger.match_id,
        kind="live_brief",
        completed_games=ledger.completed_games,
        data_root=ledger.data_root,
        generated_at=GENERATED_AT,
    )
    return build_site(ledger.conn, data_root=ledger.data_root, generated_at=GENERATED_AT)


def one(build, key: str, *, data_root=None, seals=None):
    return next(item for item in checks.run_checks(build, data_root=data_root, seals=seals) if item.key == key)


def unlink(build, target: str):
    """把指向 `target` 的链接全部改成页内锚点（让那个页面从首页不可达）。"""
    pages = []
    for page in build.tree.pages:
        html = page.html
        for href in re.findall(r'href="([^"]*)"', html):
            if resolve_href(page.path, href) == target:
                html = html.replace(f'href="{href}"', 'href="#"')
        pages.append(replace(page, html=html))
    return replace(build, tree=replace(build.tree, pages=tuple(pages)))


def break_page(build, path: str, **changes):
    """改坏一个页面（缺陷注入）。"""
    pages = tuple(
        replace(page, **changes) if page.path == path else page for page in build.tree.pages
    )
    return replace(build, tree=replace(build.tree, pages=pages))


def test_all_seven_checks_pass_on_a_healthy_build(build):
    results = checks.run_checks(build, data_root=None)
    assert [item.key for item in results] == list(CHECK_KEYS)
    for item in results:
        assert item.passed, f"{item.label}：{item.detail}"
        assert item.blocking
    assert checks.failures(results) == ()


# —— ① 全站导航唯一 ——


def test_navigation_unique_rejects_duplicate_nav_entries(build):
    assert one(build, "navigation_unique").passed
    broken = break_page(build, "index.html", nav=NAV_ITEMS + (NAV_ITEMS[0],))
    result = one(broken, "navigation_unique")
    assert not result.passed and "重复" in result.detail


def test_navigation_unique_rejects_orphan_links_and_pages(build):
    broken = break_page(
        build, "index.html", html=build.tree.page("index.html").html + '<a href="missing.html">死链</a>'
    )
    result = one(broken, "navigation_unique")
    assert not result.passed and "missing.html" in result.detail

    dropped = replace(
        build,
        tree=replace(build.tree, pages=tuple(p for p in build.tree.pages if p.path != "subscribe.html")),
    )
    dangling = one(dropped, "navigation_unique")
    assert not dangling.passed and "subscribe.html" in dangling.detail

    # 页面存在、但从首页不可达 → 孤儿页面
    orphan = one(unlink(build, report_page_path(1, "live_brief")), "navigation_unique")
    assert not orphan.passed and "孤儿页面" in orphan.detail


# —— ② 无旧模板残留 ——


def test_no_legacy_template_rejects_a_leftover_template(build):
    assert one(build, "no_legacy_template").passed
    broken = break_page(
        build,
        "history/index.html",
        html=build.tree.page("history/index.html").html + "<p>{{ intel_danmu_index }}</p>",
    )
    result = one(broken, "no_legacy_template")
    assert not result.passed and "intel_danmu" in result.detail


def test_no_legacy_template_rejects_a_legacy_path(build):
    page = build.tree.page("subscribe.html")
    broken = replace(
        build,
        tree=replace(
            build.tree,
            pages=tuple(
                replace(item, path="TEMPLATE_OLD.html") if item.path == page.path else item
                for item in build.tree.pages
            ),
        ),
    )
    result = one(broken, "no_legacy_template")
    assert not result.passed and "TEMPLATE_OLD" in result.detail


# —— ③ 付费墙正确 ——


def test_paywall_check_rejects_a_paid_page_without_a_paywall(build):
    path = report_page_path(1, "live_brief")
    broken = break_page(build, path, html=build.tree.page(path).html.replace(paywall.PAYWALL_MARK, "已上线"))
    result = one(broken, "paywall_correct")
    assert not result.passed and "没有付费墙" in result.detail


def test_paywall_check_rejects_paid_content_on_a_running_match(build):
    """付费页里泄漏正文 —— 需求 §6.10 说的「curl 拿不到」就是这条。"""
    path = report_page_path(1, "live_brief")
    leaked = escape(
        next(item for item in build.facts.reports if item.kind == "live_brief")
        .content.segment(0)
        .body.splitlines()[0]
    )
    broken = break_page(build, path, html=build.tree.page(path).html.replace("</main>", f"<p>{leaked}</p></main>"))
    result = one(broken, "paywall_correct")
    assert not result.passed and "写了" in result.detail


def test_paywall_check_rejects_a_page_that_stays_locked_after_the_match_ended(build):
    """状态机说「已结束、该公开」，页面却还锁着 → 拦截（需求 §6.8 第 3 项的后半句）。"""
    ended = replace(
        build,
        facts=replace(
            build.facts,
            matches=tuple(
                replace(match, state="ended") if match.id == 1 else match for match in build.facts.matches
            ),
        ),
    )
    result = one(ended, "paywall_correct")
    assert not result.passed and "不符" in result.detail

    # 页面可见性跟着改了、但正文没写出来（付费墙还在、正文缺失）→ 两条都要报
    path = report_page_path(1, "live_brief")
    half = replace(ended, tree=replace(ended.tree, pages=tuple(
        replace(page, visibility="public") if page.path == path else page for page in ended.tree.pages
    )))
    result = one(half, "paywall_correct")
    assert not result.passed and "仍有付费墙" in result.detail and "缺" in result.detail


# —— ④ 报告分段完整 ——


def test_report_segments_check_rejects_a_missing_segment(three_game_ledger, build):
    """账本里被删掉一段（旧版行 / 人为改动）时，产物不得发布。"""
    ledger = three_game_ledger
    row = ledger.conn.execute(
        "SELECT id, content_json FROM reports WHERE kind='live_brief' AND state='published'"
    ).fetchone()
    payload = json.loads(row["content_json"])
    payload["segments"] = [item for item in payload["segments"] if item["no"] != 9]
    ledger.conn.execute(
        "UPDATE reports SET content_json=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), row["id"])
    )
    ledger.conn.commit()
    broken = build_site(ledger.conn, data_root=ledger.data_root, generated_at=GENERATED_AT)
    result = one(broken, "report_segments_complete", data_root=ledger.data_root)
    assert not result.passed and "段" in result.detail


def test_report_segments_check_rejects_a_missing_interpretation_segment(three_game_ledger, build):
    ledger = three_game_ledger
    row = ledger.conn.execute(
        "SELECT id, content_json FROM reports WHERE kind='live_brief' AND state='published'"
    ).fetchone()
    payload = json.loads(row["content_json"])
    payload["segments"] = [
        {**item, "body": ""} if item["no"] == 3 else item for item in payload["segments"]
    ]
    ledger.conn.execute(
        "UPDATE reports SET content_json=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), row["id"])
    )
    ledger.conn.commit()
    broken = build_site(ledger.conn, data_root=ledger.data_root, generated_at=GENERATED_AT)
    result = one(broken, "report_segments_complete", data_root=ledger.data_root)
    assert not result.passed and "空段" in result.detail


# —— ⑤ 页面 × 联赛 × 标识一致 ——


def test_cross_references_rejects_a_wrong_league_on_a_league_page(build):
    broken = break_page(build, league_page_path("LPL"), league="LCK")
    result = one(broken, "cross_references")
    assert not result.passed and "LCK" in result.detail


def test_cross_references_rejects_a_dangling_identifier(build):
    broken = break_page(build, "index.html", identifiers=("match:999",))
    result = one(broken, "cross_references")
    assert not result.passed and "match:999" in result.detail


def test_cross_references_rejects_a_match_missing_from_its_league_page(build):
    keep = tuple(
        value for value in build.tree.page(league_page_path("LPL")).identifiers if value != "match:1"
    )
    broken = break_page(build, league_page_path("LPL"), identifiers=keep)
    result = one(broken, "cross_references")
    assert not result.passed and "没有列出比赛 #1" in result.detail


def test_cross_references_rejects_a_report_that_never_reached_the_tree(build):
    dropped = replace(
        build,
        tree=replace(
            build.tree,
            pages=tuple(p for p in build.tree.pages if p.path != report_page_path(1, "live_brief")),
        ),
    )
    result = one(dropped, "cross_references")
    assert not result.passed and "没有进产物" in result.detail


# —— ⑥ 无「速览卡」类残留物 ——


def test_no_quick_card_rejects_the_deprecated_component(build):
    assert one(build, "no_quick_card").passed
    broken = break_page(
        build,
        "matches/1/index.html",
        html=build.tree.page("matches/1/index.html").html + '<div class="quick-card">速览卡</div>',
    )
    result = one(broken, "no_quick_card")
    assert not result.passed and "速览卡" in result.detail


# —— ⑦ 来源引用可达（加固）——


def test_sources_check_rejects_changed_raw_records(three_game_ledger, build):
    data_root = three_game_ledger.data_root
    assert one(build, "report_sources_resolvable", data_root=data_root).passed
    raw = data_root / three_game_ledger.rel_path
    raw.write_text(raw.read_text(encoding="utf-8").replace("G1 弹幕 3", "G1 弹幕 X"), encoding="utf-8")
    result = one(build, "report_sources_resolvable", data_root=data_root)
    assert not result.passed and "来源" in result.detail


def test_sources_check_rejects_a_seal_mismatch(three_game_ledger, build):
    """采集时封存的 SHA256 与文件当前哈希对不上（原始记录被追加）也要拦。"""
    seals = {three_game_ledger.rel_path: "0" * 64}
    result = one(build, "report_sources_resolvable", data_root=three_game_ledger.data_root, seals=seals)
    assert not result.passed and "封存" in result.detail
