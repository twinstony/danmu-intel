"""Polygonscan 客户端的测试：假传输层覆盖正常 / 空历史 / 限速 / 报错，全程不连外网。"""

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

from danmu_intel.chain.polygonscan import (
    API_KEY,
    CHAIN_ID,
    DEFAULT_API_BASE,
    MAX_PAGES,
    PAGE_SIZE,
    PolygonscanClient,
    UrllibTransport,
    client_from_credentials,
)
from danmu_intel.chain.provider import ProviderError, RateLimited
from danmu_intel.chain.quota import POLYGONSCAN, QuotaLedger
from danmu_intel.chain.transfer import NATIVE_ASSET
from danmu_intel.common.credentials import CredentialError

#: 假密钥拼接构造（本文件也要能被 `tools/check_no_secrets.py` 扫过）。
FAKE_KEY = "polygonscan-" + "test" * 4

ADDRESS = "0x1234567890abcdef1234567890abcdef12345678"
OTHER = "0x000000000000000000000000000000000000dead"
USDT = "0xc2132d05d31c914a87c6611c10748aeb04b58e8f"
TS = 1_790_222_400  # 2026-09-22 12:00:00 UTC


@dataclass
class ScriptedTransport:
    """按脚本返回响应体（或抛异常），并把每次请求记下来。"""

    replies: list[Any] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def get_json(self, url: str, *, params: Mapping[str, str], timeout_s: float) -> Mapping[str, Any]:
        self.calls.append({"url": url, "params": dict(params), "timeout_s": timeout_s})
        if not self.replies:
            raise AssertionError("脚本里没有更多预设响应")
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def ok(rows: list[Mapping[str, Any]]) -> Mapping[str, Any]:
    return {"status": "1", "message": "OK", "result": rows}


def empty() -> Mapping[str, Any]:
    return {"status": "0", "message": "No transactions found", "result": []}


def rate_limited() -> Mapping[str, Any]:
    return {
        "status": "0",
        "message": "NOTOK",
        "result": "Max rate limit reached, please use API Rate Limit after some time",
    }


def native_row(
    block: int = 100,
    *,
    to: str = ADDRESS,
    value: str = "1000000000000000000",
    is_error: str = "0",
    tx: str = "0xaaa1",
    ts: int = TS,
) -> Mapping[str, Any]:
    return {
        "blockNumber": str(block),
        "timeStamp": str(ts),
        "hash": tx,
        "from": OTHER,
        "to": to,
        "value": value,
        "isError": is_error,
    }


def token_row(
    block: int = 101,
    *,
    to: str = ADDRESS,
    value: str = "25000000",
    contract: str = USDT,
    tx: str = "0xbbb2",
    ts: int = TS + 30,
) -> Mapping[str, Any]:
    return {
        "blockNumber": str(block),
        "timeStamp": str(ts),
        "hash": tx,
        "from": OTHER,
        "to": to,
        "value": value,
        "contractAddress": contract,
        "tokenSymbol": "USDT",
        "tokenDecimal": "6",
    }


def client(conn, transport, **kwargs) -> PolygonscanClient:
    return PolygonscanClient(
        api_key=FAKE_KEY, ledger=QuotaLedger(conn, POLYGONSCAN), transport=transport, **kwargs
    )


def test_request_carries_chain_id_and_key(conn):
    transport = ScriptedTransport(replies=[empty(), empty()])
    client(conn, transport).transfers(ADDRESS, from_block=42)

    first = transport.calls[0]
    assert first["url"] == DEFAULT_API_BASE
    assert first["params"]["chainid"] == str(CHAIN_ID)
    assert first["params"]["apikey"] == FAKE_KEY
    assert first["params"]["module"] == "account"
    assert first["params"]["action"] == "txlist"
    assert first["params"]["address"] == ADDRESS
    assert first["params"]["startblock"] == "42"
    assert first["params"]["sort"] == "asc"
    assert first["params"]["offset"] == str(PAGE_SIZE)
    assert transport.calls[1]["params"]["action"] == "tokentx"


def test_transfers_combine_native_and_erc20(conn):
    transport = ScriptedTransport(replies=[ok([native_row()]), ok([token_row()])])
    found = client(conn, transport).transfers(ADDRESS)

    assert [item.asset for item in found] == [NATIVE_ASSET, USDT]
    native, token = found
    assert (native.network, native.address, native.tx_ref) == ("polygon", ADDRESS, "0xaaa1")
    assert native.units == 1_000_000_000_000_000_000
    assert native.at_ms == TS * 1000
    assert native.block == 100
    assert native.memo is None
    assert token.units == 25_000_000
    assert token.block == 101


