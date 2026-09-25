"""档位、价格与收款配置（设计 §12.1；需求 FR-C6-1/C6-2/C6-3）。

档位（标准档 / 试用档）、价格、时长、宽限期、订单时效与收款配置全部存在 `config` 表的
`billing` 键下 —— **不写死在代码里**（FR-C6-2：管理员可调，改动不影响已生效的会员；
设计 §20 O1 的价格数值是开放项，下面的默认值只是「还没配过」时的起点）。

金额一律是**最小单位整数**（USDT 6 位小数）：钱不用浮点算，与 `Transfer.units`、
`orders.amount_due_units` 同一口径。

收款资产固定为两链的 USDT（公开常量，不是凭据）。`polygon_xpub` 是 watch-only 公开信息
（ADR-0004：派生地址用，无法动用资金）；`solana_address` 是单一收款地址 + 每单唯一 memo。
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, fields
from typing import Any

from danmu_intel.chain.transfer import NETWORKS, POLYGON, SOLANA
from danmu_intel.common import audit

CONFIG_KEY = "billing"

#: 金额精度：USDT 6 位小数。
UNIT_DECIMALS = 6
UNIT_SCALE = 10**UNIT_DECIMALS

#: 收款资产（两链的 USDT）。合约地址 / mint 是公开常量，不是可动用资产的凭据。
ASSET_SYMBOL = "USDT"
USDT = {
    POLYGON: "0xc2132d05d31c914a87c6611c10748aeb04b58e8f",
    SOLANA: "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
}

#: 试用档的时间与价格**显著低于**标准档（FR-C6-1）。
DEFAULT_TIERS: tuple["Tier", ...] = ()


class BillingConfigError(ValueError):
    """收款配置不完整或非法（消息里不含任何凭据值）。"""


@dataclass(frozen=True, slots=True)
class Tier:
    """一个订阅档位：付 `amount_units` 得 `days` 天。"""

    key: str
    label: str
    amount_units: int
    days: int

    def __post_init__(self) -> None:
        if not self.key or not self.label:
            raise BillingConfigError("档位必须有 key 与 label")
        if self.amount_units <= 0:
            raise BillingConfigError(f"档位 {self.key} 的价格必须为正：{self.amount_units}")
        if self.days <= 0:
            raise BillingConfigError(f"档位 {self.key} 的天数必须为正：{self.days}")

    @property
    def duration_ms(self) -> int:
        return self.days * 24 * 3600 * 1000

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "amount_units": self.amount_units, "days": self.days}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Tier":
        known = {"key", "label", "amount_units", "days"}
        unknown = sorted(set(payload) - known)
        if unknown:
            raise BillingConfigError(f"未知的档位字段：{','.join(unknown)}")
        try:
            return cls(
                key=str(payload["key"]),
                label=str(payload["label"]),
                amount_units=int(payload["amount_units"]),
                days=int(payload["days"]),
            )
        except KeyError as exc:
            raise BillingConfigError(f"档位缺少字段：{exc.args[0]}") from exc


DEFAULT_TIERS = (
    Tier(key="standard", label="标准档", amount_units=5 * UNIT_SCALE, days=30),
    Tier(key="trial", label="试用档", amount_units=UNIT_SCALE // 2, days=3),
)

#: 订单有效期（设计 §12.2 ⑧：30 分钟未付即过期）。
DEFAULT_ORDER_TTL_MS = 30 * 60 * 1000
#: 宽限期（设计 §12.6：默认 24 小时；FR-C6-14：到期降级但保留宽限期）。
DEFAULT_GRACE_MS = 24 * 3600 * 1000


@dataclass(frozen=True, slots=True)
class BillingConfig:
    """`config` 表 `billing` 键的内容（默认值 + 覆盖后的生效值）。"""

    tiers: tuple[Tier, ...] = DEFAULT_TIERS
    order_ttl_ms: int = DEFAULT_ORDER_TTL_MS
    grace_ms: int = DEFAULT_GRACE_MS
    polygon_xpub: str = ""
    solana_address: str = ""
    #: 订阅页上写的对外 API 基址（Funnel 域名）；空则不写进页面。
    api_base: str = ""

    def tier(self, key: str) -> Tier:
        for item in self.tiers:
            if item.key == key:
                return item
        raise BillingConfigError(
            f"未知的档位：{key}（可选：{','.join(item.key for item in self.tiers)}）"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "tiers": [item.as_dict() for item in self.tiers],
            "order_ttl_ms": self.order_ttl_ms,
            "grace_ms": self.grace_ms,
            "polygon_xpub": self.polygon_xpub,
            "solana_address": self.solana_address,
            "api_base": self.api_base,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "BillingConfig":
        """从 `config` 表读出的 JSON 覆盖默认值。未知键直接报错（不静默吞配置）。"""
        known = {field.name for field in fields(cls)}
        unknown = sorted(set(payload) - known)
        if unknown:
            raise BillingConfigError(f"未知的收款配置项：{','.join(unknown)}")
        data = dict(payload)
        if "tiers" in data:
            data["tiers"] = tuple(Tier.from_dict(item) for item in data["tiers"])
        config = cls(**data)
        keys = [item.key for item in config.tiers]
        if len(set(keys)) != len(keys):
            raise BillingConfigError(f"档位 key 重复：{','.join(keys)}")
        if config.order_ttl_ms <= 0 or config.grace_ms < 0:
            raise BillingConfigError(
                f"订单时效必须为正、宽限期不得为负：order_ttl_ms={config.order_ttl_ms}、"
                f"grace_ms={config.grace_ms}"
            )
        return config

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True)

    def require_xpub(self) -> str:
        """Polygon 收款 xpub；没配就拒绝下单（宁可不收钱，也不给一个不能用的地址）。"""
        if not self.polygon_xpub:
            raise BillingConfigError(
                "还没配置 Polygon 收款 xpub（watch-only 公开信息）："
                "danmu-intel billing --set polygon_xpub=xpub…"
            )
        return self.polygon_xpub

    def require_solana_address(self) -> str:
        if not self.solana_address:
            raise BillingConfigError(
                "还没配置 Solana 收款地址：danmu-intel billing --set solana_address=…"
            )
        return self.solana_address


def asset_for(network: str) -> str:
    """该网络上的收款资产标识（polygon：ERC20 合约地址小写；solana：SPL mint）。"""
    if network not in NETWORKS:
        raise BillingConfigError(f"未知的收款网络：{network}（允许：{','.join(NETWORKS)}）")
    return USDT[network]


def is_our_asset(network: str, asset: str) -> bool:
    """链上入账的 `asset` 是不是我们要的收款资产（polygon 比小写，solana 逐字节比）。"""
    expected = asset_for(network)
    if network == SOLANA:
        return asset == expected
    return asset.lower() == expected.lower()


def format_units(units: int) -> str:
    """最小单位整数 → 给人看的金额（至少两位小数，尾部多余的零去掉）。"""
    sign = "-" if units < 0 else ""
    digits = f"{abs(units):0{UNIT_DECIMALS + 1}d}"
    whole, fraction = digits[:-UNIT_DECIMALS], digits[-UNIT_DECIMALS:]
    fraction = fraction.rstrip("0")
    while len(fraction) < 2:
        fraction += "0"
    return f"{sign}{whole}.{fraction}"


def load_billing_config(conn: sqlite3.Connection) -> BillingConfig:
    """读配置：默认值 + `config` 表里 `billing` 键的覆盖。"""
    row = conn.execute("SELECT value_json FROM config WHERE key=?", (CONFIG_KEY,)).fetchone()
    if row is None:
        return BillingConfig()
    return BillingConfig.from_dict(json.loads(row["value_json"]))


def save_billing_config(
    conn: sqlite3.Connection, *, actor: str, changes: dict[str, Any], ts: int | None = None
) -> BillingConfig:
    """改档位/价格/收款配置并留审计（FR-C6-2：改价格不影响已生效的会员）。

    已生效的会员不受影响：`members.expires_at` 是开通那一刻就算好的死日子，
    不随价格或时长变化重算。
    """
    current = load_billing_config(conn)
    updated = BillingConfig.from_dict({**current.as_dict(), **changes})
    audit.record(
        conn,
        actor=actor,
        action=audit.CONFIG_UPDATE,
        target=CONFIG_KEY,
        detail={"before": current.as_dict(), "after": updated.as_dict()},
        ts=ts,
    )
    conn.execute(
        "INSERT INTO config(key, value_json, updated_at, updated_by) VALUES(?, ?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, "
        "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
        (
            CONFIG_KEY,
            updated.to_json(),
            int(time.time() * 1000) if ts is None else ts,
            actor,
        ),
    )
    conn.commit()
    return updated
