"""T10 端到端：站点产物里的 beacon → `POST /api/stats/beacon` → `GET /api/stats/daily`。

对应 issue #20 的验收标准，逐条走一遍（全程断网，只有 127.0.0.1 上的一个 aiohttp 服务）：

- **独立访客口径**：`sha256(每日盐 + IP + UA)`；**每日换盐** —— 同一个人两天得到两个不相关的
  哈希，因此「某天多少人」答得出、「这两天是不是同一个人」答不出（AC-9）。
- **可回答**：某天访问量、访问付费页人数、下单转化、留资数。
- **不可回答**：具体是谁（响应里只有计数，没有任何 IP / 访客哈希 / 身份字段）。
- **保留**：明细 90 天 → 汇总入 `stats_daily`（`daily.prune`，单测里逐条验过，这里验接口口径）。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from danmu_intel import api
from danmu_intel.billing import verify
from danmu_intel.common.matches import create_match
from danmu_intel.site_stats import beacon, daily

DAY0 = "2026-09-22"
DAY1 = "2026-09-23"
DAY0_MS = daily.day_start_ms(DAY0)  # 本地零点：把「现在」钉在固定的一天（不改系统时钟）
DAY_MS = 24 * 3600 * 1000
UA = {"User-Agent": "Mozilla/5.0 (T10 beacon)"}

#: 日汇总接口的字段全集（**只有计数**：AC-9「答不了是谁」在这里是逐字段的断言）。
DAILY_KEYS = {
    "day",
    "page_views",
    "sessions",
    "unique_visitors",
    "paid_page_views",
    "paid_unique_visitors",
    "leads",
    "orders",
    "paid_orders",
}

FORBIDDEN_TOKENS = ("ip", "user_agent", "visitor_hash", "member", "contact", "cookie", "credential")


class Client:
    """一个跑在本地端口上的真服务（beacon 与日汇总都走真 HTTP）。"""

    def __init__(self, conn) -> None:
        self.conn = conn
        self.app = api.build_app(conn)


@pytest.fixture
def client(conn) -> Client:
    return Client(conn)


def run(client: Client, body) -> None:
    """把真服务跑在一个本地端口上（仍不出网：只有 127.0.0.1）。"""

    async def runner():
        test_client = TestClient(TestServer(client.app))
        await test_client.start_server()
        try:
            await body(test_client)
        finally:
            await test_client.close()

    asyncio.run(runner())


@pytest.fixture
def clock(monkeypatch):
    """把「现在」钉在某一天，用来验跨日换盐（不改系统时钟）。"""

    class Clock:
        def __init__(self) -> None:
            self.now = DAY0_MS

        def set(self, at_ms: int) -> None:
            self.now = at_ms

    tick = Clock()
    monkeypatch.setattr(beacon, "now_ms", lambda: tick.now)
    monkeypatch.setattr(daily, "now_ms", lambda: tick.now)
    return tick


def test_beacon_records_a_page_view_without_returning_anything(client, conn, clock):
    """上报成功 → 204 无内容；库里落一条明细（IP/UA 就地哈希，不进响应也不进库）。"""

    async def scenario(http):
        response = await http.post("/api/stats/beacon", json={"page": "index.html"}, headers=UA)
        assert response.status == 204
        assert await response.text() == ""

    run(client, scenario)
    row = conn.execute("SELECT * FROM stats_events").fetchone()
    assert row["page"] == "index.html" and row["day"] == DAY0 and row["paid"] == 0
    assert row["visitor_hash"] and row["member_id"] is None
    # 库里只有哈希：UA 明文没有落库（列清单由单测逐列断言）
    assert "Mozilla" not in row["visitor_hash"]


def test_beacon_accepts_the_sendBeacon_content_type(client, conn, clock):
    """`navigator.sendBeacon` 送的是 text/plain（免预检）：接口不拿内容类型当判据。"""

    async def scenario(http):
        response = await http.post(
            "/api/stats/beacon",
            data=json.dumps({"page": "/matches/1/full.html"}),
            headers={**UA, "Content-Type": "text/plain;charset=UTF-8"},
        )
        assert response.status == 204

    run(client, scenario)
    assert conn.execute("SELECT page FROM stats_events").fetchone()["page"] == "matches/1/full.html"


@pytest.mark.parametrize("payload", [{"page": ""}, {"page": "https://elsewhere.example/x"}, {}])
def test_beacon_rejects_non_site_paths(client, conn, payload):
    async def scenario(http):
        response = await http.post("/api/stats/beacon", json=payload, headers=UA)
        assert response.status == 400
        body = await response.json()
        assert "error" in body

    run(client, scenario)
    assert conn.execute("SELECT COUNT(*) AS n FROM stats_events").fetchone()["n"] == 0


def test_beacon_rejects_a_non_json_body(client, conn):
    async def scenario(http):
        response = await http.post(
            "/api/stats/beacon", data="not json", headers={**UA, "Content-Type": "text/plain"}
        )
        assert response.status == 400

    run(client, scenario)


def test_beacon_is_rate_limited_per_ip(client, conn, monkeypatch, clock):
    """NFR-S-2：写接口一律限流（越限与其它接口同一响应形态）。"""
    monkeypatch.setattr(api, "BEACON_BY_IP", verify.RateLimit(limit=2, window_ms=60_000))

    async def scenario(http):
        for _ in range(2):
            assert (await http.post("/api/stats/beacon", json={"page": "index.html"}, headers=UA)).status == 204
        blocked = await http.post("/api/stats/beacon", json={"page": "index.html"}, headers=UA)
        assert blocked.status == 429
        assert "error" in await blocked.json()

    run(client, scenario)
    assert conn.execute("SELECT COUNT(*) AS n FROM stats_events").fetchone()["n"] == 2


def test_same_visitor_across_days_cannot_be_linked(client, conn, clock):
    """AC-9 后半段：跨日换盐 —— 两天的独立访客数各算一人，且哈希互不相关。"""

    async def scenario(http):
        for _ in range(2):
            assert (await http.post("/api/stats/beacon", json={"page": "index.html"}, headers=UA)).status == 204
        clock.set(DAY0_MS + DAY_MS)  # 第二天
        for _ in range(3):
            assert (await http.post("/api/stats/beacon", json={"page": "index.html"}, headers=UA)).status == 204

    run(client, scenario)
    rows = conn.execute("SELECT day, visitor_hash FROM stats_events ORDER BY id").fetchall()
    day0_hashes = {row["visitor_hash"] for row in rows if row["day"] == DAY0}
    day1_hashes = {row["visitor_hash"] for row in rows if row["day"] == DAY1}
    assert len(day0_hashes) == 1 and len(day1_hashes) == 1
    assert day0_hashes != day1_hashes  # 同一个人两天两个不相关的哈希
    assert daily.summary(conn, DAY0).unique_visitors == 1
    assert daily.summary(conn, DAY1).unique_visitors == 1
    # 旧盐被丢弃：库里只留当天一行
    assert conn.execute("SELECT day FROM stats_salt").fetchall()[0]["day"] == DAY1


def test_member_id_comes_only_from_a_valid_credential_cookie(client, conn, clock):
    """留资/付费者的关联靠凭据 cookie（服务端校验），客户端自称的身份一律不采信。"""
    from danmu_intel.billing import members

    member = members.get_or_create_member(conn, platform="qq", username="12345678", tier="trial")
    code = verify.issue_credential(conn, member.id)

    async def scenario(http):
        # ① 带有效凭据 → 记 member_id
        response = await http.post(
            "/api/stats/beacon",
            json={"page": "matches/1/full.html", "member_id": 999},  # 自称的身份被忽略
            headers=UA,
            cookies={verify.COOKIE_NAME: code},
        )
        assert response.status == 204
        # ② 没有凭据 → 不记身份
        assert (await http.post("/api/stats/beacon", json={"page": "index.html"}, headers=UA)).status == 204
        # ③ 假凭据 → 不记身份
        assert (
            await http.post(
                "/api/stats/beacon",
                json={"page": "index.html"},
                headers=UA,
                cookies={verify.COOKIE_NAME: "假凭据" * 8},
            )
        ).status == 204

    run(client, scenario)
    recorded = [row["member_id"] for row in conn.execute("SELECT member_id FROM stats_events ORDER BY id")]
    assert recorded == [member.id, None, None]


def test_daily_api_answers_the_five_questions_without_identity_fields(client, conn, clock):
    """可回答：访问量 / 访问付费页人数 / 下单转化 / 留资数；不可回答：具体是谁（AC-9）。"""
    from danmu_intel.billing import orders, pricing

    pricing.save_billing_config(
        conn,
        actor="管理员",
        changes={
            "api_base": "https://host.ts.net:8443",
            "solana_address": "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin",
            "tiers": [{"key": "trial", "label": "试用档", "amount_units": 500_000, "days": 7}],
        },
    )
    match_id = create_match(conn, league="LPL", team_a="iG", team_b="LNG", state="live")
    orders.create_order(
        conn,
        platform="qq",
        username="12345678",
        tier="trial",
        network="solana",
        now=DAY0_MS,
        config=pricing.load_billing_config(conn),
    )

    async def scenario(http):
        for ip in ("203.0.113.1", "203.0.113.2"):
            for page in (f"matches/{match_id}/full.html", "index.html"):
                response = await http.post(
                    "/api/stats/beacon",
                    json={"page": page},
                    headers={**UA, "X-Forwarded-For": f"{ip}, 100.64.0.1"},
                )
                assert response.status == 204
        response = await http.get(f"/api/stats/daily?day={DAY0}")
        assert response.status == 200
        payload = await response.json()
        assert payload["day"] == DAY0
        assert payload["page_views"] == 4
        assert payload["unique_visitors"] == 2
        assert payload["paid_page_views"] == 2
        assert payload["paid_unique_visitors"] == 2  # AC-9 前半句：某天有多少人来过付费页
        assert payload["leads"] == 1 and payload["orders"] == 1  # 留资与下单转化
        assert payload["paid_orders"] == 0
        # 不可回答「是谁」：字段集合只有计数，且没有任何身份/网络字段
        assert set(payload) == DAILY_KEYS
        text = json.dumps(payload, ensure_ascii=False).lower()
        for token in FORBIDDEN_TOKENS:
            assert token not in text, f"响应里出现了禁止的字段：{token}"
        assert "203.0.113" not in text and "mozilla" not in text

    run(client, scenario)


def test_daily_api_defaults_to_today_and_rejects_bad_dates(client, clock):
    async def scenario(http):
        today = await (await http.get("/api/stats/daily")).json()
        assert today["day"] == DAY0
        assert set(today) == DAILY_KEYS
        for bad in ("2026/09/22", "22-09-2026", "2026-13-01"):
            response = await http.get(f"/api/stats/daily?day={bad}")
            assert response.status == 400
            assert "error" in await response.json()

    run(client, scenario)


def test_daily_api_reads_the_rollup_after_the_detail_expires(client, conn, clock):
    """明细到期（90 天）后接口照样答得出那天的数字：读的是汇总行。"""

    async def scenario(http):
        assert (await http.post("/api/stats/beacon", json={"page": "index.html"}, headers=UA)).status == 204
        daily.prune(conn, at_ms=DAY0_MS + 91 * DAY_MS)
        payload = await (await http.get(f"/api/stats/daily?day={DAY0}")).json()
        assert payload["page_views"] == 1 and payload["unique_visitors"] == 1

    run(client, scenario)
    assert conn.execute("SELECT COUNT(*) AS n FROM stats_events").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM stats_daily").fetchone()["n"] == 1
