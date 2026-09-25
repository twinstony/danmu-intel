"""后台进程（ADR-0007 的落地）：独立进程 + 仅 tailnet + 单管理员 + 12 页 + 写操作。

| 层 | 做什么 |
|---|---|
| 绑定 | `auth.require_tailnet_host`：只肯绑 tailnet / 回环地址，`0.0.0.0` 直接拒绝启动 |
| 中间件 1 | 每个请求按对端 IP 判一次 tailnet，非 tailnet → **403**（AC-7 / NFR-S-3） |
| 中间件 2 | 除登录页外一律要有效签名 cookie，否则 **403**（FR-C8-3：非管理员不可达） |
| 路由 | 12 个页面（GET 只读）+ 16 个动作（POST 全留痕）+ 报告预览 + 登录/登出 |

与公开面（`api.py`，走 Funnel）**是两个进程、两套端口、两套 cookie**：后台不在公网面里，
因此它没有「被 Funnel 转发过来」这条路（设计 §14.2）。

会话：登录成功发一枚短期签名 cookie（12 小时，HttpOnly + SameSite=Lax），
口令用 PBKDF2 哈希存仓库外 `.env`；改口令 → 哈希变 → 所有旧会话当场失效。
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Mapping
from urllib.parse import urlencode

from aiohttp import web

from danmu_intel.admin import actions, auth, pages
from danmu_intel.admin.html import Html, esc, form, layout, raw, Field
from danmu_intel.billing import verify as billing_verify
from danmu_intel.publish.release import ReleaseContext

logger = logging.getLogger(__name__)

#: 登录尝试的限流（按 IP，1 分钟 10 次）：后台口令不能拿来做暴力破解的靶子。
LOGIN_BY_IP = billing_verify.RateLimit(limit=10, window_ms=60_000)

CONNECTION = web.AppKey("connection", sqlite3.Connection)
CONTEXT = web.AppKey("admin_context", pages.AdminContext)
ACTION_CONTEXT = web.AppKey("action_context", actions.ActionContext)
SECRETS = web.AppKey("admin_secrets", auth.AdminSecrets)
SECURE_COOKIE = web.AppKey("secure_cookie", bool)
CLIENT_IP = web.RequestKey("admin_client_ip", str)

LOGIN_PATH = "/admin/login"
LOGOUT_PATH = "/admin/logout"
PREVIEW_PATH = "/admin/reports/preview"


def build_app(
    *,
    conn: sqlite3.Connection,
    data_root: Path,
    release: Callable[[], ReleaseContext],
    secrets: auth.AdminSecrets,
    clock: Callable[[], int] = pages.now_ms,
    secure_cookie: bool = False,
) -> web.Application:
    """后台应用（依赖全部注入：测试不需要真 git、真 Vercel、真 tailnet）。"""
    app = web.Application(middlewares=[tailnet_middleware, auth_middleware])
    app[CONNECTION] = conn
    app[CONTEXT] = pages.AdminContext(conn=conn, data_root=data_root, clock=clock)
    app[ACTION_CONTEXT] = actions.ActionContext(
        conn=conn, data_root=data_root, release=release, clock=clock
    )
    app[SECRETS] = secrets
    app[SECURE_COOKIE] = secure_cookie

    app.router.add_get(LOGIN_PATH, handle_login_page)
    app.router.add_post(LOGIN_PATH, handle_login)
    app.router.add_get(LOGOUT_PATH, handle_logout)
    app.router.add_post(LOGOUT_PATH, handle_logout)
    app.router.add_get(PREVIEW_PATH, handle_preview)
    for page in pages.PAGES:
        app.router.add_get(page.path, handle_page)
    for action in actions.ACTIONS:
        app.router.add_post(f"/admin/{action.key}", handle_action)
    return app


def run(
    *,
    conn: sqlite3.Connection,
    host: str,
    port: int,
    data_root: Path,
    release: Callable[[], ReleaseContext],
    clock: Callable[[], int] = pages.now_ms,
    secure_cookie: bool = False,
) -> None:
    """起后台（前台进程；交给 systemd 托管属运维）。绑定前先验地址只属于 tailnet。"""
    auth.require_tailnet_host(host)
    secrets = auth.load_secrets()
    app = build_app(
        conn=conn,
        data_root=data_root,
        release=release,
        secrets=secrets,
        clock=clock,
        secure_cookie=secure_cookie,
    )
    web.run_app(app, host=host, port=port, print=None)


# —— 中间件 ——


@web.middleware
async def tailnet_middleware(request: web.Request, handler: Awaitable[web.Response] | web.Response) -> web.Response:
    """非 tailnet 请求一律 403：后台只该在 tailnet 里可达（AC-7 / NFR-S-3）。"""
    ip = auth.client_ip(request.headers.get("X-Forwarded-For"), request.remote)
    if not auth.is_tailnet_ip(ip):
        logger.warning("拒绝非 tailnet 访问后台：%s %s（对端 %s）", request.method, request.path, ip or "未知")
        return _plain("403：后台只在 tailnet 内可达（非 tailnet 网络不可达）", status=403)
    request[CLIENT_IP] = ip
    return await handler(request)


@web.middleware
async def auth_middleware(request: web.Request, handler: Awaitable[web.Response] | web.Response) -> web.Response:
    """除登录/登出外，一律要有效签名 cookie（FR-C8-3：非管理员不得访问任何后台页面）。"""
    if request.path in {LOGIN_PATH, LOGOUT_PATH}:
        return await handler(request)
    secrets: auth.AdminSecrets = request.app[SECRETS]
    context: pages.AdminContext = request.app[CONTEXT]
    value = auth.cookie_from_header(request.headers.get("Cookie"))
    if not auth.session_valid(secrets.password_hash, value, now=context.now()):
        return _html(
            layout(
                "需要登录",
                raw(
                    "<p>需要管理员登录才能访问后台。</p>"
                    f'<p><a href="{esc(LOGIN_PATH)}">去登录</a></p>'
                ),
                nav=(),
                active="",
            ),
            status=403,
        )
    return await handler(request)


# —— 响应小工具 ——


def _plain(message: str, *, status: int) -> web.Response:
    return web.Response(text=message, status=status, content_type="text/plain", charset="utf-8")


def _html(document: Html, *, status: int = 200, cookie: str | None = None) -> web.Response:
    headers = {"Set-Cookie": cookie} if cookie else {}
    return web.Response(
        text=str(document), status=status, content_type="text/html", charset="utf-8", headers=headers
    )


def _redirect(location: str, *, cookie: str | None = None) -> web.Response:
    headers = {"Location": location}
    if cookie:
        headers["Set-Cookie"] = cookie
    return web.Response(status=303, headers=headers)


def _back_to(page_key: str, *, ok: str | None = None, err: str | None = None, query: Mapping[str, str] | None = None) -> str:
    """动作做完回哪一页：404 之外的参数只带提示（`?ok=` / `?err=`）与原页筛选。"""
    params = {key: value for key, value in (query or {}).items() if key in {"match_id", "action", "limit"}}
    if ok is not None:
        params["ok"] = ok
    if err is not None:
        params["err"] = err
    target = pages.page_of(page_key).path
    return f"{target}?{urlencode(params)}" if params else target


# —— 路由：登录 / 登出 ——


async def handle_login_page(request: web.Request) -> web.Response:
    fields = [Field("password", "管理员口令", kind="password", required=True)]
    document = layout(
        "登录",
        raw(
            f'<section class="card"><h2>管理员登录</h2>'
            f'{form(LOGIN_PATH, fields, submit="登录")}'
            "<p class=\"note\">口令哈希存在仓库外 .env（0600）；登录态是 12 小时的签名 cookie。"
            "本进程只监听 tailnet 地址。</p></section>"
        ),
        nav=(),
    )
    return _html(document)


async def handle_login(request: web.Request) -> web.Response:
    ctx: actions.ActionContext = request.app[ACTION_CONTEXT]
    secrets: auth.AdminSecrets = request.app[SECRETS]
    ip = request[CLIENT_IP]
    if billing_verify.consume(ctx.conn, f"admin:login:{ip}", LOGIN_BY_IP, now=ctx.clock()):
        actions.log_login(ctx, ip=ip, ok=False)
        return _plain("429：登录尝试过于频繁，请稍后再试", status=429)
    form_data = await _form(request)
    ok = auth.verify_password(form_data.get("password", ""), secrets.password_hash)
    actions.log_login(ctx, ip=ip, ok=ok)
    if not ok:
        return _html(
            layout(
                "登录失败",
                raw('<p>口令不对。</p>' f'<p><a href="{esc(LOGIN_PATH)}">再试一次</a></p>'),
                nav=(),
            ),
            status=403,
        )
    cookie = auth.cookie_value(
        auth.session_value(secrets.password_hash, now=ctx.clock()),
        max_age_s=auth.SESSION_TTL_MS // 1000,
        secure=bool(request.app[SECURE_COOKIE]),
    )
    return _redirect("/admin", cookie=cookie)


async def handle_logout(request: web.Request) -> web.Response:
    ctx: actions.ActionContext = request.app[ACTION_CONTEXT]
    actions.log_logout(ctx, ip=request[CLIENT_IP])
    return _redirect(LOGIN_PATH, cookie=auth.expired_cookie())


# —— 路由：页面 / 预览 / 动作 ——


async def handle_page(request: web.Request) -> web.Response:
    context: pages.AdminContext = request.app[CONTEXT]
    page = _page_of_path(request.path)
    return _html(page.render(context, dict(request.query)))


def _page_of_path(path: str) -> pages.AdminPage:
    for page in pages.PAGES:
        if page.path == path:
            return page
    raise web.HTTPNotFound(text="没有这个后台页面")


async def handle_preview(request: web.Request) -> web.Response:
    context: pages.AdminContext = request.app[CONTEXT]
    try:
        document = pages.render_report_preview(context, dict(request.query))
    except (LookupError, ValueError, TypeError, KeyError) as exc:
        return _html(layout("预览失败", raw(f"<p>{esc(exc)}</p>"), nav=pages.nav_items()), status=404)
    return web.Response(text=str(document), content_type="text/html", charset="utf-8")


async def handle_action(request: web.Request) -> web.Response:
    """一个写操作：执行 → 成功回原页带绿条，失败回原页带红条（原因照实说）。"""
    ctx: actions.ActionContext = request.app[ACTION_CONTEXT]
    key = request.path[len("/admin/") :]
    try:
        action = actions.action_of(key)
    except LookupError:
        raise web.HTTPNotFound(text="没有这个后台动作") from None
    form_data = await _form(request)
    try:
        message = action.run(ctx, form_data)
    except actions.ActionError as exc:
        return _redirect(_back_to(action.page, err=str(exc), query=form_data))
    except Exception as exc:  # 领域层的报错（含 ValueError/LookupError/凭据/链上）都要说人话
        logger.exception("后台动作 %s 失败", key)
        return _redirect(_back_to(action.page, err=f"{exc}", query=form_data))
    return _redirect(_back_to(action.page, ok=message, query=form_data))


async def _form(request: web.Request) -> dict[str, str]:
    """表单取值：`application/x-www-form-urlencoded`（HTML 表单默认编码）。"""
    posted = await request.post()
    return {key: str(value) for key, value in posted.items()}
