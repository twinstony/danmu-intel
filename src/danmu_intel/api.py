"""对外 HTTP 面（ADR-0001：Funnel 暴露「下单 / 校验 / 领取 / 付费正文 / 统计上报」几个接口）。

这一层**只做搬运**：解析 JSON、把客户端 IP 交给限流桶、把领域函数的结论写成响应。
防枚举（AC-10）的「逐字节相同」是 `billing/verify.py` 里那个函数保证的，不是路由保证的
—— 所以它可以在断网的单测里直接被断言，而不需要起一个服务。

| 路由 | 动作 |
|---|---|
| `POST /api/orders` | 下单：档位 + 通讯账号 + 网络 → 收款要求（地址 / memo / 金额 / 到期）+ 领取令牌 |
| `POST /api/verify` | 校验：凭据有效 → 会员状态（有效期可见，FR-C6-12）；否则中性响应 |
| `POST /api/claim` | 领取凭据：账号 + 订单引用 + 领取令牌 → Set-Cookie（HttpOnly+Secure+SameSite=Lax） |
| `GET /api/report/<id>/<kind>/paid` | 付费正文：凭据有效或比赛已结束才返回（D4：静态产物里没有正文） |
| `POST /api/stats/beacon` | 站点统计上报：`{page}` → 落一条明细（响应无内容，**不含 IP / 身份**） |
| `GET /api/stats/daily?day=` | 某天的站点统计口径：只有计数（AC-9：答人数，答不了「是谁」） |

订阅页（静态产物）指向这里的基址由 `billing.api_base` 配置给出；站点产物里的自建 beacon
也打向同一个基址（`publish/site.py`，配置为空时不写脚本）。

站点统计的路由用 `aiohttp.web` 时**不查库里的身份**：访客身份（`member_id`）只在请求
自带的凭据 cookie 校验通过时才被记下 —— 客户端自称的身份一律不采信。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from aiohttp import web

from danmu_intel.billing import members, orders, pricing, verify, xpub
from danmu_intel.common import paywall
from danmu_intel.publish import access
from danmu_intel.site_stats import beacon, daily

#: 付费正文的读取频率限制（按 IP，和校验共用一套额度）。
PAID_READ_RATE = verify.VERIFY_BY_IP

#: 站点统计上报的频率限制（按 IP；NFR-S-2：写接口一律限流）。
BEACON_BY_IP = verify.RateLimit(limit=120, window_ms=60_000)

#: 应用状态里的数据库连接（`web.AppKey` 而不是字符串键：避免 aiohttp 的弃用警告）。
CONNECTION = web.AppKey("connection", sqlite3.Connection)


def client_ip(request: web.Request) -> str:
    """客户端 IP：Funnel 转发时以 `X-Forwarded-For` 的第一跳为准。"""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.remote or "unknown"


def build_app(conn: sqlite3.Connection) -> web.Application:
    app = web.Application()
    app[CONNECTION] = conn
    app.router.add_post("/api/orders", _handle_orders)
    app.router.add_post("/api/verify", _handle_verify)
    app.router.add_post("/api/claim", _handle_claim)
    app.router.add_get("/api/report/{match_id}/{kind}/paid", _handle_paid)
    app.router.add_post("/api/stats/beacon", _handle_beacon)
    app.router.add_get("/api/stats/daily", _handle_stats_daily)
    return app


def run(conn: sqlite3.Connection, *, host: str, port: int) -> None:
    """起服务（前台进程；交给 Funnel/PM2 托管属运维）。"""
    web.run_app(build_app(conn), host=host, port=port, print=None)


async def _json_body(request: web.Request, *, tolerate: bool = False) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        if tolerate:
            return {}
        raise web.HTTPBadRequest(text='{"error":"请求体应为 JSON 对象"}', content_type="application/json")
    if not isinstance(payload, dict):
        if tolerate:
            return {}
        raise web.HTTPBadRequest(text='{"error":"请求体应为 JSON 对象"}', content_type="application/json")
    return payload


async def _beacon_body(request: web.Request) -> dict[str, Any]:
    """站内 beacon 的请求体：`navigator.sendBeacon` 送的是 `text/plain`（免预检），

    因此这里按文本收再自己解析 JSON —— 内容类型不是判定依据。
    """
    try:
        payload = json.loads(await request.text() or "{}")
    except Exception:
        raise web.HTTPBadRequest(text='{"error":"请求体应为 JSON 对象"}', content_type="application/json")
    if not isinstance(payload, dict):
        raise web.HTTPBadRequest(text='{"error":"请求体应为 JSON 对象"}', content_type="application/json")
    return payload


def _error(message: str, *, status: int) -> web.Response:
    return web.json_response({"error": message}, status=status)


def _respond(result: verify.VerifyResult, *, cookie: str | None = None) -> web.Response:
    headers = {"Set-Cookie": cookie} if cookie else {}
    return web.json_response(result.as_dict(), status=result.status, headers=headers)


async def _handle_orders(request: web.Request) -> web.Response:
    conn: sqlite3.Connection = request.app[CONNECTION]
    payload = await _json_body(request)
    if verify.consume(conn, f"order:ip:{client_ip(request)}", verify.ORDER_BY_IP):
        return _error("请求过于频繁，请稍后再试", status=429)
    try:
        order, claim_token = orders.create_order(
            conn,
            platform=str(payload.get("platform") or ""),
            username=str(payload.get("username") or ""),
            tier=str(payload.get("tier") or ""),
            network=str(payload.get("network") or ""),
            config=pricing.load_billing_config(conn),
        )
    except (orders.OrderError, members.ContactError, pricing.BillingConfigError, xpub.XpubError) as exc:
        return _error(str(exc), status=400)
    return web.json_response({**order.as_dict(), "claim_token": claim_token})


async def _handle_verify(request: web.Request) -> web.Response:
    conn: sqlite3.Connection = request.app[CONNECTION]
    payload = await _json_body(request, tolerate=True)
    code = str(payload.get("code") or "") or verify.cookie_from_header(request.headers.get("Cookie"))
    result = verify.verify(
        conn,
        code=code,
        platform=payload.get("platform"),
        username=payload.get("username"),
        ip=client_ip(request),
    )
    return _respond(result)


async def _handle_claim(request: web.Request) -> web.Response:
    conn: sqlite3.Connection = request.app[CONNECTION]
    payload = await _json_body(request, tolerate=True)
    result = verify.claim(
        conn,
        platform=str(payload.get("platform") or ""),
        username=str(payload.get("username") or ""),
        order_ref=str(payload.get("order_ref") or ""),
        claim_token=str(payload.get("claim_token") or ""),
        ip=client_ip(request),
    )
    return _respond(result, cookie=result.cookie)


async def _handle_paid(request: web.Request) -> web.Response:
    conn: sqlite3.Connection = request.app[CONNECTION]
    kind = request.match_info["kind"]
    try:
        match_id = int(request.match_info["match_id"])
    except ValueError:
        return _error("比赛标识应为整数", status=400)
    if verify.consume(conn, f"paid:ip:{client_ip(request)}", PAID_READ_RATE):
        return _error("请求过于频繁，请稍后再试", status=429)

    config = pricing.load_billing_config(conn)
    code = request.query.get("code") or verify.cookie_from_header(request.headers.get("Cookie"))
    member = verify.check_credential(conn, code)
    verified = bool(
        member is not None and member.can_access(now=verify.now_ms(), grace_ms=config.grace_ms)
    )
    try:
        content = access.report_content(conn, match_id, kind, credential_verified=verified)
    except paywall.PaidAccessDenied as exc:
        return _error(str(exc), status=403)
    except LookupError as exc:
        return _error(str(exc), status=404)
    return web.json_response(content.as_dict())


async def _handle_beacon(request: web.Request) -> web.Response:
    """站点统计上报：收下页面路径，IP/UA 就地哈希，响应**无内容**（AC-9：答不了是谁）。"""
    conn: sqlite3.Connection = request.app[CONNECTION]
    ip = client_ip(request)
    if verify.consume(conn, f"beacon:ip:{ip}", BEACON_BY_IP):
        return _error("请求过于频繁，请稍后再试", status=429)
    payload = await _beacon_body(request)
    # 身份只认凭据 cookie（客户端自称的字段一律不采信）；跨站页面带不上 cookie 时就记空。
    credential = verify.cookie_from_header(request.headers.get("Cookie"))
    member = verify.check_credential(conn, credential) if credential else None
    try:
        beacon.record(
            conn,
            page=payload.get("page"),
            ip=ip,
            user_agent=request.headers.get("User-Agent", ""),
            member_id=member.id if member is not None else None,
        )
    except ValueError as exc:
        return _error(str(exc), status=400)
    return web.Response(status=204)


async def _handle_stats_daily(request: web.Request) -> web.Response:
    """某天的站点统计口径：只有计数，没有任何 IP / 访客哈希 / 身份字段（AC-9）。"""
    conn: sqlite3.Connection = request.app[CONNECTION]
    day = request.query.get("day") or daily.today()
    try:
        summary = daily.summary(conn, day)
    except ValueError as exc:
        return _error(str(exc), status=400)
    return web.json_response(summary.as_dict())
