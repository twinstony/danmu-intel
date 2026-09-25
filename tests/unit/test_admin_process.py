"""后台进程的边角（不起真服务）：绑定前的地址检查、起服务的调用形状、回跳链接。"""

from __future__ import annotations

import pytest
from aiohttp import web

from danmu_intel.admin import auth, server
from danmu_intel.publish.release import ReleaseContext


def test_client_ip_of_a_request_is_used_for_the_notice_links():
    target = server._back_to("config", ok="已保存", query={"match_id": "3", "evil": "1"})
    assert target.startswith("/admin/config?")
    assert "match_id=3" in target and "ok=" in target
    assert "evil" not in target, "只带回原页的筛选参数，别的参数不能当跳板"

    assert server._back_to("dashboard", query={}) == "/admin"
    assert "err=" in server._back_to("rooms", err="失败了")


def test_unknown_path_for_a_page_is_404():
    with pytest.raises(web.HTTPNotFound):
        server._page_of_path("/admin/不存在")


def test_run_refuses_to_bind_a_public_address(monkeypatch, conn, data_root, site_root):
    monkeypatch.setenv(auth.PASSWORD_HASH_KEY, auth.hash_password("口令"))
    with pytest.raises(auth.AuthError):
        server.run(
            conn=conn,
            host="203.0.113.9",
            port=8090,
            data_root=data_root,
            release=lambda: ReleaseContext.local(data_root=data_root, site_root=site_root),
        )


def test_run_binds_the_tailnet_address(monkeypatch, conn, data_root, site_root):
    """起服务的形状：绑 tailnet 地址、带会话密钥、发布上下文按需构造。"""
    monkeypatch.setenv(auth.PASSWORD_HASH_KEY, auth.hash_password("口令"))
    captured = {}
    monkeypatch.setattr(web, "run_app", lambda app, **kwargs: captured.update(kwargs, app=app))

    server.run(
        conn=conn,
        host="100.64.0.1",
        port=8091,
        data_root=data_root,
        release=lambda: ReleaseContext.local(data_root=data_root, site_root=site_root),
        secure_cookie=True,
    )

    assert captured["host"] == "100.64.0.1" and captured["port"] == 8091
    assert server.SECURE_COOKIE in captured["app"]
    assert captured["app"][server.SECURE_COOKIE] is True
