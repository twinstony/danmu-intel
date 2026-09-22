"""报告的事实输入（设计 §10.3：解读层的输入只有事实层产物）。

`MatchFacts` 是流水线在「原始记录 + 切片 + 规则统计」之上组装好的一个不可变
快照。规则直出渲染（`rule_render.py`）与 HTML 渲染（`html.py`）都只读它。

T4 把统计全集、终局判定、灰信号都挂在这里：**渲染层没有任何自己算数的入口**，
它只能引用事实层已经算出来的东西（ACI-11 / NFR-Q-5 的落点）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from danmu_intel.common.config import StatsConfig
from danmu_intel.common.matches import Match
from danmu_intel.slice.manual import SliceWindow
from danmu_intel.stats.basic import RawLine
from danmu_intel.stats.final import FinalJudgement, SignalFact
from danmu_intel.stats.gray import GraySignal, reportable


@dataclass(frozen=True, slots=True)
class GameFacts:
    """一个小局：切片边界 + **该边界内的**原始行 + 统计全集。"""

    window: SliceWindow
    lines: tuple[RawLine, ...]
    metrics: dict[str, dict[str, object]]


@dataclass(frozen=True, slots=True)
class SegmentFacts:
    """一个落盘文件的取证事实（`danmu_segments` 行）。"""

    rel_path: str
    platform: str
    room_id: str
    msg_count: int
    first_ts: int | None
    last_ts: int | None
    sha256: str


@dataclass(frozen=True, slots=True)
class MatchFacts:
    match: Match
    games: tuple[GameFacts, ...]
    all_lines: tuple[RawLine, ...]
    segments: tuple[SegmentFacts, ...]
    algo_version: str
    data_root: Path
    generated_at: int
    stats_config: StatsConfig
    final_judgement: FinalJudgement
    gray_signals: tuple[GraySignal, ...]
    signal_facts: tuple[SignalFact, ...]

    @property
    def platforms(self) -> list[str]:
        return sorted({segment.platform for segment in self.segments})

    @property
    def room_ids(self) -> list[str]:
        return sorted({segment.room_id for segment in self.segments})

    @property
    def reportable_gray_signals(self) -> tuple[GraySignal, ...]:
        """可进报告的灰信号：只有达门槛的（需求 §6.5 第 4/6 条）。"""
        return reportable(self.gray_signals)
