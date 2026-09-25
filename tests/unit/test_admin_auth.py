"""后台鉴权（设计 §14.2 / AC-7 / NFR-S-3）：口令哈希、签名 cookie、tailnet 判定。

这些都是纯函数，所以「非 tailnet 不可达」「非管理员不可访问」可以在不起服务的情况下逐条断言。
"""

from __future__ import annotations

import os

import pytest

from danmu_intel.admin import auth
from danmu_intel.common import credentials, paths

SECRET = auth.hash_password("口令-1234")


def test_hash_password_never_contains_plaintext():
    hashed = auth.hash_password("hunter2-很强的口令")
    assert "hunter2" not in hashed
    assert hashed.startswith(f"{auth.HASH_ALGORITHM}${auth.HASH_ITERATIONS}$")
    assert len(hashed.split("$")) == 4


def test_hash_password_uses_a_fresh_salt():
    assert auth.hash_password("同一口令") != auth.hash_password("同一口令")


def test_verify_password_roundtrip():
    assert auth.verify_password("口令-1234", SECRET) is True
    assert auth.verify_password("口令-12345", SECRET) is False
    assert auth.verify_password("", SECRET) is False


@pytest.mark.parametrize(
    "broken",
    [
        "",
        "明文口令",
        "pbkdf2_sha256$200000$nothex$dead",
        "pbkdf2_sha256$0$aa$bb",
        "pbkdf2_sha256$200000$$",
        "md5$1$aa$bb",
    ],
)
def test_verify_password_rejects_broken_hashes(broken):
    assert auth.verify_password("任何口令", broken) is False


def test_hash_password_rejects_empty():
    with pytest.raises(auth.AuthError):
        auth.hash_password("")


def test_save_password_writes_hashed_value_outside_repo(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path / "danmu-intel-data"))
    target = paths.env_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    # 文件里已经有一把别的凭据：不能被口令哈希覆盖掉
    target.write_text("DEEPSEEK_API_KEY=sk-别的\n", encoding="utf-8")
    os.chmod(target, 0o600)

    auth.save_password(target, "口令我设的")

    assert os.stat(target).st_mode & 0o777 == 0o600
    text = target.read_text(encoding="utf-8")
    assert "DEEPSEEK_API_KEY=sk-别的" in text
    assert "口令我设的" not in text
    assert credentials.load_env(target)[auth.PASSWORD_HASH_KEY].startswith(auth.HASH_ALGORITHM)


def test_save_password_replaces_previous_hash(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path / "danmu-intel-data"))
    target = paths.env_path()
    auth.save_password(target, "旧口令")
    first = credentials.load_env(target)[auth.PASSWORD_HASH_KEY]
    auth.save_password(target, "新口令")
    second = credentials.load_env(target)[auth.PASSWORD_HASH_KEY]
    assert first != second
    assert auth.verify_password("新口令", second) and not auth.verify_password("旧口令", second)


def test_save_password_refuses_unsafe_existing_file(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path / "danmu-intel-data"))
    target = paths.env_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("X=1\n", encoding="utf-8")
    os.chmod(target, 0o644)
    with pytest.raises(credentials.CredentialError):
        auth.save_password(target, "口令")


def test_load_secrets_requires_configured_hash(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path / "danmu-intel-data"))
    monkeypatch.delenv(auth.PASSWORD_HASH_KEY, raising=False)
    with pytest.raises(auth.AuthError) as excinfo:
        auth.load_secrets()
    assert auth.PASSWORD_HASH_KEY in str(excinfo.value), "报错要说清缺哪个键（但不含任何值）"
    assert "admin-passwd" in str(excinfo.value)


def test_load_secrets_rejects_plaintext(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path / "danmu-intel-data"))
    monkeypatch.setenv(auth.PASSWORD_HASH_KEY, "我的明文口令")
    with pytest.raises(auth.AuthError):
        auth.load_secrets()


def test_load_secrets_reads_env_file_first(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path / "danmu-intel-data"))
    monkeypatch.setenv(auth.PASSWORD_HASH_KEY, "pbkdf2_sha256$1$aa$bb")
    target = paths.env_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"{auth.PASSWORD_HASH_KEY}={SECRET}\n", encoding="utf-8")
    os.chmod(target, 0o600)
    assert auth.load_secrets().password_hash == SECRET


