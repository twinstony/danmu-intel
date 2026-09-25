"""凭据发放与校验（设计 §12.4；需求 AC-10 / AC-18 / NFR-P-1..2 / NFR-S-2 / ADR-0006）。

两件事，只有一个入口：

| 动作 | 入参 | 成功条件 |
|---|---|---|
| `claim`（领取） | 通讯账号 + 订单引用 + **领取令牌** | 该订单已付款、且这个账号就是订单的主人 |
| `verify`（校验） | **凭据**（cookie 或请求体里的 code） | 凭据有效且会员还能访问 |

**防枚举的最强形态**：失败一律是同一份响应体、同一个状态码 —— 「账号不存在」「账号存在但
未开通」「已过期」「已撤销」「凭据不对」「被限流」六种情况**逐字节相同**（`NEUTRAL_BODY`）。
于是校验接口回答不了「某人是不是会员」（NFR-P-2 / AC-10），也无法被当成会员名录来刷。

会员身份靠**凭据**承载（32 字节随机码，库里只存哈希，ADR-0006）。凭据怎么来？付款后由
下单的那个浏览器用「账号 + 订单引用 + 领取令牌」自助领取 —— 领取令牌明文只在下单响应里出现
一次、库里只存哈希、**不写进链上 memo**，所以链上公开信息里没有它，别人凭账号名也领不走
（否则任何知道会员账号的人都能白拿访问权）。

限流按 **IP 与账号双维度**（NFR-S-2），桶在 `rate_limits` 表里按固定窗口计数；触发限流返回与
失败**同样的响应形态**（不给攻击者额外信息）。
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from dataclasses import dataclass

from danmu_intel.billing import members, orders, pricing
from danmu_intel.billing.members import ContactError, Member
from danmu_intel.common import audit

#: 凭据字节数（ADR-0006：32 字节随机码）。
CREDENTIAL_BYTES = 32
#: 凭据 cookie 名（HttpOnly + Secure + SameSite=Lax）。
COOKIE_NAME = "dm_access"
#: 凭证发放的审计动作（只记"谁何时领了一枚凭据"，**不记凭据本身**）。
CREDENTIAL_ISSUED = "billing.credential.issued"

#: 对外一律使用同一份失败响应（AC-10：不可区分）。
NEUTRAL_BODY: dict[str, object] = {"member": False, "state": "unknown"}
NEUTRAL_STATUS = 200


@dataclass(frozen=True, slots=True)
class RateLimit:
    """一个固定窗口的限流额度。"""

    limit: int
    window_ms: int


#: 限流额度：校验按 IP；领取按账号 + 按 IP（防"拿账号名录来刷"与"帮别人领取"）。
VERIFY_BY_IP = RateLimit(limit=60, window_ms=60_000)
CLAIM_BY_ACCOUNT = RateLimit(limit=5, window_ms=3600_000)
CLAIM_BY_IP = RateLimit(limit=20, window_ms=3600_000)
ORDER_BY_IP = RateLimit(limit=30, window_ms=3600_000)


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """一次校验/领取的结果：响应体 + 状态码（+ 成功路径上的新凭据）。"""

    status: int
    body: dict[str, object]
    credential: str | None = None  # 明文：只在发放那一刻出现一次
    cookie: str | None = None  # Set-Cookie 的值
    member: Member | None = None

    @property
    def ok(self) -> bool:
        return self.member is not None

    def as_dict(self) -> dict[str, object]:
        """HTTP 层直接当 JSON 返回（`Set-Cookie` 走 `cookie` 字段）。"""
        return dict(self.body)


def now_ms() -> int:
    return int(time.time() * 1000)


def neutral() -> dict[str, object]:
    """失败路径的统一响应体（每次返回新 dict：调用方改不到共享状态）。"""
    return dict(NEUTRAL_BODY)


def consume(
    conn: sqlite3.Connection,
    bucket: str,
    limit: RateLimit,
    *,
    now: int | None = None,
) -> bool:
    """记一次命中；返回 `True` 表示**已越限**（本次请求被拒）。"""
    stamp = now_ms() if now is None else now
    row = conn.execute(
        "SELECT window_started_at, hits FROM rate_limits WHERE bucket=?", (bucket,)
    ).fetchone()
    if row is None or stamp - int(row["window_started_at"]) >= limit.window_ms:
        conn.execute(
            "INSERT INTO rate_limits(bucket, window_started_at, hits) VALUES(?, ?, 1) "
            "ON CONFLICT(bucket) DO UPDATE SET window_started_at=excluded.window_started_at, "
            "hits=1",
            (bucket, stamp),
        )
        conn.commit()
        return False
    hits = int(row["hits"]) + 1
    conn.execute("UPDATE rate_limits SET hits=? WHERE bucket=?", (hits, bucket))
    conn.commit()
    return hits > limit.limit


def issue_credential(conn: sqlite3.Connection, member_id: int, *, now: int | None = None) -> str:
    """发放一枚凭据，返回**明文**（库里只留 sha256）。"""
    stamp = now_ms() if now is None else now
    code = secrets.token_urlsafe(CREDENTIAL_BYTES)
    conn.execute(
        "INSERT INTO member_credentials(member_id, code_hash, created_at) VALUES(?, ?, ?)",
        (member_id, hash_credential(code), stamp),
    )
    conn.commit()
    audit.record(
        conn,
        actor=f"member:{member_id}",
        action=CREDENTIAL_ISSUED,
        target=str(member_id),
        detail={},  # 凭据本身**绝不**进审计
        ts=stamp,
    )
    return code


def hash_credential(code: str) -> str:
    """凭据的存储形态：sha256（明文不落库）。"""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def check_credential(
    conn: sqlite3.Connection, code: str | None, *, now: int | None = None
) -> Member | None:
    """凭据 → 会员（无效/已撤销/会员不存在都返回 `None`，由调用方给中性响应）。"""
    if not code:
        return None
    stamp = now_ms() if now is None else now
    row = conn.execute(
        "SELECT * FROM member_credentials WHERE code_hash=? AND revoked_at IS NULL",
        (hash_credential(code),),
    ).fetchone()
    if row is None:
        return None
    conn.execute("UPDATE member_credentials SET last_used_at=? WHERE id=?", (stamp, int(row["id"])))
    conn.commit()
    try:
        return members.get_member(conn, int(row["member_id"]))
    except LookupError:
        return None


def revoke_credentials(
    conn: sqlite3.Connection, member_id: int, *, now: int | None = None
) -> int:
    """撤销某会员的全部凭据（撤权/换人时用），返回撤销条数。"""
    stamp = now_ms() if now is None else now
    cursor = conn.execute(
        "UPDATE member_credentials SET revoked_at=? WHERE member_id=? AND revoked_at IS NULL",
        (stamp, member_id),
    )
    conn.commit()
    return int(cursor.rowcount)


def cookie_value(credential: str, *, max_age_s: int) -> str:
    """`Set-Cookie` 的值（HttpOnly + Secure + SameSite=Lax，ADR-0006）。"""
    return (
        f"{COOKIE_NAME}={credential}; Path=/; HttpOnly; Secure; SameSite=Lax; "
        f"Max-Age={max(0, int(max_age_s))}"
    )


def _success(
    member: Member,
    *,
    config: pricing.BillingConfig,
    now: int,
    credential: str | None = None,
) -> VerifyResult:
    tier_key = member.tier
    try:
        label = config.tier(tier_key).label
    except pricing.BillingConfigError:
        label = tier_key
    body: dict[str, object] = {
        "member": True,
        "state": member.status,
        "tier": tier_key,
        "tier_label": label,
        "expires_at": member.expires_at,
    }
    cookie = None
    if credential is not None:
        remaining = 0 if member.expires_at is None else member.expires_at - now
        cookie = cookie_value(credential, max_age_s=int(remaining // 1000))
    return VerifyResult(status=200, body=body, credential=credential, cookie=cookie, member=member)


def verify(
    conn: sqlite3.Connection,
    *,
    code: str | None,
    platform: str | None = None,
    username: str | None = None,
    ip: str | None = None,
    now: int | None = None,
    config: pricing.BillingConfig | None = None,
) -> VerifyResult:
    """校验（凭据是**唯一**成功路径）。

    `platform` / `username` 只用于按账号限流分桶与留痕，**不参与判定**：
    一旦它参与判定，这个接口就变成了会员名录（NFR-P-2 要禁的正是这件事）。
    """
    stamp = now_ms() if now is None else now
    config = config or pricing.load_billing_config(conn)
    if ip is not None and consume(conn, f"verify:ip:{ip}", VERIFY_BY_IP, now=stamp):
        return VerifyResult(status=NEUTRAL_STATUS, body=neutral())
    member = check_credential(conn, code, now=stamp)
    if member is None or not member.can_access(now=stamp, grace_ms=config.grace_ms):
        return VerifyResult(status=NEUTRAL_STATUS, body=neutral())
    return _success(member, config=config, now=stamp)


def claim(
    conn: sqlite3.Connection,
    *,
    platform: str,
    username: str,
    order_ref: str,
    claim_token: str,
    ip: str | None = None,
    now: int | None = None,
    config: pricing.BillingConfig | None = None,
) -> VerifyResult:
    """领取凭据（自助闭环第 ⑦ 步）：账号 + 订单引用 + 领取令牌三者对上、且订单已付款。

    以账号为限流桶（FR-C6-13 的骚扰面），失败的每一种原因都返回同一份响应体。
    """
    stamp = now_ms() if now is None else now
    config = config or pricing.load_billing_config(conn)
    try:
        platform_norm, username_norm = members.normalize_contact(platform, username)
    except ContactError:
        return VerifyResult(status=NEUTRAL_STATUS, body=neutral())

    # 两个维度都记一次（不短路：否则按 IP 的账会漏记）
    by_account = consume(
        conn, f"claim:acct:{platform_norm}:{username_norm}", CLAIM_BY_ACCOUNT, now=stamp
    )
    by_ip = False
    if ip is not None:
        by_ip = consume(conn, f"claim:ip:{ip}", CLAIM_BY_IP, now=stamp)
    if by_account or by_ip:
        return VerifyResult(status=NEUTRAL_STATUS, body=neutral())

    member = members.find_member(conn, platform=platform, username=username)
    order = None
    if order_ref:
        try:
            order = orders.get_order(conn, public_ref=order_ref)
        except (LookupError, orders.OrderError):
            order = None
    if (
        member is None
        or order is None
        or order.member_id != member.id
        or order.status != "paid"
        or not orders.claim_token_matches(claim_token, orders.claim_hash(conn, order.id))
    ):
        return VerifyResult(status=NEUTRAL_STATUS, body=neutral())
    if not member.can_access(now=stamp, grace_ms=config.grace_ms):
        return VerifyResult(status=NEUTRAL_STATUS, body=neutral())
    credential = issue_credential(conn, member.id, now=stamp)
    return _success(member, config=config, now=stamp, credential=credential)


def cookie_from_header(header: str | None) -> str | None:
    """从 `Cookie` 头里取凭据（只认我们那一个键，不做任何解码）。"""
    if not header:
        return None
    for item in header.split(";"):
        name, _, value = item.strip().partition("=")
        if name == COOKIE_NAME and value:
            return value.strip()
    return None
