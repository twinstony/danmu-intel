"""命令行：后台口令设置（`admin-passwd`）—— 只写哈希，明文不落盘。"""

from __future__ import annotations

import os

from danmu_intel.admin import auth
from danmu_intel.cli import main
from danmu_intel.common import credentials, paths


def feed(monkeypatch, *values: str) -> None:
    """把 `getpass` 的两次输入钉死（真实现要读终端，测试不碰终端）。"""
    answers = iter(values)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))


def test_admin_passwd_writes_hash_outside_repo(monkeypatch, capsys):
    feed(monkeypatch, "我的后台口令", "我的后台口令")

    assert main(["admin-passwd"]) == 0

    target = paths.env_path()
    assert target.exists()
    assert os.stat(target).st_mode & 0o777 == 0o600
    text = target.read_text(encoding="utf-8")
    assert "我的后台口令" not in text
    stored = credentials.load_env(target)[auth.PASSWORD_HASH_KEY]
    assert auth.verify_password("我的后台口令", stored) is True
    out = capsys.readouterr().out
    assert str(target) in out and "danmu-intel admin" in out


def test_admin_passwd_rejects_mismatched_input(monkeypatch, capsys):
    feed(monkeypatch, "第一次", "第二次")

    assert main(["admin-passwd"]) == 2

    assert "两次输入不一致" in capsys.readouterr().err
    assert not paths.env_path().exists()


def test_admin_passwd_rejects_unsafe_file_mode(monkeypatch, capsys):
    target = paths.env_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("X=1\n", encoding="utf-8")
    os.chmod(target, 0o644)
    feed(monkeypatch, "口令", "口令")

    assert main(["admin-passwd"]) == 2

    assert "权限不安全" in capsys.readouterr().err


def test_admin_passwd_rejects_empty_with_error(monkeypatch, capsys):
    feed(monkeypatch, "", "")

    assert main(["admin-passwd"]) == 2

    assert "口令不能为空" in capsys.readouterr().err


# —— `admin`：起后台进程（只监听 tailnet）——


def test_admin_refuses_a_non_tailnet_host(monkeypatch, capsys):
    monkeypatch.setenv(auth.PASSWORD_HASH_KEY, auth.hash_password("口令"))

    assert main(["admin", "--host", "8.8.8.8"]) == 2

    assert "tailnet" in capsys.readouterr().err


def test_admin_refuses_a_wildcard_host(monkeypatch, capsys):
    monkeypatch.setenv(auth.PASSWORD_HASH_KEY, auth.hash_password("口令"))

    assert main(["admin", "--host", "0.0.0.0"]) == 2

    assert "所有网卡" in capsys.readouterr().err


def test_admin_requires_a_configured_password(monkeypatch, capsys):
    monkeypatch.delenv(auth.PASSWORD_HASH_KEY, raising=False)

    assert main(["admin", "--host", "100.64.0.1"]) == 2

    assert "admin-passwd" in capsys.readouterr().err


def test_admin_starts_and_stops_cleanly(monkeypatch, capsys):
    """起服务的那一行不真的监听端口：把 `server.run` 换成记账函数。"""
    monkeypatch.setenv(auth.PASSWORD_HASH_KEY, auth.hash_password("口令"))
    started = {}

    def fake_run(**kwargs):
        started.update(kwargs)
        raise KeyboardInterrupt

    from danmu_intel.admin import server

    monkeypatch.setattr(server, "run", fake_run)

    assert main(["admin", "--host", "100.64.0.1", "--port", "8099", "--no-deploy"]) == 0

    assert started["host"] == "100.64.0.1" and started["port"] == 8099
    assert started["secure_cookie"] is False
    assert "8099" in capsys.readouterr().out


def test_admin_can_use_a_secure_cookie_and_a_custom_actor(monkeypatch):
    monkeypatch.setenv(auth.PASSWORD_HASH_KEY, auth.hash_password("口令"))
    started = {}

    def fake_run(**kwargs):
        started.update(kwargs)
        raise KeyboardInterrupt

    from danmu_intel.admin import server

    monkeypatch.setattr(server, "run", fake_run)

    assert main(["admin", "--host", "127.0.0.1", "--secure-cookie", "--actor", "运维"]) == 0
    assert started["secure_cookie"] is True
