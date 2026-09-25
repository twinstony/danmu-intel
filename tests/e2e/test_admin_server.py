"""T12 端到端：后台独立进程（真 aiohttp 服务，跑在 127.0.0.1 上）。

对应 issue #22 的验收标准，逐条走一遍：

- **非 tailnet 请求 403**（AC-7）——连登录页都不给看；
- **非管理员不可访问任何后台页面**（FR-C8-3）——没有签名 cookie 就是 403；
- **12 个页面**（设计 §14.1）——登录后每个都返回 200，且内容里带得出库里的东西；
- **所有写操作入 `audit_log`**（FR-C8-4）——登录、登记、改状态、切片修正、灰信号评审、发布都留痕；
- **配置保存后 60 秒内生效**（NFR-T-4）——保存即刻递增版本号（别的进程 ≤60 秒靠 TTL 缓存、
  采集子进程靠版本号重起，这两条在单测里逐条验过）；
- **凭据不进库**（NFR-S-4）——跑完一整轮之后，库里搜不到口令明文与哈希。

测试客户端**不跟随跳转**（`allow_redirects=False`）：303 的目标与 `?ok=` / `?err=` 提示本身
就是要断言的东西。
"""

from __future__ import annotations

import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from danmu_intel.admin import auth, pages, server
from danmu_intel.billing import pricing
from danmu_intel.common import audit, config_store, paths
from danmu_intel.common.config import load_stats_config
from tests.unit.test_billing_xpub import ACCOUNT_XPUB

PASSWORD = "后台口令-1234"
PUBLIC_IP = "203.0.113.9"  # TEST-NET-3：一个明确的「公网」地址
TAILNET_IP = "100.101.102.103"
CLOCK = 1_790_064_000_000


class Admin:
    """一台跑在本机端口的后台（仍不出网：只有 127.0.0.1）。"""

    def __init__(self, conn, data_root, site_root, secure_cookie=False):
        from danmu_intel.publish.release import ReleaseContext

        self.conn = conn
        self.app = server.build_app(
            conn=conn,
            data_root=data_root,
            release=lambda: ReleaseContext.local(actor="admin", site_root=site_root, data_root=data_root),
            secrets=auth.AdminSecrets(password_hash=auth.hash_password(PASSWORD)),
            clock=lambda: CLOCK,
            secure_cookie=secure_cookie,
        )


@pytest.fixture
def admin(conn, data_root, site_root) -> Admin:
    return Admin(conn, data_root, site_root)


def run(admin: Admin, body) -> None:
    """把真服务跑在一个本地端口上（仍不出网：只有 127.0.0.1）。"""

    async def runner():
        client = TestClient(TestServer(admin.app))
        await client.start_server()
        try:
            await body(client)
        finally:
            await client.close()

    asyncio.run(runner())


async def get(client: TestClient, path: str, **headers) -> object:
    return await client.get(path, allow_redirects=False, headers=headers or None)


async def post(client: TestClient, path: str, data: dict[str, str] | None = None, **headers) -> object:
    return await client.post(path, data=data or {}, allow_redirects=False, headers=headers or None)


async def login(client: TestClient) -> object:
    response = await post(client, server.LOGIN_PATH, {"password": PASSWORD})
    assert response.status == 303, await response.text()
    return response


def test_non_tailnet_requests_are_refused(admin):
    """AC-7：非 tailnet 请求 403（连登录页都不给看）。"""

    async def body(client: TestClient):
        for path in (
            "/admin",
            server.LOGIN_PATH,
            "/admin/config",
            "/admin/reports/preview?match_id=1&kind=full&version=1",
        ):
            response = await get(client, path, **{"X-Forwarded-For": PUBLIC_IP})
            assert response.status == 403, path
            assert "tailnet" in await response.text()
        response = await post(client, server.LOGIN_PATH, {"password": PASSWORD}, **{"X-Forwarded-For": PUBLIC_IP})
        assert response.status == 403

    run(admin, body)


def test_tailnet_and_loopback_requests_are_allowed(admin):
    async def body(client: TestClient):
        assert (await get(client, server.LOGIN_PATH)).status == 200
        assert (await get(client, server.LOGIN_PATH, **{"X-Forwarded-For": TAILNET_IP})).status == 200

    run(admin, body)


