"""灰信号识别（需求 §6.5 / 设计 §9.1）。

**定义**：弹幕中出现的、指向「比赛可能不正常」的**讨论聚集现象**。它只是风险提示。

需求 §6.5 的 6 条硬约束在本模块的落点：

1. **只作风险提示** → 产出物叫 `GraySignal`（不是 `cheat_*`），状态机只有
   `candidate|escalated|discarded`；正文里没有任何结论性判断。
2. **不指控、不点名** → `GraySample` 只有「时间 + 原文 + 取证坐标」，**结构上装不下**
   身份字段；渲染层的防线是 `contains_identity`（`report/rule_render.py` 会强制调用）。
3. **必须附样本** → 没有样本就没有 `GraySignal`；`samples` 非空是落库前置校验
   （`stats/persist.py`）。
4. **证据门槛（多人、多时段）** → 命中次数 ≥N **且** 独立用户 ≥M **且** 覆盖时段 ≥K，
   三个门槛都从 `config` 读（设计 §9.1 第 4 条）。
5. **不得用于勒索/威胁/交易** → 本模块**不提供任何对外导出接口**，也不写文件；
   数据只进库与报告页。测试逐条守住这条（`tests/unit/test_gray.py`）。
6. **不达标即 discarded 且留原因** → `reason` 必填，且 `reportable` 不会放它进报告。

纯函数层：不读时钟（`ts` 来自事件）、不读库（`config` 由调用方注入）、不写盘。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from danmu_intel.common.config import GRAY_CATEGORY_LABELS, StatsConfig
from danmu_intel.stats.basic import RawLine

STATUS_CANDIDATE = "candidate"
STATUS_ESCALATED = "escalated"
STATUS_DISCARDED = "discarded"

#: 状态机（需求 §6.5 第 1 条：只有风险提示的三种状态）。`escalated` 由人工在后台翻。
GRAY_STATUSES = (STATUS_CANDIDATE, STATUS_ESCALATED, STATUS_DISCARDED)

#: 渲染层禁止出现的身份字段名（需求 §6.5 第 2 条）。
IDENTITY_FIELDS = ("user_hash", "username", "user", "streamer", "nick", "member")


@dataclass(frozen=True, slots=True)
class GraySample:
    """一条样本：时间 + 原文片段 + 取证坐标。**没有身份字段，也不会有。**"""

    ts: int
    text: str
    rel_path: str
    line_no: int

    def as_dict(self) -> dict[str, Any]:
        return {"ts": self.ts, "text": self.text, "rel_path": self.rel_path, "line_no": self.line_no}


@dataclass(frozen=True, slots=True)
class GraySignal:
    category: str
    keyword: str
    hit_count: int
    distinct_users: int
    window_count: int
    samples: tuple[GraySample, ...]
    status: str
    reason: str | None = None

    @property
    def category_label(self) -> str:
        return GRAY_CATEGORY_LABELS.get(self.category, self.category)

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "category_label": self.category_label,
            "keyword": self.keyword,
            "hit_count": self.hit_count,
            "distinct_users": self.distinct_users,
            "window_count": self.window_count,
            "samples": [sample.as_dict() for sample in self.samples],
            "status": self.status,
            "reason": self.reason,
        }


def evaluate_gray_signals(
    lines: Sequence[RawLine], *, config: StatsConfig
) -> tuple[GraySignal, ...]:
    """逐个关键词统计聚集情况，并按门槛给出 `candidate` / `discarded`（含原因）。"""
    ordered = sorted(lines, key=lambda line: (line.event.ts, line.rel_path, line.line_no))
    signals: list[GraySignal] = []
    for keyword, category in sorted(config.gray_keywords):
        hits = [line for line in ordered if keyword in line.event.text]
        if not hits:
            continue
        users = {line.event.user_hash for line in hits}
        windows = {line.event.ts // config.gray_window_ms for line in hits}
        samples = tuple(
            GraySample(
                ts=line.event.ts, text=line.event.text, rel_path=line.rel_path, line_no=line.line_no
            )
            for line in hits[: config.gray_sample_size]
        )
        passed = (
            len(hits) >= config.gray_min_hits
            and len(users) >= config.gray_min_users
            and len(windows) >= config.gray_min_windows
        )
        if passed:
            signals.append(
                GraySignal(
                    category=category,
                    keyword=keyword,
                    hit_count=len(hits),
                    distinct_users=len(users),
                    window_count=len(windows),
                    samples=samples,
                    status=STATUS_CANDIDATE,
                )
            )
            continue
        signals.append(
            GraySignal(
                category=category,
                keyword=keyword,
                hit_count=len(hits),
                distinct_users=len(users),
                window_count=len(windows),
                samples=samples,
                status=STATUS_DISCARDED,
                reason=(
                    "未达证据门槛（需求 §6.5 第 4 条「多人、多时段」）："
                    f"命中 {len(hits)}/{config.gray_min_hits} 次、"
                    f"独立发言者 {len(users)}/{config.gray_min_users} 人、"
                    f"覆盖时段 {len(windows)}/{config.gray_min_windows} 个"
                ),
            )
        )
    return tuple(signals)


def reportable(signals: Iterable[GraySignal]) -> tuple[GraySignal, ...]:
    """进报告/页面的灰信号：只有达门槛的 `candidate`（`discarded` 只留在库里备查）。"""
    return tuple(signal for signal in signals if signal.status == STATUS_CANDIDATE)


def contains_identity(text: str, user_hashes: Iterable[str]) -> list[str]:
    """找出 `text` 里出现的身份标识（需求 §6.5 第 2 条的渲染层防线）。

    返回命中的用户哈希列表；非空即说明渲染层泄漏了身份，必须中止渲染。
    """
    return [value for value in user_hashes if value and value in text]
