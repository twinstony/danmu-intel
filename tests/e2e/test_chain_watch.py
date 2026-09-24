"""链上监听的端到端验证（AC-5 / FR-C6-11 / AC-12），全程断网。

用一个假的「链」+ 真实的客户端与监听器，把 issue #18 的验收条件逐条走一遍：

① 实时入账：一轮轮询就能看见付款，游标落库；
② 漏一笔：供应商限速 → 这一轮扫不成，**报警**（不静默），游标停在原处；
③ 补扫：不依赖游标、按地址查全历史 → 那一笔照样被发现（AC-5 原文）；
④ 额度记账：每一次调用（含被限速的那次）都记进 `quota_usage`；
⑤ 重启（新建监听器 = 新进程）从落库的游标续扫，不把已看见的付款再报一遍；
⑥ 链上数据里零命中私钥/助记词（AC-12）。
"""

from __future__ import annotations

from datetime import datetime

from danmu_intel.chain import cursor
from danmu_intel.chain.helius import HeliusClient
from danmu_intel.chain.polygonscan import PolygonscanClient
from danmu_intel.chain.quota import HELIUS, POLYGONSCAN, QuotaLedger
from danmu_intel.chain.transfer import NATIVE_ASSET
from danmu_intel.chain.watcher import WatchTarget, Watcher
from danmu_intel.common import paths
from danmu_intel.common.notifications import recent
from tools.check_no_secrets import scan_tree

BASE_MS = int(datetime(2026, 9, 24, 12, 0, 0).timestamp() * 1000)
ADDRESS = "0x1234567890abcdef1234567890abcdef12345678"
USDT = "0xc2132d05d31c914a87c6611c10748aeb04b58e8f"
WALLET = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"
FAKE_KEY = "polygonscan-" + "e2e" * 5
TS = BASE_MS // 1000


class FakeChain:
    """一条会配合测试「出故障」的假链（只实现客户端真正要的协议）。"""

    def __init__(self) -> None:
        self.polygon_txs: list[dict] = []
        self.solana_signatures: list[str] = []
        self.solana_txs: dict[str, dict] = {}
        self.rate_limited = False
        self.calls: list[str] = []

    # Polygonscan 传输层
    def get_json(self, url, *, params, timeout_s):
        self.calls.append(f"polygonscan:{params['action']}")
        if self.rate_limited:
            return {
                "status": "0",
                "message": "NOTOK",
                "result": "Max rate limit reached, please use API Rate Limit after some time",
            }
        start = int(params["startblock"])
        rows = [
            row
            for row in self.polygon_txs
            if row["_action"] == params["action"] and int(row["blockNumber"]) >= start
        ]
        if not rows:
            return {"status": "0", "message": "No transactions found", "result": []}
        return {"status": "1", "message": "OK", "result": [{k: v for k, v in row.items() if k != "_action"} for row in rows]}

    # Helius 传输层
    def post_rpc(self, url, *, payload, timeout_s):
        method = payload["method"]
        self.calls.append(f"helius:{method}")
        if self.rate_limited:
            return {"jsonrpc": "2.0", "id": 1, "error": {"code": -32429, "message": "429 Too Many Requests"}}
        if method == "getSignaturesForAddress":
            until = payload["params"][1].get("until")
            rows = [{"signature": sig, "slot": 10, "blockTime": TS, "err": None} for sig in self.solana_signatures]
            if until in self.solana_signatures:
                rows = rows[self.solana_signatures.index(until) + 1 :]
            return {"jsonrpc": "2.0", "id": 1, "result": list(reversed(rows))}
        return {"jsonrpc": "2.0", "id": 1, "result": self.solana_txs[payload["params"][0]]}

    # —— 测试用的「链上事件」——

    def pay_native(self, block: int, *, units: int, tx: str) -> None:
        self.polygon_txs.append(
            {
                "_action": "txlist",
                "blockNumber": str(block),
                "timeStamp": str(TS + block),
                "hash": tx,
                "from": "0x000000000000000000000000000000000000dead",
                "to": ADDRESS,
                "value": str(units),
                "isError": "0",
            }
        )

    def pay_token(self, block: int, *, units: int, tx: str) -> None:
        self.polygon_txs.append(
            {
                "_action": "tokentx",
                "blockNumber": str(block),
                "timeStamp": str(TS + block),
                "hash": tx,
                "from": "0x000000000000000000000000000000000000dead",
                "to": ADDRESS,
                "value": str(units),
                "contractAddress": USDT,
                "tokenSymbol": "USDT",
                "tokenDecimal": "6",
            }
        )

    def pay_solana(self, signature: str, *, lamports: int, memo: str) -> None:
        self.solana_signatures.append(signature)
        self.solana_txs[signature] = {
            "slot": 250_000_000,
            "blockTime": TS,
            "meta": {
                "err": None,
                "preBalances": [1_000_000, 500_000],
                "postBalances": [1_000_000 + lamports, 500_000],
                "preTokenBalances": [],
                "postTokenBalances": [],
                "innerInstructions": [],
            },
            "transaction": {
                "signatures": [signature],
                "message": {
                    "accountKeys": [{"pubkey": WALLET}, {"pubkey": "other"}],
                    "instructions": [{"program": "spl-memo", "parsed": memo}],
                },
            },
        }


def build_watcher(conn, chain: FakeChain, *, clock) -> Watcher:
    polygon = PolygonscanClient(
        api_key=FAKE_KEY,
        ledger=QuotaLedger(conn, POLYGONSCAN, clock=clock),
        transport=chain,
    )
    helius = HeliusClient(
        api_key="helius-" + "e2e" * 5,
        ledger=QuotaLedger(conn, HELIUS, clock=clock),
        transport=chain,
    )
    return Watcher(
        conn,
        [WatchTarget("polygon", ADDRESS), WatchTarget("solana", WALLET)],
        polygon=polygon,
        helius=helius,
        clock=clock,
    )