def test_pages_require_a_login(admin):
    """FR-C8-3：没有签名 cookie，任何后台页面都不可访问（403）。"""

    async def body(client: TestClient):
        for page in pages.PAGES:
            response = await get(client, page.path)
            assert response.status == 403, page.path
            assert "需要登录" in await response.text()

    run(admin, body)


def test_login_flow_grants_access_and_logs_out(admin, conn):
    async def body(client: TestClient):
        # 口令不对：403（且不设 cookie）
        bad = await post(client, server.LOGIN_PATH, {"password": "错的"})
        assert bad.status == 403 and "Set-Cookie" not in bad.headers

        good = await login(client)
        assert good.headers["Location"] == "/admin"
        cookie = good.headers["Set-Cookie"]
        assert auth.SESSION_COOKIE in cookie and "HttpOnly" in cookie and "SameSite=Lax" in cookie
        assert "Secure" not in cookie, "tailnet 内直连 http，默认不加 Secure"

        for page in pages.PAGES:
            response = await get(client, page.path)
            assert response.status == 200, page.path
            assert "弹幕情报库后台" in await response.text()

        out = await get(client, server.LOGOUT_PATH)
        assert out.status == 303 and out.headers["Location"] == server.LOGIN_PATH
        assert "Max-Age=0" in out.headers["Set-Cookie"]

    run(admin, body)
    recorded = [entry.action for entry in audit.entries(conn)]
    assert recorded[:2] == ["admin.login_failed", "admin.login"]
    assert "admin.logout" in recorded


def test_secure_cookie_flag_is_honoured(conn, data_root, site_root):
    """经 https 反代时给 cookie 加 Secure（`--secure-cookie`）。"""
    admin = Admin(conn, data_root, site_root, secure_cookie=True)

    async def body(client: TestClient):
        response = await post(client, server.LOGIN_PATH, {"password": PASSWORD})
        assert "Secure" in response.headers["Set-Cookie"]

    run(admin, body)


def test_tampered_or_expired_cookie_is_refused(admin):
    """改过的签名、过期的会话都不认（签名是唯一凭据）。"""

    async def body(client: TestClient):
        for value in ("v1.9999999999999.deadbeef", "乱写", "v1.abc.def"):
            response = await get(client, "/admin", Cookie=f"{auth.SESSION_COOKIE}={value}")
            assert response.status == 403

    run(admin, body)


def test_login_is_rate_limited(admin):
    """口令不能拿来做暴力破解的靶子：同一 IP 1 分钟 10 次。"""

    async def body(client: TestClient):
        for _ in range(server.LOGIN_BY_IP.limit):
            assert (await post(client, server.LOGIN_PATH, {"password": "错的"})).status == 403
        blocked = await post(client, server.LOGIN_PATH, {"password": PASSWORD})
        assert blocked.status == 429
        assert "频繁" in await blocked.text()

    run(admin, body)


def test_config_change_takes_effect_immediately_and_bumps_version(admin, conn):
    """NFR-T-4：后台保存配置 → 本进程立刻生效 + 版本号递增（跨进程 ≤60 秒的硬保证靠它）。"""

    async def body(client: TestClient):
        await login(client)
        before = config_store.version(conn)

        response = await post(
            client, "/admin/config/stats", {"gray_min_hits": "7", "gray_keywords": "假赛=cheat_suspicion"}
        )
        assert response.status == 303
        location = response.headers["Location"]
        assert location.startswith("/admin/config?") and "ok=" in location
        assert "v" in location, "提示里要写明这次改成了第几版"

        assert config_store.version(conn) == before + 1
        assert load_stats_config(conn).gray_min_hits == 7

        # 页面立刻显示新值（本进程缓存已失效，不等 60 秒）
        page = await get(client, "/admin/config")
        assert 'value="7"' in await page.text()

    run(admin, body)


def test_failed_action_shows_the_reason_on_the_page(admin):
    """失败要说人话：回原页带红条，原因照实写（不静默）。"""

    async def body(client: TestClient):
        await login(client)
        response = await post(client, "/admin/config/stats", {"gray_min_hits": "很多"})
        assert response.status == 303 and "err=" in response.headers["Location"]
        assert "应该是数字" in await (await get(client, response.headers["Location"])).text()

        missing = await post(client, "/admin/matches/delete", {"match_id": "999"})
        assert "err=" in missing.headers["Location"]
        assert "未找到比赛" in await (await get(client, missing.headers["Location"])).text()

        unknown = await post(client, "/admin/nope/nope", {})
        assert unknown.status == 404

    run(admin, body)


