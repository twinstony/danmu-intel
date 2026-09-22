"""报告分段结构（需求 §6.6 / 设计 §10.1）。

**段号、标题、顺序不可增删，标题逐字一致。** 缺段即报告不合格 ——
`build_segments` 会直接抛错，渲染层因此永远产不出缺段的页面。

每段的**内容性质**只有两种标记：`fact`（事实）与 `interpretation`（解读）。
需求 §6.6 里「事实 + 解读」的段**两种标记并存**（`kinds` 元组有两个元素）；
「事实（风险提示）」是事实标记加一条限定说明（`note`）。标记 + 说明合成
`nature`，与需求 §6.6「内容性质」列逐字一致。

标题与性质的权威来源是 `docs/requirements/DANMU_INTEL_REQUIREMENTS.md` §6.6，
`tests/unit/test_report.py` 逐行比对两者，防止这里悄悄漂移。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from danmu_intel.common.sources import SourceRef

KIND_FACT = "fact"
KIND_INTERPRETATION = "interpretation"
KIND_LABELS = {KIND_FACT: "事实", KIND_INTERPRETATION: "解读"}


@dataclass(frozen=True, slots=True)
class SegmentSpec:
    """一段的固定定义：段号 + 标题 + 内容性质标记（+ 限定说明）。"""

    no: int
    title: str
    kinds: tuple[str, ...]
    note: str | None = None

    @property
    def nature(self) -> str:
        """需求 §6.6「内容性质」列的原文（如「事实 + 解读」「事实（风险提示）」）。"""
        label = " + ".join(KIND_LABELS[kind] for kind in self.kinds)
        return f"{label}（{self.note}）" if self.note else label

    @property
    def has_interpretation(self) -> bool:
        return KIND_INTERPRETATION in self.kinds

    @property
    def has_fact(self) -> bool:
        return KIND_FACT in self.kinds


SEGMENTS: tuple[SegmentSpec, ...] = (
    SegmentSpec(0, "比赛信息", (KIND_FACT,)),
    SegmentSpec(1, "结果总览", (KIND_FACT,)),
    SegmentSpec(2, "逐局复盘", (KIND_FACT, KIND_INTERPRETATION)),
    SegmentSpec(3, "队伍画像", (KIND_INTERPRETATION,)),
    SegmentSpec(4, "人员画像", (KIND_INTERPRETATION,)),
    SegmentSpec(5, "灰信号汇总", (KIND_FACT,), note="风险提示"),
    SegmentSpec(6, "联赛规律与版本", (KIND_INTERPRETATION,)),
    SegmentSpec(7, "预测验证", (KIND_FACT, KIND_INTERPRETATION)),
    SegmentSpec(8, "盘口讨论", (KIND_INTERPRETATION,)),
    SegmentSpec(9, "情报含义与后续观察点", (KIND_INTERPRETATION,)),
    SegmentSpec(10, "数据与溯源", (KIND_FACT,)),
)

ALL_SEGMENT_NOS: tuple[int, ...] = tuple(spec.no for spec in SEGMENTS)
INTERPRETATION_SEGMENTS: tuple[int, ...] = tuple(
    spec.no for spec in SEGMENTS if spec.has_interpretation
)
FACT_SEGMENTS: tuple[int, ...] = tuple(spec.no for spec in SEGMENTS if spec.has_fact)

SPECS_BY_NO: dict[int, SegmentSpec] = {spec.no: spec for spec in SEGMENTS}


class MissingSegmentError(ValueError):
    """缺段 / 空段 / 本形态不发布的段 —— 报告不合格。"""


@dataclass(frozen=True, slots=True)
class Segment:
    no: int
    title: str
    kinds: tuple[str, ...]
    nature: str
    body: str
    sources: tuple[SourceRef, ...] = ()

    @classmethod
    def of(
        cls, spec: SegmentSpec, body: str, sources: Sequence[SourceRef] = ()
    ) -> "Segment":
        return cls(
            no=spec.no,
            title=spec.title,
            kinds=spec.kinds,
            nature=spec.nature,
            body=body,
            sources=tuple(sources),
        )

    @property
    def has_interpretation(self) -> bool:
        return KIND_INTERPRETATION in self.kinds

    @property
    def has_fact(self) -> bool:
        return KIND_FACT in self.kinds

    def as_dict(self) -> dict[str, object]:
        return {
            "no": self.no,
            "title": self.title,
            "kinds": list(self.kinds),
            "nature": self.nature,
            "body": self.body,
            "sources": [ref.as_dict() for ref in self.sources],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "Segment":
        return cls(
            no=int(payload["no"]),  # type: ignore[arg-type]
            title=str(payload["title"]),
            kinds=tuple(str(kind) for kind in payload["kinds"]),  # type: ignore[arg-type]
            nature=str(payload["nature"]),
            body=str(payload["body"]),
            sources=tuple(SourceRef.from_dict(ref) for ref in payload["sources"]),  # type: ignore[arg-type]
        )


def build_segments(
    bodies: Mapping[int, tuple[str, Sequence[SourceRef]]],
    *,
    nos: Sequence[int] | None = None,
) -> list[Segment]:
    """把「段正文 + 来源」组装成一份报告的段列表。

    `nos` 是这份**报告形态**要发布的段号（缺省即全部十一 段）。给的正文与它必须
    严格一致：缺段、空段、或出现本形态不发布的段都会直接抛错 —— 报告不合格。
    """
    expected = list(ALL_SEGMENT_NOS if nos is None else nos)
    unknown = [no for no in expected if no not in SPECS_BY_NO]
    if unknown:
        raise MissingSegmentError(f"出现未定义的段号：{','.join(str(no) for no in unknown)}")
    missing = [no for no in expected if no not in bodies]
    if missing:
        raise MissingSegmentError(f"缺段：{','.join(str(no) for no in missing)}")
    extra = sorted(set(bodies) - set(expected))
    if extra:
        unknown_extra = [no for no in extra if no not in SPECS_BY_NO]
        if unknown_extra:
            raise MissingSegmentError(
                f"出现未定义的段号：{','.join(str(no) for no in unknown_extra)}"
            )
        raise MissingSegmentError(f"出现本形态不发布的段号：{','.join(str(no) for no in extra)}")
    segments: list[Segment] = []
    for no in expected:
        body, sources = bodies[no]
        spec = SPECS_BY_NO[no]
        if not body.strip():
            raise MissingSegmentError(f"第 {no} 段（{spec.title}）为空段")
        segments.append(Segment.of(spec, body, sources))
    return segments
