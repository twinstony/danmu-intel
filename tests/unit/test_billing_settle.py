"""对账与开通（T9）：足额开通、重复只开一次、不足记差额、补扫找回、人工补开通。"""

from __future__ import annotations

import pytest

from danmu_intel.billing import members, orders, pricing, settle
from danmu_intel.billing.orders import OrderError
from danmu_intel.chain.transfer import NATIVE_ASSET, Transfer
from danmu_intel.chain.watcher import WatchTarget
from danmu_intel.common import audit
from danmu_intel.common.notifications import recent
from tests.unit.test_billing_xpub import ACCOUNT_XPUB, ADDRESSES

BASE_MS = 1_790_000_000_000
MINUTE = 60 * 1000
DAY = 24 * 3600 * 1000
WALLET = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"


@pytest.fixture
def config(conn) -> pricing.BillingConfig:
    return pricing.save_billing_config(
        conn, actor="测试", changes={"polygon_xpub": ACCOUNT_XPUB, "solana_address": WALLET}
    )


def polygon_transfer(order, *, tx_ref="0xpay", units=None, address=None, asset=None) -> Transfer:
    return Transfer(
        network="polygon",
        address=address or order.address,
        tx_ref=tx_ref,
        asset=asset or order.asset,
        units=order.amount_due_units if units is None else units,
        at_ms=BASE_MS,
        block=100,
    )


def solana_transfer(order, *, tx_ref="sig-1", units=None, memo=None, address=None) -> Transfer:
    return Transfer(
        network="solana",
        address=address or order.address,
        tx_ref=tx_ref,
        asset=order.asset,
        units=order.amount_due_units if units is None else units,
        at_ms=BASE_MS,
        memo=order.memo if memo is None else memo,
        block=250_000_000,
    )


def make_order(conn, config, *, network="polygon", tier="standard", username="@payer", now=BASE_MS):
    order, _ = orders.create_order(
        conn, platform="telegram", username=username, tier=tier, network=network, now=now,
        config=config,
    )
    return order


def test_full_payment_opens_access_without_a_human(conn, config):
    """AC-3：入账 → 订单 paid → 会员 active，全程没有人工步骤。"""
    order = make_order(conn, config)
    result = settle.settle(conn, [polygon_transfer(order)], now=BASE_MS + MINUTE, config=config)

    assert result.failures == []
    assert [item.outcome for item in result.payments] == ["recorded"]
    assert result.granted == [order.member_id]
    paid = orders.get_order(conn, order_id=order.id)
    assert paid.status == "paid" and paid.tx_ref == "0xpay" and paid.shortage_units == 0
    assert paid.paid_units == paid.amount_due_units
    member = members.get_member(conn, order.member_id)
    assert member.status == "active"
    assert member.expires_at == BASE_MS + MINUTE + config.tier("standard").duration_ms
    assert member.can_access(now=BASE_MS + DAY, grace_ms=config.grace_ms)
    # 开通留痕（FR-C6-16）：谁、何时、因哪笔交易、开通到何时
    [entry] = audit.entries(conn, action=members.MEMBER_GRANTED)
    assert entry.detail["tx_ref"] == "0xpay" and entry.detail["to_expires_at"] == member.expires_at


def test_repeated_detection_only_opens_once(conn, config):
    """AC-4：同一笔付款被重复扫到多少次，只开通一次、不重复累加。"""
    order = make_order(conn, config)
    transfer = polygon_transfer(order)
    for _ in range(3):
        result = settle.settle(conn, [transfer], now=BASE_MS + MINUTE, config=config)
    assert [item.outcome for item in result.payments] == ["duplicate"]
    assert result.granted == []
    member = members.get_member(conn, order.member_id)
    assert member.expires_at == BASE_MS + MINUTE + config.tier("standard").duration_ms
    assert len(orders.list_orders(conn, status="paid")) == 1


