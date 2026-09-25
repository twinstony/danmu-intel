"""会员与定价（T9）：身份规范化、状态机（开通/续费顺延/宽限/撤权）、收款配置。

时间一律注入（`now`），因此「到期 → 宽限 → 降级 → 续费顺延」这条时间线在一瞬间走完。
"""

from __future__ import annotations

import pytest

from danmu_intel.billing import members, pricing
from danmu_intel.billing.members import ContactError, MemberError
from danmu_intel.common import audit

BASE_MS = 1_790_000_000_000
DAY = 24 * 3600 * 1000


def test_normalize_contact_accepts_telegram_and_qq():
    assert members.normalize_contact("Telegram", "@TonyChan") == ("telegram", "tonychan")
    assert members.normalize_contact("qq", "12345678") == ("qq", "12345678")


@pytest.mark.parametrize(
    "platform, username",
    [
        ("discord", "@someone"),  # 平台不在白名单
        ("telegram", "ab"),  # 太短
        ("telegram", "1abc"),  # 不能以数字开头
        ("telegram", "名字"),  # 非 ASCII
        ("qq", "@12345"),  # QQ 号必须是数字
        ("qq", "123"),  # 太短
        ("", ""),
    ],
)
def test_normalize_contact_refuses_bad_identifiers(platform, username):
    with pytest.raises(ContactError):
        members.normalize_contact(platform, username)


def test_member_is_created_once_per_contact(conn):
    first = members.get_or_create_member(conn, platform="telegram", username="@TonyX", tier="standard", now=BASE_MS)
    again = members.get_or_create_member(conn, platform="telegram", username="tonyx", tier="trial", now=BASE_MS + 1)
    assert first.id == again.id
    assert again.status == "pending" and again.expires_at is None  # 下单还不算开通
    assert again.as_dict()["username"] == "@TonyX"


def test_find_member_is_none_for_unknown_or_invalid(conn):
    members.get_or_create_member(conn, platform="qq", username="12345678", tier="trial", now=BASE_MS)
    assert members.find_member(conn, platform="qq", username="12345678") is not None
    assert members.find_member(conn, platform="qq", username="12345679") is None
    assert members.find_member(conn, platform="discord", username="@nobody") is None


def test_grant_opens_and_extends_from_the_original_expiry(conn):
    """AC-6：续费从**原到期日**顺延，不是从付款日起算。"""
    member = members.get_or_create_member(conn, platform="telegram", username="@payer", tier="standard", now=BASE_MS)
    opened, changed = members.grant(conn, member_id=member.id, tier="standard", tx_ref="0xtx1", now=BASE_MS)
    assert changed and opened.status == "active"
    assert opened.expires_at == BASE_MS + 30 * DAY

    # 10 天后续费 → 到期日是「原到期日 + 30 天」，不是「付款日 + 30 天」
    renewed, changed = members.grant(
        conn, member_id=member.id, tier="standard", tx_ref="0xtx2", now=BASE_MS + 10 * DAY
    )
    assert changed and renewed.expires_at == BASE_MS + 60 * DAY
    assert renewed.status == "active"


def test_grant_is_idempotent_per_transaction(conn):
    """AC-4：同一笔交易（同一个 tx_ref）重复处理只开通一次。"""
    member = members.get_or_create_member(conn, platform="qq", username="12345678", tier="standard", now=BASE_MS)
    members.grant(conn, member_id=member.id, tier="standard", tx_ref="0xdup", now=BASE_MS)
    again, changed = members.grant(
        conn, member_id=member.id, tier="standard", tx_ref="0xdup", now=BASE_MS + 1
    )
    assert changed is False
    assert again.expires_at == BASE_MS + 30 * DAY  # 没有被重复累加
    assert members.already_granted(conn, "0xdup") and not members.already_granted(conn, "0xother")


def test_grant_after_expiry_starts_from_now(conn):
    member = members.get_or_create_member(conn, platform="qq", username="12345678", tier="standard", now=BASE_MS)
    members.grant(conn, member_id=member.id, tier="standard", tx_ref="0x1", now=BASE_MS)
    late = BASE_MS + 90 * DAY  # 早过了宽限期
    members.sweep(conn, now=late)
    assert members.get_member(conn, member.id).status == "expired"
    refreshed, _ = members.grant(conn, member_id=member.id, tier="trial", tx_ref="0x2", now=late)
    assert refreshed.status == "active" and refreshed.expires_at == late + 3 * DAY


