"""报告分段结构（需求 §6.6 / 设计 §10.1）。

**段号、标题、顺序不可增删，标题逐字一致。** 缺段即报告不合格 ——
`build_segments` 会直接抛错，渲染层因此永远产不出缺段的页面。

标题的权威来源是 `docs/requirements/DANMU_INTEL_REQUIREMENTS.md` §6.6，
`tests/unit/test_segments.py` 逐字比对两者，防止这里悄悄漂移。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from danmu_intel.common.sources import SourceRef

# 内容性质取值：事实 / 事实+解读 / 解读 / 事实（风险提示）
KIND_FACT = "fact"
KIND_FACT_INTERPRETATION = "fact+interpretation"
KIND_INTERPRETATION = "interpretation"
KIND_FACT_GRAY = "fact(gray)"

KIND_LABELS = {
    KIND_FACT: "事实",
    KIND_FACT_INTERPRETATION: "事实 + 解读",
    KIND_INTERPRETATION: "解读",
    KIND_FACT_GRAY: "事实（风险提示）",
}


@dataclass(frozen=True, slots=True)
class SegmentSpec:
    no: int
    title: str
    kind: str


SEGMENTS: tuple[SegmentSpec, ...] = (
    SegmentSpec(0, "比赛信息", KIND_FACT),
    SegmentSpec(1, "结果总览", KIND_FACT),
    SegmentSpec(2, "逐局复盘", KIND_FACT_INTERPRETATION),
    SegmentSpec(3, "队伍画像", KIND_INTERPRETATION),
    SegmentSpec(4, "人员画像", KIND_INTERPRETATION),
    SegmentSpec(5, "灰信号汇总", KIND_FACT_GRAY),
    SegmentSpec(6, "联赛规律与版本", KIND_INTERPRETATION),
    SegmentSpec(7, "预测验证", KIND_FACT_INTERPRETATION),
    SegmentSpec(8, "盘口讨论", KIND_INTERPRETATION),
    SegmentSpec(9, "情报含义与后续观察点", KIND_INTERPRETATION),
    SegmentSpec(10, "数据与溯源", KIND_FACT),
)

INTERPRETATION_SEGMENTS = tuple(
    spec.no for spec in SEGMENTS if spec.kind in (KIND_INTERPRETATION, KIND_FACT_INTERPRETATION)
)


class MissingSegmentError(ValueError):
    """缺段 / 空段 —— 报告不合格。"""


@dataclass(frozen=True, slots=True)
class Segment:
    no: int
    title: str
    kind: str
    body: str
    sources: tuple[SourceRef, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "no": self.no,
            "title": self.title,
            "kind": self.kind,
            "body": self.body,
            "sources": [ref.as_dict() for ref in self.sources],
        }


def build_segments(bodies: Mapping[int, tuple[str, Sequence[SourceRef]]]) -> list[Segment]:
    """把「段正文 + 来源」组装成完整的十一段；缺段或空段即抛错。"""
    missing = [spec.no for spec in SEGMENTS if spec.no not in bodies]
    if missing:
        raise MissingSegmentError(f"缺段：{','.join(str(no) for no in missing)}")
    extra = sorted(set(bodies) - {spec.no for spec in SEGMENTS})
    if extra:
        raise MissingSegmentError(f"出现未定义的段号：{','.join(str(no) for no in extra)}")
    segments: list[Segment] = []
    for spec in SEGMENTS:
        body, sources = bodies[spec.no]
        if not body.strip():
            raise MissingSegmentError(f"第 {spec.no} 段（{spec.title}）为空段")
        segments.append(
            Segment(no=spec.no, title=spec.title, kind=spec.kind, body=body, sources=tuple(sources))
        )
    return segments
