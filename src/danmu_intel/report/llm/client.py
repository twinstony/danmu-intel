"""DeepSeek 客户端（OpenAI 兼容，设计 §10.3 / ADR-0003）。

只做两件事：把 `(system, user)` 发出去并拿回文本与 token 用量；按输出契约
（`{"segments": {"<段号>": "<正文>"}}`）取回段正文。三条纪律：

1. **单次调用 25 秒超时**（设计 §10.2 的 `interpretation` 阶段预算）；超时按失败处理，
   绝不无限等——快报 2 分钟上线比"等一个慢响应"重要。
2. **错误分类**，因为处理方式不同：超时（`LLMTimeout`）、连不上/HTTP 报错/响应不是 JSON
   （`LLMUnavailable` / `LLMProtocolError`）。三者都是 `LLMError` 的子类。
3. **不泄漏凭据**：异常消息里只有状态码与原因，绝不含 API key；响应正文只截断进日志式
   说明，不进异常（正文可能很长且含模型输出）。

HTTP 传输层是注入缝（`Transport` 协议，缺省 `urllib`）：测试用假传输层覆盖
正常 / 超时 / 报错 / 畸形响应四种返回值，全程不连外网（NFR-GA-4）。
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

#: 单次调用超时（秒）：设计 §10.2 给 `interpretation` 阶段的预算是 25 秒。
CALL_TIMEOUT_S = 25.0
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MAX_TOKENS = 800


class LLMError(RuntimeError):
    """LLM 调用失败（超时 / 不可达 / 响应不合契约）。消息里不含任何凭据。"""


class LLMTimeout(LLMError):
    """单次调用超时。"""


class LLMUnavailable(LLMError):
    """断网、连接失败或 HTTP 报错。"""


class LLMProtocolError(LLMError):
    """响应能拿到，但不是我们约定的形状（缺字段、非法 JSON）。"""


@dataclass(frozen=True, slots=True)
class LLMReply:
    """一次成功调用的结果。"""

    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cache_hit_tokens: int
    latency_ms: int


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
            headers=dict(headers),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                raw = response.read().decode("utf-8")
        except socket.timeout as exc:  # pragma: no cover - 真实网络路径
            raise LLMTimeout(f"LLM 调用超时（{timeout_s:.0f} 秒）") from exc
        except urllib.error.HTTPError as exc:  # pragma: no cover - 真实网络路径
            raise LLMUnavailable(f"LLM 接口返回 HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:  # pragma: no cover
            raise LLMUnavailable(f"LLM 接口不可达（{type(exc).__name__}）") from exc
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMProtocolError("LLM 响应不是 JSON") from exc


@dataclass(frozen=True, slots=True)
class DeepSeekClient:
    """DeepSeek 官方 API（OpenAI 兼容的 `/chat/completions`）。

    `transport` 与 `clock` 可注入（测试用假传输层 + 假时钟）；`api_key` 只用于请求头，
    不会出现在任何异常或日志里。
    """

    api_key: str
    model: str = "deepseek-v4-flash"
    base_url: str = DEFAULT_BASE_URL
    transport: Transport | None = None
    timeout_s: float = CALL_TIMEOUT_S
    max_tokens: int = DEFAULT_MAX_TOKENS
    clock: Callable[[], float] | None = None

    @property
    def endpoint(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"

    def complete(self, *, system: str, user: str, timeout_s: float | None = None) -> LLMReply:
        """发一次调用并解析响应。`timeout_s` 可收紧（解读阶段剩余预算），不会放宽。"""
        budget = self.timeout_s if timeout_s is None else min(self.timeout_s, timeout_s)
        if budget <= 0:
            raise LLMTimeout("解读阶段预算已用尽，本次不调用")
        payload: dict[str, object] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.3,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        clock = self.clock or time.monotonic
        started = clock()
        transport = self.transport or UrllibTransport()
        try:
            body = transport.post_json(
                self.endpoint, headers=headers, payload=payload, timeout_s=budget
            )
        except LLMError:
            raise
        except socket.timeout as exc:
            raise LLMTimeout(f"LLM 调用超时（{budget:.0f} 秒）") from exc
        except Exception as exc:  # 传输层的任何异常都不许带出凭据
            raise LLMUnavailable(f"LLM 接口不可达（{type(exc).__name__}）") from exc
        latency_ms = max(0, int((clock() - started) * 1000))
        return _parse_reply(body, model=self.model, latency_ms=latency_ms)


def _parse_reply(body: object, *, model: str, latency_ms: int) -> LLMReply:
    if not isinstance(body, dict):
        raise LLMProtocolError("LLM 响应不是 JSON 对象")
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMProtocolError("LLM 响应缺少 choices")
    try:
        text = choices[0]["message"]["content"]
    except (KeyError, TypeError, IndexError):
        raise LLMProtocolError("LLM 响应缺少 choices[0].message.content") from None
    if not isinstance(text, str) or not text.strip():
        raise LLMProtocolError("LLM 响应正文为空")
    usage = body.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    return LLMReply(
        text=text,
        model=str(body.get("model") or model),
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        cache_hit_tokens=int(usage.get("prompt_cache_hit_tokens") or 0),
        latency_ms=latency_ms,
    )


def parse_segment_text(text: str, *, segment_no: int) -> str:
    """按输出契约取回段正文：`{"segments": {"<段号>": "<正文>"}}`。

    键集必须**正好**是本次要求的段号（少一个键、多一个键、正文为空都不合格），
    这是"受约束输出"的落点：模型多写一段就没人认领，少写一段就没内容可发。
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMProtocolError("模型输出不是 JSON") from exc
    if not isinstance(payload, dict):
        raise LLMProtocolError("模型输出不是 JSON 对象")
    segments = payload.get("segments")
    if not isinstance(segments, dict):
        raise LLMProtocolError("模型输出缺少 segments 对象")
    keys: set[int] = set()
    for key in segments:
        try:
            keys.add(int(key))
        except (TypeError, ValueError):
            raise LLMProtocolError(f"模型输出的段号不是数字：{key!r}") from None
    if keys != {segment_no}:
        raise LLMProtocolError(
            f"模型输出的段号不符：期望 {segment_no}，实际 {sorted(keys)}"
        )
    value = segments[str(segment_no)]
    if not isinstance(value, str) or not value.strip():
        raise LLMProtocolError(f"第 {segment_no} 段的正文不是非空字符串")
    return value.strip()
