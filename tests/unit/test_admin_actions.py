"""后台写操作（FR-C8-4：所有后台改动必须留痕）。

逐条验的是一个闭环：**每个动作都改到了东西 + 都写了一条 `audit_log`**，
失败时页面能拿到一句人话（而不是静默什么都没发生）。
"""

from __future__ import annotations

import json

import pytest

from danmu_intel.admin import actions
from danmu_intel.billing import members, orders, pricing
from danmu_intel.common import audit, config_store, gray_review, rooms
from danmu_intel.common.config import load_stats_config
from danmu_intel.common.matches import create_match, get_match
from danmu_intel.publish.release import ReleaseContext
from danmu_intel.slice.manual import load_slices
from tests.unit.test_billing_xpub import ACCOUNT_XPUB

BASE = 1_790_064_000_000


@pytest.fixture
def act(conn, data_root, site_root) -> actions.ActionContext:
    """写操作上下文：发布走本地模式（不推 git、不调 Vercel），时钟钉死在 BASE。"""
    return actions.ActionContext(
        conn=conn,
        data_root=data_root,
        release=lambda: ReleaseContext.local(actor="admin", site_root=site_root, data_root=data_root),
        clock=lambda: BASE,
    )


def run(act: actions.ActionContext, key: str, form: dict[str, str]) -> str:
    return actions.action_of(key).run(act, form)


def actions_of(conn) -> list[str]:
    return [entry.action for entry in audit.entries(conn)]


def configure_billing(conn, **extra) -> None:
    pricing.save_billing_config(
        conn, actor="admin", changes={"polygon_xpub": ACCOUNT_XPUB, **extra}
    )


# —— 比赛 ——


def test_match_add_then_state_change_then_delete(act, conn):
    message = run(act, "matches/add", {
        "league": "LPL", "team_a": "iG", "team_b": "LNG", "state": "scheduled", "stage": "常规赛",
    })
    assert "已登记比赛 #1" in message
    assert get_match(conn, 1).stage == "常规赛"

    run(act, "matches/state", {"match_id": "1", "state": "live"})
    assert get_match(conn, 1).state == "live"
    message = run(act, "matches/state", {"match_id": "1", "state": "ended", "official_result": '{"score":"2:0"}'})
    assert "再发布" in message, "转 ended 必须尝试自动再发布公开版（FR-C5-10）"
    assert get_match(conn, 1).state == "ended" and get_match(conn, 1).ended_at == BASE

    message = run(act, "matches/delete", {"match_id": "1"})
    assert "已删除比赛" in message
    assert actions_of(conn)[:2] == ["match.add", "match.state"]
    assert "match.delete" in actions_of(conn)


def test_match_state_rejects_a_bad_state(act):
    run(act, "matches/add", {"league": "LPL", "team_a": "iG", "team_b": "LNG"})
    with pytest.raises(ValueError):
        run(act, "matches/state", {"match_id": "1", "state": "猜的"})


def test_match_add_requires_fields(act):
    with pytest.raises(actions.ActionError) as excinfo:
        run(act, "matches/add", {"league": "LPL", "team_a": "iG", "team_b": ""})
    assert "team_b" in str(excinfo.value)


def test_match_add_rejects_bad_json(act):
    with pytest.raises(actions.ActionError) as excinfo:
        run(act, "matches/add", {
            "league": "LPL", "team_a": "iG", "team_b": "LNG", "official_result": "{不是 JSON}",
        })
    assert "JSON" in str(excinfo.value)


def test_match_delete_refuses_when_data_exists(act, conn):
    run(act, "matches/add", {"league": "LPL", "team_a": "iG", "team_b": "LNG"})
    conn.execute("INSERT INTO slices(match_id, game_no, start_ms, end_ms, boundary_source) VALUES(1, 1, 1, 2, 'manual')")
    conn.commit()
    with pytest.raises(ValueError):
        run(act, "matches/delete", {"match_id": "1"})


# —— 房间 ——


