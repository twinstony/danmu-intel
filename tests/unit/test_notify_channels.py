"""投递通道：QQ Bot（主）+ Telegram（备）。

测试缝是**注入的假传输层**（`FakeTransport`）：断言"发出去的请求长什么样"，
因此全程不连外网、也不碰任何真实凭据（NFR-GA-4 / AC-14）。
"""

from __future__ import annotations

import json

import pytest

from danmu_intel.notify.channels import (
    QQ_DEFAULT_BASE,
    TG_DEFAULT_BASE,
    ChannelError,
    ChannelNotConfigured,
    ChannelSet,
    QQBotChannel,
    TelegramChannel,
    channels_from_credentials,
)

TOKEN_URL = f"{QQ_DEFAULT_BASE}/app/getAppAccessToken"
SEND_URL = f"{QQ_DEFAULT_BASE}/v2/users/openid-1/messages"


class FakeTransport:
    """按脚本返回响应；记下每一次请求（含 URL 与请求体）。"""

    def __init__(self, *responses, error: Exception | None = None) -> None:
        self.responses = list(responses)
        self.error = error
        self.calls: list[dict] = []

    def post_json(self, url, *, headers, payload, timeout_s):
        self.calls.append({"url": url, "headers": dict(headers), "payload": dict(payload)})
        if self.error is not None:
            raise self.error
        return self.responses.pop(0) if self.responses else {}


class Recorder:
    """假通道：只记下发了什么。"""

    def __init__(self, name: str, *, fails: bool = False) -> None:
        self.name = name
        self.fails = fails
        self.texts: list[str] = []

    def send(self, text: str) -> None:
        if self.fails:
            raise ChannelError(f"{self.name} 挂了")
        self.texts.append(text)


# —— QQ Bot ——


def test_qq_fetches_token_then_sends_with_qqbot_header():
    transport = FakeTransport({"access_token": "tok-1", "expires_in": "7200"}, {"id": "msg-1"})
    channel = QQBotChannel(
        app_id="app", app_secret="secret", openid="openid-1", transport=transport, clock=lambda: 1000.0
    )

    channel.send("【严重】磁盘将满")

    token_call, send_call = transport.calls
    assert token_call["url"] == TOKEN_URL
    assert token_call["payload"] == {"appId": "app", "clientSecret": "secret"}
    assert send_call["url"] == SEND_URL
    assert send_call["headers"]["Authorization"] == "QQBot tok-1"
    assert send_call["payload"]["content"] == "【严重】磁盘将满"
    assert send_call["payload"]["msg_type"] == 0


def test_qq_reuses_token_until_it_is_close_to_expiry():
    transport = FakeTransport(
        {"access_token": "tok-1", "expires_in": "7200"},
        {"id": "1"},
        {"id": "2"},
        {"access_token": "tok-2", "expires_in": "7200"},
        {"id": "3"},
    )
    now = [1000.0]
    channel = QQBotChannel(app_id="app", app_secret="s", openid="o", transport=transport, clock=lambda: now[0])

    channel.send("一")
    now[0] += 7000.0  # 还在有效期内（7200 - 60 的余量）
    channel.send("二")
    now[0] += 200.0  # 进到续期余量里
    channel.send("三")

    urls = [call["url"] for call in transport.calls]
    assert urls.count(TOKEN_URL) == 2
    assert transport.calls[-1]["headers"]["Authorization"] == "QQBot tok-2"


def test_qq_group_target_uses_group_path():
    transport = FakeTransport({"access_token": "t"}, {"id": "1"})
    channel = QQBotChannel(app_id="a", app_secret="s", group_openid="g-1", transport=transport)

    channel.send("群里报到")

    assert transport.calls[1]["url"] == f"{QQ_DEFAULT_BASE}/v2/groups/g-1/messages"


def test_qq_requires_a_recipient():
    with pytest.raises(ChannelError, match="缺接收方"):
        QQBotChannel(app_id="a", app_secret="s")


def test_qq_token_without_access_token_is_an_error():
    channel = QQBotChannel(
        app_id="a", app_secret="s", openid="o", transport=FakeTransport({"message": "bad appid"})
    )
    with pytest.raises(ChannelError, match="没有 access_token"):
        channel.send("x")


def test_qq_message_level_error_code_is_not_silent():
    transport = FakeTransport({"access_token": "t"}, {"code": 11244, "message": "主动消息频次受限"})
    channel = QQBotChannel(app_id="a", app_secret="s", openid="o", transport=transport)

    with pytest.raises(ChannelError, match="11244"):
        channel.send("x")


def test_qq_expires_in_garbage_falls_back_to_default_ttl():
    transport = FakeTransport({"access_token": "t", "expires_in": "n/a"}, {"id": "1"})
    channel = QQBotChannel(app_id="a", app_secret="s", openid="o", transport=transport, clock=lambda: 0.0)

    channel.send("x")

    assert channel._token_deadline > 0


# —— Telegram ——


def test_telegram_sends_text_to_chat():
    transport = FakeTransport({"ok": True})
    channel = TelegramChannel(token="bot-token", chat_id="42", transport=transport)

    channel.send("【注意】订单待补款")

    [call] = transport.calls
    assert call["url"].endswith("/botbot-token/sendMessage")
    assert call["payload"] == {"chat_id": "42", "text": "【注意】订单待补款", "disable_web_page_preview": True}


