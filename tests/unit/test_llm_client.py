"""DeepSeek 客户端测试：假传输层覆盖正常 / 超时 / 报错 / 畸形响应，全程不连外网。

假密钥一律拼接构造，本文件也要能被 `tools/check_no_secrets.py` 扫过。
"""

from __future__ import annotations

import gc
import io
import json
import socket
import urllib.error
import warnings
from dataclasses import dataclass, field
from typing import Any, Mapping

import pytest

from danmu_intel.report.llm.client import (
    CALL_TIMEOUT_S,
    DEFAULT_MAX_TOKENS,
    DeepSeekClient,
    LLMError,
    LLMProtocolError,
    LLMTimeout,
    LLMUnavailable,
    UrllibTransport,
)

FAKE_KEY = "sk-" + "client" * 6


@dataclass
class FakeTransport:
    """假传输层：按脚本返回响应体，或抛异常。"""

    replies: list[object] = field(default_factory=list)
    error: Exception | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    def post_json(self, url, *, headers, payload, timeout_s):
        self.calls.append({"url": url, "headers": dict(headers), "payload": dict(payload), "timeout_s": timeout_s})
        if self.error is not None:
            raise self.error
        return self.replies.pop(0) if self.replies else _body("{}")


def _body(text: str, *, prompt_tokens=100, completion_tokens=20, cache_hit=0, model="deepseek-v4-flash"):
    return {
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "prompt_cache_hit_tokens": cache_hit,
        },
    }


def client(transport, **kwargs) -> DeepSeekClient:
    return DeepSeekClient(api_key=FAKE_KEY, transport=transport, clock=_clock(), **kwargs)


def _clock(values: list[float] | None = None):
    ticks = iter(values or [0.0, 1.5])

    def read() -> float:
        return next(ticks, 1.5)

    return read


def test_complete_sends_the_constrained_request():
    transport = FakeTransport(replies=[_body('{"segments": {"3": "队伍画像"}}')])
    reply = client(transport).complete(system="纪律", user="事实层 JSON")

    sent = transport.calls[0]
    assert sent["url"] == "https://api.deepseek.com/chat/completions"
    assert sent["headers"]["Authorization"] == f"Bearer {FAKE_KEY}"
    assert sent["headers"]["Content-Type"] == "application/json"
    assert sent["payload"]["messages"] == [
        {"role": "system", "content": "纪律"},
        {"role": "user", "content": "事实层 JSON"},
    ]
    assert sent["payload"]["response_format"] == {"type": "json_object"}
    assert sent["payload"]["max_tokens"] == DEFAULT_MAX_TOKENS
    assert sent["payload"]["stream"] is False

    assert reply.text == '{"segments": {"3": "队伍画像"}}'
    assert reply.model == "deepseek-v4-flash"
    assert (reply.prompt_tokens, reply.completion_tokens) == (100, 20)
    assert reply.latency_ms == 1500  # 假时钟：1.5 秒


def test_timeout_defaults_to_25_seconds_and_can_only_be_tightened():
    transport = FakeTransport(replies=[_body("{}"), _body("{}")])
    deepseek = client(transport)
    assert CALL_TIMEOUT_S == 25.0
    deepseek.complete(system="s", user="u")
    assert transport.calls[0]["timeout_s"] == 25.0
    deepseek.complete(system="s", user="u", timeout_s=5.0)
    assert transport.calls[1]["timeout_s"] == 5.0
    deepseek.complete(system="s", user="u", timeout_s=999.0)
    assert transport.calls[2]["timeout_s"] == 25.0  # 不放宽


def test_exhausted_budget_never_calls_out():
    transport = FakeTransport()
    with pytest.raises(LLMTimeout, match="预算已用尽"):
        client(transport).complete(system="s", user="u", timeout_s=0)
    assert transport.calls == []


def test_socket_timeout_becomes_llm_timeout():
    transport = FakeTransport(error=socket.timeout("timed out"))
    with pytest.raises(LLMTimeout, match="超时"):
        client(transport).complete(system="s", user="u")


def test_network_errors_become_unavailable():
    transport = FakeTransport(error=urllib.error.URLError("dns failure"))
    with pytest.raises(LLMUnavailable, match="不可达"):
        client(transport).complete(system="s", user="u")


def test_errors_from_the_transport_are_wrapped_without_the_key():
    transport = FakeTransport(error=ValueError(f"bad thing with {FAKE_KEY}"))
    with pytest.raises(LLMUnavailable) as excinfo:
        client(transport).complete(system="s", user="u")
    assert FAKE_KEY not in str(excinfo.value)  # 异常里不许出现凭据


