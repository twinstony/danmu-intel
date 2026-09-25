"""订单状态机与收款要求（T9）：派生地址只前进、Solana memo、金额尾数、复用与过期。"""

from __future__ import annotations

import pytest

from danmu_intel.billing import members, orders, pricing
from danmu_intel.billing.orders import OrderError
from danmu_intel.chain.transfer import NATIVE_ASSET, Transfer
from tests.unit.test_billing_xpub import ACCOUNT_XPUB, ADDRESSES

BASE_MS = 1_790_000_000_000
MINUTE = 60 * 1000
WALLET = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"
USDT_POLYGON = pricing.USDT["polygon"]
USDT_SOLANA = pricing.USDT["solana"]


@pytest.fixture
def config(conn) -> pricing.BillingConfig:
    return pricing.save_billing_config(
        conn, actor="测试", changes={"polygon_xpub": ACCOUNT_XPUB, "solana_address": WALLET}
    )


def test_polygon_order_derives_a_dedicated_address(conn, config):
    order, token = orders.create_order(
        conn, platform="telegram", username="@payer", tier="standard", network="polygon",
        now=BASE_MS, config=config,
    )
    assert order.address == ADDRESSES[0] and order.address_index == 0
    assert order.memo is None and order.asset == USDT_POLYGON and order.status == "pending"
    assert order.public_ref.startswith("DM") and len(order.public_ref) == 10
    assert order.expires_at == BASE_MS + config.order_ttl_ms
    assert order.amount_due_units > config.tier("standard").amount_units  # 带唯一尾数
    assert orders.claim_token_matches(token, orders.claim_hash(conn, order.id))
    assert not orders.claim_token_matches(token + "x", orders.claim_hash(conn, order.id))
    assert not orders.claim_token_matches("", orders.claim_hash(conn, order.id))
    assert len(token) >= 32  # 不是短令牌


def test_addresses_advance_and_are_never_reused(conn, config):
    first, _ = orders.create_order(
        conn, platform="telegram", username="@payer", tier="standard", network="polygon",
        now=BASE_MS, config=config,
    )
    second, _ = orders.create_order(
        conn, platform="qq", username="12345678", tier="standard", network="polygon",
        now=BASE_MS, config=config,
    )
    third, _ = orders.create_order(
        conn, platform="qq", username="87654321", tier="trial", network="polygon",
        now=BASE_MS, config=config,
    )
    assert [first.address, second.address, third.address] == list(ADDRESSES[:3])
    assert [first.address_index, second.address_index, third.address_index] == [0, 1, 2]

    # 第一单过期后重来：索引继续前进，绝不复用已展示过的地址（设计 §12.2 ⑧）
    orders.expire_due(conn, now=BASE_MS + config.order_ttl_ms + 1, config=config)
    again, _ = orders.create_order(
        conn, platform="telegram", username="@payer", tier="standard", network="polygon",
        now=BASE_MS + config.order_ttl_ms + 2, config=config,
    )
    assert again.address == ADDRESSES[3] and again.address_index == 3


def test_unpaid_order_is_reused_and_the_claim_token_rotates(conn, config):
    first, first_token = orders.create_order(
        conn, platform="telegram", username="@payer", tier="standard", network="polygon",
        now=BASE_MS, config=config,
    )
    again, second_token = orders.create_order(
        conn, platform="telegram", username="@payer", tier="standard", network="polygon",
        now=BASE_MS + MINUTE, config=config,
    )
    assert again.id == first.id and again.address == first.address  # 不生成新地址
    assert second_token != first_token
    assert orders.claim_token_matches(second_token, orders.claim_hash(conn, again.id))
    assert not orders.claim_token_matches(first_token, orders.claim_hash(conn, again.id))


def test_trial_tier_is_once_per_member(conn, config):
    order, _ = orders.create_order(
        conn, platform="qq", username="12345678", tier="trial", network="polygon", now=BASE_MS,
        config=config,
    )
    assert members.trial_used(conn, order.member_id) is False
    conn.execute("UPDATE orders SET status='paid' WHERE id=?", (order.id,))
    conn.commit()
    assert members.trial_used(conn, order.member_id) is True
    with pytest.raises(OrderError, match="试用档每位会员只能享受一次"):
        orders.create_order(
            conn, platform="qq", username="12345678", tier="trial", network="polygon",
            now=BASE_MS + MINUTE, config=config,
        )
    # 标准档不受限
    orders.create_order(
        conn, platform="qq", username="12345678", tier="standard", network="polygon",
        now=BASE_MS + MINUTE, config=config,
    )


def test_solana_order_uses_one_address_plus_a_unique_memo(conn, config):
    order, _ = orders.create_order(
        conn, platform="telegram", username="@payer", tier="standard", network="solana",
        now=BASE_MS, config=config,
    )
    assert order.address == WALLET and order.address_index is None
    assert order.memo == order.public_ref and order.asset == USDT_SOLANA

    other, _ = orders.create_order(
        conn, platform="qq", username="12345678", tier="standard", network="solana",
        now=BASE_MS, config=config,
    )
    assert other.address == order.address and other.memo != order.memo  # 同地址靠 memo 区分