def test_grant_refuses_revoked_member(conn):
    member = members.get_or_create_member(conn, platform="qq", username="12345678", tier="standard", now=BASE_MS)
    members.revoke(conn, member_id=member.id, actor="运营者", reason="退款", now=BASE_MS)
    with pytest.raises(MemberError, match="撤权"):
        members.grant(conn, member_id=member.id, tier="standard", tx_ref="0x3", now=BASE_MS)


def test_sweep_walks_active_to_grace_to_expired(conn):
    member = members.get_or_create_member(conn, platform="qq", username="12345678", tier="trial", now=BASE_MS)
    members.grant(conn, member_id=member.id, tier="trial", tx_ref="0x1", now=BASE_MS)
    expires_at = BASE_MS + 3 * DAY

    assert members.sweep(conn, now=expires_at) == []  # 还没到期：什么都不做
    [grace] = members.sweep(conn, now=expires_at + 1)
    assert grace.status == "grace"
    assert grace.can_access(now=expires_at + 2 * 3600 * 1000, grace_ms=DAY)  # 宽限期内仍可访问
    [expired] = members.sweep(conn, now=expires_at + 2 * DAY)
    assert expired.status == "expired"
    assert not expired.can_access(now=expires_at + 2 * DAY, grace_ms=DAY)

    actions = [entry.action for entry in audit.entries(conn)]
    assert actions.count(members.MEMBER_GRACE) == 1
    assert actions.count(members.MEMBER_EXPIRED) == 1


def test_sweep_is_quiet_when_nothing_changes(conn):
    member = members.get_or_create_member(conn, platform="qq", username="12345678", tier="trial", now=BASE_MS)
    members.grant(conn, member_id=member.id, tier="trial", tx_ref="0x1", now=BASE_MS)
    expires_at = BASE_MS + 3 * DAY
    members.sweep(conn, now=expires_at + 1)  # → grace
    assert members.sweep(conn, now=expires_at + 2) == []  # 已经是 grace：不再重复改
    assert members.sweep(conn, now=expires_at - DAY) == []


def test_tier_field_validation():
    with pytest.raises(pricing.BillingConfigError, match="必须有 key"):
        pricing.Tier(key="", label="", amount_units=1, days=1)
    with pytest.raises(pricing.BillingConfigError, match="未知的档位字段"):
        pricing.Tier.from_dict({"key": "x", "label": "X", "amount_units": 1, "days": 1, "vip": True})
    with pytest.raises(pricing.BillingConfigError, match="缺少字段"):
        pricing.Tier.from_dict({"key": "x", "label": "X"})
    with pytest.raises(pricing.BillingConfigError, match="档位 key 重复"):
        pricing.BillingConfig.from_dict(
            {
                "tiers": [
                    {"key": "x", "label": "X", "amount_units": 1, "days": 1},
                    {"key": "x", "label": "X2", "amount_units": 2, "days": 2},
                ]
            }
        )


def test_revoke_needs_a_reason_and_is_audited(conn):
    member = members.get_or_create_member(conn, platform="qq", username="12345678", tier="standard", now=BASE_MS)
    with pytest.raises(MemberError, match="理由"):
        members.revoke(conn, member_id=member.id, actor="运营者", reason="")
    revoked = members.revoke(conn, member_id=member.id, actor="运营者", reason="疑似盗号", now=BASE_MS)
    assert revoked.status == "revoked" and revoked.revoked_at == BASE_MS
    [entry] = audit.entries(conn, action=members.MEMBER_REVOKED)
    assert entry.actor == "运营者" and entry.detail["reason"] == "疑似盗号"


def test_list_members_filters_by_status(conn):
    members.get_or_create_member(conn, platform="qq", username="12345678", tier="standard", now=BASE_MS)
    members.get_or_create_member(conn, platform="telegram", username="@second", tier="trial", now=BASE_MS)
    assert len(members.list_members(conn)) == 2
    assert [item.username for item in members.list_members(conn, status="pending", limit=1)] == ["12345678"]
    with pytest.raises(MemberError, match="未知的会员状态"):
        members.list_members(conn, status="vip")


def test_contact_label_is_for_operators_only(conn):
    member = members.get_or_create_member(conn, platform="telegram", username="@payer", tier="standard", now=BASE_MS)
    assert member.contact == "@payer（telegram）"
    assert "username" in member.as_dict()  # 运营者看得到
    assert "payer" not in str(member.as_dict()["expires_at"])  # 对外输出里没有身份


# —— 定价与收款配置 ——