# —— 会话 cookie ——


def test_session_cookie_roundtrip_and_expiry():
    now = 1_790_064_000_000
    value = auth.session_value(SECRET, now=now, ttl_ms=1000)
    assert auth.session_valid(SECRET, value, now=now + 999) is True
    assert auth.session_valid(SECRET, value, now=now + 1000) is False


def test_session_cookie_rejects_tampering():
    now = 1_790_064_000_000
    value = auth.session_value(SECRET, now=now)
    _, expires, signature = value.split(".")

    assert auth.session_valid(SECRET, f"{auth.SESSION_VERSION}.{int(expires) + 10_000_000}.{signature}", now=now) is False
    assert auth.session_valid(SECRET, f"{auth.SESSION_VERSION}.{expires}.{signature[:-2]}00", now=now) is False
    assert auth.session_valid(auth.hash_password("别的口令"), value, now=now) is False, "改口令即失效"
    assert auth.session_valid(SECRET, None) is False
    assert auth.session_valid(SECRET, "乱写") is False
    assert auth.session_valid(SECRET, "v2.123.abc") is False
    assert auth.session_valid(SECRET, "v1.abc.def") is False


def test_session_cookie_has_no_identity_and_is_hardened():
    header = auth.cookie_value(auth.session_value(SECRET), max_age_s=60)
    assert header.startswith(f"{auth.SESSION_COOKIE}=v1.")
    assert "HttpOnly" in header and "SameSite=Lax" in header
    assert "Max-Age=60" in header
    assert "Secure" not in header, "tailnet 内走 http，带 Secure 浏览器会直接丢掉这枚 cookie"
    assert "Secure" in auth.cookie_value("v1.x.y", secure=True)
    assert auth.expired_cookie().endswith("Max-Age=0")


def test_cookie_from_header():
    value = auth.session_value(SECRET)
    assert auth.cookie_from_header(f"other=1; {auth.SESSION_COOKIE}={value}; x=2") == value
    assert auth.cookie_from_header(f"{auth.SESSION_COOKIE}=") is None
    assert auth.cookie_from_header("other=1") is None
    assert auth.cookie_from_header(None) is None


# —— tailnet ——


@pytest.mark.parametrize(
    "value",
    ["100.64.0.1", "100.101.102.103", "127.0.0.1", "::1", "fd7a:115c:a1e0::1", "::ffff:127.0.0.1"],
)
def test_tailnet_addresses_are_allowed(value):
    assert auth.is_tailnet_ip(value) is True


@pytest.mark.parametrize(
    "value",
    ["8.8.8.8", "203.0.113.9", "100.63.255.255", "100.128.0.1", "2001:db8::1", "not-an-ip", ""],
)
def test_public_addresses_are_rejected(value):
    assert auth.is_tailnet_ip(value) is False


@pytest.mark.parametrize("host", ["100.101.102.103", "127.0.0.1", "localhost", "fd7a:115c:a1e0::1", "[fd7a:115c:a1e0::1]"])
def test_require_tailnet_host_accepts_tailnet_and_loopback(host):
    auth.require_tailnet_host(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "*"])
def test_require_tailnet_host_refuses_wildcard(host):
    with pytest.raises(auth.AuthError) as excinfo:
        auth.require_tailnet_host(host)
    assert "所有网卡" in str(excinfo.value)


@pytest.mark.parametrize("host", ["8.8.8.8", "192.168.1.10", "example.com"])
def test_require_tailnet_host_refuses_public(host):
    with pytest.raises(auth.AuthError) as excinfo:
        auth.require_tailnet_host(host)
    assert "tailnet" in str(excinfo.value)


def test_client_ip_prefers_forwarded_first_hop():
    assert auth.client_ip("100.64.0.9, 10.0.0.1", "127.0.0.1") == "100.64.0.9"
    assert auth.client_ip(None, "100.64.0.9") == "100.64.0.9"
    assert auth.client_ip("不是IP", "100.64.0.9") == "100.64.0.9"
    assert auth.client_ip("", "") == "", "拿不到合法 IP 就交给调用方 403"
