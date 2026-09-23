"""成本口径与硬闸（设计 §10.4、ADR-0003、ADR-0014）。

本模块只有**纯函数**：价格表、按 API 报回的 usage 算钱、按累计花费判定闸门、
本地日历日边界。读写账本（`llm_calls`）在 `ledger.py`，两者分开是为了让
"这单花多少钱、该不该停"可以脱离数据库被断言。

价格取自 DeepSeek 官方定价页的**高峰价**（元 / 百万 tokens）。选高峰价是有意的：
成本闸是安全阀，宁可高估成本早停，也不要事后才发现已经花超了。调价时改这一处，
注意历史账本按当时的代码价记账（ADR-0014 的已知局限）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo

#: 硬闸（ADR-0003：单场 ≤ ¥0.3、每日 ≤ ¥10）。**达到即闸**（`>=`），不是超过才闸。
MATCH_LIMIT_CNY = 0.3
DAILY_LIMIT_CNY = 10.0

MILLION = 1_000_000


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """一个模型的单价（元 / 百万 tokens）。"""

    cache_hit_input: float
    cache_miss_input: float
    output: float


#: 官方定价（高峰价，2026-09 核对 https://api-docs.deepseek.com/zh-cn/quick_start/pricing/）。
MODEL_PRICES: dict[str, ModelPrice] = {
    "deepseek-v4-flash": ModelPrice(cache_hit_input=0.04, cache_miss_input=2.0, output=8.0),
    "deepseek-v4-pro": ModelPrice(cache_hit_input=0.30, cache_miss_input=9.0, output=27.0),
}

DEFAULT_MODEL = "deepseek-v4-flash"


def price_for(model: str) -> ModelPrice:
    """取单价。未登记的模型直接报错——**宁可不发，不可算错**（ADR-0014）。"""
    try:
        return MODEL_PRICES[model]
    except KeyError:
        raise LookupError(
            f"未登记价格的模型：{model}（已登记：{','.join(sorted(MODEL_PRICES))}）"
        ) from None


def estimate_cost_cny(
    model: str,
    *,
    prompt_tokens: int,
    completion_tokens: int,
    cache_hit_tokens: int = 0,
) -> float:
    """按 usage 算这一次调用的钱（元，六位小数）。

    `cache_hit_tokens` 是提示词前缀命中的部分（DeepSeek 按命中价计），超出
    `prompt_tokens` 或为负都不信，直接夹到 `[0, prompt_tokens]`。
    """
    price = price_for(model)
    hit = max(0, min(int(cache_hit_tokens), int(prompt_tokens)))
    miss = int(prompt_tokens) - hit
    cost = (
        hit * price.cache_hit_input
        + miss * price.cache_miss_input
        + int(completion_tokens) * price.output
    ) / MILLION
    return round(cost, 6)


def day_bounds(now_ms: int, *, tz: tzinfo | None = None) -> tuple[int, int]:
    """`now_ms` 所在**本地日历日**的 [起, 止) 毫秒（ADR-0001 单机部署）。"""
    moment = datetime.fromtimestamp(now_ms / 1000, tz=tz)
    start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp() * 1000), int((start + timedelta(days=1)).timestamp() * 1000)


@dataclass(frozen=True, slots=True)
class Spend:
    """累计花费：这一场 + 当日（含这一场）。"""

    match_cny: float
    day_cny: float


@dataclass(frozen=True, slots=True)
class GateDecision:
    """闸门判定结果。`limit_kind` 是触及的那一档：`match` / `day`。"""

    allowed: bool
    reason: str = ""
    limit_kind: str | None = None

    @property
    def blocked(self) -> bool:
        return not self.allowed


ALLOWED = GateDecision(allowed=True)


def gate(
    spend: Spend,
    *,
    match_limit: float = MATCH_LIMIT_CNY,
    daily_limit: float = DAILY_LIMIT_CNY,
) -> GateDecision:
    """单场 / 当日硬闸判定（调用**前**判）。"""
    if spend.match_cny >= match_limit:
        return GateDecision(
            False,
            f"单场成本已达 ¥{spend.match_cny:.4f}（硬闸 ¥{match_limit}），本次不再调用 LLM",
            "match",
        )
    if spend.day_cny >= daily_limit:
        return GateDecision(
            False,
            f"当日成本已达 ¥{spend.day_cny:.4f}（硬闸 ¥{daily_limit}），本次不再调用 LLM",
            "day",
        )
    return ALLOWED
