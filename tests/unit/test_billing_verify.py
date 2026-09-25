"""凭据与防枚举（T9）：领取、校验、不可区分响应、限流、宽限期内仍可访问。"""

from __future__ import annotations

import json

import pytest

from danmu_intel.billing import members, orders, pricing, settle, verify
from danmu_intel.chain.transfer import Transfer
from danmu_intel.common import audit
from tests.unit.test_billing_xpub import ACCOUNT_XPUB

BASE_MS = 1_790_000_000_000
MINUTE = 60 * 1000
DAY = 24 * 3600 * 1000
WALLET = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"


@pytest.fixture
def config(conn) -> pricing.BillingConfig:
    return pricing.save_billing_config(
        conn, actor="测试", changes={"polygon_xpub": ACCOUNT_XPUB, "solana_address": WALLET}
    )


def open_order(conn, config, *, username="@payer", now=BASE_MS):
    order, token = orders.create_order(
        conn, platform="telegram", username=username, tier="standard", network="polygon",
        now=now, config=config,
    )
    return order, token


def pay(conn, config, order, *, now=BASE_MS + MINUTE):
    transfer = Transfer(
        network="polygon",
        address=order.address,
        tx_ref=f"0x{order.id}",
        asset=order.asset,
        units=order.amount_due_units,
        at_ms=now,
        block=1,
    )
    settle.settle(conn, [transfer], now=now, config=config)
    return orders.get_order(conn, order_id=order.id)


def claim(conn, config, order, token, *, username="@payer", now=BASE_MS + 2 * MINUTE, **extra):
    return verify.claim(
        conn,
        platform="telegram",
        username=username,
        order_ref=order.public_ref,
        claim_token=token,
        now=now,
        config=config,
        **extra,
    )


def test_claim_gives_a_hashed_credential_and_a_safe_cookie(conn, config):
    order, token = open_order(conn, config)
    pay(conn, config, order)
    result = claim(conn, config, order, token)

    assert result.ok and result.member is not None and result.member.status == "active"
    assert result.body["member"] is True and result.body["state"] == "active"
    assert result.body["tier_label"] == "标准档"
    assert result.body["expires_at"] == result.member.expires_at
    assert "username" not in json.dumps(result.body)  # NFR-P-1：对外不泄露身份

    cookie = result.cookie or ""
    assert cookie.startswith(f"{verify.COOKIE_NAME}={result.credential};")
    for attribute in ("HttpOnly", "Secure", "SameSite=Lax", "Path=/"):
        assert attribute in cookie

    # 库里只有哈希：明文既不在凭据表里，也不在审计里
    row = conn.execute("SELECT * FROM member_credentials ORDER BY id DESC").fetchone()
    assert row["code_hash"] == verify.hash_credential(result.credential)
    assert result.credential not in json.dumps(dict(row))
    assert result.credential not in json.dumps([entry.detail for entry in audit.entries(conn)])
    assert audit.entries(conn, action=verify.CREDENTIAL_ISSUED), "发放凭据也要留痕"


def test_every_failure_path_returns_the_identical_response(conn, config):
    """AC-10：『不存在』『未开通』『已过期』…… 六种失败逐字节相同。"""
    payer_order, payer_token = open_order(conn, config)
    unknown = claim(conn, config, payer_order, payer_token, username="@nobody")
    pending_order, pending_token = open_order(conn, config, username="@waiting")
    not_yet = claim(conn, config, pending_order, pending_token, username="@waiting")

    paid_order, paid_token = open_order(conn, config, username="@expired")
    pay(conn, config, paid_order)
    expired_at = BASE_MS + MINUTE + config.tier("standard").duration_ms + config.grace_ms + 1
    members.sweep(conn, now=expired_at)
    expired = claim(conn, config, paid_order, paid_token, username="@expired", now=expired_at)

    bad_token = claim(conn, config, paid_order, "wrong-token", username="@expired")
    bad_ref = verify.claim(
        conn, platform="telegram", username="@expired", order_ref="DMNOPE00",
        claim_token=paid_token, now=BASE_MS + 2 * MINUTE, config=config,
    )
    bad_platform = verify.claim(
        conn, platform="discord", username="@expired", order_ref=paid_order.public_ref,
        claim_token=paid_token, now=BASE_MS + 2 * MINUTE, config=config,
    )
    other_account = claim(conn, config, paid_order, paid_token, username="@stranger")

    bodies = {
        json.dumps(item.body, sort_keys=True)
        for item in (unknown, not_yet, expired, bad_token, bad_ref, bad_platform, other_account)
    }
    statuses = {
        item.status
        for item in (unknown, not_yet, expired, bad_token, bad_ref, bad_platform, other_account)
    }
    assert bodies == {json.dumps(verify.NEUTRAL_BODY, sort_keys=True)}
    assert statuses == {verify.NEUTRAL_STATUS}
    assert all(not item.ok and item.credential is None and item.cookie is None
               for item in (unknown, not_yet, expired, bad_token, bad_ref, bad_platform, other_account))