def test_missed_payment_is_found_by_rescan(conn, data_root):
    chain = FakeChain()
    chain.pay_native(100, units=1_000_000_000_000_000_000, tx="0xpay1")
    chain.pay_token(101, units=25_000_000, tx="0xpay2")
    chain.pay_solana("sig-pay1", lamports=250_000_000, memo="mem-3f7a")
    watcher = build_watcher(conn, chain, clock=lambda: BASE_MS)

    # ① 实时入账：一笔 Polygon 原生 + 一笔 USDT + 一笔 Solana（带 memo）
    first = watcher.rescan()
    assert first.failures == []
    assets = {f"{item.network}:{item.asset}" for item in first.transfers}
    assert assets == {f"polygon:{NATIVE_ASSET}", f"polygon:{USDT}", f"solana:{NATIVE_ASSET}"}
    assert next(item for item in first.transfers if item.network == "solana").memo == "mem-3f7a"
    assert cursor.get(conn, "polygon", ADDRESS) == "101"
    assert cursor.get(conn, "solana", WALLET) == "sig-pay1"

    # ② 新到一笔付款，但这一轮供应商限速 → 扫不成，**但不静默**（写一条报警）
    chain.pay_native(200, units=5_000_000_000_000_000_000, tx="0xmissed")
    chain.rate_limited = True
    blind = watcher.poll()
    assert blind.transfers == []
    assert len(blind.failures) == 2  # 两个地址都没扫成
    kinds = {item.kind for item in recent(conn, limit=5)}
    assert "chain_rate_limited" in kinds
    assert cursor.get(conn, "polygon", ADDRESS) == "101"  # 没扫成就不推游标

    # ③ 恢复正常：补扫按地址查全历史 → 那一笔照样被找到（AC-5）
    chain.rate_limited = False
    recovered = watcher.rescan()
    assert "0xmissed" in [item.tx_ref for item in recovered.transfers]
    assert cursor.get(conn, "polygon", ADDRESS) == "200"

    # ④ 每一次调用都记了账（含被限速的那次）
    polygonscan = QuotaLedger(conn, POLYGONSCAN).usage()
    helius = QuotaLedger(conn, HELIUS).usage()
    assert polygonscan.day_used == 5  # 补扫 2 次（txlist+tokentx）+ 被限速那轮 1 次 + 再补扫 2 次
    assert helius.day_used == 5  # 同上：每笔签名还要一次 getTransaction 取金额与 memo
    assert polygonscan.last_error is None  # 最近一次是成功的

    # ⑤ 链上数据里零命中可动用资产的凭据（AC-12）
    assert scan_tree(data_root) == []
    assert scan_tree(paths.repo_root()) == []


def test_restart_continues_from_the_persisted_cursor(conn, data_root):
    """重启续扫：新进程（= 新建监听器与客户端，游标只在库里）从断点接着扫，不重报旧账。"""
    chain = FakeChain()
    chain.pay_native(100, units=1_000, tx="0xfirst")
    chain.pay_solana("sig-1", lamports=250_000_000, memo="mem-1")
    first = build_watcher(conn, chain, clock=lambda: BASE_MS).poll()
    assert {item.tx_ref for item in first.transfers} == {"0xfirst", "sig-1"}

    # 停一会儿，链上又进两笔，然后进程重启（同一个数据目录）
    chain.pay_native(150, units=2_000, tx="0xsecond")
    chain.pay_solana("sig-2", lamports=300_000_000, memo="mem-2")
    restarted = build_watcher(conn, chain, clock=lambda: BASE_MS).poll()

    assert {item.tx_ref for item in restarted.transfers} == {"0xsecond", "sig-2"}  # 旧账不再出现
    assert cursor.get(conn, "polygon", ADDRESS) == "150"
    assert cursor.get(conn, "solana", WALLET) == "sig-2"


def test_watcher_keeps_running_after_a_provider_failure(conn):
    """限速只影响那一轮：下一轮恢复后照样看得见新入账。"""
    chain = FakeChain()
    watcher = build_watcher(conn, chain, clock=lambda: BASE_MS)

    chain.rate_limited = True
    assert watcher.poll().transfers == []
    chain.rate_limited = False
    chain.pay_native(300, units=7, tx="0xlate")
    assert [item.tx_ref for item in watcher.poll().transfers] == ["0xlate"]


def test_quota_alert_fires_on_the_polygonscan_daily_window(conn):
    from danmu_intel.chain.quota import Limit

    chain = FakeChain()
    polygon = PolygonscanClient(
        api_key=FAKE_KEY,
        ledger=QuotaLedger(
            conn,
            POLYGONSCAN,
            limit=Limit(unit="calls", window="day", cap=4, rate_per_s=100),
            clock=lambda: BASE_MS,
        ),
        transport=chain,
    )
    watcher = Watcher(conn, [WatchTarget("polygon", ADDRESS)], polygon=polygon, clock=lambda: BASE_MS)

    watcher.poll()  # 2 次调用（txlist + tokentx）
    assert recent(conn, limit=5) == []
    watcher.poll()  # 累计 4 次 = 100% → 越 80% 阈值
    [item] = recent(conn, limit=5)
    assert item.kind == "chain_quota_high"
    assert item.severity == "critical"
    assert item.payload["used"] == 4 and item.payload["cap"] == 4
