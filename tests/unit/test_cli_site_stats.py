"""命令行：站点统计（`site-stats`）—— 某天的口径 + 90 天保留。"""

from __future__ import annotations

from danmu_intel.billing import members, orders, pricing
from danmu_intel.cli import main
from danmu_intel.common.matches import create_match
from danmu_intel.site_stats import beacon, daily

DAY = "2026-09-22"
DAY_MS = 24 * 3600 * 1000
WALLET = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"


def configure(conn) -> None:
    pricing.save_billing_config(
        conn,
        actor="管理员",
        changes={
            "api_base": "https://host.ts.net:8443",
            "solana_address": WALLET,
            "tiers": [{"key": "trial", "label": "试用档", "amount_units": 500_000, "days": 7}],
        },
    )


def seed(conn) -> int:
    """某天：两个访客（其中一个来过付费页）、一次留资 + 一次下单。"""
    match_id = create_match(conn, league="LPL", team_a="iG", team_b="LNG", state="live")
    configure(conn)
    at = daily.day_start_ms(DAY)
    orders.create_order(
        conn,
        platform="qq",
        username="12345678",
        tier="trial",
        network="solana",
        now=at,
        config=pricing.load_billing_config(conn),
    )
    beacon.record(conn, page="index.html", ip="203.0.113.1", ts=at)
    beacon.record(conn, page="index.html", ip="203.0.113.2", ts=at)
    beacon.record(conn, page=f"matches/{match_id}/full.html", ip="203.0.113.1", ts=at)
    return match_id


def test_site_stats_prints_the_day(conn, capsys):
    """可回答：访问量 / 访问付费页人数 / 下单转化 / 留资数；并如实写明答不了「是谁」。"""
    seed(conn)
    assert main(["site-stats", "--day", DAY]) == 0
    out = capsys.readouterr().out
    assert f"{DAY}｜页面访问 3 次｜会话 2｜独立访客 2" in out
    assert "付费页 1 次 / 1 人｜留资 1｜下单 1（转化 100.0%）｜付费 0（转化 0.0%）" in out
    assert f"最近有数据的日期（1 天）：{DAY}" in out
    assert "每日换盐" in out and "答不了「具体是谁」" in out


def test_site_stats_defaults_to_today(conn, capsys):
    assert main(["site-stats"]) == 0
    assert capsys.readouterr().out.startswith(f"{daily.today()}｜页面访问 0 次")


def test_site_stats_rejects_a_bad_day(conn, capsys):
    assert main(["site-stats", "--day", "2026/09/22"]) == 2
    assert "日期应为 YYYY-MM-DD" in capsys.readouterr().err


def test_site_stats_prune_rolls_up_expired_detail(conn, capsys):
    """90 天保留：明细先汇总入 `stats_daily` 再删 —— 删完仍答得出那天的数字。"""
    old_day = beacon.day_of(beacon.now_ms() - 120 * DAY_MS)
    at = daily.day_start_ms(old_day)
    beacon.record(conn, page="index.html", ip="203.0.113.1", ts=at)
    beacon.record(conn, page="index.html", ip="203.0.113.2", ts=at)

    assert main(["site-stats", "--prune", "--day", old_day]) == 0
    out = capsys.readouterr().out
    assert "已汇总并删除 1 天明细" in out
    assert f"{old_day}｜页面访问 2 次｜会话 2｜独立访客 2" in out
    assert daily.detail_days(conn) == ()  # 明细删了
    assert daily.summary(conn, old_day).unique_visitors == 2  # 数字还在


def test_site_stats_prune_respects_a_longer_retention(conn, capsys):
    old_day = beacon.day_of(beacon.now_ms() - 120 * DAY_MS)
    beacon.record(conn, page="index.html", ip="203.0.113.1", ts=daily.day_start_ms(old_day))
    assert main(["site-stats", "--prune", "--retention-days", "200"]) == 0
    assert "没有超过保留期的明细" in capsys.readouterr().out
    assert daily.detail_days(conn) == (old_day,)


def test_site_stats_prune_rejects_a_non_positive_retention(conn, capsys):
    assert main(["site-stats", "--prune", "--retention-days", "0"]) == 2
    assert "保留天数必须为正" in capsys.readouterr().err


def test_site_stats_marks_conversion_as_unknown_without_paid_page_visitors(conn, capsys):
    """分母为 0 时如实说「—」，不编一个数字。"""
    configure(conn)
    members.get_or_create_member(
        conn, platform="qq", username="12345678", tier="trial", now=daily.day_start_ms(DAY)
    )
    assert main(["site-stats", "--day", DAY]) == 0
    assert "留资 1｜下单 0（转化 —）" in capsys.readouterr().out
