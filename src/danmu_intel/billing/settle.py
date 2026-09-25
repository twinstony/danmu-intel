"""链上入账 → 订单匹配 → 幂等开通（设计 §12.2 ⑤⑥⑦ / §12.3；AC-3/4/5/7）。

这是 T8（看见入账）与 T9（订单/会员）之间的那道缝 —— 设计 §18 的 S3「支付决策缝」：
`decision = f(observed_transfers, orders)`。输入是**纯数据**（一笔笔 `Transfer`），
因此整条「付款 → 开通」链路在断网下可验，不需要真链（NFR-GA-4）。

三条规矩：

- **重复检测只开通一次**（AC-4）：`order_payments.tx_ref` 唯一，重复的交易什么都不做；
  开通本身还有第二道幂等键（`members.grant` 按 `tx_ref` 去重）。
- **不足额不静默**（FR-C6-7）：记差额、订单转 `short`、写一条待投递通知；
  补一笔到账后继续累计，够了就开通。
- **异常不静默**：找不到订单的入账、已付订单的重复入账、开通失败，都写 `notifications`
  （开通失败是 `critical`，设计 §15 第 7 行）。
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field

from danmu_intel.billing import members, orders, pricing
from danmu_intel.billing.orders import Order, OrderError
from danmu_intel.chain.transfer import POLYGON, SOLANA, Transfer

from danmu_intel.chain.watcher import WatchTarget
from danmu_intel.common import audit, notifications

#: 通知类型（设计 §15 第 7/8 行 + 两类「对不上账」的入账）。
PAYMENT_SHORT = "billing_payment_short"
PAYMENT_UNMATCHED = "billing_transfer_unmatched"
PAYMENT_EXTRA = "billing_payment_extra"
GRANT_FAILED = "billing_grant_failed"
MANUAL_GRANT = "billing.member.granted_manual"


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True, slots=True)
class Payment:
    """一笔入账处理的结果。"""

    order_id: int
    public_ref: str
    tx_ref: str
    units: int
    total_units: int
    outcome: str  # recorded | duplicate | extra | unmatched

    @property
    def label(self) -> str:
        return {
            "recorded": "已记账",
            "duplicate": "重复检测（忽略）",
            "extra": "已付订单又收到一笔（忽略并提醒）",
            "unmatched": "没有对应订单（提醒）",
        }[self.outcome]


@dataclass(frozen=True, slots=True)
class SettleResult:
    payments: list[Payment] = field(default_factory=list)
    granted: list[int] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def duplicates(self) -> list[Payment]:
        return [item for item in self.payments if item.outcome == "duplicate"]

    @property
    def unmatched(self) -> list[Payment]:
        return [item for item in self.payments if item.outcome == "unmatched"]

    def summary(self) -> str:
        return (
            f"入账 {len(self.payments)} 笔（重复 {len(self.duplicates)}、"
            f"对不上账 {len(self.unmatched)}）｜开通 {len(self.granted)} 次｜"
            f"失败 {len(self.failures)}"
        )


def watch_targets(
    conn: sqlite3.Connection, *, config: pricing.BillingConfig | None = None, now: int | None = None
) -> list[WatchTarget]:
    """从**未过期且未付清**的订单派生监听目标（AC-3：不需要人手工填地址）。

    Polygon 一单一地址（各自的派生地址）；Solana 一个收款地址就够 —— 订单靠 memo 区分。
    """
    config = config or pricing.load_billing_config(conn)
    open_orders = orders.open_orders(conn, now=now)
    targets: list[WatchTarget] = []
    polygon_addresses = [order.address for order in open_orders if order.network == POLYGON]
    if polygon_addresses:
        targets.extend(WatchTarget(POLYGON, address) for address in sorted(set(polygon_addresses)))
    if any(order.network == SOLANA for order in open_orders) and config.solana_address:
        targets.append(WatchTarget(SOLANA, config.solana_address))
    return targets


def settle(
    conn: sqlite3.Connection,
    transfers: list[Transfer],
    *,
    now: int | None = None,
    config: pricing.BillingConfig | None = None,
    actor: str = "chain",
) -> SettleResult:
    """把一批入账对到订单上：记账 → 足额则开通（幂等）／不足则记差额。"""
    stamp = now_ms() if now is None else now
    config = config or pricing.load_billing_config(conn)
    orders.expire_due(conn, now=stamp, config=config)

    payments: list[Payment] = []
    granted: list[int] = []
    failures: list[str] = []
    for transfer in transfers:
        order = match_order(conn, transfer)
        if order is None:
            payments.append(
                Payment(
                    order_id=0,
                    public_ref="-",
                    tx_ref=transfer.tx_ref,
                    units=transfer.units,
                    total_units=0,
                    outcome="unmatched",
                )
            )
            notifications.emit(
                conn,
                PAYMENT_UNMATCHED,
                severity="warning",
                payload={
                    "network": transfer.network,
                    "address": transfer.address,
                    "tx_ref": transfer.tx_ref,
                    "asset": transfer.asset,
                    "units": transfer.units,
                    "memo": transfer.memo,
                },
                timestamp=stamp,
            )
            continue

        recorded = record_payment(
            conn,
            order,
            tx_ref=transfer.tx_ref,
            units=transfer.units,
            at_ms=transfer.at_ms,
            now=stamp,
        )
        total = orders.paid_total(conn, order.id)
        if not recorded:
            payments.append(
                Payment(order.id, order.public_ref, transfer.tx_ref, transfer.units, total, "duplicate")
            )
            continue  # AC-4：同一笔付款再扫到多少次都只算一次

        if order.status == "paid":
            payments.append(
                Payment(order.id, order.public_ref, transfer.tx_ref, transfer.units, total, "extra")
            )
            notifications.emit(
                conn,
                PAYMENT_EXTRA,
                severity="warning",
                payload={
                    "order_ref": order.public_ref,
                    "tx_ref": transfer.tx_ref,
                    "units": transfer.units,
                    "total_units": total,
                    "amount_due_units": order.amount_due_units,
                },
                timestamp=stamp,
            )
            continue

        payments.append(
            Payment(order.id, order.public_ref, transfer.tx_ref, transfer.units, total, "recorded")
        )
        if total < order.amount_due_units:
            shortage = order.amount_due_units - total
            set_short(conn, order, total=total, shortage=shortage)
            notifications.emit(
                conn,
                PAYMENT_SHORT,
                severity="warning",
                payload={
                    "order_ref": order.public_ref,
                    "tx_ref": transfer.tx_ref,
                    "paid_units": total,
                    "amount_due_units": order.amount_due_units,
                    "shortage_units": shortage,
                },
                timestamp=stamp,
            )
            continue
        try:
            granted.append(open_access(conn, order, total=total, tx_ref=transfer.tx_ref, now=stamp, config=config, actor=actor))
        except Exception as exc:  # 开通失败绝不许静默（设计 §15 第 7 行：高危）
            failures.append(f"订单 {order.public_ref}：{exc}")
            notifications.emit(
                conn,
                GRANT_FAILED,
                severity="critical",
                payload={
                    "order_ref": order.public_ref,
                    "tx_ref": transfer.tx_ref,
                    "member_id": order.member_id,
                    "reason": str(exc),
                },
                timestamp=stamp,
            )
    return SettleResult(payments=payments, granted=granted, failures=failures)


def match_order(conn: sqlite3.Connection, transfer: Transfer) -> Order | None:
    """这笔入账属于哪个订单（地址 / memo 定位，资产与网络在 `orders.matches` 里复核）。

    已付订单也算匹配上：重复/多付的入账要能被认出来并提醒，而不是当成"对不上账"。
    """
    candidates = orders.candidates_for(conn, transfer)
    matching = [order for order in candidates if orders.matches(order, transfer)]
    if not matching:
        return None
    open_ones = [order for order in matching if order.is_open]
    return (open_ones or matching)[-1]


def record_payment(
    conn: sqlite3.Connection,
    order: Order,
    *,
    tx_ref: str,
    units: int,
    at_ms: int,
    now: int,
) -> bool:
    """记一笔入账；`tx_ref` 已存在则返回 `False`（AC-4 的幂等键）。"""
    try:
        conn.execute(
            "INSERT INTO order_payments(order_id, tx_ref, network, asset, units, at_ms, recorded_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            (order.id, tx_ref, order.network, order.asset, units, at_ms, now),
        )
    except sqlite3.IntegrityError:
        return False
    conn.commit()
    return True


def set_short(conn: sqlite3.Connection, order: Order, *, total: int, shortage: int) -> Order:
    """订单转「待补款」并记差额（FR-C6-7：不静默失败，明确告知还差多少）。"""
    conn.execute(
        "UPDATE orders SET status='short', paid_units=?, shortage_units=? WHERE id=?",
        (total, shortage, order.id),
    )
    conn.commit()
    return orders.get_order(conn, order_id=order.id)


def open_access(
    conn: sqlite3.Connection,
    order: Order,
    *,
    total: int,
    tx_ref: str,
    now: int,
    config: pricing.BillingConfig,
    actor: str = "chain",
) -> int:
    """订单转 `paid` 并开通/续费（幂等：同一 `tx_ref` 只开一次），返回会员 id。"""
    conn.execute(
        "UPDATE orders SET status='paid', paid_at=?, tx_ref=?, paid_units=?, shortage_units=0 "
        "WHERE id=?",
        (now, tx_ref, total, order.id),
    )
    conn.commit()
    _, changed = members.grant(
        conn,
        member_id=order.member_id,
        tier=order.tier,
        tx_ref=tx_ref,
        now=now,
        grace_ms=config.grace_ms,
        actor=actor,
    )
    if not changed:
        # 重复检测：订单状态还是被纠成 paid（账要对），但**不重复累加有效期**（AC-4）
        notifications.emit(
            conn,
            PAYMENT_EXTRA,
            severity="warning",
            payload={"order_ref": order.public_ref, "tx_ref": tx_ref, "repeat_grant": True},
            timestamp=now,
        )
    return order.member_id


def manual_payment(
    conn: sqlite3.Connection,
    *,
    order_ref: str,
    tx_ref: str,
    actor: str,
    reason: str,
    units: int | None = None,
    at_ms: int | None = None,
    now: int | None = None,
    config: pricing.BillingConfig | None = None,
) -> tuple[Order, bool]:
    """人工补开通（AC-5 后半段）：运营者凭交易凭证补记一笔入账 → 开通 + 留痕。

    必须给 `tx_ref` 与理由：人工动作也要能复核，而且同样受幂等键保护
    （同一笔交易被人工补一次、又被补扫认一次，只开通一次）。
    """
    if not reason:
        raise OrderError("人工补开通必须写明理由（FR-C6-16：资金操作必须可审计）")
    if not tx_ref:
        raise OrderError("人工补开通要给出链上交易凭证（tx_ref）")
    stamp = now_ms() if now is None else now
    config = config or pricing.load_billing_config(conn)
    order = orders.get_order(conn, public_ref=order_ref)
    amount = order.amount_due_units if units is None else units
    recorded = record_payment(
        conn, order, tx_ref=tx_ref, units=amount, at_ms=at_ms or stamp, now=stamp
    )
    total = orders.paid_total(conn, order.id)
    if order.status == "paid":
        return orders.get_order(conn, order_id=order.id), False
    if total < order.amount_due_units:
        set_short(conn, order, total=total, shortage=order.amount_due_units - total)
        return orders.get_order(conn, order_id=order.id), False
    audit.record(
        conn,
        actor=actor,
        action=MANUAL_GRANT,
        target=str(order.id),
        detail={"order_ref": order.public_ref, "tx_ref": tx_ref, "units": amount, "reason": reason},
        ts=stamp,
    )
    open_access(conn, order, total=total, tx_ref=tx_ref, now=stamp, config=config, actor=actor)
    return orders.get_order(conn, order_id=order.id), True