def test_match_slice_gray_release_actions_are_audited(admin, conn):
    """所有写操作入 `audit_log`：登记、切片修正、灰信号评审、发布都留痕。"""

    async def body(client: TestClient):
        await login(client)
        assert (await post(client, "/admin/matches/add", {
            "league": "LPL", "team_a": "iG", "team_b": "LNG", "state": "live",
        })).status == 303
        assert (await post(client, "/admin/rooms/add", {
            "platform": "huya", "room_id": "660000", "url": "https://www.huya.com/660000",
        })).status == 303
        assert (await post(client, "/admin/slices/override", {
            "match_id": "1", "game_no": "1", "start_ms": "1000", "end_ms": "2000", "reason": "对齐官方",
        })).status == 303
        conn.execute(
            "INSERT INTO gray_signals(match_id, category, keyword, hit_count, distinct_users,"
            " window_count, samples_json, status, created_at) VALUES(1, 'betting', '盘口', 9, 4, 3, '[]', 'candidate', 1)"
        )
        conn.commit()
        assert (await post(client, "/admin/gray/review", {
            "signal_id": "1", "action": "escalate", "reason": "样本集中",
        })).status == 303
        publish = await post(client, "/admin/releases/publish", {"reason": "后台手动"})
        assert publish.status == 303
        assert "ok=" in publish.headers["Location"] or "err=" in publish.headers["Location"]

        # 改状态：转 ended 会尝试自动再发布（本测试是本地模式，没有 Vercel，如实说清）
        state = await post(client, "/admin/matches/state", {"match_id": "1", "state": "ended"})
        assert state.status == 303

    run(admin, body)
    recorded = {entry.action for entry in audit.entries(conn)}
    assert {"match.add", "room.add", "slice.manual", "gray.review", "admin.login"} <= recorded
    assert "release.publish" in recorded or "release.failed" in recorded


def test_report_preview_needs_login_and_renders_a_real_report(admin, ledger, data_root, conn):
    """报告预览：未登录 403；登录后给出报告页自己的 HTML（管理员完整视图）。"""
    from danmu_intel.pipeline import generate_and_publish
    from danmu_intel.report.interpreter import RuleInterpreter

    result = generate_and_publish(
        conn, ledger.match_id, kind="full", data_root=data_root, interpreter=RuleInterpreter()
    )
    query = f"?match_id={ledger.match_id}&kind=full&version={result.version}"

    async def body(client: TestClient):
        assert (await get(client, server.PREVIEW_PATH + query)).status == 403
        await login(client)
        response = await get(client, server.PREVIEW_PATH + query)
        assert response.status == 200
        assert "text/html" in response.headers["Content-Type"]
        assert "<html" in await response.text()
        missing = await get(client, server.PREVIEW_PATH + "?match_id=9999&kind=full&version=1")
        assert missing.status == 404

    run(admin, body)


def test_no_credentials_end_up_in_the_database(admin, conn):
    """NFR-S-4：跑完一整轮之后，库里搜不到口令明文与哈希（`check_no_secrets.py` 的库内那一半）。"""

    async def body(client: TestClient):
        await login(client)
        await post(client, "/admin/config/billing", {
            "order_ttl_minutes": "30", "grace_hours": "24", "polygon_xpub": ACCOUNT_XPUB,
            "solana_address": "", "api_base": "",
        })
        await post(client, "/admin/config/stats", {"gray_min_hits": "5"})

    run(admin, body)

    stored = "\n".join(
        str(row) for table in ("config", "audit_log", "config_version")
        for row in conn.execute(f"SELECT * FROM {table}").fetchall()
    )
    assert PASSWORD not in stored
    assert auth.hash_password(PASSWORD) not in stored
    assert "pbkdf2_sha256" not in stored
    assert pricing.load_billing_config(conn).polygon_xpub == ACCOUNT_XPUB, "公开的 xpub 可以进库"


def test_env_file_is_outside_the_repo():
    """凭据文件必须在仓库外（`.env` 落在数据目录，不是仓库目录）。"""
    repo = paths.repo_root().resolve()
    assert repo not in paths.env_path().resolve().parents
