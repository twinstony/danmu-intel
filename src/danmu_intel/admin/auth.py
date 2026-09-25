"""后台鉴权：单管理员口令 + 短期签名 cookie + **仅 tailnet 可达**（设计 §14.2）。

三条硬规矩（需求 Q-8 / FR-C8-3 / NFR-S-3 / AC-7）：

1. **只有管理员一类角色**（需求 Q-8）：口令以 **PBKDF2 哈希**存在仓库外 `.env`
   （`ADMIN_PASSWORD_HASH`，0600），明文既不落库也不落文件；登录成功发一枚
   **短期签名 cookie**（12 小时，HttpOnly + SameSite=Lax）。
2. **口令哈希同时是会话签名密钥**：改口令 → 哈希变 → 所有旧会话当场失效（正是想要的效果），
   因此不需要再加一把「会话密钥」这种第二份秘密。
3. **非 tailnet 不可达**：进程只肯绑在 tailnet 地址（`require_tailnet_host`），每个请求再按
   对端 IP 判一次（`is_tailnet_ip`）—— 两层都拦，任一层的误配都不会把后台暴露到公网。

cookie 里只有「到期时刻 + 签名」，没有任何身份信息：签名过不了就当没登录（403）。
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from danmu_intel.common import credentials, paths

#: 口令哈希在 `.env` 里的键名（值形如 `pbkdf2_sha256$200000$<salt>$<digest>`）。
PASSWORD_HASH_KEY = "ADMIN_PASSWORD_HASH"

#: 后台会话 cookie 的名字（与会员凭据 `dm_access` 分开：两套身份不共用一枚 cookie）。
SESSION_COOKIE = "danmu_admin"

#: 登录态有效期（短期：12 小时；到期重新登录，不自动续期）。
SESSION_TTL_MS = 12 * 3600 * 1000

# 口令哈希参数（PBKDF2-HMAC-SHA256）。迭代次数写进哈希串，因此以后调大不用迁移旧哈希
# （旧口令照样验得过，新口令用新参数）。
HASH_ALGORITHM = "pbkdf2_sha256"
HASH_ITERATIONS = 200_000
SALT_BYTES = 16
SESSION_VERSION = "v1"

#: Tailscale 的地址段：IPv4 用 CGNAT `100.64/10`，IPv6 用官方 ULA `fd7a:115c:a1e0::/48`。
TAILNET_NETWORKS = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fd7a:115c:a1e0::/48"),
)

#: 本机回环：运维在机器上（或经 `tailscale serve` 本机代理）访问后台时走这里。
LOOPBACK_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("::ffff:127.0.0.0/104"),
)


class AuthError(RuntimeError):
    """后台不可用（口令未配置 / 绑到了非 tailnet 地址）。消息里不含任何口令值。"""


@dataclass(frozen=True, slots=True)
class AdminSecrets:
    """后台要用的那份秘密：口令哈希（同时充当会话签名密钥）。"""

    password_hash: str


# —— 口令 ——


def hash_password(plaintext: str, *, salt: bytes | None = None) -> str:
    """口令 → 可存进 `.env` 的哈希串（每次都用新盐，同一口令两次哈希不相同）。"""
    if not plaintext:
        raise AuthError("口令不能为空")
    material = secrets.token_bytes(SALT_BYTES) if salt is None else salt
    digest = hashlib.pbkdf2_hmac("sha256", plaintext.encode("utf-8"), material, HASH_ITERATIONS)
    return f"{HASH_ALGORITHM}${HASH_ITERATIONS}${material.hex()}${digest.hex()}"


def verify_password(plaintext: str, stored: str) -> bool:
    """口令是否与哈希匹配（常数时间比对；哈希串损坏即 False，不抛异常）。"""
    parts = stored.split("$")
    if len(parts) != 4 or parts[0] != HASH_ALGORITHM:
        return False
    try:
        iterations = int(parts[1])
        salt = bytes.fromhex(parts[2])
        expected = bytes.fromhex(parts[3])
    except ValueError:
        return False
    if iterations <= 0 or not salt or not expected:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", plaintext.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(digest, expected)


def load_secrets(*, path: Path | None = None) -> AdminSecrets:
    """读后台口令哈希（`.env` 优先、进程环境兜底）。没配就拒绝起服务，不给默认口令。"""
    value = credentials.get_secret(PASSWORD_HASH_KEY, path=path)
    if not value:
        target = path or paths.env_path()
        raise AuthError(
            f"缺少后台口令哈希：先跑 `danmu-intel admin-passwd` 写入 {target}"
            f"（0600），或设进程环境 {PASSWORD_HASH_KEY}"
        )
    if not value.startswith(f"{HASH_ALGORITHM}$"):
        raise AuthError(f"{PASSWORD_HASH_KEY} 不是 {HASH_ALGORITHM} 哈希（口令明文不许放进配置）")
    return AdminSecrets(password_hash=value)


def save_password(path: Path, plaintext: str) -> Path:
    """把口令哈希写进 `.env`（0600，保留文件里其它键；**只写哈希**，明文不落盘）。"""
    hashed = hash_password(plaintext)
    lines: list[str] = []
    if path.exists():
        credentials.check_permissions(path)
        lines = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if not line.strip().startswith(f"{PASSWORD_HASH_KEY}=")
        ]
    lines.append(f"{PASSWORD_HASH_KEY}={hashed}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, credentials.REQUIRED_MODE)
    try:
        os.write(descriptor, ("\n".join(lines) + "\n").encode("utf-8"))
    finally:
        os.close(descriptor)
    os.chmod(path, credentials.REQUIRED_MODE)
    return path


# —— 会话 cookie ——


def now_ms() -> int:
    return int(time.time() * 1000)


def _signature(secret: str, expires_ms: int) -> str:
    message = f"{SESSION_VERSION}|{expires_ms}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def session_value(secret: str, *, now: int | None = None, ttl_ms: int = SESSION_TTL_MS) -> str:
    """登录成功时发出去的 cookie 值：`v1.<到期毫秒>.<签名>`。"""
    expires_ms = (now_ms() if now is None else now) + ttl_ms
    return f"{SESSION_VERSION}.{expires_ms}.{_signature(secret, expires_ms)}"


def session_valid(secret: str, value: str | None, *, now: int | None = None) -> bool:
    """cookie 值是否是我们签发的且还没过期（签名不对/格式不对/过期都是 False）。"""
    if not value:
        return False
    parts = value.split(".")
    if len(parts) != 3 or parts[0] != SESSION_VERSION:
        return False
    try:
        expires_ms = int(parts[1])
    except ValueError:
        return False
    if not hmac.compare_digest(parts[2], _signature(secret, expires_ms)):
        return False
    return expires_ms > (now_ms() if now is None else now)


def cookie_value(value: str, *, max_age_s: int = SESSION_TTL_MS // 1000, secure: bool = False) -> str:
    """`Set-Cookie` 的值。

    默认**不带** `Secure`：后台在 tailnet 内走 http（`https://*.ts.net` 由 Funnel/serve 提供，
    自建进程拿到的是明文回环），带 Secure 会让 cookie 在 http 下直接被浏览器丢掉 ——
    需要 https 时用 `secure=True`。`HttpOnly` + `SameSite=Lax` 是常开的（设计 §14.2）。
    """
    flags = "HttpOnly; SameSite=Lax; " + ("Secure; " if secure else "")
    return f"{SESSION_COOKIE}={value}; Path=/; {flags}Max-Age={max(0, int(max_age_s))}"


def expired_cookie() -> str:
    """登出：把 cookie 置空并立刻过期。"""
    return f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"


def cookie_from_header(header: str | None) -> str | None:
    """从 `Cookie` 头里取后台会话（只认我们那一个键，不做任何解码）。"""
    if not header:
        return None
    for item in header.split(";"):
        name, _, value = item.strip().partition("=")
        if name == SESSION_COOKIE and value:
            return value.strip()
    return None


# —— tailnet ——


def is_tailnet_ip(value: str) -> bool:
    """这个对端 IP 是不是 tailnet 内的（回环也算：本机运维与 tailscale serve）。"""
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError:
        return False
    return any(address in network for network in (*TAILNET_NETWORKS, *LOOPBACK_NETWORKS))


def require_tailnet_host(host: str) -> None:
    """只肯绑在 tailnet（或回环）地址上：别的地址直接拒绝启动（NFR-S-3）。

    `0.0.0.0` 这种「绑所有网卡」的写法也拒 —— 那正是把后台暴露到公网的经典写法。
    """
    candidate = host.strip().strip("[]")
    if candidate in {"localhost", "localhost.localdomain"}:
        return
    if candidate in {"0.0.0.0", "::", "*"}:
        raise AuthError(
            f"后台不许绑 {candidate}（等于监听所有网卡）：请绑 tailscale 地址"
            "（`tailscale ip -4`）或 127.0.0.1"
        )
    if not is_tailnet_ip(candidate):
        raise AuthError(
            f"后台只监听 tailnet 接口：{candidate} 不是 tailnet / 回环地址"
            "（NFR-S-3：非 tailnet 网络不可达）"
        )


def client_ip(forwarded: str | None, peer: str | None) -> str:
    """请求的对端 IP：本机代理（`tailscale serve`）转发时以 `X-Forwarded-For` 第一跳为准。

    拿不到合法 IP 时返回空串 —— 调用方据此 **403**（默认拒绝，不默认放行）。
    """
    for candidate in ((forwarded or "").split(",")[0].strip(), (peer or "").strip()):
        if not candidate:
            continue
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            continue
        return candidate
    return ""
