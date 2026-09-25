"""订单状态机与收款要求（设计 §12.2；需求 FR-C6-4..10）。

一个订单 = 一个收款要求：

- **Polygon**：从 xpub 派生的**专属地址**（`m/44'/60'/0'/0/i`，索引只前进不回退，设计 §12.2 ⑧）；
- **Solana**：**单一收款地址 + 每单唯一 memo**（`public_ref`，ADR-0004 允许的"唯一标识"）；
- 金额 = 档位价 + 唯一小额尾数（同额多单也能区分，设计 §12.2 ④）；
- 30 分钟未付 → `expired`；已展示过的派生地址永不分配给他人。

状态机：`pending`（待付）→ `short`（已到账但不足，记差额）→ `paid`；`pending`/`short` 超时 → `expired`。

**领取令牌**：下单时下发一枚随机令牌（明文只在下单响应里出现一次，库里只存哈希）。
领凭据时必须同时给出「通讯账号 + 订单引用 + 领取令牌」——于是「凭账号名冒领别人的会员」
这条路走不通，而付款的人自己一定拿得到（令牌就在他下单的那个浏览器里）。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time
from dataclasses import dataclass

from danmu_intel.billing import members, pricing, xpub
from danmu_intel.chain.transfer import NETWORKS, POLYGON, SOLANA, Transfer

ORDER_STATUSES = ("pending", "short", "paid", "expired")
#: 还能继续收钱的订单状态（`short` 可以再补一笔）。
OPEN_STATUSES = ("pending", "short")

PUBLIC_REF_PREFIX = "DM"
#: 金额尾数上限（同额多单靠它区分，设计 §12.2 ④）。
MAX_TAIL = 999
#: 领取令牌字节数（明文一次性下发，库里只存哈希）。
CLAIM_TOKEN_BYTES = 32


class OrderError(ValueError):
    """订单请求非法（档位/网络/账号/复用条件不成立）。"""


@dataclass(frozen=True, slots=True)
class Order:
    id: int
    public_ref: str
    member_id: int
    tier: str
    network: str
    address: str
    address_index: int | None
    memo: str | None
    asset: str
    amount_due_units: int
    status: str
    created_at: int
    expires_at: int
    paid_at: int | None
    tx_ref: str | None
    paid_units: int
    shortage_units: int

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @property
    def amount_display(self) -> str:
        return pricing.format_units(self.amount_due_units)

    @property
    def shortage_display(self) -> str:
        return pricing.format_units(self.shortage_units)

    def as_dict(self) -> dict[str, object]:
        return {
            "public_ref": self.public_ref,
            "network": self.network,
            "address": self.address,
            "memo": self.memo,
            "asset": self.asset,
            "amount_units": self.amount_due_units,
            "amount": self.amount_display,
            "tier": self.tier,
            "status": self.status,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "shortage_units": self.shortage_units,
        }


def now_ms() -> int:
    return int(time.time() * 1000)


def _to_order(row: sqlite3.Row) -> Order:
    return Order(
        id=int(row["id"]),
        public_ref=row["public_ref"],
        member_id=int(row["member_id"]),
        tier=row["tier"],
        network=row["network"],
        address=row["address"],
        address_index=row["address_index"],
        memo=row["memo"],
        asset=row["asset"],
        amount_due_units=int(row["amount_due_units"]),
        status=row["status"],
        created_at=int(row["created_at"]),
        expires_at=int(row["expires_at"]),
        paid_at=row["paid_at"],
        tx_ref=row["tx_ref"],
        paid_units=int(row["paid_units"]),
        shortage_units=int(row["shortage_units"]),
    )


def hash_claim_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_claim_token() -> str:
    """下单时下发的领取令牌（明文只出现一次）。"""
    return secrets.token_urlsafe(CLAIM_TOKEN_BYTES)


def claim_token_matches(token: str, stored_hash: str) -> bool:
    """恒定时间比对领取令牌（不泄露"差在哪一位"）。"""
    if not token:
        return False
    return hmac.compare_digest(hash_claim_token(token), stored_hash)


def claim_hash(conn: sqlite3.Connection, order_id: int) -> str:
    row = conn.execute("SELECT claim_hash FROM orders WHERE id=?", (order_id,)).fetchone()
    if row is None:
        raise LookupError(f"未找到订单 #{order_id}")
    return str(row["claim_hash"])


def next_address_index(conn: sqlite3.Connection) -> int:
    """下一个 Polygon 派生索引：**只前进不回退**（用过的地址不再分配给别人）。"""
    row = conn.execute(
        "SELECT MAX(address_index) AS m FROM orders WHERE network=? AND address_index IS NOT NULL",
        (POLYGON,),
    ).fetchone()
    top = row["m"] if row is not None else None
    return 0 if top is None else int(top) + 1


def _free_tail(conn: sqlite3.Connection, *, network: str, base_units: int) -> int:
    rows = conn.execute(
        f"SELECT amount_due_units FROM orders WHERE network=? AND status IN {OPEN_STATUSES}",
        (network,),
    ).fetchall()
    taken = {int(row["amount_due_units"]) - base_units for row in rows}
    for tail in range(1, MAX_TAIL + 1):
        if tail not in taken:
            return tail
    raise OrderError(f"{network} 上未付订单已有 {MAX_TAIL} 笔同价订单，换一档或稍后再试")


def _public_ref(conn: sqlite3.Connection) -> str:
    for _ in range(8):
        candidate = PUBLIC_REF_PREFIX + secrets.token_hex(4).upper()
        row = conn.execute("SELECT 1 FROM orders WHERE public_ref=?", (candidate,)).fetchone()
        if row is None:
            return candidate
    raise OrderError("生成订单引用失败（连撞 8 次，几乎不可能是巧合）")


def create_order(
    conn: sqlite3.Connection,
    *,
    platform: str,
    username: str,
    tier: str,
    network: str,
    now: int | None = None,
    config: pricing.BillingConfig | None = None,
) -> tuple[Order, str | None]:
    """生成（或复用）一个收款要求。返回 `(订单, 领取令牌明文)`；复用时令牌已轮换。

    全程无人工（AC-3）：地址/memo/金额都由这里一次算好，付款由 `settle` 自动对账开通。
    """
    config = config or pricing.load_billing_config(conn)
    tier_config = config.tier(tier)
    if network not in NETWORKS:
        raise OrderError(f"未知的收款网络：{network}（允许：{','.join(NETWORKS)}）")
    stamp = now_ms() if now is None else now
    member = members.get_or_create_member(
        conn, platform=platform, username=username, tier=tier, now=stamp
    )
    expire_due(conn, now=stamp, config=config)
    if tier == "trial" and members.trial_used(conn, member.id):
        raise OrderError("试用档每位会员只能享受一次（FR-C6-1）")
    existing = _open_order(conn, member_id=member.id, tier=tier, network=network)
    token = issue_claim_token()
    token_hash = hash_claim_token(token)
    if existing is not None:
        # 复用未付订单：不生成新地址（设计 §12.2 ②），只轮换领取令牌
        conn.execute("UPDATE orders SET claim_hash=? WHERE id=?", (token_hash, existing.id))
        conn.commit()
        return get_order(conn, order_id=existing.id), token

    asset = pricing.asset_for(network)
    public_ref = _public_ref(conn)
    if network == POLYGON:
        index = next_address_index(conn)
        address = xpub.derive_address(config.require_xpub(), index)
        memo: str | None = None
    else:
        index = None
        address = config.require_solana_address()
        memo = public_ref  # Solana 的每单唯一标识（ADR-0004）
    tail = _free_tail(conn, network=network, base_units=tier_config.amount_units)
    amount = tier_config.amount_units + tail
    cursor = conn.execute(
        """
        INSERT INTO orders(public_ref, claim_hash, member_id, tier, network, address,
                           address_index, memo, asset, amount_due_units, status,
                           created_at, expires_at, paid_at, tx_ref, paid_units, shortage_units)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, NULL, NULL, 0, 0)
        """,
        (
            public_ref,
            token_hash,
            member.id,
            tier,
            network,
            address,
            index,
            memo,
            asset,
            amount,
            stamp,
            stamp + config.order_ttl_ms,
        ),
    )
    conn.commit()
    return get_order(conn, order_id=int(cursor.lastrowid)), token


def _open_order(
    conn: sqlite3.Connection, *, member_id: int, tier: str, network: str
) -> Order | None:
    row = conn.execute(
        f"""
        SELECT * FROM orders
        WHERE member_id=? AND tier=? AND network=? AND status IN {OPEN_STATUSES}
        ORDER BY id DESC LIMIT 1
        """,
        (member_id, tier, network),
    ).fetchone()
    return None if row is None else _to_order(row)


def get_order(
    conn: sqlite3.Connection, *, order_id: int | None = None, public_ref: str | None = None
) -> Order:
    if (order_id is None) == (public_ref is None):
        raise OrderError("查订单要给出 order_id 或 public_ref 其中之一")
    if public_ref is not None:
        row = conn.execute("SELECT * FROM orders WHERE public_ref=?", (public_ref,)).fetchone()
    else:
        row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if row is None:
        raise LookupError(f"未找到订单（{'public_ref' if public_ref else 'id'}={public_ref or order_id}）")
    return _to_order(row)


def list_orders(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    network: str | None = None,
    limit: int | None = None,
) -> list[Order]:
    sql = "SELECT * FROM orders"
    conditions: list[str] = []
    params: list[object] = []
    if status is not None:
        if status not in ORDER_STATUSES:
            raise OrderError(f"未知的订单状态：{status}（允许：{','.join(ORDER_STATUSES)}）")
        conditions.append("status=?")
        params.append(status)
    if network is not None:
        conditions.append("network=?")
        params.append(network)
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [_to_order(row) for row in conn.execute(sql, params)]


def open_orders(conn: sqlite3.Connection, *, now: int | None = None) -> list[Order]:
    """还能收钱的订单（未过期的 pending/short）—— 监听目标与对账的输入。"""
    stamp = now_ms() if now is None else now
    rows = conn.execute(
        f"SELECT * FROM orders WHERE status IN {OPEN_STATUSES} AND expires_at > ? ORDER BY id",
        (stamp,),
    ).fetchall()
    return [_to_order(row) for row in rows]


def expire_due(
    conn: sqlite3.Connection, *, now: int | None = None, config: pricing.BillingConfig | None = None
) -> list[Order]:
    """把超时未付的订单标成 `expired`（设计 §12.2 ⑧：30 分钟未付即过期）。"""
    stamp = now_ms() if now is None else now
    if config is None:
        config = pricing.load_billing_config(conn)
    rows = conn.execute(
        f"SELECT * FROM orders WHERE status IN {OPEN_STATUSES} AND expires_at <= ? ORDER BY id",
        (stamp,),
    ).fetchall()
    expired = [_to_order(row) for row in rows]
    for order in expired:
        conn.execute("UPDATE orders SET status='expired' WHERE id=?", (order.id,))
    if expired:
        conn.commit()
    return [get_order(conn, order_id=order.id) for order in expired]


def matches(order: Order, transfer: Transfer) -> bool:
    """这笔入账是不是冲着这个订单来的（地址 / memo / 资产三项都要对上）。

    金额不在这里判：不足额要记差额（FR-C6-7），属 `settle` 的活。
    """
    if order.network != transfer.network:
        return False
    if not pricing.same_asset(order.network, transfer.asset, order.asset):
        return False
    if order.network == SOLANA:
        # Solana 是单一收款地址：区分不同订单靠 memo（ADR-0004）
        return order.memo is not None and transfer.memo == order.memo and transfer.address == order.address
    return order.address.lower() == transfer.address.lower()


def paid_total(conn: sqlite3.Connection, order_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(units), 0) AS total FROM order_payments WHERE order_id=?",
        (order_id,),
    ).fetchone()
    return int(row["total"])