def test_short_payment_records_the_difference_and_completes_later(conn, config):
    """AC-4/FR-C6-7：不足额转「待补款」+ 差额通知；补一笔够了就开通。"""
    order = make_order(conn, config)
    half = order.amount_due_units // 2
    settle.settle(conn, [polygon_transfer(order, units=half)], now=BASE_MS + MINUTE, config=config)

    short = orders.get_order(conn, order_id=order.id)
    assert short.status == "short"
    assert short.paid_units == half and short.shortage_units == order.amount_due_units - half
    [notice] = [item for item in recent(conn) if item.kind == settle.PAYMENT_SHORT]
    assert notice.severity == "warning"
    assert notice.payload["shortage_units"] == order.amount_due_units - half

    settle.settle(
        conn,
        [polygon_transfer(order, tx_ref="0xtop", units=order.amount_due_units - half)],
        now=BASE_MS + 2 * MINUTE,
        config=config,
    )
    paid = orders.get_order(conn, order_id=order.id)
    assert paid.status == "paid" and paid.paid_units == order.amount_due_units


def test_missed_payment_is_found_by_rescan_and_settled(conn, config):
    """AC-5：实时检测漏掉一笔（限速那一轮什么都没看见），事后补扫拿到的入账照样开通。"""
    order = make_order(conn, config)
    settle.settle(conn, [], now=BASE_MS + MINUTE, config=config)  # 「这一轮供应商限速」
    assert orders.get_order(conn, order_id=order.id).status == "pending"

    late = polygon_transfer(order, tx_ref="0xmissed")
    result = settle.settle(conn, [late], now=BASE_MS + 2 * MINUTE, config=config)
    assert result.granted == [order.member_id]
    assert orders.get_order(conn, order_id=order.id).status == "paid"


def test_unmatched_and_extra_transfers_are_reported_not_swallowed(conn, config):
    order = make_order(conn, config)
    other = polygon_transfer(order, tx_ref="0xother", address=ADDRESSES[2])
    native = polygon_transfer(order, tx_ref="0xnative", asset=NATIVE_ASSET)
    result = settle.settle(conn, [other, native], now=BASE_MS + MINUTE, config=config)

    assert [item.outcome for item in result.payments] == ["unmatched", "unmatched"]
    kinds = [item.kind for item in recent(conn)]
    assert kinds.count(settle.PAYMENT_UNMATCHED) == 2
    assert all(item.severity == "warning" for item in recent(conn))

    # 已付订单又收到一笔：记账并提醒，但**不再开通一次**
    settle.settle(conn, [polygon_transfer(order, tx_ref="0xfirst")], now=BASE_MS + MINUTE, config=config)
    again = settle.settle(
        conn,
        [polygon_transfer(order, tx_ref="0xsecond", units=1_000_000)],
        now=BASE_MS + 2 * MINUTE,
        config=config,
    )
    assert [item.outcome for item in again.payments] == ["extra"]
    assert again.granted == []
    assert settle.PAYMENT_EXTRA in [item.kind for item in recent(conn)]
    member = members.get_member(conn, order.member_id)
    assert member.expires_at == BASE_MS + MINUTE + config.tier("standard").duration_ms


def test_solana_orders_are_matched_by_memo(conn, config):
    order = make_order(conn, config, network="solana")
    wrong_memo = settle.settle(
        conn, [solana_transfer(order, memo="DMOTHER")], now=BASE_MS + MINUTE, config=config
    )
    assert [item.outcome for item in wrong_memo.payments] == ["unmatched"]

    good = settle.settle(conn, [solana_transfer(order)], now=BASE_MS + MINUTE, config=config)
    assert good.granted == [order.member_id]
    assert orders.get_order(conn, order_id=order.id).status == "paid"


def test_watch_targets_come_from_open_orders(conn, config):
    polygon = make_order(conn, config, username="@payer")
    solana = make_order(conn, config, network="solana", username="@second")
    targets = settle.watch_targets(conn, config=config, now=BASE_MS)
    assert WatchTarget("polygon", polygon.address) in targets
    assert WatchTarget("solana", WALLET) in targets
    assert len(targets) == 2

    # 付清之后就不再监听它（订单结清 = 目标消失）
    settle.settle(conn, [polygon_transfer(polygon)], now=BASE_MS + MINUTE, config=config)
    settle.settle(conn, [solana_transfer(solana)], now=BASE_MS + MINUTE, config=config)
    assert settle.watch_targets(conn, config=config, now=BASE_MS + 2 * MINUTE) == []


