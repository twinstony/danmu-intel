"""站点产物测试：页面清单、导航唯一、付费/公开边界、画像来源、灰信号与验证闭环。"""

from __future__ import annotations

import json
import re
from html import escape

import pytest

from danmu_intel.common import paywall
from danmu_intel.common.matches import create_match
from danmu_intel.pipeline import generate_and_publish
from danmu_intel.publish import site
from danmu_intel.publish.site import (
    NAV_ITEMS,
    SitePage,
    SiteTree,
    build_site,
    identifier,
    league_page_path,
    page_links,
    parse_identifier,
    relative_href,
    report_page_path,
    resolve_href,
    resolve_identifier,
    slug,
    team_page_path,
)

GENERATED_AT = 1_790_064_400_000
LINEUPS = {
    "lineups": {
        "team_a": ["选手甲", "选手乙"],
        "team_b": ["选手丙"],
    }
}


def publish_forms(ledger, *, brief: bool = True) -> None:
    """发一份赛中快报（比赛进行中）与赛后完整版（比赛已结束）。"""
    if brief:
        generate_and_publish(
            ledger.conn,
            ledger.match_id,
            kind="live_brief",
            completed_games=(1,),
            data_root=ledger.data_root,
            generated_at=GENERATED_AT,
        )
    ledger.conn.execute("UPDATE matches SET state='ended' WHERE id=?", (ledger.match_id,))
    ledger.conn.commit()
    generate_and_publish(
        ledger.conn, ledger.match_id, kind="full", data_root=ledger.data_root, generated_at=GENERATED_AT
    )


def add_gray_signal(conn, match_id: int) -> None:
    samples = [{"ts": GENERATED_AT, "text": "假赛？", "rel_path": "raw/x.jsonl", "line_no": 3}]
    conn.execute(
        """
        INSERT INTO gray_signals(match_id, category, keyword, hit_count, distinct_users,
                                 window_count, samples_json, status, reason, created_at, evaluated_at)
        VALUES(?, 'cheat_suspicion', '假赛', 9, 5, 3, ?, 'candidate', NULL, ?, ?)
        """,
        (match_id, json.dumps(samples, ensure_ascii=False), GENERATED_AT, GENERATED_AT),
    )
    conn.commit()


# —— 路径、标识与链接（纯函数）——


def test_slug_is_url_safe_and_collision_free():
    assert slug("iG") == "iG"
    assert slug("LPL") == "LPL"
    assert slug("滔搏") == slug("滔搏")
    assert slug("滔搏") != slug("TES")
    for name in ("滔搏", "a b", "队/", "", "  "):
        assert not any(char in slug(name) for char in "/\\ :?")
        assert slug(name) == slug(name)  # 同名同值（可复现）


def test_identifiers_resolve_to_page_paths():
    assert identifier("league", "LPL") == "league:LPL"
    assert resolve_identifier("league:LPL") == league_page_path("LPL")
    assert resolve_identifier("match:7") == "matches/7/index.html"
    assert resolve_identifier("team:iG") == team_page_path("iG")
    assert resolve_identifier("player:选手甲") == "profile/players/x-" + slug("选手甲")[2:] + ".html"
    assert parse_identifier("match:7") == ("match", "7")
    with pytest.raises(ValueError):
        parse_identifier("unknown:1")
    with pytest.raises(ValueError):
        identifier("unknown", "1")
    with pytest.raises(ValueError):
        resolve_identifier("match:abc")


def test_relative_links_resolve_back_to_the_target():
    for page, target in (
        ("index.html", "history/index.html"),
        ("matches/1/index.html", "history/index.html"),
        (report_page_path(1, "full"), "leagues/LPL.html"),
        ("profile/teams/iG.html", "index.html"),
    ):
        href = relative_href(page, target)
        assert resolve_href(page, href) == target
    assert resolve_href("index.html", "https://example.com/x") is None
    assert resolve_href("index.html", "#seg-1") is None
    assert resolve_href("index.html", "") is None
    assert page_links("index.html", '<a href="history/index.html">a</a><a href="#x">b</a>') == (
        "history/index.html",
    )


def test_tree_digest_is_content_addressed():
    tree = SiteTree((SitePage(path="a.html", title="A", html="<p>1</p>"),))
    same = SiteTree((SitePage(path="a.html", title="A", html="<p>1</p>"),))
    other = SiteTree((SitePage(path="a.html", title="A", html="<p>2</p>"),))
    assert tree.digest() == same.digest()
    assert tree.digest() != other.digest()
    assert tree.paths == ("a.html",) and tree.has("a.html") and not tree.has("b.html")
    assert tree.files() == {"a.html": "<p>1</p>"}
    with pytest.raises(KeyError):
        tree.page("b.html")