def test_room_crud_is_audited(act, conn):
    run(act, "rooms/add", {"platform": "huya", "room_id": "660000", "url": "https://a", "streamer": "甲"})
    room = rooms.list_rooms(conn)[0]
    assert room.streamer == "甲"

    run(act, "rooms/update", {"room_row_id": str(room.id), "url": "https://b", "streamer": ""})
    assert rooms.list_rooms(conn)[0].url == "https://b"

    message = run(act, "rooms/delete", {"room_row_id": str(room.id)})
    assert "已删除直播间" in message
    assert actions_of(conn) == ["room.add", "room.update", "room.delete"]


def test_room_add_needs_all_three_fields(act):
    with pytest.raises(actions.ActionError):
        run(act, "rooms/add", {"platform": "huya", "room_id": "660000", "url": ""})


def test_room_update_needs_a_row(act):
    with pytest.raises(actions.ActionError):
        run(act, "rooms/update", {"room_row_id": "", "url": "https://a"})


# —— 切片 ——


def test_slice_override_records_actor_and_reason(act, conn):
    create_match(conn, league="LPL", team_a="iG", team_b="LNG", state="live")
    message = run(act, "slices/override", {
        "match_id": "1", "game_no": "1", "start_ms": str(BASE), "end_ms": str(BASE + 60_000),
        "reason": "对齐官方开赛时间",
    })
    window = load_slices(conn, 1)[0]
    assert window.boundary_source == "manual" and window.override_by == "admin"
    assert window.override_reason == "对齐官方开赛时间" and window.override_at == BASE
    assert "已新建切片" in message
    entry = audit.entries(conn, action=audit.SLICE_MANUAL)[-1]
    assert entry.actor == "admin" and entry.detail["reason"] == "对齐官方开赛时间"

    # 覆盖已有边界：走 `slice.override`（带前后值），动作留痕在领域函数里
    message = run(act, "slices/override", {
        "match_id": "1", "game_no": "1", "start_ms": str(BASE + 5_000), "end_ms": str(BASE + 65_000),
        "reason": "再对齐一次",
    })
    assert "人工修正" in message
    entry = audit.entries(conn, action=audit.SLICE_OVERRIDE)[-1]
    assert entry.detail["before"]["start_ms"] == BASE and entry.detail["after"]["start_ms"] == BASE + 5_000
    assert entry.detail["reason"] == "再对齐一次"


def test_slice_override_requires_a_reason(act, conn):
    create_match(conn, league="LPL", team_a="iG", team_b="LNG", state="live")
    with pytest.raises(actions.ActionError):
        run(act, "slices/override", {
            "match_id": "1", "game_no": "1", "start_ms": str(BASE), "end_ms": str(BASE + 1000), "reason": "",
        })


# —— 灰信号 ——


def test_gray_review_escalates_and_discards(act, conn):
    create_match(conn, league="LPL", team_a="iG", team_b="LNG", state="live")
    for index, keyword in enumerate(("盘口", "内幕")):
        conn.execute(
            "INSERT INTO gray_signals(match_id, category, keyword, hit_count, distinct_users,"
            " window_count, samples_json, status, reason, created_at)"
            " VALUES(1, 'betting', ?, 9, 4, 3, '[]', 'candidate', NULL, ?)",
            (keyword, BASE + index),
        )
    conn.commit()

    assert "升级" in run(act, "gray/review", {"signal_id": "1", "action": "escalate", "reason": "值得复核"})
    assert "作废" in run(act, "gray/review", {"signal_id": "2", "action": "discard", "reason": "同一人刷"})
    assert [item.status for item in gray_review.list_signals(conn)] == ["escalated", "discarded"]
    assert actions_of(conn) == ["gray.review", "gray.review"]


def test_gray_review_requires_reason(act, conn):
    create_match(conn, league="LPL", team_a="iG", team_b="LNG", state="live")
    conn.execute(
        "INSERT INTO gray_signals(match_id, category, keyword, hit_count, distinct_users,"
        " window_count, samples_json, status, created_at) VALUES(1, 'betting', '盘口', 9, 4, 3, '[]', 'candidate', ?)",
        (BASE,),
    )
    conn.commit()
    with pytest.raises(actions.ActionError):
        run(act, "gray/review", {"signal_id": "1", "action": "discard", "reason": ""})