def test_expired_orders_drop_out_of_watch_targets(conn, config):
    order = make_order(conn, config)
    assert settle.watch_targets(conn, config=config, now=BASE_MS)
    later = BASE_MS + config.order_ttl_ms + 1
    assert settle.watch_targets(conn, config=config, now=later) == []
    assert orders.get_order(conn, order_id=order.id).status == "pending"  # 只是不问它，不改状态


def test_grant_failure_is_reported_as_critical(conn, config, monkeypatch):
    order = make_order(conn, config)

    def boom(*args, **kwargs):
        raise RuntimeError("会员表写不进去")

    monkeypatch.setattr(members, "grant", boom)
    result = settle.settle(conn, [polygon_transfer(order)], now=BASE_MS + MINUTE, config=config)
    assert result.granted == [] and len(result.failures) == 1
    [notice] = [item for item in recent(conn) if item.kind == settle.GRANT_FAILED]
    assert notice.severity == "critical" and notice.payload["order_ref"] == order.public_ref


def test_manual_payment_completes_a_missed_payment_with_an_audit_trail(conn, config):
    """AC-5 后半段：管理员凭交易凭证补开通，操作留痕。"""
    order = make_order(conn, config)
    paid, opened = settle.manual_payment(
        conn,
        order_ref=order.public_ref,
        tx_ref="0xmanual",
        actor="运营者",
        reason="用户提供了区块浏览器链接，链上确认已足额",
        now=BASE_MS + MINUTE,
        config=config,
    )
    assert opened and paid.status == "paid"
    opened_member_expiry = members.get_member(conn, order.member_id).expires_at
    assert orders.paid_total(conn, order.id) == order.amount_due_units
    [entry] = audit.entries(conn, action=settle.MANUAL_GRANT)
    assert entry.actor == "运营者" and "链上确认" in entry.detail["reason"]
    assert members.get_member(conn, order.member_id).status == "active"

    # 幂等：人工补一次、补扫又认一次 → 只开通一次
    again, opened_again = settle.manual_payment(
        conn, order_ref=order.public_ref, tx_ref="0xmanual", actor="运营者", reason="重试",
        now=BASE_MS + 2 * MINUTE, config=config,
    )
    assert opened_again is False
    assert members.get_member(conn, order.member_id).expires_at == opened_member_expiry
    result = settle.settle(
        conn,
        [polygon_transfer(order, tx_ref="0xmanual")],
        now=BASE_MS + 3 * MINUTE,
        config=config,
    )
    assert [item.outcome for item in result.payments] == ["duplicate"]
    assert members.get_member(conn, order.member_id).expires_at == opened_member_expiry


def test_manual_payment_requires_a_reason_and_a_transaction(conn, config):
    order = make_order(conn, config)
    with pytest.raises(OrderError, match="理由"):
        settle.manual_payment(
            conn, order_ref=order.public_ref, tx_ref="0x1", actor="运营者", reason="",
            now=BASE_MS, config=config,
        )
    with pytest.raises(OrderError, match="交易凭证"):
        settle.manual_payment(
            conn, order_ref=order.public_ref, tx_ref="", actor="运营者", reason="补",
            now=BASE_MS, config=config,
        )
    with pytest.raises(LookupError):
        settle.manual_payment(
            conn, order_ref="DMNOPE00", tx_ref="0x2", actor="运营者", reason="补",
            now=BASE_MS, config=config,
        )


def test_manual_payment_can_only_short_pay(conn, config):
    order = make_order(conn, config)
    short, opened = settle.manual_payment(
        conn, order_ref=order.public_ref, tx_ref="0xsmall", units=1, actor="运营者",
        reason="用户先补了一小笔", now=BASE_MS + MINUTE, config=config,
    )
    assert opened is False and short.status == "short"
    assert short.shortage_units == order.amount_due_units - 1