# —— 产物清单与导航 ——


def test_site_tree_contains_every_declared_artifact(ledger, site_root):
    publish_forms(ledger)
    build = build_site(ledger.conn, data_root=ledger.data_root, generated_at=GENERATED_AT)
    paths = set(build.tree.paths)
    assert {
        "index.html",
        "history/index.html",
        league_page_path("LPL"),
        "matches/1/index.html",
        report_page_path(1, "live_brief"),
        report_page_path(1, "full"),
        "profile/index.html",
        "gray/index.html",
        "verification/index.html",
        "subscribe.html",
    } <= paths
    for path, html in build.tree.files().items():
        assert html.startswith("<!DOCTYPE html>"), path
        # 没配 API 基址：静态站保持零脚本（自建 beacon 也不写，见下一个测试）
        assert "<script" not in html, path
        # 无外部请求（NFR-P-3）：不加载任何站外资源、不引第三方脚本。
        # 订阅页会写出**我们自己那台机器**的 API 基址（绝对 URL，用户要拿它调接口），
        # 那不是资源请求，所以只有这一页允许出现绝对 URL。
        assert not re.search(r'(?:src|href)="https?://', html), path
        if path != "subscribe.html":
            assert "http://" not in html and "https://" not in html, path


def test_site_pages_report_their_visit_to_our_own_api(ledger):
    """配了 API 基址后，每页带一个**自建** beacon（自己那台 API，无第三方、无 cookie）。"""
    from danmu_intel.billing import pricing

    publish_forms(ledger)
    pricing.save_billing_config(
        ledger.conn, actor="管理员", changes={"api_base": "https://host.ts.net:8443/"}
    )
    build = build_site(ledger.conn, data_root=ledger.data_root, generated_at=GENERATED_AT)
    for page in build.tree.pages:
        html = page.html
        assert html.count("<script") == 1, page.path
        assert "<script src=" not in html, page.path  # 没有外部脚本
        assert 'navigator.sendBeacon("https://host.ts.net:8443/api/stats/beacon"' in html, page.path
        assert f'page:"{page.path}"' in html, page.path
        # 只打自己那台机器：页面上的 http(s) 地址只有这一个（订阅页另有接口说明）
        if page.path != "subscribe.html":
            assert html.count("https://") == 1, page.path


def test_no_beacon_when_the_api_base_is_not_configured(ledger):
    """宁可不统计，也不往不知道的地址发请求：没配基址就不写脚本。"""
    from danmu_intel.billing import pricing

    publish_forms(ledger)
    pricing.save_billing_config(ledger.conn, actor="管理员", changes={"api_base": ""})
    build = build_site(ledger.conn, data_root=ledger.data_root, generated_at=GENERATED_AT)
    assert all("sendBeacon" not in page.html for page in build.tree.pages)


def test_navigation_is_the_same_on_every_page_and_every_link_lands(ledger):
    publish_forms(ledger)
    build = build_site(ledger.conn, data_root=ledger.data_root, generated_at=GENERATED_AT)
    paths = set(build.tree.paths)
    for page in build.tree.pages:
        assert page.nav == NAV_ITEMS
        links = set(page.links)
        assert links, f"{page.path} 没有任何站内链接"
        for _, target in page.nav:
            assert target in paths
            assert target in links, f"{page.path} 的导航项没有渲染到页面上：{target}"
        for target in page.links:
            assert target in paths, f"{page.path} 链到了不存在的页面 {target}"
    # 每个页面都从首页可达（无孤儿页面）
    reachable = {"index.html"}
    frontier = ["index.html"]
    while frontier:
        current = frontier.pop()
        for target in build.tree.page(current).links:
            if target not in reachable:
                reachable.add(target)
                frontier.append(target)
    assert reachable == paths


def test_index_lists_reports_leagues_and_matches(ledger):
    publish_forms(ledger)
    build = build_site(ledger.conn, data_root=ledger.data_root, generated_at=GENERATED_AT)
    html = build.tree.page("index.html").html
    assert "最新报告" in html and "赛中快报" in html and "完整版" in html
    assert league_page_path("LPL") in build.tree.page("index.html").links
    assert "matches/1/index.html" in build.tree.page("index.html").links


def test_history_lists_four_facets(ledger):
    publish_forms(ledger)
    build = build_site(ledger.conn, data_root=ledger.data_root, generated_at=GENERATED_AT)
    page = build.tree.page("history/index.html")
    for facet in ("按日期", "按联赛", "按队伍", "按比赛"):
        assert facet in page.html
    assert team_page_path("iG") in page.links
    assert report_page_path(1, "full") in page.links