# —— 发布与回滚 ——


def test_release_publish_and_rollback_go_through_the_cli_loop(act, conn, data_root):
    create_match(conn, league="LPL", team_a="iG", team_b="LNG", state="ended")
    message = run(act, "releases/publish", {"reason": "后台手动发布"})
    assert "已发布 v1" in message
    assert "release.publish" in actions_of(conn)

    with pytest.raises(actions.ActionError) as excinfo:
        run(act, "releases/rollback", {"to": "0"})
    assert "回滚没做成" in str(excinfo.value), "本地发布没有部署标识，回滚必须明说原因"


def test_release_publish_reports_a_refusal(act, conn, monkeypatch):
    from danmu_intel.publish import release as release_module

    def refuse(*args, **kwargs):
        raise release_module.ReleaseRefused(())

    monkeypatch.setattr(release_module, "publish_site", refuse)
    with pytest.raises(actions.ActionError) as excinfo:
        run(act, "releases/publish", {"reason": "手动"})
    assert "一个条目都没换" in str(excinfo.value)


# —— 会员与订单 ——


def test_member_grant_revoke_and_sweep(act, conn):
    configure_billing(conn)
    order, _ = orders.create_order(
        conn, platform="telegram", username="tester", tier="standard", network="polygon",
        now=BASE, config=pricing.load_billing_config(conn),
    )
    message = run(act, "members/grant", {
        "order_ref": order.public_ref, "tx_ref": "0xdead", "reason": "用户反馈已付款",
    })
    assert "已开通" in message
    member = members.list_members(conn)[0]
    assert member.status == "active"

    assert "幂等" in run(act, "members/grant", {
        "order_ref": order.public_ref, "tx_ref": "0xdead", "reason": "重复补一次",
    })

    assert "已撤权" in run(act, "members/revoke", {"member_id": str(member.id), "reason": "退款"})
    assert members.get_member(conn, member.id).status == "revoked"
    assert "没有需要降级的会员" in run(act, "members/sweep", {})
    assert "billing.member.granted" in actions_of(conn)
    assert "billing.member.revoked" in actions_of(conn)


def test_member_grant_needs_a_reason_and_a_tx(act, conn):
    configure_billing(conn)
    order, _ = orders.create_order(
        conn, platform="telegram", username="tester", tier="standard", network="polygon",
        now=BASE, config=pricing.load_billing_config(conn),
    )
    with pytest.raises(actions.ActionError):
        run(act, "members/grant", {"order_ref": order.public_ref, "tx_ref": "0xdead", "reason": ""})
    with pytest.raises(actions.ActionError):
        run(act, "members/grant", {"order_ref": order.public_ref, "tx_ref": "", "reason": "有理由"})


def test_member_grant_reports_a_short_payment(act, conn):
    configure_billing(conn)
    order, _ = orders.create_order(
        conn, platform="telegram", username="tester", tier="standard", network="polygon",
        now=BASE, config=pricing.load_billing_config(conn),
    )
    message = run(act, "members/grant", {
        "order_ref": order.public_ref, "tx_ref": "0xbeef", "units": "1", "reason": "只到了零头",
    })
    assert "仍差" in message
    assert orders.get_order(conn, public_ref=order.public_ref).status == "short"


def test_member_revoke_needs_a_reason(act, conn):
    member = members.get_or_create_member(conn, platform="telegram", username="tester", tier="standard")
    with pytest.raises(actions.ActionError):
        run(act, "members/revoke", {"member_id": str(member.id), "reason": "  "})


# —— 配置 ——


def test_config_stats_saves_and_bumps_version(act, conn):
    message = run(act, "config/stats", {"gray_min_hits": "7", "gray_keywords": "假赛=cheat_suspicion\n盘口 betting"})
    assert "配置" in message and "v1" in message
    config = load_stats_config(conn)
    assert config.gray_min_hits == 7
    assert config.gray_keywords == (("假赛", "cheat_suspicion"), ("盘口", "betting"))
    assert config_store.version(conn) == 1
    assert "config.update" in actions_of(conn)