def test_matching_rules_per_chain(conn, config):
    polygon, _ = orders.create_order(
        conn, platform="telegram", username="@payer", tier="standard", network="polygon",
        now=BASE_MS, config=config,
    )
    solana, _ = orders.create_order(
        conn, platform="qq", username="12345678", tier="standard", network="solana",
        now=BASE_MS, config=config,
    )

    def transfer(network, address, asset, **extra):
        return Transfer(
            network=network, address=address, tx_ref=extra.pop("tx_ref", "0xtx"),
            asset=asset, units=extra.pop("units", 1_000_000), at_ms=BASE_MS, **extra,
        )

    assert orders.matches(polygon, transfer("polygon", polygon.address.upper(), USDT_POLYGON.upper()))
    assert not orders.matches(polygon, transfer("polygon", polygon.address, NATIVE_ASSET))
    assert not orders.matches(polygon, transfer("solana", polygon.address, USDT_POLYGON))
    assert not orders.matches(
        polygon, transfer("polygon", ADDRESSES[1], USDT_POLYGON)
    )  # 别人的派生地址

    assert orders.matches(
        solana, transfer("solana", WALLET, USDT_SOLANA, memo=solana.memo)
    )
    assert not orders.matches(solana, transfer("solana", WALLET, USDT_SOLANA, memo="DMOTHER"))
    assert not orders.matches(solana, transfer("solana", WALLET, USDT_SOLANA))  # 没有 memo
    assert not orders.matches(solana, transfer("solana", WALLET, NATIVE_ASSET, memo=solana.memo))
    assert not orders.matches(
        solana, transfer("solana", "9xOtherWallet", USDT_SOLANA, memo=solana.memo)
    )


def test_amounts_have_unique_tails_until_the_ceiling(conn, config, monkeypatch):
    monkeypatch.setattr(orders, "MAX_TAIL", 2)
    amounts = []
    for index, username in enumerate(("12345678", "87654321")):
        order, _ = orders.create_order(
            conn, platform="qq", username=username, tier="standard", network="polygon",
            now=BASE_MS, config=config,
        )
        amounts.append(order.amount_due_units)
    assert len(set(amounts)) == 2
    with pytest.raises(OrderError, match="同价订单"):
        orders.create_order(
            conn, platform="qq", username="11111111", tier="standard", network="polygon",
            now=BASE_MS, config=config,
        )


def test_order_lookups_and_expiry(conn, config):
    order, _ = orders.create_order(
        conn, platform="qq", username="12345678", tier="standard", network="polygon",
        now=BASE_MS, config=config,
    )
    assert orders.get_order(conn, public_ref=order.public_ref).id == order.id
    assert orders.get_order(conn, order_id=order.id).public_ref == order.public_ref
    with pytest.raises(OrderError, match="其中之一"):
        orders.get_order(conn)
    with pytest.raises(OrderError, match="其中之一"):
        orders.get_order(conn, order_id=1, public_ref="DMX")
    with pytest.raises(LookupError, match="未找到订单"):
        orders.get_order(conn, public_ref="DMNOPE00")

    assert [item.id for item in orders.open_orders(conn, now=BASE_MS)] == [order.id]
    assert orders.expire_due(conn, now=BASE_MS) == []  # 还没到期
    [expired] = orders.expire_due(conn, now=order.expires_at, config=config)
    assert expired.status == "expired" and not expired.is_open
    assert orders.open_orders(conn, now=order.expires_at) == []
    assert [item.id for item in orders.list_orders(conn, status="expired")] == [order.id]
    with pytest.raises(OrderError, match="未知的订单状态"):
        orders.list_orders(conn, status="cancelled")
    assert orders.list_orders(conn, network="solana") == []
    assert orders.list_orders(conn, limit=1)[0].id == order.id


def test_paid_total_sums_recorded_payments(conn, config):
    order, _ = orders.create_order(
        conn, platform="qq", username="12345678", tier="standard", network="polygon",
        now=BASE_MS, config=config,
    )
    assert orders.paid_total(conn, order.id) == 0
    for tx_ref, units in (("0xa", 1), ("0xb", 2)):
        conn.execute(
            "INSERT INTO order_payments(order_id, tx_ref, network, asset, units, at_ms, recorded_at) "
            "VALUES(?, ?, 'polygon', ?, ?, ?, ?)",
            (order.id, tx_ref, USDT_POLYGON, units, BASE_MS, BASE_MS),
        )
    conn.commit()
    assert orders.paid_total(conn, order.id) == 3


def test_receiving_config_must_be_present_before_taking_money(conn):
    config = pricing.load_billing_config(conn)
    with pytest.raises(pricing.BillingConfigError, match="xpub"):
        orders.create_order(
            conn, platform="qq", username="12345678", tier="standard", network="polygon",
            now=BASE_MS, config=config,
        )
    with pytest.raises(pricing.BillingConfigError, match="Solana 收款地址"):
        orders.create_order(
            conn, platform="qq", username="12345678", tier="standard", network="solana",
            now=BASE_MS, config=config,
        )
    with pytest.raises(pricing.BillingConfigError, match="未知的档位"):
        orders.create_order(
            conn, platform="qq", username="12345678", tier="vip", network="polygon",
            now=BASE_MS, config=config,
        )
    with pytest.raises(OrderError, match="未知的收款网络"):
        orders.create_order(
            conn, platform="qq", username="12345678", tier="standard", network="base",
            now=BASE_MS, config=config,
        )


def test_order_display_fields(conn, config):
    order, _ = orders.create_order(
        conn, platform="qq", username="12345678", tier="trial", network="solana",
        now=BASE_MS, config=config,
    )
    body = order.as_dict()
    assert body["amount"] == order.amount_display and body["memo"] == order.public_ref
    assert order.shortage_display == "0.00"
    assert body["status"] == "pending"
