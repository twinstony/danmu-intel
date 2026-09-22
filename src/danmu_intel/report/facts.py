"""报告的事实输入（设计 §10.3：解读层的输入只有事实层产物）。

`MatchFacts` 是流水线在「原始记录 + 切片 + 规则统计」之上组装好的一个不可变
快照。规则直出渲染（`rule_render.py`）与 HTML 渲染（`html.py`）都只读它。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from danmu_intel.common.matches import Match
from danmu_intel.slice.manual import SliceWindow
from danmu_intel.stats.basic import RawLine


@dataclass(frozen=True, slots=True)
class GameFacts:
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

    @property
    def platforms(self) -> list[str]:
        return sorted({segment.platform for segment in self.segments})

    @property
    def room_ids(self) -> list[str]:
        return sorted({segment.room_id for segment in self.segments})
