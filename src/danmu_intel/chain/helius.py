"""Helius 客户端（ADR-0005：Solana 走 Helius）。

两步取一笔入账，与 ADR-0005 记的额度账（约 2 次调用/分钟）对得上：

1. `getSignaturesForAddress(address, limit, until=游标)` —— 拿**新签名**（新→旧，
   传 `until` 就只返回它之后的新记录），一次 1 credit；
2. `getTransaction(signature, encoding=jsonParsed)` —— 拿这笔交易的**到账金额与 memo**，
   一次 1 credit。

金额怎么算（不用自建代币账户推导，全部靠节点回给我们的余额快照）：

| 资产 | 判据 |
|---|---|
| SOL | 本地址在 `meta.postBalances - meta.preBalances` 上为正 |
| SPL 代币 | 属于本地址（`owner`）的代币账户 `postTokenBalances - preTokenBalances` 为正 |

`memo` 由 `spl-memo` 程序的指令正文取出——它是每单唯一标识（ADR-0004 的 Solana 方案）。

纪律与 Polygonscan 客户端一致：每次调用都记 `quota_usage`、撞限速抛 `RateLimited`、
异常消息里绝不含 `api-key`、传输层可注入（断网可跑）。
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Any, Mapping, Protocol

from danmu_intel.chain.provider import ProviderError, RateLimited
from danmu_intel.chain.quota import HELIUS, QuotaLedger, RateLimiter
from danmu_intel.chain.transfer import NATIVE_ASSET, SOLANA, Transfer

DEFAULT_RPC_BASE = "https://mainnet.helius-rpc.com"

#: 凭据键名（只入仓库外 `.env`，永不入 git —— AC-12 / NFR-S-4）。
API_KEY = "HELIUS_API_KEY"

CALL_TIMEOUT_S = 15.0

#: 一次最多取多少条签名（`getSignaturesForAddress` 的上限就是 1000）。
PAGE_SIZE = 1000

#: 翻页上限（补扫全历史时最多翻这么多页）。
MAX_PAGES = 10

#: `spl-memo` 程序（每单唯一标识就写在它的指令正文里）。
MEMO_PROGRAM = "spl-memo"


class Transport(Protocol):
    """HTTP 传输层注入缝：一次 JSON-RPC POST，拿回 JSON-RPC 响应。"""

    def post_rpc(
        self, url: str, *, payload: Mapping[str, Any], timeout_s: float
    ) -> Mapping[str, Any]: ...


class UrllibTransport:
    """缺省传输层（stdlib `urllib`：一次 POST，不引入新依赖）。

    `api-key` 在 URL 查询串里，所以异常消息里**只出现状态码**，绝不带 URL。
    """

    def post_rpc(
        self, url: str, *, payload: Mapping[str, Any], timeout_s: float
    ) -> Mapping[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:  # pragma: no cover - 真实网络路径
            if exc.code == 429:
                raise RateLimited("Helius 限速：HTTP 429") from None
            raise ProviderError(f"Helius 请求失败：HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:  # pragma: no cover
            raise ProviderError(f"Helius 不可达（{type(exc).__name__}）") from None
        try:
            data = json.loads(body)
        except json.JSONDecodeError:  # pragma: no cover - 真实网络路径
            raise ProviderError(f"Helius 返回的不是 JSON（{len(body)} 字节）") from None
        if not isinstance(data, Mapping):  # pragma: no cover - 真实网络路径
            raise ProviderError("Helius 返回了非对象 JSON")
        return data


class HeliusClient:
    """按地址查 Solana 入账（原生 SOL + SPL 代币），带 memo。"""

    def __init__(
        self,
        *,
        api_key: str,
        ledger: QuotaLedger,
        transport: Transport | None = None,
        throttle: RateLimiter | None = None,
        rpc_base: str = DEFAULT_RPC_BASE,
        timeout_s: float = CALL_TIMEOUT_S,
    ) -> None:
        self._api_key = api_key
        self._ledger = ledger
        self._transport = transport or UrllibTransport()
        self._throttle = throttle or RateLimiter(ledger.limit.rate_per_s)
        self._rpc_base = rpc_base.rstrip("/")
        self._timeout_s = timeout_s

    @property
    def provider(self) -> str:
        return HELIUS

    @property
    def endpoint(self) -> str:
        return f"{self._rpc_base}/?api-key={self._api_key}"

    @property
    def ledger(self) -> QuotaLedger:
        return self._ledger

    def transfers(self, address: str, *, until: str | None = None) -> list[Transfer]:
        """从游标 `until`（不含）之后的所有入账，按链上时间升序。"""
        found: list[Transfer] = []
        for item in self.signatures(address, until=until):
            found.extend(self.transaction(item["signature"], address=address))
        found.sort(key=lambda transfer: (transfer.at_ms, transfer.tx_ref, transfer.asset))
        return found

    def signatures(self, address: str, *, until: str | None = None) -> list[Mapping[str, Any]]:
        """新签名（旧→新），跳过失败交易，越界翻页。

        `until` 是上一次处理到的最新签名：它之后的才算新记录（Solana 官方语义）。
        翻满 `MAX_PAGES` 后**再探一次**确认到底还有没有更早的：有就报错，不把半截历史
        当全部（否则游标会越过没取回来的那段——静默漏检，FR-C6-11）。
        """
        collected: list[Mapping[str, Any]] = []
        cursor = until
        for _ in range(MAX_PAGES):
            params: dict[str, Any] = {"limit": PAGE_SIZE}
            if cursor:
                params["until"] = cursor
            batch = self._rpc("getSignaturesForAddress", [address, params])
            if not isinstance(batch, list):
                raise ProviderError("Helius getSignaturesForAddress 返回的不是列表")
            fresh = [item for item in batch if isinstance(item, Mapping) and item.get("signature")]
            if not fresh:
                break
            collected.extend(fresh)
            if len(batch) < PAGE_SIZE:
                break
            cursor = str(fresh[-1]["signature"])
        else:
            if self._has_older_signatures(address, cursor):
                raise ProviderError(
                    f"Helius getSignaturesForAddress 翻到上限（{MAX_PAGES} 页 × {PAGE_SIZE} 条）"
                    "还有更早的签名：宁可停下报警，也不把半截历史当全部"
                )
        ok = [item for item in collected if not item.get("err")]
        ok.reverse()
        return ok

    def _has_older_signatures(self, address: str, until: str | None) -> bool:
        """探一页：还有没有比 `until` 更早的签名（只看有无，不取其内容）。"""
        if until is None:  # pragma: no cover - 翻满页时 cursor 一定已有值
            return False
        probe = self._rpc("getSignaturesForAddress", [address, {"limit": 1, "until": until}])
        if not isinstance(probe, list):
            raise ProviderError("Helius getSignaturesForAddress 返回的不是列表")
        return bool(probe)

    def transaction(self, signature: str, *, address: str) -> list[Transfer]:
        """一笔交易的到账（可能同时有原生币与代币，甚至多条代币）。"""
        result = self._rpc(
            "getTransaction",
            [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
        )
        if result is None:
            # 交易被节点丢了（很老/被回滚）：如实记一笔错，等补扫再试。
            raise ProviderError(f"Helius 找不到交易：{signature[:12]}…")
        if not isinstance(result, Mapping):
            raise ProviderError("Helius getTransaction 返回的不是对象")
        return transfers_from_transaction(result, address=address)

    def _rpc(self, method: str, params: list[Any]) -> Any:
        self._throttle.acquire()
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            body = self._transport.post_rpc(
                self.endpoint, payload=payload, timeout_s=self._timeout_s
            )
            result = _result_of(body, method)
        except (RateLimited, ProviderError) as exc:
            # 请求已经打出去了，因此无论结局如何都记一笔（1 call = 1 credit）。
            self._ledger.record(calls=1, credits=1, error=str(exc))
            raise
        self._ledger.record(calls=1, credits=1)
        return result


def _result_of(body: Mapping[str, Any], method: str) -> Any:
    """JSON-RPC 响应 → `result`；`error` 里的限速措辞也认（不只看 HTTP 429）。"""
    error = body.get("error")
    if error:
        text = json.dumps(error, ensure_ascii=False)[:200]
        if "429" in text or "rate" in text.lower():
            raise RateLimited(f"Helius 限速：{text}")
        raise ProviderError(f"Helius {method} 返回错误：{text}")
    if "result" not in body:
        raise ProviderError(f"Helius {method} 响应里没有 result")
    return body["result"]


def transfers_from_transaction(result: Mapping[str, Any], *, address: str) -> list[Transfer]:
    """从 `getTransaction` 的结果里取出**到账**（纯函数，不联网）。

    只看"变多了"：本地址的余额变化为正才算入账——同一笔交易里我们既有付出又有收入时
    （极少见，但允许），净额为正才是事实。
    """
    meta = result.get("meta")
    if not isinstance(meta, Mapping):
        return []
    transaction = result.get("transaction")
    if not isinstance(transaction, Mapping):
        return []
    signature = str(transaction.get("signatures", [""])[0]) if transaction.get("signatures") else ""
    if not signature:
        raise ProviderError("Helius 交易缺少签名")
    slot = _opt_int(result.get("slot"))
    at_ms = (_opt_int(result.get("blockTime")) or 0) * 1000
    memo = memo_of(transaction, meta)
    found: list[Transfer] = [
        Transfer(
            network=SOLANA,
            address=address,
            tx_ref=signature,
            asset=NATIVE_ASSET,
            units=units,
            at_ms=at_ms,
            memo=memo,
            block=slot,
        )
        for units in [_native_delta(meta, transaction, address)]
        if units and units > 0
    ]
    for mint, units in sorted(_token_deltas(meta, address).items()):
        if units > 0:
            found.append(
                Transfer(
                    network=SOLANA,
                    address=address,
                    tx_ref=signature,
                    asset=mint,
                    units=units,
                    at_ms=at_ms,
                    memo=memo,
                    block=slot,
                )
            )
    return found


def memo_of(transaction: Mapping[str, Any], meta: Mapping[str, Any]) -> str | None:
    """取交易里的 memo（`spl-memo` 指令正文）。没有就是 None（不编一个）。"""
    texts: list[str] = []
    message = transaction.get("message")
    groups: list[Any] = []
    if isinstance(message, Mapping):
        groups.append(message.get("instructions"))
    groups.append(meta.get("innerInstructions"))
    stack: list[Any] = list(groups)
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            if current.get("program") == MEMO_PROGRAM or current.get("programId") == MEMO_PROGRAM_ID:
                parsed = current.get("parsed")
                if isinstance(parsed, str) and parsed.strip():
                    texts.append(parsed.strip())
                elif isinstance(current.get("data"), str):
                    texts.append(str(current["data"]))
            else:
                stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return " ".join(texts) if texts else None


#: `spl-memo` 程序的 programId（jsonParsed 下也会出现，两条判据都认）。
MEMO_PROGRAM_ID = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"


def _native_delta(meta: Mapping[str, Any], transaction: Mapping[str, Any], address: str) -> int:
    """原生 SOL 到账（lamports）。"""
    pre = meta.get("preBalances")
    post = meta.get("postBalances")
    message = transaction.get("message")
    if not isinstance(pre, list) or not isinstance(post, list) or not isinstance(message, Mapping):
        return 0
    index = _account_index(message.get("accountKeys"), address)
    if index is None or index >= len(pre) or index >= len(post):
        return 0
    return int(post[index]) - int(pre[index])


def _token_deltas(meta: Mapping[str, Any], address: str) -> dict[str, int]:
    """属于本地址的代币账户余额变化（mint → 最小单位差额）。"""
    before = _token_amounts(meta.get("preTokenBalances"), address)
    after = _token_amounts(meta.get("postTokenBalances"), address)
    deltas: dict[str, int] = {}
    for index, (mint, amount) in after.items():
        deltas[mint] = deltas.get(mint, 0) + amount - before.get(index, (mint, 0))[1]
    return deltas


def _token_amounts(rows: Any, address: str) -> dict[int, tuple[str, int]]:
    """`[{accountIndex, mint, owner, uiTokenAmount.amount}]` → `{accountIndex: (mint, 余额)}`。

    自己名下的账户快照解不开就**报错**（当 0 处理会静默漏掉一笔入账）。
    """
    amounts: dict[int, tuple[str, int]] = {}
    if not isinstance(rows, list):
        return amounts
    for row in rows:
        if not isinstance(row, Mapping) or str(row.get("owner", "")) != address:
            continue
        index = _opt_int(row.get("accountIndex"))
        mint = row.get("mint")
        token_amount = row.get("uiTokenAmount")
        amount = _opt_int(token_amount.get("amount")) if isinstance(token_amount, Mapping) else None
        if index is None or not isinstance(mint, str) or amount is None:
            raise ProviderError("Helius 代币余额快照不可解析（accountIndex/mint/amount）")
        amounts[index] = (mint, amount)
    return amounts


def _account_index(account_keys: Any, address: str) -> int | None:
    """地址在 `accountKeys` 里的下标（jsonParsed 下每项是带 `pubkey` 的对象）。"""
    if not isinstance(account_keys, list):
        return None
    for position, entry in enumerate(account_keys):
        key = entry.get("pubkey") if isinstance(entry, Mapping) else entry
        if str(key) == address:
            return position
    return None


def _opt_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def client_from_credentials(*, conn, path=None, **kwargs) -> HeliusClient:
    """按仓库外 `.env` 造真实客户端（缺 `HELIUS_API_KEY` 即报错）。"""
    from danmu_intel.common import credentials

    return HeliusClient(
        api_key=credentials.require_secret(API_KEY, path=path),
        ledger=QuotaLedger(conn, HELIUS),
        **kwargs,
    )
