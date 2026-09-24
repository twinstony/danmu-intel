"""一条入账事实（两条链统一的形状）。

监听层的产出**只有一个形状**：`Transfer`。Polygon 与 Solana 的差异（区块号 vs 槽位、
合约地址 vs mint、memo 只有 Solana 有）都在这一个类型里如实表达——上层（订单匹配属 T9）
不需要知道任何链上细节。

金额一律是**最小单位整数**（wei / lamports / 代币最小单位），与 `orders.amount_due_units`
同一口径：钱不能用浮点算（设计 §6.4 的 `TEXT` 整数约定）。
"""

from __future__ import annotations

from dataclasses import dataclass

POLYGON = "polygon"
SOLANA = "solana"

#: 收款网络白名单（设计 §12.5：明确排除 Base）。
NETWORKS: tuple[str, ...] = (POLYGON, SOLANA)

#: 原生资产（polygon=MATIC，solana=SOL）。代币则用合约地址 / mint 当标识。
NATIVE_ASSET = "native"


@dataclass(frozen=True, slots=True)
class Transfer:
    """一笔到我们监听地址的入账。"""

    network: str
    address: str  # 监听的收款地址（polygon：派生地址；solana：单一收款地址）
    tx_ref: str  # 交易标识（polygon：交易哈希；solana：签名）—— 幂等键
    asset: str  # `native` 或代币标识（polygon：ERC20 合约地址；solana：SPL mint）
    units: int  # 到账金额（最小单位整数）
    at_ms: int  # 链上时间（毫秒）
    memo: str | None = None  # solana 每单唯一标识（polygon 恒为 None）
    block: int | None = None  # polygon 区块号 / solana 槽位（游标推进用）

    def __post_init__(self) -> None:
        if self.network not in NETWORKS:
            raise ValueError(f"未知的收款网络：{self.network}（允许：{','.join(NETWORKS)}）")
        if self.units <= 0:
            raise ValueError(f"入账金额必须为正：{self.units}")


def sort_key(transfer: Transfer) -> tuple[int, str]:
    """稳定排序键：链上时间 → 交易标识（同一时刻的排序不依赖接口返回顺序）。"""
    return (transfer.at_ms, transfer.tx_ref)
