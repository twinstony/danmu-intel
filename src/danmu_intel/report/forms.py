"""报告三形态（需求 FR-C4-2 / §6.6 / §7.1，设计 §10.2）。

| 形态 | 触发 | 时限（NFR-T） | 段落范围 |
|---|---|---|---|
| `live_brief` 赛中快报 | 节点（小局）结束 | **2 分钟** | 不依赖终局的段 |
| `full` 完整版 | 比赛结束 | **10 分钟** | 全十一段 |
| `review` 复盘版 | 比赛结束 | **15 分钟** | 全十一段 |

**形态是段集的唯一决定者**：同一形态每次发布的段号集合固定（NFR-Q-2 结构稳定）。
赛中快报只发布不依赖「比赛终局」的段 —— 段 7「预测验证」要拿最终结果做对照，
赛中发布它只能写出空话，因此不进快报；其余十段都能仅凭**已完成节点**产出
（issue #8 范围第 3 条）。

时限与预算：设计 §10.2 把赛中快报的 120s 拆成逐阶段预算，本模块把这张表
**落成常量**，并给各阶段实测耗时（`Timing`）；实测超预算**不阻断发布**
（NFR-T 末句：准确性优先），但会记进发布检查并在命令输出里显示。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Sequence

from danmu_intel.report.segments import ALL_SEGMENT_NOS

KIND_LIVE_BRIEF = "live_brief"
KIND_FULL = "full"
KIND_REVIEW = "review"
FORM_KINDS: tuple[str, ...] = (KIND_LIVE_BRIEF, KIND_FULL, KIND_REVIEW)

TRIGGER_NODE_END = "node_end"
TRIGGER_MATCH_END = "match_end"

LIVE_BRIEF_DEADLINE_MS = 2 * 60 * 1000
FULL_DEADLINE_MS = 10 * 60 * 1000
REVIEW_DEADLINE_MS = 15 * 60 * 1000

# 赛中快报不发布的段：需要比赛终局才有对照项（设计 §10.2 的「核心事实段」）
ENDED_ONLY_SEGMENTS: tuple[int, ...] = (7,)
LIVE_BRIEF_SEGMENTS: tuple[int, ...] = tuple(
    no for no in ALL_SEGMENT_NOS if no not in ENDED_ONLY_SEGMENTS
)


@dataclass(frozen=True, slots=True)
class ReportForm:
    kind: str
    label: str  # 中文名（CONTEXT.md 术语表）
    trigger: str
    deadline_ms: int
    segments: tuple[int, ...]

    def __post_init__(self) -> None:
        if not self.segments or tuple(sorted(self.segments)) != self.segments:
            raise ValueError(f"形态 {self.kind} 的段号必须是不为空的升序序列")


FORMS: tuple[ReportForm, ...] = (
    ReportForm(KIND_LIVE_BRIEF, "赛中快报", TRIGGER_NODE_END, LIVE_BRIEF_DEADLINE_MS, LIVE_BRIEF_SEGMENTS),
    ReportForm(KIND_FULL, "完整版", TRIGGER_MATCH_END, FULL_DEADLINE_MS, ALL_SEGMENT_NOS),
    ReportForm(KIND_REVIEW, "复盘版", TRIGGER_MATCH_END, REVIEW_DEADLINE_MS, ALL_SEGMENT_NOS),
)

FORMS_BY_KIND: dict[str, ReportForm] = {form.kind: form for form in FORMS}


def form_of(kind: str) -> ReportForm:
    try:
        return FORMS_BY_KIND[kind]
    except KeyError:
        raise ValueError(f"未注册的报告形态：{kind}（允许：{','.join(FORM_KINDS)}）") from None


# 设计 §10.2 的时效预算拆解（原表以赛中快报的 120s 为例）。`deploy` 阶段归 T7 的发布闭环。
LIVE_BRIEF_STAGE_BUDGET_MS: tuple[tuple[str, int], ...] = (
    ("stats_ready", 20_000),
    ("fact_assembly", 10_000),
    ("interpretation", 25_000),
    ("validation", 5_000),
    ("render", 5_000),
    ("publish_checks", 10_000),
    ("deploy", 40_000),
)

STAGE_BUDGET_MS: dict[str, int] = dict(LIVE_BRIEF_STAGE_BUDGET_MS)


class Timing:
    """逐阶段实测耗时（毫秒），对照设计 §10.2 的预算表。

    `clock` 可注入（测试用假时钟），缺省 `time.monotonic`。
    """

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or time.monotonic
        self._last = self._clock()
        self._stages: dict[str, int] = {}

    def mark(self, stage: str) -> None:
        now = self._clock()
        self._stages[stage] = max(0, int(round((now - self._last) * 1000)))
        self._last = now

    @property
    def stages(self) -> dict[str, int]:
        return dict(self._stages)

    @property
    def elapsed_ms(self) -> int:
        return sum(self._stages.values())

    def over_budget(self, form: ReportForm) -> list[str]:
        """实测超过阶段预算的阶段名（未测的阶段不判定）。"""
        return [
            stage
            for stage, cost in self._stages.items()
            if stage in STAGE_BUDGET_MS and cost > STAGE_BUDGET_MS[stage]
        ]

    def as_dict(self, form: ReportForm) -> dict[str, object]:
        return {
            "elapsed_ms": self.elapsed_ms,
            "deadline_ms": form.deadline_ms,
            "within_deadline": self.elapsed_ms <= form.deadline_ms,
            "stages": self.stages,
            "over_budget_stages": self.over_budget(form),
        }


@dataclass(frozen=True, slots=True)
class ReportScope:
    """一份报告的取材范围：本次发布覆盖哪些节点（小局）。

    `completed_games=None` 表示「全部已登记的小局」（赛后形态用）；
    赛中快报必须显式给已完成节点，未完成的节点不进正文（issue #8 范围第 3 条）。
    """

    completed_games: tuple[int, ...] | None = None
    trigger_game_no: int | None = None

    def covers(self, game_no: int) -> bool:
        return self.completed_games is None or game_no in self.completed_games
