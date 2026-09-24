"""Polygonscan 客户端（ADR-0005：Polygon 走 Polygonscan）。

两条查历史的路径都用 `account` 模块，**都是按地址查**（不依赖游标，所以补扫天然可用）：

| action | 查到什么 | 入账判定 |
|---|---|---|
| `txlist` | 原生币（MATIC）转账 | `to == 监听地址` 且 `value > 0` |
| `tokentx` | ERC20 转账（USDT 这类稳定币） | `to == 监听地址` 且 `value > 0` |

**端点用 Etherscan V2 多链 API**（`api.etherscan.io/v2/api?chainid=137`）：
`api.polygonscan.com/api` 已 301 迁移过去，继续打旧域名会拿到重定向而不是 JSON。
`chainid=137` 就是 Polygon PoS。

纪律：

- 每次对外请求（含失败与限速）都记一次 `quota_usage`（`QuotaLedger`）；
- 撞限速抛 `RateLimited`（调用方据此**报警**，不静默重试——FR-C6-11）；
- 异常消息里只有状态码与供应商原文，绝不含 `apikey`（AC-12）；
- 传输层可注入 → **断网可跑**（NFR-GA-4），测试全程不连外网。
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping, Protocol

from danmu_intel.chain.provider import ProviderError, RateLimited
from danmu_intel.chain.quota import POLYGONSCAN, QuotaLedger, RateLimiter
from danmu_intel.chain.transfer import NATIVE_ASSET, POLYGON, Transfer

DEFAULT_API_BASE = "https://api.etherscan.io/v2/api"

#: 凭据键名（只入仓库外 `.env`，永不入 git —— AC-12 / NFR-S-4）。
API_KEY = "POLYGONSCAN_API_KEY"

#: Polygon PoS 的 chainid（Etherscan V2 用它选链）。
CHAIN_ID = 137

#: 单次请求超时（秒）。链上监听不赶时间，但绝不无限等：卡住的 watcher 等于瞎。
CALL_TIMEOUT_S = 15.0

#: 一次最多取多少条（ADR-0005：1000 条/次）。
PAGE_SIZE = 1000

#: 翻页上限（补扫全历史时最多翻这么多页）。够覆盖真实收款地址的全部历史，
#: 又不至于在数据异常时无限翻页把额度打光。
MAX_PAGES = 10

#: "扫到最新区块"的惯用写法（Etherscan 约定的大数）。
HEAD_BLOCK = 99_999_999


class Transport(Protocol):
    """HTTP 传输层注入缝：一次带查询参数的 GET，拿回解析后的 JSON 对象。"""

    def get_json(
        self, url: str, *, params: Mapping[str, str], timeout_s: float
    ) -> Mapping[str, Any]: ...


class UrllibTransport:
    """缺省传输层（stdlib `urllib`：一次 GET，不引入新依赖）。"""

    def get_json(
        self, url: str, *, params: Mapping[str, str], timeout_s: float
    ) -> Mapping[str, Any]:
        request = urllib.request.Request(
            f"{url}?{urllib.parse.urlencode(params)}",
            headers={"Accept": "application/json", "User-Agent": "danmu-intel/0.1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:  # pragma: no cover - 真实网络路径
            if exc.code == 429:
                raise RateLimited("Polygonscan 限速：HTTP 429") from None
            raise ProviderError(f"Polygonscan 请求失败：HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:  # pragma: no cover
            raise ProviderError(f"Polygonscan 不可达（{type(exc).__name__}）") from None
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:  # pragma: no cover - 真实网络路径
            raise ProviderError(f"Polygonscan 返回的不是 JSON（{len(body)} 字节）") from None
        if not isinstance(payload, Mapping):  # pragma: no cover - 真实网络路径
            raise ProviderError("Polygonscan 返回了非对象 JSON")
        return payload


class PolygonscanClient:
    """按地址查 Polygon 入账（原生 + ERC20）。

    `transport` / `throttle` 可注入（测试注入假传输层与假时钟）；`api_key` 只进查询参数，
    不出现在任何异常或日志里。
    """

    def __init__(
        self,
        *,
        api_key: str,
        ledger: QuotaLedger,
        transport: Transport | None = None,
        throttle: RateLimiter | None = None,
        base_url: str = DEFAULT_API_BASE,
        chain_id: int = CHAIN_ID,
        timeout_s: float = CALL_TIMEOUT_S,
    ) -> None:
        self._api_key = api_key
        self._ledger = ledger
        self._transport = transport or UrllibTransport()
        self._throttle = throttle or RateLimiter(ledger.limit.rate_per_s)
        self._base_url = base_url
        self._chain_id = chain_id
        self._timeout_s = timeout_s

    @property
    def provider(self) -> str:
        return POLYGONSCAN

    def transfers(self, address: str, *, from_block: int = 0) -> list[Transfer]:
        """从 `from_block`（含）起的所有入账：原生币 + ERC20。"""
        found = [
            *self.native_transfers(address, from_block=from_block),
            *self.token_transfers(address, from_block=from_block),
        ]
        found.sort(key=lambda item: (item.block or 0, item.tx_ref, item.asset))
        return found

    def native_transfers(self, address: str, *, from_block: int = 0) -> list[Transfer]:
        rows = self._walk("txlist", address, from_block=from_block)
        return [item for row in rows if (item := self._native(address, row)) is not None]

    def token_transfers(self, address: str, *, from_block: int = 0) -> list[Transfer]:
        rows = self._walk("tokentx", address, from_block=from_block)
        return [item for row in rows if (item := self._token(address, row)) is not None]

    # —— 内部 ——

    def _walk(self, action: str, address: str, *, from_block: int) -> list[Mapping[str, Any]]:
        """按地址翻页取记录（`sort=asc`，因此游标可以一路往前推）。"""
        rows: list[Mapping[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            batch = self._call(
                action,
                {
                    "address": address,
                    "startblock": str(max(0, from_block)),
                    "endblock": str(HEAD_BLOCK),
                    "sort": "asc",
                    "page": str(page),
                    "offset": str(PAGE_SIZE),
                },
            )
            rows.extend(batch)
            if len(batch) < PAGE_SIZE:
                break
        return rows

    def _call(self, action: str, params: Mapping[str, str]) -> list[Mapping[str, Any]]:
        self._throttle.acquire()
        query = {
            "chainid": str(self._chain_id),
            "module": "account",
            "action": action,
            "apikey": self._api_key,
            **params,
        }
        try:
            body = self._transport.get_json(self._base_url, params=query, timeout_s=self._timeout_s)
            rows = _rows_of(body)
        except (RateLimited, ProviderError) as exc:
            # 请求已经打出去了，因此无论结局如何都记一笔；出错时把原因一并记上
            # （宁可高估用量：账本要回答"打出去多少次"，不是"成功的多少次"）。
            self._ledger.record(calls=1, error=str(exc))
            raise
        self._ledger.record(calls=1)
        return rows

    def _native(self, address: str, row: Mapping[str, Any]) -> Transfer | None:
        """原生币入账（只看进账；出去的转账不是入账）。"""
        if str(row.get("isError")) == "1":
            return None
        if str(row.get("to", "")).lower() != address.lower():
            return None
        units = _int(row, "value")
        if units <= 0:
            return None
        return Transfer(
            network=POLYGON,
            address=address,
            tx_ref=str(row.get("hash")),
            asset=NATIVE_ASSET,
            units=units,
            at_ms=_int(row, "timeStamp") * 1000,
            block=_int(row, "blockNumber"),
        )

    def _token(self, address: str, row: Mapping[str, Any]) -> Transfer | None:
        """ERC20 入账（`asset` 用合约地址当标识，不猜币种）。"""
        if str(row.get("to", "")).lower() != address.lower():
            return None
        units = _int(row, "value")
        if units <= 0:
            return None
        return Transfer(
            network=POLYGON,
            address=address,
            tx_ref=str(row.get("hash")),
            asset=str(row.get("contractAddress", "")).lower(),
            units=units,
            at_ms=_int(row, "timeStamp") * 1000,
            block=_int(row, "blockNumber"),
        )


def _int(row: Mapping[str, Any], key: str) -> int:
    """取一个整数字段；取不到就报错（**不静默跳过**：漏一条记录就是漏一笔付款）。"""
    try:
        return int(row[key])
    except (KeyError, TypeError, ValueError):
        raise ProviderError(f"Polygonscan 记录缺少可用字段：{key}") from None


def _rows_of(body: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """把响应体变成记录列表（空历史是正常结果，限速与报错不是）。"""
    status = str(body.get("status", ""))
    message = str(body.get("message", ""))
    result = body.get("result")
    if status == "1":
        if not isinstance(result, list):
            raise ProviderError("Polygonscan 返回 status=1 但 result 不是列表")
        return [row for row in result if isinstance(row, Mapping)]
    text = f"{message} {result if isinstance(result, str) else ''}".lower()
    if "rate limit" in text:
        raise RateLimited("Polygonscan 限速：Max rate limit reached")
    if "no transactions found" in text or "no records found" in text:
        return []
    detail = str(result)[:160] if not isinstance(result, list) else f"{len(result)} 条"
    raise ProviderError(f"Polygonscan 返回错误：{message or status}｜{detail}")


def client_from_credentials(*, conn, path=None, **kwargs) -> PolygonscanClient:
    """按仓库外 `.env` 造真实客户端（缺 `POLYGONSCAN_API_KEY` 即报错）。"""
    from danmu_intel.common import credentials

    return PolygonscanClient(
        api_key=credentials.require_secret(API_KEY, path=path),
        ledger=QuotaLedger(conn, POLYGONSCAN),
        **kwargs,
    )