def test_config_stats_rejects_unknown_category_and_empty_change(act):
    with pytest.raises(actions.ActionError) as excinfo:
        run(act, "config/stats", {"gray_keywords": "假赛=猜的类别"})
    assert "未知的灰信号类别" in str(excinfo.value)
    with pytest.raises(actions.ActionError):
        run(act, "config/stats", {"gray_keywords": "这一行没有分隔符"})
    with pytest.raises(actions.ActionError):
        run(act, "config/stats", {})


def test_config_stats_rejects_non_numeric(act):
    with pytest.raises(actions.ActionError) as excinfo:
        run(act, "config/stats", {"gray_min_hits": "很多"})
    assert "应该是数字" in str(excinfo.value)


def test_config_billing_saves_prices_and_addresses(act, conn):
    message = run(act, "config/billing", {
        "tiers": json.dumps([{"key": "standard", "label": "标准档", "amount_units": 5_000_000, "days": 30}]),
        "order_ttl_minutes": "45",
        "grace_hours": "12",
        "polygon_xpub": ACCOUNT_XPUB,
        "solana_address": "",
        "api_base": "https://host.ts.net",
    })
    config = pricing.load_billing_config(conn)
    assert config.order_ttl_ms == 45 * 60_000 and config.grace_ms == 12 * 3_600_000
    assert config.tiers[0].label == "标准档" and config.api_base == "https://host.ts.net"
    assert "标准档" in message and "v1" in message


def test_config_billing_validates_input(act):
    with pytest.raises(actions.ActionError) as excinfo:
        run(act, "config/billing", {"tiers": '{"key":"standard"}'})
    assert "JSON 数组" in str(excinfo.value)
    with pytest.raises(actions.ActionError):
        run(act, "config/billing", {"tiers": "[不是 JSON]"})
    with pytest.raises(actions.ActionError):
        run(act, "config/billing", {})


def test_config_billing_float_minutes_are_rounded_to_ms(act, conn):
    run(act, "config/billing", {"order_ttl_minutes": "30.5", "grace_hours": "0"})
    config = pricing.load_billing_config(conn)
    assert config.order_ttl_ms == 1_830_000 and config.grace_ms == 0


# —— 报告 ——


def test_report_generate_refuses_an_unknown_kind(act):
    with pytest.raises(actions.ActionError) as excinfo:
        run(act, "reports/generate", {"match_id": "1", "kind": "随便"})
    assert "未知的报告形态" in str(excinfo.value)


def test_report_generate_reports_publish_refusal(act, conn, monkeypatch):
    create_match(conn, league="LPL", team_a="iG", team_b="LNG", state="ended")
    import danmu_intel.pipeline as pipeline

    def refuse(*args, **kwargs):
        from danmu_intel.report.publish import CheckResult, PublishRefused

        raise PublishRefused((CheckResult("sources", "来源可达", False, "第 3 行对不上"),))

    monkeypatch.setattr(pipeline, "generate_and_publish", refuse)
    with pytest.raises(actions.ActionError) as excinfo:
        run(act, "reports/generate", {"match_id": "1", "kind": "full"})
    assert "来源可达" in str(excinfo.value)


# —— 登录留痕 ——


def test_login_and_logout_are_audited(act, conn):
    actions.log_login(act, ip="100.64.0.9", ok=False)
    actions.log_login(act, ip="100.64.0.9", ok=True)
    actions.log_logout(act, ip="100.64.0.9")
    entries = audit.entries(conn)
    assert [entry.action for entry in entries] == ["admin.login_failed", "admin.login", "admin.logout"]
    assert entries[0].actor == "unknown@100.64.0.9" and entries[1].actor == "admin"


def test_every_action_has_a_registered_page():
    """每个动作都要回得到一个已注册的页面（不然 303 会把人带进 404）。"""
    from danmu_intel.admin.pages import page_of

    for action in actions.ACTIONS:
        assert page_of(action.page).path.startswith("/admin")


def test_unknown_action_key_is_a_lookup_error():
    with pytest.raises(LookupError):
        actions.action_of("不存在/动作")