def test_a_paid_order_can_only_be_claimed_by_its_own_account(conn, config):
    order, token = open_order(conn, config, username="@owner")
    pay(conn, config, order)
    stolen = claim(conn, config, order, token, username="@stranger")
    assert not stolen.ok
    assert claim(conn, config, order, token, username="@owner").ok


def test_claim_is_rate_limited_per_account_and_per_ip(conn, config):
    order, token = open_order(conn, config)
    pay(conn, config, order)
    results = [claim(conn, config, order, token) for _ in range(verify.CLAIM_BY_ACCOUNT.limit)]
    assert all(item.ok for item in results)  # 额度内照常
    blocked = claim(conn, config, order, token)
    assert not blocked.ok
    assert blocked.body == verify.NEUTRAL_BODY  # 限流与失败同一副面孔

    # 换一个账号：不受别人账号桶的影响，但同 IP 桶会拦住（双维度都算）
    other_order, other_token = open_order(conn, config, username="@second")
    pay(conn, config, other_order)
    by_ip = claim(conn, config, other_order, other_token, username="@second", ip="1.2.3.4")
    assert by_ip.ok
    hits = [claim(conn, config, other_order, other_token, username="@second", ip="1.2.3.4")
            for _ in range(verify.CLAIM_BY_IP.limit)]
    assert not hits[-1].ok

    # 窗口过去后重新可用
    later = BASE_MS + 2 * MINUTE + verify.CLAIM_BY_ACCOUNT.window_ms
    assert claim(conn, config, order, token, now=later).ok


def test_verify_only_succeeds_with_a_valid_credential(conn, config):
    order, token = open_order(conn, config)
    pay(conn, config, order)
    credential = claim(conn, config, order, token).credential

    ok = verify.verify(conn, code=credential, platform="telegram", username="@payer", now=BASE_MS + 3 * MINUTE)
    assert ok.ok and ok.body["member"] is True and ok.body["state"] == "active"
    assert ok.cookie is None  # 校验不再发新凭据（凭据可重用）

    for code in (None, "", "not-a-credential", credential + "x"):
        result = verify.verify(conn, code=code, now=BASE_MS + 3 * MINUTE)
        assert not result.ok
        assert result.body == verify.NEUTRAL_BODY and result.status == verify.NEUTRAL_STATUS

    # 账号字段不参与判定：写成别人也一样（否则这个接口就是会员名录）
    same = verify.verify(
        conn, code=credential, platform="qq", username="12345678", now=BASE_MS + 3 * MINUTE
    )
    assert same.ok


def test_credential_stops_working_when_access_ends(conn, config):
    order, token = open_order(conn, config)
    pay(conn, config, order)
    credential = claim(conn, config, order, token).credential
    member_id = order.member_id

    grace_end = BASE_MS + MINUTE + config.tier("standard").duration_ms + config.grace_ms
    in_grace = verify.verify(conn, code=credential, now=BASE_MS + MINUTE + config.tier("standard").duration_ms + 1)
    assert in_grace.ok and in_grace.body["state"] in {"active", "grace"}  # 宽限期内仍可访问（AC-6）

    after_grace = verify.verify(conn, code=credential, now=grace_end + 1)
    assert not after_grace.ok  # 宽限期满 = 访客

    members.sweep(conn, now=grace_end + 1)
    assert not verify.verify(conn, code=credential, now=grace_end + 2).ok

    # 撤权同样立即失效，并且可以显式撤销凭据
    members.revoke(conn, member_id=member_id, actor="运营者", reason="退款", now=grace_end + 3)
    assert verify.revoke_credentials(conn, member_id, now=grace_end + 3) == 1
    assert verify.revoke_credentials(conn, member_id, now=grace_end + 4) == 0
    assert not verify.verify(conn, code=credential, now=grace_end + 5).ok


