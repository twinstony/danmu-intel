"""会员身份与状态机（设计 §5.1 `members`、§12.6；需求 FR-C6-12..16、AC-6）。

会员**凭既有的第三方通讯账号标识**识别，不要求注册本站账号（FR-C6-21 / Q-5）：
`(contact_platform, username_norm)` 唯一。用户名只用于联系与运营者核对，
**对外零泄露**（NFR-P-1：任何公开页面、统计、导出都不含用户名）。

状态机（设计 §12.6）：

```
pending ──开通──> active ──到期──> grace ──宽限期满──> expired
   │                 ↑                                  │
   └─────────────────┴──────── 续费（从原到期日顺延）────┘
（任意状态 ──运营者撤权──> revoked）
```

- `active` / `grace` 都**可以访问**（FR-C6-14：到期后保留宽限期，避免误伤）；
- 续费**从原到期日顺延**，不是从付款日起算（AC-6 / ADR-0006）；
- 开通/续费/降级/撤权全部写 `audit_log`（FR-C6-16：谁、何时、因哪笔交易、开通到何时）。
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass

from danmu_intel.billing.pricing import load_billing_config
from danmu_intel.common import audit

#: 允许的通讯平台（需求 Q-5：沿用既有做法；本站不接第三方登录，只存标识）。
CONTACT_PLATFORMS = ("telegram", "qq")

MEMBER_STATUSES = ("pending", "active", "grace", "expired", "revoked")
#: 能访问付费内容的会员状态（宽限期内仍可访问 —— AC-6）。
ACCESS_STATUSES = ("active", "grace")

MEMBER_GRANTED = "billing.member.granted"
MEMBER_REVOKED = "billing.member.revoked"
MEMBER_EXPIRED = "billing.member.expired"
MEMBER_GRACE = "billing.member.grace"

_TELEGRAM_USERNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
_QQ_NUMBER = re.compile(r"^[1-9][0-9]{4,11}$")


class ContactError(ValueError):
    """通讯账号标识非法（不存在「随便填个名字就能开通」的口子）。"""


class MemberError(ValueError):
    """会员状态机的非法操作。"""


@dataclass(frozen=True, slots=True)
class Member:
    id: int
    contact_platform: str
    username: str
    username_norm: str
    tier: str
    status: str
    expires_at: int | None
    created_at: int
    revoked_at: int | None

    @property
    def contact(self) -> str:
        """只给运营者看的联系标识（`@name（telegram）`）—— 不进任何对外输出。"""
        return f"{self.username}（{self.contact_platform}）"

    def can_access(self, *, now: int, grace_ms: int = 0) -> bool:
        """能否读付费内容：状态在 `active`/`grace` 且确实还没过宽限期（AC-6）。"""
        if self.status not in ACCESS_STATUSES or self.expires_at is None:
            return False
        return self.expires_at + grace_ms > now

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "username": self.username,
            "status": self.status,
            "tier": self.tier,
            "expires_at": self.expires_at,
        }


def now_ms() -> int:
    return int(time.time() * 1000)


def normalize_contact(platform: str, username: str) -> tuple[str, str]:
    """规范化通讯账号标识 → `(platform, username_norm)`；非法即拒绝。"""
    platform = (platform or "").strip().lower()
    if platform not in CONTACT_PLATFORMS:
        raise ContactError(f"未知的通讯平台：{platform or '（空）'}（允许：{'、'.join(CONTACT_PLATFORMS)}）")
    raw = (username or "").strip()
    if platform == "telegram":
        candidate = raw.lstrip("@")
        if not _TELEGRAM_USERNAME.match(candidate):
            raise ContactError("Telegram 用户名应为 5–32 位字母/数字/下划线（可带前导 @）")
        return platform, candidate.lower()
    if not _QQ_NUMBER.match(raw):
        raise ContactError("QQ 号应为 5–12 位数字")
    return platform, raw


def _to_member(row: sqlite3.Row) -> Member:
    return Member(
        id=int(row["id"]),
        contact_platform=row["contact_platform"],
        username=row["username"],
        username_norm=row["username_norm"],
        tier=row["tier"],
        status=row["status"],
        expires_at=row["expires_at"],
        created_at=int(row["created_at"]),
        revoked_at=row["revoked_at"],
    )


def _row(conn: sqlite3.Connection, member_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM members WHERE id=?", (member_id,)).fetchone()
    if row is None:
        raise LookupError(f"未找到会员 #{member_id}")
    return row


def get_member(conn: sqlite3.Connection, member_id: int) -> Member:
    return _to_member(_row(conn, member_id))


def find_member(conn: sqlite3.Connection, *, platform: str, username: str) -> Member | None:
    """按通讯账号标识查会员；标识非法或不存在都返回 `None`（调用方据此给中性响应）。"""
    try:
        platform, norm = normalize_contact(platform, username)
    except ContactError:
        return None
    row = conn.execute(
        "SELECT * FROM members WHERE contact_platform=? AND username_norm=?", (platform, norm)
    ).fetchone()
    return None if row is None else _to_member(row)


def get_or_create_member(
    conn: sqlite3.Connection, *, platform: str, username: str, tier: str, now: int | None = None
) -> Member:
    """下单时取（或建）会员行：同一个通讯账号始终是同一个会员。"""
    platform, norm = normalize_contact(platform, username)
    stamp = now_ms() if now is None else now
    existing = conn.execute(
        "SELECT * FROM members WHERE contact_platform=? AND username_norm=?", (platform, norm)
    ).fetchone()
    if existing is not None:
        return _to_member(existing)
    cursor = conn.execute(
        """
        INSERT INTO members(contact_platform, username, username_norm, tier, status,
                            expires_at, created_at, revoked_at)
        VALUES(?, ?, ?, ?, 'pending', NULL, ?, NULL)
        """,
        (platform, username.strip(), norm, tier, stamp),
    )
    conn.commit()
    return get_member(conn, int(cursor.lastrowid))


def list_members(
    conn: sqlite3.Connection, *, status: str | None = None, limit: int | None = None
) -> list[Member]:
    sql = "SELECT * FROM members"
    params: list[object] = []
    if status is not None:
        if status not in MEMBER_STATUSES:
            raise MemberError(f"未知的会员状态：{status}（允许：{','.join(MEMBER_STATUSES)}）")
        sql += " WHERE status=?"
        params.append(status)
    sql += " ORDER BY id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [_to_member(row) for row in conn.execute(sql, params)]


def trial_used(conn: sqlite3.Connection, member_id: int) -> bool:
    """该会员是否已经用过试用档（FR-C6-1：试用档只享受一次）。"""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM orders WHERE member_id=? AND tier='trial' AND status='paid'",
        (member_id,),
    ).fetchone()
    return int(row["n"]) > 0


def already_granted(conn: sqlite3.Connection, tx_ref: str) -> bool:
    """这笔链上交易是否已经开通过（`tx_ref` 是开通的幂等键 —— AC-4）。"""
    for entry in audit.entries(conn, action=MEMBER_GRANTED):
        if entry.detail.get("tx_ref") == tx_ref:
            return True
    return False


def grant(
    conn: sqlite3.Connection,
    *,
    member_id: int,
    tier: str,
    tx_ref: str,
    now: int | None = None,
    grace_ms: int = 0,
    actor: str = "chain",
    reason: str | None = None,
) -> tuple[Member, bool]:
    """开通或续费。返回 `(会员, 本次是否真的改了什么)`。

    - **幂等**：同一个 `tx_ref` 重复调用只生效一次（AC-4）。判定依据是审计里那条
      `billing.member.granted` —— 审计只增不改，于是幂等键与留痕是同一份事实。
    - **顺延**：`active`/`grace` 且还没过宽限期的会员，从**原到期日**往后加时长（AC-6）；
      已经过期或首次开通的，从此刻起算。
    """
    tier_days_ms = _tier_duration_ms(conn, tier)
    stamp = now_ms() if now is None else now
    current = get_member(conn, member_id)
    if already_granted(conn, tx_ref):
        return current, False
    if current.status == "revoked":
        raise MemberError(f"会员 #{member_id} 已被撤权，不能凭付款自动开通（须人工复核）")

    if (
        current.status in ACCESS_STATUSES
        and current.expires_at is not None
        and current.expires_at + grace_ms > stamp
    ):
        base = int(current.expires_at)
    else:
        base = stamp
    expires_at = base + tier_days_ms
    conn.execute(
        "UPDATE members SET tier=?, status='active', expires_at=?, revoked_at=NULL WHERE id=?",
        (tier, expires_at, member_id),
    )
    conn.commit()
    audit.record(
        conn,
        actor=actor,
        action=MEMBER_GRANTED,
        target=str(member_id),
        detail={
            "tx_ref": tx_ref,
            "tier": tier,
            "from_status": current.status,
            "from_expires_at": current.expires_at,
            "to_expires_at": expires_at,
            "renewed": current.status in ACCESS_STATUSES,
            "reason": reason,
        },
        ts=stamp,
    )
    return get_member(conn, member_id), True


def _tier_duration_ms(conn: sqlite3.Connection, tier_key: str) -> int:
    return load_billing_config(conn).tier(tier_key).duration_ms


def revoke(
    conn: sqlite3.Connection, *, member_id: int, actor: str, reason: str, now: int | None = None
) -> Member:
    """撤权（运营者动作，必填理由）：状态转 `revoked`，凭据随即失效（`verify` 看不到 revoked）。"""
    if not reason:
        raise MemberError("撤权必须写明理由（无理由的撤权不可复核）")
    stamp = now_ms() if now is None else now
    before = get_member(conn, member_id)
    conn.execute("UPDATE members SET status='revoked', revoked_at=? WHERE id=?", (stamp, member_id))
    conn.commit()
    audit.record(
        conn,
        actor=actor,
        action=MEMBER_REVOKED,
        target=str(member_id),
        detail={"from_status": before.status, "reason": reason},
        ts=stamp,
    )
    return get_member(conn, member_id)


def sweep(
    conn: sqlite3.Connection, *, now: int | None = None, grace_ms: int | None = None
) -> list[Member]:
    """到期降级：`active` →（到期）`grace` →（宽限期满）`expired`（FR-C6-14、AC-6）。

    时间是可注入的（`now`），于是「到期 → 宽限 → 降级 → 续费顺延」这条时间线
    在测试里可以一步跨过去，不需要真的等一天。
    """
    stamp = now_ms() if now is None else now
    if grace_ms is None:
        grace_ms = load_billing_config(conn).grace_ms
    changed: list[Member] = []
    rows = conn.execute(
        "SELECT * FROM members WHERE status IN ('active', 'grace') AND expires_at IS NOT NULL"
    ).fetchall()
    for row in rows:
        member = _to_member(row)
        if stamp <= int(row["expires_at"]):
            continue
        expired = stamp > int(row["expires_at"]) + grace_ms
        status = "expired" if expired else "grace"
        if status == member.status:
            continue
        conn.execute("UPDATE members SET status=? WHERE id=?", (status, member.id))
        conn.commit()
        audit.record(
            conn,
            actor="billing-sweep",
            action=MEMBER_EXPIRED if expired else MEMBER_GRACE,
            target=str(member.id),
            detail={
                "from_status": member.status,
                "to_status": status,
                "expires_at": member.expires_at,
                "grace_ms": grace_ms,
            },
            ts=stamp,
        )
        changed.append(get_member(conn, member.id))
    return changed
