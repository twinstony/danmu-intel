"""T10 站点统计自建：口径、日汇总、90 天保留（issue #20 的验收标准逐条对应）。

- **独立访客口径**：`sha256(每日盐 + IP + UA)`，每日换盐（跨日不可还原同一人，AC-9）。
- **可回答**：某天访问量、访问付费页人数、下单转化、留资数。
- **不可回答**：具体是谁（明细里没有 IP / UA / 联系方式，只有当日盐下的哈希）。
- **保留**：明细 90 天 → 汇总入 `stats_daily` 后删除（汇总是删不掉的口径）。
- **付费页判定**：只由比赛状态机给出（ADR-0009），路径只用来找「哪场比赛」。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

import pytest

from danmu_intel.billing import members, orders, pricing
from danmu_intel.common import paywall
from danmu_intel.common.matches import create_match, set_match_state
from danmu_intel.site_stats import beacon, daily

DAY0 = "2026-09-22"
DAY1 = "2026-09-23"


def at(day: str, *, hour: int = 12, minute: int = 0) -> int:
    moment = datetime.strptime(day, "%Y-%m-%d").replace(hour=hour, minute=minute)
    return int(moment.timestamp() * 1000)


def visit(conn, *, day: str, page: str, ip: str, ua: str = "UA", hour: int = 12, minute: int = 0,
          member_id: int | None = None) -> str:
    return beacon.record(
        conn, page=page, ip=ip, user_agent=ua, member_id=member_id, ts=at(day, hour=hour, minute=minute)
    )


# —— 口径：每日盐与访客哈希 ——


def test_visitor_hash_is_salted_sha256_of_ip_and_user_agent(conn):
    _day, salt = beacon.current_salt(conn, at_ms=at(DAY0))
    digest = beacon.visitor_hash(salt, "203.0.113.7", "UA-1")
    assert digest == hashlib.sha256(f"{salt}203.0.113.7UA-1".encode("utf-8")).hexdigest()
    # 换 UA / 换 IP 都是另一个人
    assert digest != beacon.visitor_hash(salt, "203.0.113.7", "UA-2")
    assert digest != beacon.visitor_hash(salt, "203.0.113.8", "UA-1")


def test_salt_is_stable_within_a_day_and_rotates_daily(conn):
    day0, salt0 = beacon.current_salt(conn, at_ms=at(DAY0, hour=1))
    again = beacon.current_salt(conn, at_ms=at(DAY0, hour=23))
    assert again == (day0, salt0)  # 同一天同一个盐
    day1, salt1 = beacon.current_salt(conn, at_ms=at(DAY1, hour=1))
    assert day1 == DAY1 and salt1 != salt0
    # 旧盐被丢弃：库里的盐只剩当天一行
    assert conn.execute("SELECT COUNT(*) AS n FROM stats_salt").fetchone()["n"] == 1
    assert conn.execute("SELECT day FROM stats_salt").fetchone()["day"] == DAY1


def test_same_visitor_gets_different_hashes_on_different_days(conn):
    """AC-9 后半段：跨日不可还原同一人（盐每天换一次）。"""
    first = visit(conn, day=DAY0, page="index.html", ip="10.0.0.1")
    second = visit(conn, day=DAY1, page="index.html", ip="10.0.0.1")
    assert first != second
    assert daily.summary(conn, DAY0).unique_visitors == 1
    assert daily.summary(conn, DAY1).unique_visitors == 1


def test_detail_rows_carry_no_ip_or_identity_fields(conn):
    """明细只有当日盐下的哈希：没有 IP 列、没有 UA 列，也没有联系方式。"""
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(stats_events)").fetchall()
    }
    assert columns == {"id", "day", "ts", "page", "visitor_hash", "paid", "member_id"}
    assert not {"ip", "user_agent", "ua", "username", "contact"} & columns


# —— 页面路径与付费页判定 ——


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("index.html", "index.html"),
        ("/matches/1/full.html", "matches/1/full.html"),
        ("matches/1/full.html?code=abc#seg-3", "matches/1/full.html"),
        ("/a/./b//c.html", "a/b/c.html"),
    ],
)
def test_normalize_page_收成站内相对路径(raw, expected):
    assert beacon.normalize_page(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "https://elsewhere.example/x", "//host/x", "../secrets", "x" * 300])
def test_normalize_page_拒绝非站内路径(raw):
    with pytest.raises(ValueError):
        beacon.normalize_page(raw)


def test_paid_page_follows_the_match_state_machine(conn):
    """ADR-0009：付费与否只看状态机，不看文件名/路径。"""
    match_id = create_match(conn, league="LPL", team_a="iG", team_b="LNG", state="live")
    assert beacon.is_paid_page(conn, f"matches/{match_id}/index.html")
    assert beacon.is_paid_page(conn, f"matches/{match_id}/full.html")
    # 其它页面（首页/历史库/订阅页）与不存在的比赛都不是付费页
    assert not beacon.is_paid_page(conn, "index.html")
    assert not beacon.is_paid_page(conn, "subscribe.html")
    assert not beacon.is_paid_page(conn, "matches/999/full.html")
    # 路径形态认不出来的一律按公开页
    assert not beacon.is_paid_page(conn, f"matches/{match_id}/unknown.html")
    # 比赛结束 → 自动转公开
    set_match_state(conn, match_id, state="ended")
    assert not beacon.is_paid_page(conn, f"matches/{match_id}/full.html")


def test_paid_flag_is_frozen_at_visit_time(conn):
    """页面当晚是付费的，第二天比赛结束再汇总也不会把昨天的访问改写成公开页访问。"""
    match_id = create_match(conn, league="LPL", team_a="iG", team_b="LNG", state="live")
    visit(conn, day=DAY0, page=f"matches/{match_id}/full.html", ip="10.0.0.1")
    set_match_state(conn, match_id, state="ended")
    assert daily.summary(conn, DAY0).paid_unique_visitors == 1
    assert daily.summary(conn, DAY0).paid_page_views == 1


# —— 日汇总：访问量 / 会话 / 独立访客 / 付费页 ——


def test_summary_counts_views_sessions_and_unique_visitors(conn):
    visit(conn, day=DAY0, page="index.html", ip="10.0.0.1", hour=10, minute=0)
    visit(conn, day=DAY0, page="index.html", ip="10.0.0.1", hour=10, minute=10)  # 同一会话
    visit(conn, day=DAY0, page="index.html", ip="10.0.0.1", hour=11, minute=30)  # 隔 >30 分钟：新会话
    visit(conn, day=DAY0, page="history/index.html", ip="10.0.0.2", hour=12, minute=0)
    summary = daily.summary(conn, DAY0)
    assert summary.page_views == 4
    assert summary.unique_visitors == 2
    assert summary.sessions == 3
    assert summary.paid_page_views == 0 and summary.paid_unique_visitors == 0


def test_summary_counts_paid_page_visitors(conn):
    """AC-9 前半句：某天有多少人来过付费页。"""
    match_id = create_match(conn, league="LPL", team_a="iG", team_b="LNG", state="live")
    visit(conn, day=DAY0, page=f"matches/{match_id}/full.html", ip="10.0.0.1")
    visit(conn, day=DAY0, page=f"matches/{match_id}/full.html", ip="10.0.0.1")  # 同一个人再来一次
    visit(conn, day=DAY0, page=f"matches/{match_id}/full.html", ip="10.0.0.2", ua="UA-2")
    visit(conn, day=DAY0, page="index.html", ip="10.0.0.3")
    summary = daily.summary(conn, DAY0)
    assert summary.paid_page_views == 3
    assert summary.paid_unique_visitors == 2  # 「多少人」而不是「多少次」
    assert summary.unique_visitors == 3


def test_member_id_is_recorded_only_when_the_caller_proved_it(conn):
    """留资/付费者由会员账本给出：`record(member_id=…)` 只在凭据校验通过时才被调用。"""
    member = members.get_or_create_member(conn, platform="qq", username="12345678", tier="trial")
    visit(conn, day=DAY0, page="index.html", ip="10.0.0.1", member_id=member.id)
    visit(conn, day=DAY0, page="index.html", ip="10.0.0.2")
    rows = conn.execute("SELECT member_id FROM stats_events ORDER BY id").fetchall()
    assert [row["member_id"] for row in rows] == [member.id, None]


# —— 下单转化与留资（来自账本，不复制进汇总） ——


def test_funnel_counts_come_from_the_ledgers(conn):
    """可回答：下单转化 + 留资数 —— 与访问付费页人数并列即可算转化。"""
    pricing.save_billing_config(
        conn,
        actor="管理员",
        changes={
            "api_base": "https://host.ts.net:8443",
            "solana_address": "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin",
            "tiers": [
                {"key": "trial", "label": "试用档", "amount_units": 500_000, "days": 7},
            ],
        },
    )
    order, _token = orders.create_order(
        conn,
        platform="qq",
        username="12345678",
        tier="trial",
        network="solana",
        config=pricing.load_billing_config(conn),
        now=at(DAY0, hour=13),
    )
    match_id = create_match(conn, league="LPL", team_a="iG", team_b="LNG", state="live")
    visit(conn, day=DAY0, page=f"matches/{match_id}/full.html", ip="10.0.0.1", hour=13)

    summary = daily.summary(conn, DAY0)
    assert summary.leads == 1 and summary.orders == 1
    assert summary.paid_orders == 0
    assert summary.paid_unique_visitors == 1  # 转化率的分母
    assert summary.as_dict()["orders"] == 1
    # 付费那天算付费转化
    conn.execute("UPDATE orders SET status='paid', paid_at=? WHERE id=?", (at(DAY1, hour=9), order.id))
    conn.commit()
    assert daily.summary(conn, DAY1).paid_orders == 1
    assert daily.summary(conn, DAY0).paid_orders == 0  # 付款另算一天


# —— 保留：明细 90 天 → 汇总入 stats_daily ——


def test_rollup_is_idempotent_and_matches_the_detail(conn):
    visit(conn, day=DAY0, page="index.html", ip="10.0.0.1")
    visit(conn, day=DAY0, page="index.html", ip="10.0.0.2", ua="UA-3")
    first = daily.rollup(conn, DAY0)
    second = daily.rollup(conn, DAY0)
    assert first == second == daily.summary(conn, DAY0)
    rows = conn.execute("SELECT COUNT(*) AS n FROM stats_daily").fetchone()["n"]
    assert rows == 1  # 重算不新增行


def test_prune_rolls_up_before_deleting_expired_detail(conn):
    """明细 90 天到期：先汇总入 `stats_daily` 再删，汇总后仍然答得出那天的口径。"""
    visit(conn, day=DAY0, page="index.html", ip="10.0.0.1")
    visit(conn, day=DAY0, page="index.html", ip="10.0.0.2", ua="UA-3")
    fresh = DAY1
    visit(conn, day=fresh, page="index.html", ip="10.0.0.9")
    # DAY1 中午起算 90 天：裁剪分界正好是 DAY1，于是 DAY0 到期、DAY1 还在保留期内
    now = at(fresh) + 90 * 24 * 3600 * 1000

    result = daily.prune(conn, at_ms=now)

    assert result.days == (DAY0,) and result.events == 2
    assert "已汇总并删除 1 天明细" in result.summary()
    assert daily.detail_days(conn) == (fresh,)  # 保留期内的明细还在
    after = daily.summary(conn, DAY0)  # 明细没了，数字还在
    assert (after.page_views, after.unique_visitors) == (2, 2)
    # 再裁一次：没有到期的了（空明细不会改写既有汇总）
    assert daily.prune(conn, at_ms=now).days == ()


def test_prune_keeps_stored_summary_when_detail_is_gone(conn):
    visit(conn, day=DAY0, page="index.html", ip="10.0.0.1")
    daily.rollup(conn, DAY0)
    conn.execute("DELETE FROM stats_events")
    conn.commit()
    assert daily.rollup(conn, DAY0).page_views == 1  # 空明细不改写汇总
    assert daily.summary(conn, DAY0).unique_visitors == 1


def test_prune_rejects_non_positive_retention(conn):
    with pytest.raises(ValueError):
        daily.prune(conn, retention_days=0)


def test_recent_days_merges_detail_and_summary(conn):
    visit(conn, day=DAY0, page="index.html", ip="10.0.0.1")
    visit(conn, day=DAY1, page="index.html", ip="10.0.0.2", ua="UA-3")
    daily.rollup(conn, DAY1)
    conn.execute("DELETE FROM stats_events WHERE day=?", (DAY1,))
    conn.commit()
    assert daily.recent_days(conn) == (DAY0, DAY1)


def test_summary_of_a_day_without_traffic_is_all_zeros(conn):
    summary = daily.summary(conn, DAY0)
    assert summary.page_views == 0 and summary.unique_visitors == 0
    assert summary.orders == 0 and summary.leads == 0


@pytest.mark.parametrize("raw", ["", "2026/09/22", "22-09-2026", "2026-13-01"])
def test_parse_day_rejects_bad_dates(raw):
    with pytest.raises(ValueError):
        daily.parse_day(raw)


def test_day_key_matches_local_calendar_day():
    assert daily.parse_day("2026-09-22") == "2026-09-22"
    midnight = datetime.strptime("2026-09-22", "%Y-%m-%d")
    assert daily.day_start_ms("2026-09-22") == int(midnight.timestamp() * 1000)
    assert beacon.day_of(int((midnight + timedelta(hours=23)).timestamp() * 1000)) == "2026-09-22"


def test_paywall_vocabulary_is_the_only_visibility_source(conn):
    """付费页口径与 `common.paywall` 同源：不在这里另立一套词。"""
    assert paywall.VISIBILITY_PAID in paywall.VISIBILITIES
    assert daily.DETAIL_RETENTION_DAYS == 90


# —— 页面上的一行脚本：自建 beacon ——


def test_snippet_targets_our_own_api_and_carries_only_the_page_path():
    script = beacon.snippet("matches/7/full.html", "https://host.ts.net:8443/")
    assert script.startswith("<script>navigator.sendBeacon(")
    assert '"https://host.ts.net:8443/api/stats/beacon"' in script  # 尾部斜杠不双写
    assert 'page:"matches/7/full.html"' in script
    assert "http" not in script.replace('"https://host.ts.net:8443/api/stats/beacon"', "")  # 没有第二个地址
    assert "document.cookie" not in script and "referrer" not in script


def test_snippet_is_absent_without_an_api_base():
    assert beacon.snippet("index.html", "") == ""
    assert beacon.snippet("index.html", "   ") == ""


def test_snippet_breaks_the_script_tag_escape_hatch():
    """拼进去的值不能提前关掉 `<script>`（配置是运维给的，但脚本必须自己兜住）。"""
    script = beacon.snippet("index.html", "https://host.ts.net:8443/</script><b>x</b>")
    assert "</script>" not in script[:-len("</script>")]
    assert "\\u003c" in script