def test_unavailable_is_a_llm_error():
    transport = FakeTransport(error=urllib.error.URLError("no route"))
    with pytest.raises(LLMError):
        client(transport).complete(system="s", user="u")


def test_non_json_body_is_a_protocol_error():
    transport = FakeTransport(replies=["<html>502 bad gateway</html>"])
    with pytest.raises(LLMProtocolError, match="不是 JSON"):
        client(transport).complete(system="s", user="u")


@pytest.mark.parametrize(
    "body",
    [
        [],
        {},
        {"choices": []},
        {"choices": [{}]},
        {"choices": [{"message": {}}]},
        {"choices": [{"message": {"content": "   "}}]},
    ],
)
def test_malformed_bodies_are_protocol_errors(body):
    transport = FakeTransport(replies=[body])
    with pytest.raises(LLMProtocolError):
        client(transport).complete(system="s", user="u")


def test_usage_may_be_missing_and_defaults_to_zero():
    transport = FakeTransport(
        replies=[{"model": "deepseek-v4-flash", "choices": [{"message": {"content": "解读"}}]}]
    )
    reply = client(transport).complete(system="s", user="u")
    assert (reply.prompt_tokens, reply.completion_tokens, reply.cache_hit_tokens) == (0, 0, 0)


def test_model_falls_back_to_the_requested_one():
    transport = FakeTransport(replies=[{"choices": [{"message": {"content": "解读"}}]}])
    reply = client(transport, model="deepseek-v4-pro").complete(system="s", user="u")
    assert reply.model == "deepseek-v4-pro"


def test_base_url_can_be_overridden():
    transport = FakeTransport(replies=[_body("{}")])
    client(transport, base_url="https://example.test/v1/").complete(system="s", user="u")
    assert transport.calls[0]["url"] == "https://example.test/v1/chat/completions"


def test_default_transport_posts_with_urllib(monkeypatch):
    """缺省传输层用 stdlib urllib 发 POST：不引入新依赖。"""
    seen: dict[str, Any] = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self) -> bytes:
            return json.dumps(_body("解读")).encode("utf-8")

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["method"] = request.get_method()
        seen["data"] = json.loads(request.data.decode("utf-8"))
        seen["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    reply = UrllibTransport().post_json(
        "https://api.deepseek.com/chat/completions",
        headers={"Content-Type": "application/json"},
        payload={"model": "m"},
        timeout_s=25.0,
    )
    assert seen["method"] == "POST" and seen["timeout"] == 25.0
    assert seen["data"] == {"model": "m"}
    assert reply["choices"][0]["message"]["content"] == "解读"


def test_default_transport_wraps_timeout(monkeypatch):
    def fake_urlopen(request, timeout):
        raise socket.timeout("timed out")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(LLMTimeout):
        UrllibTransport().post_json("https://x/y", headers={}, payload={}, timeout_s=1.0)


def test_default_transport_wraps_http_error(monkeypatch):
    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError("https://x/y", 429, "rate limited", {}, io.BytesIO(b""))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    # CPython 在回收 HTTPError 时会发 ResourceWarning（临时文件清理），
    # 本项目 pytest 配置把 warning 当错误，因此这里显式收口。
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(LLMUnavailable, match="HTTP 429"):
            UrllibTransport().post_json("https://x/y", headers={}, payload={}, timeout_s=1.0)
        gc.collect()


def test_default_transport_wraps_connection_error(monkeypatch):
    def fake_urlopen(request, timeout):
        raise urllib.error.URLError("no route")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(LLMUnavailable, match="不可达"):
        UrllibTransport().post_json("https://x/y", headers={}, payload={}, timeout_s=1.0)


def test_default_transport_rejects_non_json(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self) -> bytes:
            return b"not json"

    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: FakeResponse())
    with pytest.raises(LLMProtocolError, match="不是 JSON"):
        UrllibTransport().post_json("https://x/y", headers={}, payload={}, timeout_s=1.0)


def test_payload_headers_are_json_serialisable():
    transport = FakeTransport(replies=[_body("{}")])
    client(transport).complete(system="纪律", user="事实")
    headers: Mapping[str, str] = transport.calls[0]["headers"]
    assert json.dumps(dict(headers), ensure_ascii=False)
    assert json.dumps(transport.calls[0]["payload"], ensure_ascii=False)
