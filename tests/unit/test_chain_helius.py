"""Helius 客户端的测试：假 JSON-RPC 传输层，全程不连外网。

金额与 memo 的解析是纯函数（`transfers_from_transaction`），因此可以用真实的
`jsonParsed` 响应形状逐条断言，不需要任何网络。
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

from danmu_intel.chain.helius import (
    API_KEY,
    DEFAULT_RPC_BASE,
    MAX_PAGES,
    PAGE_SIZE,
    HeliusClient,
    UrllibTransport,
    client_from_credentials,
    memo_of,
    transfers_from_transaction,
)
from danmu_intel.chain.provider import ProviderError, RateLimited
from danmu_intel.chain.quota import HELIUS, QuotaLedger
from danmu_intel.chain.transfer import NATIVE_ASSET

FAKE_KEY = "helius-" + "test" * 4
WALLET = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"
OTHER = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TS = 1_790_222_400


@dataclass
class ScriptedTransport:
    replies: list[Any] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def post_rpc(self, url: str, *, payload: Mapping[str, Any], timeout_s: float) -> Mapping[str, Any]:
        self.calls.append({"url": url, "payload": dict(payload), "timeout_s": timeout_s})
        if not self.replies:
            raise AssertionError("脚本里没有更多预设响应")
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def rpc(result: Any) -> Mapping[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "result": result}


def rpc_error(text: str) -> Mapping[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": text}}


def signature(sig: str, *, slot: int = 100, err: Any = None) -> Mapping[str, Any]:
    return {"signature": sig, "slot": slot, "blockTime": TS, "err": err, "memo": None}


def tx_result(
    *,
    sig: str = "sig-1",
    slot: int = 100,
    block_time: int = TS,
    pre: list[int] | None = None,
    post: list[int] | None = None,
    account_keys: list[Any] | None = None,
    memo: str | None = None,
    pre_token: list[Mapping[str, Any]] | None = None,
    post_token: list[Mapping[str, Any]] | None = None,
) -> Mapping[str, Any]:
    keys = account_keys if account_keys is not None else [{"pubkey": WALLET}, {"pubkey": OTHER}]
    instructions: list[Mapping[str, Any]] = []
    if memo is not None:
        instructions.append({"program": "spl-memo", "programId": "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr", "parsed": memo})
    return {
        "slot": slot,
        "blockTime": block_time,
        "meta": {
            "err": None,
            "preBalances": [1000, 500] if pre is None else pre,
            "postBalances": [1500, 500] if post is None else post,
            "preTokenBalances": list(pre_token or []),
            "postTokenBalances": list(post_token or []),
            "innerInstructions": [],
        },
        "transaction": {"signatures": [sig], "message": {"accountKeys": keys, "instructions": instructions}},
    }


def token_balance(index: int, amount: str, *, mint: str = USDC, owner: str = WALLET) -> Mapping[str, Any]:
    return {"accountIndex": index, "mint": mint, "owner": owner, "uiTokenAmount": {"amount": amount, "decimals": 6}}


def client(conn, transport, **kwargs) -> HeliusClient:
    return HeliusClient(api_key=FAKE_KEY, ledger=QuotaLedger(conn, HELIUS), transport=transport, **kwargs)


def test_endpoint_carries_the_api_key(conn):
    built = HeliusClient(api_key=FAKE_KEY, ledger=QuotaLedger(conn, HELIUS))
    assert built.endpoint == f"{DEFAULT_RPC_BASE}/?api-key={FAKE_KEY}"


def test_native_income_is_detected(conn):
    transport = ScriptedTransport(replies=[rpc([signature("sig-1")]), rpc(tx_result())])
    found = client(conn, transport).transfers(WALLET)

    assert len(found) == 1
    [transfer] = found
    assert transfer.network == "solana"
    assert (transfer.address, transfer.tx_ref, transfer.asset) == (WALLET, "sig-1", NATIVE_ASSET)
    assert (transfer.units, transfer.at_ms, transfer.block) == (500, TS * 1000, 100)
    assert transport.calls[0]["payload"]["method"] == "getSignaturesForAddress"
    assert transport.calls[0]["payload"]["params"] == [WALLET, {"limit": PAGE_SIZE}]
    assert transport.calls[1]["payload"]["method"] == "getTransaction"
    assert transport.calls[1]["payload"]["params"][1]["encoding"] == "jsonParsed"


def test_memo_is_carried_on_every_transfer(conn):
    transport = ScriptedTransport(
        replies=[
            rpc([signature("sig-1")]),
            rpc(
                tx_result(
                    memo="mem-7f3a",
                    post_token=[token_balance(2, "25000000")],
                )
            ),
        ]
    )
    found = client(conn, transport).transfers(WALLET)
    assert {item.memo for item in found} == {"mem-7f3a"}
    assert {item.asset for item in found} == {NATIVE_ASSET, USDC}


def test_cursor_is_passed_as_until_and_only_new_signatures_come_back(conn):
    transport = ScriptedTransport(replies=[rpc([signature("sig-2")]), rpc(tx_result(sig="sig-2"))])
    client(conn, transport).transfers(WALLET, until="sig-1")
    assert transport.calls[0]["payload"]["params"][1]["until"] == "sig-1"


def test_failed_transactions_are_skipped(conn):
    transport = ScriptedTransport(
        replies=[rpc([signature("sig-new"), signature("sig-failed", err={"InstructionError": []})]),
                 rpc(tx_result(sig="sig-new"))]
    )
    found = client(conn, transport).transfers(WALLET)
    assert [item.tx_ref for item in found] == ["sig-new"]
    assert len(transport.calls) == 2


def test_signatures_are_returned_oldest_first(conn):
    transport = ScriptedTransport(
        replies=[rpc([signature("sig-3"), signature("sig-2"), signature("sig-1")])]
    )
    rows = client(conn, transport).signatures(WALLET)
    assert [row["signature"] for row in rows] == ["sig-1", "sig-2", "sig-3"]


def test_signature_paging_follows_full_pages(conn):
    full_page = [signature(f"sig-{index:04d}") for index in range(PAGE_SIZE)]
    transport = ScriptedTransport(replies=[rpc(full_page), rpc([signature("sig-last")])])
    client(conn, transport).signatures(WALLET)

    assert transport.calls[1]["payload"]["params"][1]["until"] == "sig-0999"
    assert len(transport.calls) == 2


def test_signature_paging_stops_at_the_cap(conn):
    full_page = [signature(f"sig-{index:04d}") for index in range(PAGE_SIZE)]
    transport = ScriptedTransport(replies=[rpc(full_page)] * MAX_PAGES)
    found = client(conn, transport).signatures(WALLET)
    assert len(found) == PAGE_SIZE * MAX_PAGES
    assert len(transport.calls) == MAX_PAGES


def test_empty_history_stops_immediately(conn):
    transport = ScriptedTransport(replies=[rpc([])])
    assert client(conn, transport).transfers(WALLET) == []
    assert len(transport.calls) == 1


def test_quota_counts_one_credit_per_rpc_call(conn):
    transport = ScriptedTransport(replies=[rpc([signature("sig-1")]), rpc(tx_result())])
    client(conn, transport).transfers(WALLET)

    usage = QuotaLedger(conn, HELIUS).usage()
    assert usage.used == 2
    assert usage.month_credits == 2.0
    assert usage.last_error is None


def test_http_429_is_reported_and_recorded(conn):
    transport = ScriptedTransport(replies=[RateLimited("Helius 限速：HTTP 429")])
    with pytest.raises(RateLimited, match="HTTP 429"):
        client(conn, transport).signatures(WALLET)
    assert QuotaLedger(conn, HELIUS).usage().used == 1


def test_rpc_error_is_recorded(conn):
    transport = ScriptedTransport(replies=[rpc_error("Invalid param: wrong encoding")])
    with pytest.raises(ProviderError, match="wrong encoding"):
        client(conn, transport).signatures(WALLET)

    usage = QuotaLedger(conn, HELIUS).usage()
    assert usage.used == 1
    assert "wrong encoding" in (usage.last_error or "")


def test_rate_wording_in_rpc_error_becomes_rate_limited(conn):
    transport = ScriptedTransport(replies=[rpc_error("429 Too Many Requests")])
    with pytest.raises(RateLimited, match="429"):
        client(conn, transport).signatures(WALLET)


def test_response_without_result_is_an_error(conn):
    transport = ScriptedTransport(replies=[{"jsonrpc": "2.0", "id": 1}])
    with pytest.raises(ProviderError, match="没有 result"):
        client(conn, transport).signatures(WALLET)


def test_missing_signature_list_is_an_error(conn):
    transport = ScriptedTransport(replies=[rpc({"not": "a list"})])
    with pytest.raises(ProviderError, match="不是列表"):
        client(conn, transport).signatures(WALLET)


def test_dropped_transaction_is_an_error_not_a_silent_zero(conn):
    transport = ScriptedTransport(replies=[rpc([signature("sig-1")]), rpc(None)])
    with pytest.raises(ProviderError, match="找不到交易"):
        client(conn, transport).transfers(WALLET)


def test_transaction_result_must_be_an_object(conn):
    transport = ScriptedTransport(replies=[rpc("nope")])
    with pytest.raises(ProviderError, match="不是对象"):
        client(conn, transport).transaction("sig-1", address=WALLET)


def test_outgoing_only_transaction_yields_nothing():
    assert transfers_from_transaction(tx_result(pre=[1500], post=[1000]), address=WALLET) == []


def test_missing_meta_or_transaction_yields_nothing():
    assert transfers_from_transaction({"slot": 1}, address=WALLET) == []
    assert transfers_from_transaction({"meta": {}, "transaction": None}, address=WALLET) == []


def test_transaction_without_signature_is_an_error():
    payload = tx_result()
    payload["transaction"]["signatures"] = []
    with pytest.raises(ProviderError, match="缺少签名"):
        transfers_from_transaction(payload, address=WALLET)


def test_address_not_in_account_keys_yields_no_native_income():
    payload = tx_result(account_keys=[{"pubkey": OTHER}])
    assert transfers_from_transaction(payload, address=WALLET) == []


def test_plain_string_account_keys_are_understood():
    payload = tx_result(account_keys=[WALLET, OTHER])
    assert [item.units for item in transfers_from_transaction(payload, address=WALLET)] == [500]


def test_missing_balances_are_tolerated():
    payload = tx_result()
    payload["meta"].pop("preBalances")
    payload["meta"].pop("postBalances")
    assert transfers_from_transaction(payload, address=WALLET) == []


def test_corrupt_balance_fields_yield_no_income():
    payload = tx_result(pre=["x"], post=["y"], account_keys="nope")
    assert transfers_from_transaction(payload, address=WALLET) == []


def test_token_account_balance_change_is_income():
    payload = tx_result(
        pre_token=[token_balance(2, "10000000")], post_token=[token_balance(2, "35000000")]
    )
    found = [item for item in transfers_from_transaction(payload, address=WALLET) if item.asset == USDC]
    assert [item.units for item in found] == [25_000_000]


def test_token_accounts_of_other_owners_are_ignored():
    payload = tx_result(
        post_token=[token_balance(2, "25000000", owner=OTHER)], pre=[1000], post=[1000]
    )
    assert transfers_from_transaction(payload, address=WALLET) == []


@pytest.mark.parametrize(
    "row",
    [
        {"accountIndex": "x", "mint": USDC, "owner": WALLET, "uiTokenAmount": {"amount": "1"}},
        {"accountIndex": 2, "mint": None, "owner": WALLET, "uiTokenAmount": {"amount": "1"}},
        {"accountIndex": 3, "mint": USDC, "owner": WALLET, "uiTokenAmount": None},
        {"accountIndex": 4, "mint": USDC, "owner": WALLET, "uiTokenAmount": {"amount": "bad"}},
    ],
)
def test_unparseable_own_token_snapshot_is_an_error(row):
    """自己名下的代币账户快照解不开就报错——当 0 处理会静默漏掉一笔入账。"""
    payload = tx_result(pre=[1000], post=[1000], post_token=[row])
    with pytest.raises(ProviderError, match="代币余额快照不可解析"):
        transfers_from_transaction(payload, address=WALLET)


def test_non_mapping_token_rows_are_skipped():
    payload = tx_result(pre=[1000], post=[1000], post_token=["not-a-row"])
    assert transfers_from_transaction(payload, address=WALLET) == []


def test_zero_token_delta_yields_nothing():
    payload = tx_result(
        pre=[1000], post=[1000], pre_token=[token_balance(2, "5")], post_token=[token_balance(2, "5")]
    )
    assert transfers_from_transaction(payload, address=WALLET) == []


def test_memo_from_program_id_and_inner_instructions():
    payload = tx_result()
    payload["meta"]["innerInstructions"] = [
        {
            "index": 0,
            "instructions": [
                {"programId": "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr", "parsed": "order-9"},
                {"programId": "x", "parsed": "ignored"},
            ],
        }
    ]
    assert memo_of(payload["transaction"], payload["meta"]) == "order-9"


def test_memo_falls_back_to_raw_data():
    payload = tx_result()
    payload["transaction"]["message"]["instructions"] = [
        {"program": "spl-memo", "data": "b3JkZXItMQ=="}
    ]
    assert memo_of(payload["transaction"], payload["meta"]) == "b3JkZXItMQ=="


def test_no_memo_is_none():
    payload = tx_result()
    assert memo_of(payload["transaction"], payload["meta"]) is None


def test_client_from_credentials_needs_the_key(conn, tmp_path):
    from danmu_intel.common.credentials import CredentialError

    with pytest.raises(CredentialError, match=API_KEY):
        client_from_credentials(conn=conn, path=tmp_path / "missing.env")


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def test_default_transport_posts_json(monkeypatch):
    seen: dict[str, Any] = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["method"] = request.get_method()
        seen["data"] = json.loads(request.data.decode("utf-8"))
        seen["timeout"] = timeout
        return FakeResponse(json.dumps(rpc([])).encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    body = UrllibTransport().post_rpc(DEFAULT_RPC_BASE, payload={"method": "getVersion"}, timeout_s=15.0)

    assert seen["method"] == "POST"
    assert seen["timeout"] == 15.0
    assert seen["data"] == {"method": "getVersion"}
    assert body["result"] == []


def test_default_transport_wraps_http_429(monkeypatch):
    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError("https://x", 429, "too many", {}, io.BytesIO(b""))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(RateLimited, match="HTTP 429"):
            UrllibTransport().post_rpc("https://x", payload={}, timeout_s=1.0)
        gc.collect()


def test_default_transport_wraps_http_error(monkeypatch):
    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError("https://x", 500, "boom", {}, io.BytesIO(b""))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(ProviderError, match="HTTP 500"):
            UrllibTransport().post_rpc("https://x", payload={}, timeout_s=1.0)
        gc.collect()


def test_default_transport_wraps_connection_error(monkeypatch):
    def fake_urlopen(request, timeout):
        raise urllib.error.URLError("no route")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(ProviderError, match="不可达（URLError）"):
        UrllibTransport().post_rpc("https://x", payload={}, timeout_s=1.0)


def test_default_transport_wraps_timeout(monkeypatch):
    def fake_urlopen(request, timeout):
        raise socket.timeout("timed out")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(ProviderError, match="不可达（TimeoutError）"):
        UrllibTransport().post_rpc("https://x", payload={}, timeout_s=1.0)


def test_default_transport_rejects_non_json(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: FakeResponse(b"<html>"))
    with pytest.raises(ProviderError, match="不是 JSON"):
        UrllibTransport().post_rpc("https://x", payload={}, timeout_s=1.0)


def test_default_transport_rejects_non_object_json(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: FakeResponse(b"[]"))
    with pytest.raises(ProviderError, match="非对象 JSON"):
        UrllibTransport().post_rpc("https://x", payload={}, timeout_s=1.0)


def test_provider_label_is_helius(conn):
    assert client(conn, ScriptedTransport()).provider == HELIUS