def test_only_incoming_successful_transactions_count(conn):
    transport = ScriptedTransport(
        replies=[
            ok(
                [
                    native_row(to=OTHER, tx="0xout"),
                    native_row(is_error="1", tx="0xfailed"),
                    native_row(value="0", tx="0xzero"),
                    native_row(),
                ]
            ),
            ok([token_row(to=OTHER, tx="0xout2"), token_row()]),
        ]
    )
    found = client(conn, transport).transfers(ADDRESS)
    assert [item.tx_ref for item in found] == ["0xaaa1", "0xbbb2"]


def test_upper_case_addresses_match_case_insensitively(conn):
    transport = ScriptedTransport(replies=[ok([native_row(to=ADDRESS.upper())]), empty()])
    assert len(client(conn, transport).native_transfers(ADDRESS)) == 1


def test_transfers_are_sorted_by_block_then_tx(conn):
    transport = ScriptedTransport(
        replies=[ok([native_row(block=200, tx="0xz"), native_row(block=100)]), ok([token_row(block=150)])]
    )
    found = client(conn, transport).transfers(ADDRESS)
    assert [item.block for item in found] == [100, 150, 200]


def test_full_page_triggers_a_second_page(conn):
    full_page = [native_row(block=index + 1, tx=f"0x{index:04x}") for index in range(PAGE_SIZE)]
    transport = ScriptedTransport(replies=[ok(full_page), ok([native_row(block=9999, tx="0xlast")]), empty()])
    found = client(conn, transport).native_transfers(ADDRESS)

    assert len(found) == PAGE_SIZE + 1
    assert transport.calls[1]["params"]["page"] == "2"


def test_paging_stops_at_the_page_cap(conn):
    full_page = [native_row(block=index + 1, tx=f"0x{index:04x}") for index in range(PAGE_SIZE)]
    transport = ScriptedTransport(replies=[ok(full_page)] * MAX_PAGES)
    found = client(conn, transport).native_transfers(ADDRESS)

    assert len(found) == PAGE_SIZE * MAX_PAGES
    assert len(transport.calls) == MAX_PAGES


def test_empty_history_is_not_an_error(conn):
    transport = ScriptedTransport(replies=[empty(), empty()])
    assert client(conn, transport).transfers(ADDRESS) == []
    assert QuotaLedger(conn, POLYGONSCAN).usage().day_calls == 2


def test_no_records_found_wording_is_also_empty(conn):
    transport = ScriptedTransport(replies=[{"status": "0", "message": "No records found", "result": []}, empty()])
    assert client(conn, transport).transfers(ADDRESS) == []


def test_rate_limit_is_reported_and_recorded(conn):
    transport = ScriptedTransport(replies=[rate_limited()])
    with pytest.raises(RateLimited) as exc:
        client(conn, transport).native_transfers(ADDRESS)

    assert "Max rate limit reached" in str(exc.value)
    assert FAKE_KEY not in str(exc.value)
    ledger = QuotaLedger(conn, POLYGONSCAN)
    assert ledger.usage().day_calls == 1  # 被限速的请求也照记
    assert "Max rate limit reached" in (ledger.usage().last_error or "")


def test_http_429_is_reported_and_recorded(conn):
    transport = ScriptedTransport(replies=[RateLimited("Polygonscan 限速：HTTP 429")])
    with pytest.raises(RateLimited, match="HTTP 429"):
        client(conn, transport).native_transfers(ADDRESS)
    assert QuotaLedger(conn, POLYGONSCAN).usage().day_calls == 1


def test_provider_error_is_reported_and_recorded(conn):
    transport = ScriptedTransport(replies=[{"status": "0", "message": "NOTOK", "result": "Invalid Address format"}])
    with pytest.raises(ProviderError, match="Invalid Address format"):
        client(conn, transport).native_transfers(ADDRESS)

    ledger = QuotaLedger(conn, POLYGONSCAN)
    assert ledger.usage().day_calls == 1
    assert ledger.usage().last_error == "Polygonscan 返回错误：NOTOK｜Invalid Address format"


def test_transport_failure_is_recorded(conn):
    transport = ScriptedTransport(replies=[ProviderError("Polygonscan 不可达（URLError）")])
    with pytest.raises(ProviderError, match="不可达"):
        client(conn, transport).native_transfers(ADDRESS)
    assert QuotaLedger(conn, POLYGONSCAN).usage().day_calls == 1


def test_successful_call_clears_last_error(conn):
    ledger = QuotaLedger(conn, POLYGONSCAN)
    transport = ScriptedTransport(replies=[{"status": "0", "message": "NOTOK", "result": "boom"}, ok([]), empty()])
    client = PolygonscanClient(api_key=FAKE_KEY, ledger=ledger, transport=transport)
    with pytest.raises(ProviderError):
        client.native_transfers(ADDRESS)
    client.transfers(ADDRESS)
    assert ledger.usage().last_error is None