def test_telegram_ok_false_is_an_error_without_leaking_the_token():
    channel = TelegramChannel(
        token="bot-token", chat_id="42", transport=FakeTransport({"ok": False, "description": "chat not found"})
    )

    with pytest.raises(ChannelError) as excinfo:
        channel.send("x")

    assert "chat not found" in str(excinfo.value)
    assert "bot-token" not in str(excinfo.value)


def test_telegram_http_error_propagates_as_channel_error():
    channel = TelegramChannel(
        token="t", chat_id="1", transport=FakeTransport(error=ChannelError("HTTP 429｜Too Many Requests"))
    )
    with pytest.raises(ChannelError, match="HTTP 429"):
        channel.send("x")


# —— 发送策略 ——


def test_critical_goes_to_both_channels_and_records_both():
    qq, tg = Recorder("qq"), Recorder("telegram")

    assert ChannelSet(primary=qq, backup=tg).send("高危", severity="critical") == "qq+telegram"
    assert qq.texts == ["高危"] and tg.texts == ["高危"]


def test_warning_goes_to_primary_only():
    qq, tg = Recorder("qq"), Recorder("telegram")

    assert ChannelSet(primary=qq, backup=tg).send("中危", severity="warning") == "qq"
    assert tg.texts == []


def test_backup_covers_a_dead_primary_for_non_critical():
    qq, tg = Recorder("qq", fails=True), Recorder("telegram")

    assert ChannelSet(primary=qq, backup=tg).send("中危", severity="warning") == "telegram"
    assert tg.texts == ["中危"]


def test_all_channels_down_is_an_error_listing_each_failure():
    qq, tg = Recorder("qq", fails=True), Recorder("telegram", fails=True)

    with pytest.raises(ChannelError) as excinfo:
        ChannelSet(primary=qq, backup=tg).send("x", severity="critical")

    message = str(excinfo.value)
    assert "qq" in message and "telegram" in message


def test_single_channel_set_needs_no_backup():
    qq = Recorder("qq")
    assert ChannelSet(primary=qq).send("x", severity="critical") == "qq"


# —— 从凭据造通道 ——


def test_channels_from_credentials_reads_env_and_pairs_primary_with_backup():
    channels = channels_from_credentials(
        environ={
            "QQ_BOT_APP_ID": "app",
            "QQ_BOT_APP_SECRET": "secret",
            "QQ_BOT_OPENID": "openid-1",
            "TG_BOT_TOKEN": "bot-token",
            "TG_CHAT_ID": "42",
        }
    )

    assert isinstance(channels.primary, QQBotChannel) and isinstance(channels.backup, TelegramChannel)
    assert channels.primary.base_url == QQ_DEFAULT_BASE
    assert channels.backup.base_url == TG_DEFAULT_BASE


def test_channels_from_credentials_uses_telegram_as_primary_when_qq_is_incomplete():
    channels = channels_from_credentials(environ={"QQ_BOT_APP_ID": "app", "TG_BOT_TOKEN": "t", "TG_CHAT_ID": "1"})

    assert isinstance(channels.primary, TelegramChannel) and channels.backup is None


def test_channels_from_credentials_honours_base_overrides():
    channels = channels_from_credentials(
        environ={
            "QQ_BOT_APP_ID": "app",
            "QQ_BOT_APP_SECRET": "s",
            "QQ_BOT_GROUP_OPENID": "g",
            "QQ_BOT_API_BASE": "http://127.0.0.1:9000/",
            "TG_BOT_TOKEN": "t",
            "TG_CHAT_ID": "1",
            "TG_API_BASE": "http://127.0.0.1:9001/",
        }
    )

    assert channels.primary.base_url == "http://127.0.0.1:9000"
    assert channels.backup.base_url == "http://127.0.0.1:9001"


def test_channels_from_credentials_without_any_channel_raises():
    with pytest.raises(ChannelNotConfigured, match="没有可用的通知通道"):
        channels_from_credentials(environ={})


def test_channels_from_credentials_never_echoes_secrets_in_errors():
    with pytest.raises(ChannelNotConfigured) as excinfo:
        channels_from_credentials(environ={"QQ_BOT_APP_ID": "app", "QQ_BOT_APP_SECRET": "top-secret"})

    assert "top-secret" not in str(excinfo.value)


def test_urllib_transport_posts_json_via_http(monkeypatch):
    """真传输层：把 `urlopen` 换成假的，断言请求体与解析结果。"""
    from danmu_intel.notify import channels as module

    captured: dict = {}

    class FakeResponse:
        status = 200

        def read(self):
            return json.dumps({"ok": True}).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["content_type"] = request.get_header("Content-type")
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)

    body = module.UrllibTransport().post_json(
        "https://example.invalid/api", headers={"Accept": "application/json"}, payload={"a": 1}, timeout_s=3.0
    )

    assert body == {"ok": True}
    assert captured["url"] == "https://example.invalid/api"
    assert captured["body"] == {"a": 1}
    assert captured["content_type"] == "application/json"
    assert captured["timeout"] == 3.0