def test_league_page_lists_every_match_of_that_league(ledger):
    create_match(ledger.conn, league="LCK", team_a="T1", team_b="GEN", state="live")
    publish_forms(ledger)
    build = build_site(ledger.conn, data_root=ledger.data_root, generated_at=GENERATED_AT)
    lpl = build.tree.page(league_page_path("LPL"))
    lck = build.tree.page(league_page_path("LCK"))
    assert "matches/1/index.html" in lpl.links and "matches/2/index.html" not in lpl.links
    assert "matches/2/index.html" in lck.links and "matches/1/index.html" not in lck.links
    assert "共 1 场比赛" in lpl.html


# —— 付费 / 公开边界 ——


def body_snippet(build, kind: str, segment_no: int) -> str:
    """报告内容里的一段正文（转义后），用来断言它有没有出现在静态页面上。"""
    material = next(item for item in build.facts.reports if item.kind == kind)
    first_line = material.content.segment(segment_no).body.splitlines()[0]
    assert first_line.strip()
    return escape(first_line)


def test_live_match_pages_are_paid_and_ended_on_public(three_game_ledger):
    ledger = three_game_ledger
    generate_and_publish(
        ledger.conn,
        ledger.match_id,
        kind="live_brief",
        completed_games=ledger.completed_games,
        data_root=ledger.data_root,
        generated_at=GENERATED_AT,
    )
    build = build_site(ledger.conn, data_root=ledger.data_root, generated_at=GENERATED_AT)
    brief = build.tree.page(report_page_path(ledger.match_id, "live_brief"))
    match_page = build.tree.page(f"matches/{ledger.match_id}/index.html")
    snippet = body_snippet(build, "live_brief", 0)
    assert brief.visibility == paywall.VISIBILITY_PAID
    assert paywall.PAYWALL_MARK in brief.html
    assert snippet not in brief.html, "进行中的比赛：正文不进静态产物"
    assert match_page.visibility == paywall.VISIBILITY_PAID
    assert "会员（付费）" in match_page.html
    assert "matches/1/live_brief.html" in match_page.links

    # 比赛转为结束 → 页面按状态机自动转公开（同一次构建，无需按页面动作）
    ledger.conn.execute("UPDATE matches SET state='ended' WHERE id=?", (ledger.match_id,))
    ledger.conn.commit()
    after = build_site(ledger.conn, data_root=ledger.data_root, generated_at=GENERATED_AT)
    brief_after = after.tree.page(report_page_path(ledger.match_id, "live_brief"))
    assert brief_after.visibility == paywall.VISIBILITY_PUBLIC
    assert paywall.PAYWALL_MARK not in brief_after.html
    assert body_snippet(build, "live_brief", 0) in brief_after.html


def test_report_pages_carry_the_site_navigation(ledger):
    """报告页也是站点的一部分：读者从报告页能走回索引、历史库、画像库、灰信号、校验、订阅。"""
    publish_forms(ledger)
    build = build_site(ledger.conn, data_root=ledger.data_root, generated_at=GENERATED_AT)
    page = build.tree.page(report_page_path(1, "full"))
    assert '<nav class="site">' in page.html
    for label, target in NAV_ITEMS:
        assert label in page.html
        assert target in page.links


def test_match_page_reports_segment_scale(ledger):
    publish_forms(ledger)
    build = build_site(ledger.conn, data_root=ledger.data_root, generated_at=GENERATED_AT)
    html = build.tree.page("matches/1/index.html").html
    assert "1 个落盘文件" in html
    assert f"共 {len(ledger.events)} 条弹幕" in html


# —— 画像库 ——


def test_profile_pages_come_from_official_data_only(ledger, conn):
    publish_forms(ledger)
    build = build_site(ledger.conn, data_root=ledger.data_root, generated_at=GENERATED_AT)
    index = build.tree.page("profile/index.html")
    teams = {team.name: team for team in build.facts.teams}
    assert teams["iG"].record == "1 胜 0 负"
    assert teams["LNG"].record == "0 胜 1 负"
    assert "官方阵容数据尚未接入" in index.html
    assert team_page_path("iG") in index.links
    assert not [path for path in build.tree.paths if "players/" in path]


def test_profile_pages_include_lineups_when_official_data_has_them(ledger):
    ledger.conn.execute(
        "UPDATE matches SET official_result=? WHERE id=?",
        (json.dumps({**LINEUPS, "score": "2:0"}, ensure_ascii=False), ledger.match_id),
    )
    ledger.conn.commit()
    publish_forms(ledger)
    build = build_site(ledger.conn, data_root=ledger.data_root, generated_at=GENERATED_AT)
    player_path = "profile/players/x-" + slug("选手甲")[2:] + ".html"
    assert player_path in build.tree.paths
    html = build.tree.page(player_path).html
    assert "选手甲" in html and "iG" in html and "出场：1 场" in html
    assert "官方阵容数据" in html
    assert player_path in build.tree.page("profile/index.html").links


