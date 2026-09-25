"""投递通道：**QQ Bot（主）+ Telegram（备）**（ADR-0010 / 设计 §15）。

一条通知出去只有这两个出口，凭据一律只从仓库外 `.env`（0600）读：

| 通道 | 凭据键 | 说明 |
|---|---|---|
| QQ Bot（主） | `QQ_BOT_APP_ID` / `QQ_BOT_APP_SECRET` / `QQ_BOT_OPENID`（单聊）或 `QQ_BOT_GROUP_OPENID`（群） | 官方 Bot OpenAPI：先换 `access_token`，再发消息 |
| Telegram（备） | `TG_BOT_TOKEN` / `TG_CHAT_ID` | Bot API `sendMessage` |
| 可选 | `QQ_BOT_API_BASE` / `TG_API_BASE` | 指向自建代理时用 |

**高危主备都发、其余只发主通道**（设计 §15 的通道列：高=match「QQ Bot + TG」，中=「QQ Bot」）。
主通道全挂时备通道兜底 —— 否则"备"对中等级别就毫无意义。哪条通道成功由
`ChannelSet.send` 返回，投递方据此记 `notifications.channel`。

**不静默失败**：通道报错一律抛 `ChannelError`（HTTP 状态码 + 响应片段，**绝不含凭据**），
由投递方按重试与时效闸门处理；没有配任何通道时 `channels_from_credentials` 直接抛错，
而不是"发了个寂寞"。

HTTP 走 stdlib `urllib`（不新增依赖），`transport` 可注入 —— 测试全程不连外网（AC-14）。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

from danmu_intel.common import credentials

QQ_DEFAULT_BASE = "https://api.bot.qq.com"
TG_DEFAULT_BASE = "https://api.telegram.org"
QQ_TOKEN_PATH = "/app/getAppAccessToken"
QQ_USER_PATH = "/v2/users/{openid}/messages"
QQ_GROUP_PATH = "/v2/groups/{group_openid}/messages"
#: 主动消息的 msg_seq：同一 openid 的被动回复靠它去重。主动推送用通知 id 之类
#: 单调递增值即可（QQ 只要求同一会话内不重复）。
DEFAULT_TIMEOUT_S = 10.0
TOKEN_REFRESH_MARGIN_S = 60.0


class ChannelError(RuntimeError):
    """投递失败（网络 / HTTP 非 2xx / 响应不合契约）。消息里不含任何凭据。"""


class ChannelNotConfigured(ChannelError):
    """一个通道都没配（`.env` 里缺键）—— 宁可报错，也不假装送达。"""


class Transport(Protocol):
    """HTTP 传输层注入缝：发一个 JSON POST，拿回解析后的 JSON。"""

    def post_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout_s: float,
    ) -> dict[str, Any]: ...


class UrllibTransport:
    """缺省传输层（stdlib `urllib`：一次 POST，不引入新依赖）。"""

    def post_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout_s: float,
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={**headers, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:  # pragma: no cover - 真实网络路径
            detail = exc.read().decode("utf-8", errors="replace")[:200]
            raise ChannelError(f"HTTP {exc.code}｜{detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:  # pragma: no cover - 真实网络
            raise ChannelError(f"连不上（{type(exc).__name__}）") from None
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            raise ChannelError(f"响应不是 JSON（{len(raw)} 字节）") from None
        if not isinstance(body, dict):
            raise ChannelError("响应不是 JSON 对象")
        return body


class Channel(Protocol):
    name: str

    def send(self, text: str) -> None: ...


@dataclass
class QQBotChannel:
    """QQ 官方 Bot OpenAPI（主通道）。

    两步：`/app/getAppAccessToken` 换 `access_token`（按 `expires_in` 缓存，提前
    60 秒续），再 `POST /v2/users/{openid}/messages`（群同理走 `/v2/groups/…`），
    授权头是 `Authorization: QQBot <access_token>`。文档：
    <https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/api-call-guide.html>
    """

    app_id: str
    app_secret: str
    openid: str | None = None
    group_openid: str | None = None
    base_url: str = QQ_DEFAULT_BASE
    transport: Transport | None = None
    timeout_s: float = DEFAULT_TIMEOUT_S
    clock: Callable[[], float] = time.monotonic
    name: str = "qq"
    _token: str | None = field(default=None, init=False, repr=False)
    _token_deadline: float = field(default=0.0, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.openid and not self.group_openid:
            raise ChannelError("QQ Bot 通道缺接收方：请配 QQ_BOT_OPENID（单聊）或 QQ_BOT_GROUP_OPENID（群）")
        self.base_url = self.base_url.rstrip("/")

    @property
    def target_path(self) -> str:
        """接收方的消息路径（单聊优先，其次群）。"""
        if self.openid:
            return QQ_USER_PATH.format(openid=urllib.parse.quote(self.openid))
        return QQ_GROUP_PATH.format(group_openid=urllib.parse.quote(str(self.group_openid)))

    def _transport(self) -> Transport:
        return self.transport or UrllibTransport()

    def access_token(self) -> str:
        """取（必要时续）`access_token`。缓存只活在本进程内，不入库。"""
        if self._token and self.clock() < self._token_deadline:
            return self._token
        payload = self._transport().post_json(
            f"{self.base_url}{QQ_TOKEN_PATH}",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            payload={"appId": self.app_id, "clientSecret": self.app_secret},
            timeout_s=self.timeout_s,
        )
        token = payload.get("access_token")
        if not token:
            raise ChannelError("QQ Bot 换 access_token 失败：响应里没有 access_token")
        expires_in = _as_float(payload.get("expires_in"), default=7200.0)
        self._token = str(token)
        self._token_deadline = self.clock() + max(expires_in - TOKEN_REFRESH_MARGIN_S, 0.0)
        return self._token

    def send(self, text: str) -> None:
        response = self._transport().post_json(
            f"{self.base_url}{self.target_path}",
            headers={"Authorization": f"QQBot {self.access_token()}", "Accept": "application/json"},
            payload={"content": text, "msg_type": 0, "msg_seq": self._next_seq()},
            timeout_s=self.timeout_s,
        )
        code = response.get("code")
        if code not in (None, 0):
            raise ChannelError(f"QQ Bot 发消息被拒：code={code}｜{response.get('message') or ''}")

    def _next_seq(self) -> int:
        """同一接收方内不重复的序号（毫秒时间戳对主动消息足够）。"""
        return int(self.clock() * 1000) % 2_147_483_647


@dataclass
class TelegramChannel:
    """Telegram Bot API（备通道）：`POST {base}/bot<token>/sendMessage`。"""

    token: str
    chat_id: str
    base_url: str = TG_DEFAULT_BASE
    transport: Transport | None = None
    timeout_s: float = DEFAULT_TIMEOUT_S
    name: str = "telegram"

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")

    def send(self, text: str) -> None:
        transport = self.transport or UrllibTransport()
        # 注意：URL 里带 token，因此异常消息只说"sendMessage 失败"，绝不回显 URL。
        response = transport.post_json(
            f"{self.base_url}/bot{self.token}/sendMessage",
            headers={"Accept": "application/json"},
            payload={"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True},
            timeout_s=self.timeout_s,
        )
        if not response.get("ok"):
            raise ChannelError(f"Telegram sendMessage 失败：{response.get('description') or 'ok=false'}")


@dataclass
class ChannelSet:
    """主通道 + 备通道的发送策略（`both_for` 里的级别主备都发）。"""

    primary: Channel
    backup: Channel | None = None
    both_for: tuple[str, ...] = ("critical",)

    def send(self, text: str, *, severity: str) -> str:
        """按级别投递，返回走通的通道名（多个用 `+` 连接）。全部失败才抛错。"""
        targets: list[Channel] = [self.primary]
        if self.backup is not None and severity in self.both_for:
            targets.append(self.backup)
        sent, errors = _attempt(targets, text)
        if not sent and self.backup is not None and self.backup not in targets:
            sent, fallback_errors = _attempt([self.backup], text)
            errors.extend(fallback_errors)
        if not sent:
            raise ChannelError(f"全部通道失败：{'；'.join(errors)}")
        return "+".join(sent)


def _attempt(channels: list[Channel], text: str) -> tuple[list[str], list[str]]:
    sent: list[str] = []
    errors: list[str] = []
    for channel in channels:
        try:
            channel.send(text)
        except ChannelError as exc:
            errors.append(f"{channel.name}：{exc}")
        else:
            sent.append(channel.name)
    return sent, errors


def channels_from_credentials(
    *,
    path=None,
    environ: Mapping[str, str] | None = None,
    transport: Transport | None = None,
) -> ChannelSet:
    """按仓库外 `.env`（0600）+ 进程环境造通道集合；一个都没配即抛错。"""
    values = _secrets(path=path, environ=environ)
    qq = _qq_from(values, transport)
    telegram = _telegram_from(values, transport)
    if qq is None and telegram is None:
        raise ChannelNotConfigured(
            "没有可用的通知通道：请在仓库外 .env（chmod 600）配 QQ_BOT_APP_ID / "
            "QQ_BOT_APP_SECRET / QQ_BOT_OPENID（或 QQ_BOT_GROUP_OPENID），"
            "或 TG_BOT_TOKEN / TG_CHAT_ID"
        )
    if qq is None:  # 只有备通道可用：它就成了主通道，不再谈"备"
        assert telegram is not None
        return ChannelSet(primary=telegram)
    return ChannelSet(primary=qq, backup=telegram)


def _secrets(*, path=None, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """一次读全 `.env`（文件优先，进程环境兜底），避免逐键重复解析与重复查权限。"""
    environment = dict(os.environ if environ is None else environ)
    environment.update(credentials.load_env(path))
    return environment


def _qq_from(values: Mapping[str, str], transport: Transport | None) -> QQBotChannel | None:
    app_id = values.get("QQ_BOT_APP_ID")
    app_secret = values.get("QQ_BOT_APP_SECRET")
    openid = values.get("QQ_BOT_OPENID")
    group_openid = values.get("QQ_BOT_GROUP_OPENID")
    if not app_id or not app_secret or not (openid or group_openid):
        return None
    return QQBotChannel(
        app_id=app_id,
        app_secret=app_secret,
        openid=openid or None,
        group_openid=group_openid or None,
        base_url=values.get("QQ_BOT_API_BASE") or QQ_DEFAULT_BASE,
        transport=transport,
    )


def _telegram_from(values: Mapping[str, str], transport: Transport | None) -> TelegramChannel | None:
    token = values.get("TG_BOT_TOKEN")
    chat_id = values.get("TG_CHAT_ID")
    if not token or not chat_id:
        return None
    return TelegramChannel(
        token=token,
        chat_id=chat_id,
        base_url=values.get("TG_API_BASE") or TG_DEFAULT_BASE,
        transport=transport,
    )


def _as_float(value: object, *, default: float) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
