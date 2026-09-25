"""T9 端到端：下单 → 链上入账 → 自动开通 → 领取凭据 → 读付费正文（全程断网）。

对应 issue #19 的验收标准，逐条走一遍：

- **AC-3**：`POST /api/orders` → 假链入账 → `chain-watch --orders` 的同一条路径（订单派生
  监听目标 + watcher + 对账）→ 会员 `active` → `POST /api/claim` 领到凭据 →
  `GET /api/report/…/paid` 读到付费正文。全程没有人工步骤。
- **AC-4**：同一笔付款被重复扫到多次，只开通一次、有效期不累加。
- **AC-5**：故意让一轮检测漏掉（供应商限速）→ 补扫发现它 → 照样开通；管理员也能人工补开通。
- **AC-6**：到期 → 宽限期内仍可访问 → 宽限期满降级；续费从原到期日顺延。
- **AC-10**：校验接口对「不存在」「未开通」「已过期」+ 错凭据 + 被限流的响应**逐字节相同**。
- **AC-12**：数据目录与仓库全库零命中可动用资产的凭据。
"""

from __future__ import annotations

import asyncio
import json

from aiohttp.test_utils import TestClient, TestServer

from danmu_intel import api
from danmu_intel.billing import members, orders, pricing, settle, verify
from danmu_intel.chain.polygonscan import PolygonscanClient
from danmu_intel.chain.quota import POLYGONSCAN, QuotaLedger
from danmu_intel.chain.transfer import SOLANA, Transfer
from danmu_intel.chain.watcher import Watcher
from danmu_intel.common import paths
from danmu_intel.common.matches import set_match_state
from danmu_intel.pipeline import generate_and_publish
from tests.e2e.test_publish_loop import publish_brief
from tests.unit.test_billing_xpub import ACCOUNT_XPUB
from tools.check_no_secrets import scan_tree

BASE_MS = 1_790_064_000_000
MINUTE = 60 * 1000
DAY = 24 * 3600 * 1000
WALLET = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"
USDT_POLYGON = pricing.USDT["polygon"]
FAKE_KEY = "polygonscan-" + "e2e" * 5