def test_status_1_with_non_list_result_is_an_error(conn):
    transport = ScriptedTransport(replies=[{"status": "1", "message": "OK", "result": {"nope": 1}}])
    with pytest.raises(ProviderError, match="不是列表"):
        client(conn, transport).native_transfers(ADDRESS)


def test_missing_field_is_an_error_not_a_silent_skip(conn):
    transport = ScriptedTransport(replies=[ok([{"blockNumber": "1", "hash": "0xaaa1", "to": ADDRESS, "value": "5"}])])
    with pytest.raises(ProviderError, match="缺少可用字段：timeStamp"):
        client(conn, transport).native_transfers(ADDRESS)


def test_quota_is_recorded_once_per_request(conn):
    transport = ScriptedTransport(replies=[ok([]), ok([])])
    client(conn, transport).transfers(ADDRESS)
    usage = QuotaLedger(conn, POLYGONSCAN).usage()
    assert usage.day_calls == 2
    assert usage.over_threshold is False


def test_throttle_spaces_back_to_back_requests(conn):
    class Clock:
        def __init__(self) -> None:
            self.now = 0.0
            self.slept: list[float] = []

        def monotonic(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.slept.append(seconds)
            self.now += seconds

    from danmu_intel.chain.quota import RateLimiter

    clock = Clock()
    limiter = RateLimiter(2.0, clock=clock.monotonic, sleep=clock.sleep)
    transport = ScriptedTransport(replies=[ok([]), ok([])])
    client(conn, transport, throttle=limiter).transfers(ADDRESS)
    assert clock.slept == [pytest.approx(0.5)]


def test_client_from_credentials_needs_the_key(conn, tmp_path):
    with pytest.raises(CredentialError, match=API_KEY):
        client_from_credentials(conn=conn, path=tmp_path / "missing.env")


def test_client_from_credentials_uses_the_env_file(conn, tmp_path):
    env = tmp_path / ".env"
    env.write_text(f"{API_KEY}={FAKE_KEY}\n", encoding="utf-8")
    env.chmod(0o600)
    built = client_from_credentials(conn=conn, path=env, transport=ScriptedTransport(replies=[empty(), empty()]))
    assert built.transfers(ADDRESS) == []


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def test_default_transport_sends_a_get(monkeypatch):
    seen: dict[str, Any] = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["method"] = request.get_method()
        seen["timeout"] = timeout
        return FakeResponse(json.dumps(ok([])).encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    body = UrllibTransport().get_json(DEFAULT_API_BASE, params={"chainid": "137"}, timeout_s=15.0)

    assert seen["method"] == "GET"
    assert seen["timeout"] == 15.0
    assert "chainid=137" in seen["url"]
    assert body["status"] == "1"


def test_default_transport_wraps_http_429(monkeypatch):
    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError("https://x", 429, "too many", {}, io.BytesIO(b""))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(RateLimited, match="HTTP 429"):
            UrllibTransport().get_json("https://x", params={}, timeout_s=1.0)
        gc.collect()


def test_default_transport_wraps_http_error(monkeypatch):
    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError("https://x", 502, "bad gateway", {}, io.BytesIO(b""))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(ProviderError, match="HTTP 502"):
            UrllibTransport().get_json("https://x", params={}, timeout_s=1.0)
        gc.collect()


def test_default_transport_wraps_connection_error(monkeypatch):
    def fake_urlopen(request, timeout):
        raise urllib.error.URLError("no route")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(ProviderError, match="不可达（URLError）"):
        UrllibTransport().get_json("https://x", params={}, timeout_s=1.0)


def test_default_transport_wraps_timeout(monkeypatch):
    def fake_urlopen(request, timeout):
        raise socket.timeout("timed out")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(ProviderError, match="不可达（TimeoutError）"):
        UrllibTransport().get_json("https://x", params={}, timeout_s=1.0)


def test_default_transport_rejects_non_json(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: FakeResponse(b"<html>"))
    with pytest.raises(ProviderError, match="不是 JSON"):
        UrllibTransport().get_json("https://x", params={}, timeout_s=1.0)


def test_default_transport_rejects_non_object_json(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: FakeResponse(b"[1, 2]"))
    with pytest.raises(ProviderError, match="非对象 JSON"):
        UrllibTransport().get_json("https://x", params={}, timeout_s=1.0)


def test_provider_label_is_polygonscan(conn):
    assert client(conn, ScriptedTransport()).provider == POLYGONSCAN