def test_renewal_keeps_the_existing_credential_working(conn, config):
    """续费只延期，不换凭据：老 cookie 照样能用（用户不用重新领取）。"""
    order, token = open_order(conn, config)
    pay(conn, config, order)
    credential = claim(conn, config, order, token).credential

    renewal, renewal_token = orders.create_order(
        conn, platform="telegram", username="@payer", tier="standard", network="polygon",
        now=BASE_MS + 2 * MINUTE, config=config,
    )
    pay(conn, config, renewal, now=BASE_MS + 3 * MINUTE)
    assert claim(conn, config, renewal, renewal_token, now=BASE_MS + 4 * MINUTE).ok
    extended = verify.verify(conn, code=credential, now=BASE_MS + 4 * MINUTE)
    assert extended.ok
    assert extended.member is not None and extended.member.expires_at == (
        BASE_MS + MINUTE + 2 * config.tier("standard").duration_ms
    )


def test_verify_rate_limit_by_ip(conn, config):
    order, token = open_order(conn, config)
    pay(conn, config, order)
    credential = claim(conn, config, order, token).credential
    for _ in range(verify.VERIFY_BY_IP.limit):
        assert verify.verify(conn, code=credential, ip="10.0.0.1", now=BASE_MS + 3 * MINUTE).ok
    blocked = verify.verify(conn, code=credential, ip="10.0.0.1", now=BASE_MS + 3 * MINUTE)
    assert not blocked.ok and blocked.body == verify.NEUTRAL_BODY

    later = BASE_MS + 3 * MINUTE + verify.VERIFY_BY_IP.window_ms
    assert verify.verify(conn, code=credential, ip="10.0.0.1", now=later).ok


def test_consume_counts_fixed_windows(conn):
    limit = verify.RateLimit(limit=2, window_ms=1000)
    assert verify.consume(conn, "b", limit, now=BASE_MS) is False
    assert verify.consume(conn, "b", limit, now=BASE_MS + 1) is False
    assert verify.consume(conn, "b", limit, now=BASE_MS + 2) is True  # 第 3 次越限
    assert verify.consume(conn, "b", limit, now=BASE_MS + 3) is True
    assert verify.consume(conn, "b", limit, now=BASE_MS + 1000) is False  # 新窗口


def test_cookie_header_parsing():
    assert verify.cookie_from_header(f"other=1; {verify.COOKIE_NAME}=abc") == "abc"
    assert verify.cookie_from_header(f"{verify.COOKIE_NAME}=abc; other=1") == "abc"
    assert verify.cookie_from_header("nothing=1") is None
    assert verify.cookie_from_header(None) is None
    assert verify.cookie_from_header(f"{verify.COOKIE_NAME}=") is None


def test_credentials_die_with_the_member_lookup(conn, config):
    """凭据行指向一个不存在的会员时（数据被手工删过）不能崩，只能当无效凭据。"""
    order, token = open_order(conn, config)
    pay(conn, config, order)
    credential = claim(conn, config, order, token).credential
    conn.execute("DELETE FROM members WHERE id=?", (order.member_id,))
    conn.commit()
    assert not verify.verify(conn, code=credential).ok
    assert verify.check_credential(conn, credential) is None


def test_tier_label_falls_back_when_the_tier_left_the_config(conn, config):
    """档位被后台改名/删掉后，老会员的响应不能崩（FR-C6-2：改价格不影响已生效的会员）。"""
    order, token = open_order(conn, config)
    pay(conn, config, order)
    conn.execute("UPDATE members SET tier='legacy' WHERE id=?", (order.member_id,))
    conn.commit()
    result = claim(conn, config, order, token)
    assert result.ok and result.body["tier"] == "legacy" and result.body["tier_label"] == "legacy"