def test_team_page_shows_record_and_matches(ledger):
    publish_forms(ledger)
    build = build_site(ledger.conn, data_root=ledger.data_root, generated_at=GENERATED_AT)
    page = build.tree.page(team_page_path("iG"))
    assert "LPL" in page.html and "1 胜 0 负" in page.html
    assert "matches/1/index.html" in page.links
    assert page.identifiers[0] == identifier("team", "iG")


# —— 灰信号与验证闭环 ——


def test_gray_page_shows_threshold_signals_without_identity(ledger):
    add_gray_signal(ledger.conn, ledger.match_id)
    publish_forms(ledger)
    build = build_site(ledger.conn, data_root=ledger.data_root, generated_at=GENERATED_AT)
    html = build.tree.page("gray/index.html").html
    assert "假赛" in html and "命中 9 条" in html and "独立发言者 5 人" in html
    assert "只作风险提示" in html and "不指控" in html
    for event in ledger.events:
        assert event.user_hash not in html


def test_gray_page_says_so_when_there_is_nothing(ledger):
    publish_forms(ledger)
    build = build_site(ledger.conn, data_root=ledger.data_root, generated_at=GENERATED_AT)
    assert "目前没有任何达到门槛的灰信号" in build.tree.page("gray/index.html").html


def test_verification_page_freezes_hashes_and_source_verdicts(ledger):
    publish_forms(ledger)
    build = build_site(ledger.conn, data_root=ledger.data_root, generated_at=GENERATED_AT)
    html = build.tree.page("verification/index.html").html
    for material in build.facts.reports:
        assert material.content.fact_layer_hash in html
        assert material.unresolved == ()
    assert "来源全部复核通过" in html
    assert "不做预测，也不做事后追认" in html

    # 原始记录被改动 → 构建时就能看见（校验页如实写出来）
    raw = ledger.data_root / ledger.rel_path
    raw.write_text(raw.read_text(encoding="utf-8").replace("G1 散落 1", "G1 散落 X"), encoding="utf-8")
    after = build_site(ledger.conn, data_root=ledger.data_root, generated_at=GENERATED_AT)
    broken = after.tree.page("verification/index.html").html
    assert "项来源复核失败" in broken
    assert all(material.unresolved for material in after.facts.reports)


def test_reports_with_a_removed_segment_still_render_for_the_check_layer(ledger):
    """缺段的报告行不该让构建崩掉：它由发布检查拦下（检查 ④），不是渲染层的事。"""
    publish_forms(ledger)
    row = ledger.conn.execute(
        "SELECT id, content_json FROM reports WHERE kind='full' AND state='published'"
    ).fetchone()
    payload = json.loads(row["content_json"])
    payload["segments"] = [item for item in payload["segments"] if item["no"] != 9]
    ledger.conn.execute(
        "UPDATE reports SET content_json=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), row["id"])
    )
    ledger.conn.commit()
    build = build_site(ledger.conn, data_root=ledger.data_root, generated_at=GENERATED_AT)
    assert len(build.facts.reports[-1].content.segments) == 10


def test_subscribe_page_shows_the_configured_plans_and_api_base(ledger):
    """T9：订阅页把档位与价格（来自 `config`，不写死）与四个接口如实写出。"""
    from danmu_intel.billing import pricing

    publish_forms(ledger)
    html = build_site(ledger.conn, data_root=ledger.data_root, generated_at=GENERATED_AT).tree.page(
        "subscribe.html"
    ).html
    assert "5.00 USDT" in html and "30 天" in html and "0.50 USDT" in html
    assert "POST /api/orders" in html and "POST /api/claim" in html
    assert "POST /api/verify" in html and "/api/report/" in html
    assert "无法用来枚举会员" in html

    pricing.save_billing_config(
        ledger.conn, actor="管理员", changes={"api_base": "https://host.ts.net:8443/"}
    )
    with_base = build_site(
        ledger.conn, data_root=ledger.data_root, generated_at=GENERATED_AT
    ).tree.page("subscribe.html").html
    assert "POST https://host.ts.net:8443/api/orders" in with_base
    assert "https://host.ts.net:8443//api" not in with_base  # 基址尾部斜杠不双写


def test_subscribe_page_says_so_when_prices_are_not_configured(ledger):
    from danmu_intel.billing import pricing

    publish_forms(ledger)
    pricing.save_billing_config(ledger.conn, actor="管理员", changes={"tiers": []})
    html = build_site(ledger.conn, data_root=ledger.data_root, generated_at=GENERATED_AT).tree.page(
        "subscribe.html"
    ).html
    assert "档位与价格还没配置" in html
    assert "USDT" not in html