class FakeChain:
    """假的 Polygonscan 传输层：只回我们声明过的入账，还能一键「限速」。"""

    def __init__(self) -> None:
        self.transfers: list[dict] = []
        self.rate_limited = False

    def get_json(self, url, *, params, timeout_s):
        if self.rate_limited:
            return {
                "status": "0",
                "message": "NOTOK",
                "result": "Max rate limit reached, please use API Rate Limit after some time",
            }
        rows = [
            row
            for row in self.transfers
            if row["_action"] == params["action"]
            and int(row["blockNumber"]) >= int(params["startblock"])
        ]
        if not rows:
            return {"status": "0", "message": "No transactions found", "result": []}
        return {
            "status": "1",
            "message": "OK",
            "result": [{k: v for k, v in row.items() if k != "_action"} for row in rows],
        }

    def pay_usdt(self, block: int, *, to: str, units: int, tx_ref: str) -> None:
        self.transfers.append(
            {
                "_action": "tokentx",
                "blockNumber": str(block),
                "timeStamp": str(BASE_MS // 1000 + block),
                "hash": tx_ref,
                "from": "0x000000000000000000000000000000000000dead",
                "to": to,
                "value": str(units),
                "contractAddress": USDT_POLYGON,
                "tokenSymbol": "USDT",
                "tokenDecimal": "6",
            }
        )


def configured(conn, *, now: int = BASE_MS) -> pricing.BillingConfig:
    return pricing.save_billing_config(
        conn,
        actor="测试",
        changes={"polygon_xpub": ACCOUNT_XPUB, "solana_address": WALLET},
        ts=now,
    )


def build_watcher(conn, *, now: int = BASE_MS) -> tuple[Watcher, FakeChain]:
    """按订单派生的监听目标造 watcher（与 `chain-watch --orders` 同一条路径）。"""
    chain = FakeChain()
    client = PolygonscanClient(
        api_key=FAKE_KEY,
        ledger=QuotaLedger(conn, POLYGONSCAN, clock=lambda: BASE_MS),
        transport=chain,
    )
    watcher = Watcher(
        conn, settle.watch_targets(conn, now=now), polygon=client, clock=lambda: now
    )
    return watcher, chain


def scan_and_settle(
    conn, *, watcher: Watcher, chain: FakeChain, rescan: bool = False, now: int | None = None
) -> settle.SettleResult:
    """一轮扫描（增量或补扫）→ 对账开通：`chain-watch --orders` 的核心两步。"""
    stamp = BASE_MS + MINUTE if now is None else now
    observation = watcher.rescan() if rescan else watcher.poll()
    return settle.settle(conn, observation.transfers, now=stamp)


def run_scenario(scenario) -> None:
    """把 aiohttp 的真服务跑在一个本地端口上（仍不出网：只有 127.0.0.1）。"""

    async def runner():
        client = TestClient(TestServer(scenario.app))
        await client.start_server()
        try:
            await scenario.run(client)
        finally:
            await client.close()

    asyncio.run(runner())


def test_subscribe_pay_open_claim_read(three_game_ledger, data_root):
    """AC-3：下单 → 链上入账 → 自动开通 → 领取凭据 → 读到付费正文，全程无人工。"""
    conn = three_game_ledger.conn
    configured(conn)
    publish_brief(three_game_ledger)  # 一场进行中的比赛：正文对付费墙内的人开放
    seen: dict[str, int] = {}

    class Scenario:
        app = api.build_app(conn)

        async def run(self, client):
            # ① 订阅页下单（Solana：单一收款地址 + 每单唯一 memo）
            response = await client.post(
                "/api/orders",
                json={"platform": "qq", "username": "12345678", "tier": "trial", "network": "solana"},
            )
            assert response.status == 200
            order = await response.json()
            assert order["address"] == WALLET and order["memo"] == order["public_ref"]
            assert order["amount"] == "0.500001" and order["status"] == "pending"  # 0.50 + 唯一尾数
            claim_body = {
                "platform": "qq",
                "username": "12345678",
                "order_ref": order["public_ref"],
                "claim_token": order["claim_token"],
            }

            # ② 还没付款 → 领取是中性响应（不能提前放行）
            early = await client.post("/api/claim", json=claim_body)
            assert await early.json() == verify.NEUTRAL_BODY
            assert "Set-Cookie" not in early.headers

            # ③ 假链上入账（带 memo）→ 对账 → 自动开通
            stored = orders.get_order(conn, public_ref=order["public_ref"])
            settlement = settle.settle(
                conn,
                [
                    Transfer(
                        network=SOLANA,
                        address=WALLET,
                        tx_ref="sig-e2e-1",
                        asset=pricing.USDT[SOLANA],
                        units=stored.amount_due_units,
                        at_ms=BASE_MS,
                        memo=order["public_ref"],
                        block=250_000_000,
                    )
                ],
                now=BASE_MS + MINUTE,
                config=configured(conn),
            )
            assert settlement.granted == [stored.member_id]
            assert settlement.failures == []
            assert members.get_member(conn, stored.member_id).status == "active"
            seen["member_id"] = stored.member_id

            # ④ 凭「账号 + 订单引用 + 领取令牌」领取凭据（HttpOnly + Secure + SameSite=Lax）
            claim = await client.post("/api/claim", json=claim_body)
            assert claim.status == 200
            body = await claim.json()
            assert body["member"] is True and body["state"] == "active" and body["tier_label"] == "试用档"
            assert body["expires_at"] == members.get_member(conn, stored.member_id).expires_at
            cookie = claim.headers["Set-Cookie"].split(";")[0]
            assert "HttpOnly" in claim.headers["Set-Cookie"]
            assert "SameSite=Lax" in claim.headers["Set-Cookie"]

            # ⑤ 凭 cookie 校验：会员状态与有效期对用户可见（FR-C6-12）
            verified = await client.post("/api/verify", json={"platform": "qq", "username": "12345678"},
                                         headers={"Cookie": cookie})
            assert (await verified.json())["member"] is True

            # ⑥ 付费正文：会员拿得到，访客拿不到（正文不进静态产物 —— D4 的落点）
            path = f"/api/report/{three_game_ledger.match_id}/live_brief/paid"
            paid = await client.get(path, headers={"Cookie": cookie})
            assert paid.status == 200
            content = await paid.json()
            assert [segment["no"] for segment in content["segments"]]
            assert any(segment["nature"].startswith("事实") for segment in content["segments"])
            anonymous = await client.get(path)
            assert anonymous.status == 403

            # ⑦ 比赛结束 → 该场报告对所有人公开（可见性只由状态机驱动，ADR-0009）
            set_match_state(conn, three_game_ledger.match_id, state="ended")
            public = await client.get(path)
            assert public.status == 200 and (await public.json())["segments"]

    run_scenario(Scenario())
    assert seen["member_id"] == 1


def test_api_rejects_bad_requests(conn, data_root):
    """坏请求给明确的 4xx（这里不涉及会员信息，不需要"不可区分"）。"""
    configured(conn)

    class Scenario:
        app = api.build_app(conn)

        async def run(self, client):
            bad_tier = await client.post(
                "/api/orders",
                json={"platform": "qq", "username": "12345678", "tier": "vip", "network": "polygon"},
            )
            assert bad_tier.status == 400 and "档位" in (await bad_tier.json())["error"]

            bad_username = await client.post(
                "/api/orders",
                json={"platform": "qq", "username": "@nope", "tier": "standard", "network": "polygon"},
            )
            assert bad_username.status == 400 and "QQ" in (await bad_username.json())["error"]

            not_json = await client.post("/api/orders", data="not json")
            assert not_json.status == 400

            # 提交率限：订单接口也有限流（NFR-S-2）
            order_missing = await client.post("/api/claim", json={})
            assert await order_missing.json() == verify.NEUTRAL_BODY

            missing = await client.get("/api/report/9999/live_brief/paid")
            assert missing.status == 404
            bad_id = await client.get("/api/report/abc/live_brief/paid")
            assert bad_id.status == 400

    run_scenario(Scenario())


def test_api_rate_limits_tolerant_bodies_and_code_query(conn, data_root):
    """HTTP 层的三条边：限流给 429、坏 JSON 当空请求、付费正文也认 `?code=`。"""
    config = configured(conn)
    order, _ = orders.create_order(
        conn, platform="qq", username="12345678", tier="standard", network="polygon",
        now=BASE_MS, config=config,
    )
    result = settle.settle(
        conn,
        [
            Transfer(
                network="polygon", address=order.address, tx_ref="0xquery",
                asset=USDT_POLYGON, units=order.amount_due_units, at_ms=BASE_MS, block=1,
            )
        ],
        now=BASE_MS + MINUTE,
        config=config,
    )
    assert result.granted == [order.member_id]
    credential = verify.issue_credential(conn, order.member_id, now=BASE_MS + 2 * MINUTE)

    async def collect():
        client = TestClient(TestServer(api.build_app(conn)))
        await client.start_server()
        try:
            for _ in range(verify.ORDER_BY_IP.limit):
                allowed = await client.post(
                    "/api/orders",
                    json={"platform": "qq", "username": "12345678", "tier": "standard",
                          "network": "polygon"},
                )
                assert allowed.status == 200
            blocked = await client.post(
                "/api/orders",
                json={"platform": "qq", "username": "12345678", "tier": "standard",
                      "network": "polygon"},
            )
            assert blocked.status == 429 and "频繁" in (await blocked.json())["error"]

            broken_claim = await client.post("/api/claim", data="not json")
            assert await broken_claim.json() == verify.NEUTRAL_BODY
            broken_verify = await client.post("/api/verify", data="not json")
            assert await broken_verify.json() == verify.NEUTRAL_BODY

            by_query = await client.get(
                "/api/report/9999/live_brief/paid", params={"code": credential}
            )
            assert by_query.status == 404  # 凭据走 query 也认，只是这场报告还不存在
        finally:
            await client.close()

    asyncio.run(collect())


def test_api_edge_cases(conn, data_root, monkeypatch):
    """剩下三条边：转发头取 IP、请求体不是对象、付费正文也会被限流。"""
    configured(conn)

    async def collect():
        client = TestClient(TestServer(api.build_app(conn)))
        await client.start_server()
        try:
            forwarded = await client.post(
                "/api/orders",
                json={"platform": "qq", "username": "12345678", "tier": "standard", "network": "polygon"},
                headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.1"},
            )
            assert forwarded.status == 200  # 取第一跳当限流桶
            not_object = await client.post("/api/orders", json=[1, 2, 3])
            assert not_object.status == 400

            for _ in range(api.PAID_READ_RATE.limit):  # 打满付费正文的额度
                await client.get("/api/report/1/live_brief/paid")
            blocked = await client.get("/api/report/1/live_brief/paid")
            assert blocked.status == 429
        finally:
            await client.close()

    asyncio.run(collect())

    calls: dict[str, object] = {}
    monkeypatch.setattr(api.web, "run_app", lambda app, **kwargs: calls.update(kwargs, app=app))
    api.run(conn, host="127.0.0.1", port=9999)
    assert calls["host"] == "127.0.0.1" and calls["port"] == 9999


def test_verification_is_indistinguishable_over_http(conn, data_root):
    """AC-10：真 HTTP 上，「不存在」「未开通」「已过期」+ 错凭据 + 被限流，全部逐字节相同。"""
    config = configured(conn)
    open_order, _ = orders.create_order(
        conn, platform="telegram", username="@notyet", tier="standard", network="polygon",
        now=BASE_MS, config=config,
    )
    expired_order, _ = orders.create_order(
        conn, platform="telegram", username="@expired", tier="standard", network="polygon",
        now=BASE_MS, config=config,
    )
    settle.settle(
        conn,
        [
            Transfer(
                network="polygon", address=expired_order.address, tx_ref="0xold",
                asset=USDT_POLYGON, units=expired_order.amount_due_units, at_ms=BASE_MS, block=1,
            )
        ],
        now=BASE_MS + MINUTE,
        config=config,
    )
    members.sweep(conn, now=BASE_MS + MINUTE + config.tier("standard").duration_ms + config.grace_ms + 1)
    assert members.get_member(conn, expired_order.member_id).status == "expired"
    assert members.get_member(conn, open_order.member_id).status == "pending"

    bodies: list[tuple[int, bytes]] = []

    async def collect():
        client = TestClient(TestServer(api.build_app(conn)))
        await client.start_server()
        try:
            payloads = [
                {"platform": "telegram", "username": "@nobody"},  # 不存在
                {"platform": "telegram", "username": "@notyet"},  # 未开通
                {"platform": "telegram", "username": "@expired"},  # 已过期
                {"platform": "qq", "username": "99999999"},  # 不存在（另一个平台）
                {"platform": "telegram", "username": "@nobody", "code": "wrong"},  # 凭据不对
                {"platform": "discord", "username": "@nobody"},  # 账号标识非法
            ]
            for payload in payloads:
                response = await client.post("/api/verify", json=payload)
                bodies.append((response.status, await response.read()))
            for _ in range(verify.VERIFY_BY_IP.limit):  # 把 IP 额度打满
                await client.post("/api/verify", json={"platform": "telegram", "username": "@nobody"})
            blocked = await client.post("/api/verify", json={"platform": "telegram", "username": "@nobody"})
            bodies.append((blocked.status, await blocked.read()))
        finally:
            await client.close()

    asyncio.run(collect())
    assert len(bodies) == 7
    assert len({body for _, body in bodies}) == 1  # 逐字节相同
    assert {status for status, _ in bodies} == {200}
    assert json.loads(bodies[0][1]) == verify.NEUTRAL_BODY


def test_repeat_and_rescan_behaviour_end_to_end(conn, data_root):
    """AC-4/AC-5：重复检测只开通一次；限速漏掉的那一笔由补扫找回。"""
    config = configured(conn)
    order, _ = orders.create_order(
        conn, platform="telegram", username="@missed", tier="standard", network="polygon",
        now=BASE_MS, config=config,
    )
    watcher, chain = build_watcher(conn)
    chain.pay_usdt(100, to=order.address, units=order.amount_due_units, tx_ref="0xmissed")

    chain.rate_limited = True  # 这一轮供应商限速：什么都没看见，但不静默
    blind = watcher.poll()
    assert blind.transfers == [] and len(blind.failures) == 1
    assert orders.get_order(conn, order_id=order.id).status == "pending"

    chain.rate_limited = False
    settlement = scan_and_settle(conn, watcher=watcher, chain=chain, rescan=True)
    assert settlement.granted == [order.member_id]
    member = members.get_member(conn, order.member_id)
    assert member.status == "active"
    expires_at = member.expires_at

    # 又扫了几轮（游标增量 + 补扫）：同一笔付款不会重复开通、有效期不累加（AC-4）
    incremental = scan_and_settle(conn, watcher=watcher, chain=chain)
    again = scan_and_settle(conn, watcher=watcher, chain=chain, rescan=True)
    assert incremental.granted == [] and again.granted == []
    assert any(item.outcome == "duplicate" for item in again.payments)
    assert members.get_member(conn, order.member_id).expires_at == expires_at
    assert scan_tree(data_root) == [] and scan_tree(paths.repo_root()) == []


def test_expiry_grace_renewal_and_manual_grant(conn, data_root):
    """AC-6 + AC-5 后半段：续费顺延 → 到期 → 宽限 → 降级；人工补开通留痕且幂等。"""
    config = configured(conn)
    tier = config.tier("standard")
    order, _ = orders.create_order(
        conn, platform="qq", username="12345678", tier="standard", network="polygon",
        now=BASE_MS, config=config,
    )
    watcher, chain = build_watcher(conn)
    chain.pay_usdt(100, to=order.address, units=order.amount_due_units, tx_ref="0xfirst")
    scan_and_settle(conn, watcher=watcher, chain=chain)
    member = members.get_member(conn, order.member_id)
    assert member.expires_at == BASE_MS + MINUTE + tier.duration_ms
    assert member.can_access(now=BASE_MS + 20 * DAY, grace_ms=config.grace_ms)

    # 续费顺延：到期日过后、宽限期内续费，从**原到期日**往后加（AC-6）
    renewal, renewal_token = orders.create_order(
        conn, platform="qq", username="12345678", tier="standard", network="polygon",
        now=BASE_MS + 2 * DAY, config=config,
    )
    renewal_watcher, renewal_chain = build_watcher(conn, now=BASE_MS + 2 * DAY)
    renewal_chain.pay_usdt(200, to=renewal.address, units=renewal.amount_due_units, tx_ref="0xrenew")
    in_grace = member.expires_at + 1
    renewal_result = scan_and_settle(
        conn, watcher=renewal_watcher, chain=renewal_chain, now=in_grace
    )
    assert renewal_result.granted == [order.member_id]
    renewed = members.get_member(conn, order.member_id)
    assert renewed.expires_at == member.expires_at + tier.duration_ms
    assert renewed.status == "active"
    claim = verify.claim(
        conn, platform="qq", username="12345678", order_ref=renewal.public_ref,
        claim_token=renewal_token, now=in_grace, config=config,
    )
    assert claim.ok and claim.member is not None and claim.member.expires_at == renewed.expires_at

    # 到期 → 宽限期内仍可访问（AC-6）→ 宽限期满降级为访客，凭据随即失效
    after_expiry = renewed.expires_at + 1
    assert verify.verify(conn, code=claim.credential, now=after_expiry).ok
    members.sweep(conn, now=after_expiry)
    assert members.get_member(conn, order.member_id).status == "grace"
    past_grace = renewed.expires_at + config.grace_ms + 1
    members.sweep(conn, now=past_grace)
    expired = members.get_member(conn, order.member_id)
    assert expired.status == "expired"
    assert not expired.can_access(now=past_grace, grace_ms=config.grace_ms)
    assert not verify.verify(conn, code=claim.credential, now=past_grace).ok
    orders.expire_due(conn, now=past_grace, config=config)  # 顺手把超时未付的订单标掉

    # 人工补开通（AC-5 后半段）：凭交易凭证 + 理由，幂等且留痕
    manual_order, _ = orders.create_order(
        conn, platform="qq", username="87654321", tier="standard", network="polygon",
        now=BASE_MS + 3 * DAY, config=config,
    )
    long_after = past_grace + DAY
    paid, opened = settle.manual_payment(
        conn, order_ref=manual_order.public_ref, tx_ref="0xmanual", actor="运营者",
        reason="用户提供了区块浏览器链接，链上确认足额", now=long_after, config=config,
    )
    assert opened and paid.status == "paid"
    granted_member = members.get_member(conn, manual_order.member_id)
    assert granted_member.status == "active"
    # 早过了宽限期：从此刻起算（顺延只对还在有效期内的人有意义）
    assert granted_member.expires_at == long_after + tier.duration_ms
    _, opened_again = settle.manual_payment(
        conn, order_ref=manual_order.public_ref, tx_ref="0xmanual", actor="运营者",
        reason="重试一次", now=long_after + MINUTE, config=config,
    )
    assert opened_again is False
    assert members.get_member(conn, manual_order.member_id).expires_at == granted_member.expires_at
    assert scan_tree(data_root) == [] and scan_tree(paths.repo_root()) == []


def test_generate_and_publish_is_imported(three_game_ledger):
    """守住 e2e 依赖（`publish_brief` 与 `generate_and_publish` 是同一件事的两个入口）。"""
    assert callable(generate_and_publish) and callable(publish_brief)