def test_default_tiers_follow_the_requirement_shape(conn):
    config = pricing.load_billing_config(conn)
    standard = config.tier("standard")
    trial = config.tier("trial")
    assert trial.amount_units < standard.amount_units and trial.days < standard.days  # FR-C6-1
    assert pricing.format_units(standard.amount_units) == "5.00"
    assert pricing.format_units(config.tiers[1].amount_units) == "0.50"
    assert pricing.format_units(1) == "0.000001"
    with pytest.raises(pricing.BillingConfigError, match="未知的档位"):
        config.tier("vip")


def test_save_billing_config_is_audited_and_partial(conn):
    pricing.save_billing_config(
        conn,
        actor="管理员",
        changes={"polygon_xpub": "xpub6DUMMY", "solana_address": "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"},
        ts=BASE_MS,
    )
    config = pricing.load_billing_config(conn)
    assert config.tiers == pricing.DEFAULT_TIERS  # 没改的部分保持默认
    assert config.require_xpub() == "xpub6DUMMY"
    [entry] = audit.entries(conn, action=audit.CONFIG_UPDATE, target_prefix="billing")
    assert entry.actor == "管理员" and entry.detail["after"]["polygon_xpub"] == "xpub6DUMMY"


def test_billing_config_refuses_unknown_keys_and_bad_values(conn):
    with pytest.raises(pricing.BillingConfigError, match="未知的收款配置项"):
        pricing.save_billing_config(conn, actor="管理员", changes={"vip_price": 1})
    with pytest.raises(pricing.BillingConfigError, match="订单时效"):
        pricing.save_billing_config(conn, actor="管理员", changes={"order_ttl_ms": 0})
    with pytest.raises(pricing.BillingConfigError, match="价格必须为正"):
        pricing.save_billing_config(
            conn, actor="管理员", changes={"tiers": [{"key": "x", "label": "X", "amount_units": 0, "days": 1}]}
        )
    with pytest.raises(pricing.BillingConfigError, match="天数必须为正"):
        pricing.Tier(key="x", label="X", amount_units=1, days=0)


def test_missing_receiving_config_is_refused_with_a_pointer(conn):
    config = pricing.load_billing_config(conn)
    with pytest.raises(pricing.BillingConfigError, match="xpub"):
        config.require_xpub()
    with pytest.raises(pricing.BillingConfigError, match="Solana 收款地址"):
        config.require_solana_address()


def test_assets_are_public_constants_and_match_per_chain(conn):
    assert pricing.asset_for("polygon").startswith("0x")
    assert pricing.asset_for("solana").startswith("Es9")
    # polygon 的合约地址来自链上接口，大小写不保证；solana 的 mint 逐字节比
    assert pricing.is_our_asset("polygon", pricing.USDT["polygon"].upper())
    assert not pricing.is_our_asset("solana", pricing.USDT["solana"].lower())
    with pytest.raises(pricing.BillingConfigError, match="未知的收款网络"):
        pricing.asset_for("base")


def test_sweep_keeps_going_and_reports_the_failures(conn, monkeypatch):
    """批处理失败不静默（设计 §15 #10）：一个会员出错不拖垮整批，失败名单进通知。"""
    from danmu_intel.common.notifications import recent

    first = members.get_or_create_member(conn, platform="qq", username="12345678", tier="trial", now=BASE_MS)
    second = members.get_or_create_member(conn, platform="telegram", username="@tonychan", tier="trial", now=BASE_MS)
    members.grant(conn, member_id=first.id, tier="trial", tx_ref="0x1", now=BASE_MS)
    members.grant(conn, member_id=second.id, tier="trial", tx_ref="0x2", now=BASE_MS)
    expires_at = BASE_MS + 3 * DAY

    original = members.get_member

    def flaky(connection, member_id):
        if member_id == second.id:
            raise RuntimeError("会员行读不出来")
        return original(connection, member_id)

    monkeypatch.setattr(members, "get_member", flaky)

    changed = members.sweep(conn, now=expires_at + 1)

    assert [member.id for member in changed] == [first.id], "坏掉的那个不拖垮整批"
    [item] = recent(conn, limit=5)
    assert item.kind == members.MEMBER_SWEEP_FAILED
    assert item.severity == "warning"
    assert item.payload == {
        "failed": 1,
        "changed": 1,
        "members": [{"member_id": second.id, "to_status": "grace", "error": "会员行读不出来"}],
    }


def test_sweep_reports_nothing_when_all_members_succeed(conn):
    from danmu_intel.common.notifications import recent

    member = members.get_or_create_member(conn, platform="qq", username="12345678", tier="trial", now=BASE_MS)
    members.grant(conn, member_id=member.id, tier="trial", tx_ref="0x1", now=BASE_MS)

    members.sweep(conn, now=BASE_MS + 3 * DAY + 1)

    assert recent(conn) == []
