"""报告组装（需求 §6.6，设计 §10.1/§10.3）：事实层 + 解读层 → 段级 `content_json`。

一条硬规矩：**解读层的输入只有事实层产物**。组装时先算 `fact_layer_hash(facts)`
（解读层输入的指纹），再让解读层只用同一份 `facts` 说话；解读段一律带
「解读，非事实」标注（§6.9 第 2 条），因此读者不会把分析当事实。

段集由报告形态决定（`forms.ReportForm.segments`）：赛中快报只发布不依赖终局的段，
其余形态全十一段。缺段、空段、多段都会在 `build_segments` 处直接抛错。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping, Sequence

from danmu_intel.common.sources import SourceRef
from danmu_intel.report.facts import MatchFacts, fact_layer_hash
from danmu_intel.report.forms import ReportForm, ReportHeader, Timing
from danmu_intel.report.interpreter import Interpreter, ensure_interpreter
from danmu_intel.report.rule_render import INTERPRETATION_MARK, fact_body, sources_for
from danmu_intel.report.segments import (
    SPECS_BY_NO,
    MissingSegmentError,
    Segment,
    build_segments,
)


@dataclass(frozen=True, slots=True)
class ReportContent:
    """一份报告的完整内容（`reports.content_json` 的载体）。

    段级结构，顺序即发布顺序；`meta` 是页面头部要用的取材事实（与正文同源，
    避免页面另算一遍数字）。
    """

    match_id: int
    kind: str
    version: int
    generated_at: int
    fact_layer_hash: str
    llm_state: str
    meta: dict[str, object]
    segments: tuple[Segment, ...]

    @property
    def header(self) -> ReportHeader:
        return ReportHeader(
            kind=self.kind,
            version=self.version,
            fact_layer_hash=self.fact_layer_hash,
            llm_state=self.llm_state,
        )

    def segment(self, no: int) -> Segment:
        for segment in self.segments:
            if segment.no == no:
                return segment
        raise KeyError(f"报告里没有第 {no} 段")

    @property
    def interpretation_segments(self) -> tuple[Segment, ...]:
        return tuple(segment for segment in self.segments if segment.has_interpretation)

    def as_dict(self) -> dict[str, object]:
        return {
            "match_id": self.match_id,
            "kind": self.kind,
            "version": self.version,
            "generated_at": self.generated_at,
            "fact_layer_hash": self.fact_layer_hash,
            "llm_state": self.llm_state,
            "meta": self.meta,
            "segments": [segment.as_dict() for segment in self.segments],
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "ReportContent":
        return cls(
            match_id=int(payload["match_id"]),  # type: ignore[arg-type]
            kind=str(payload["kind"]),
            version=int(payload["version"]),  # type: ignore[arg-type]
            generated_at=int(payload["generated_at"]),  # type: ignore[arg-type]
            fact_layer_hash=str(payload["fact_layer_hash"]),
            llm_state=str(payload["llm_state"]),
            meta=dict(payload["meta"]),  # type: ignore[arg-type]
            segments=tuple(
                Segment.from_dict(item) for item in payload["segments"]  # type: ignore[arg-type]
            ),
        )


def report_meta(facts: MatchFacts, *, trigger_game_no: int | None = None) -> dict[str, object]:
    match = facts.match
    return {
        "league": match.league,
        "match_title": match.title,
        "state": match.state,
        "danmu_count": len(facts.all_lines),
        "algo_version": facts.algo_version,
        "covered_games": list(facts.game_nos),
        "excluded_games": list(facts.excluded_games),
        "trigger_game_no": trigger_game_no,
    }


def build_content(
    facts: MatchFacts,
    *,
    form: ReportForm,
    version: int,
    generated_at: int,
    interpreter: Interpreter | None = None,
    trigger_game_no: int | None = None,
    timing: Timing | None = None,
) -> ReportContent:
    """组装一份报告的内容。纯函数（除可注入的解读层与计时器）。"""
    speaker = ensure_interpreter(interpreter)
    header = ReportHeader(
        kind=form.kind,
        version=version,
        fact_layer_hash=fact_layer_hash(facts),
        llm_state=speaker.state,
    )
    specs = [SPECS_BY_NO[no] for no in form.segments]
    bodies: dict[int, tuple[str, Sequence[SourceRef]]] = {}

    fact_parts: dict[int, str] = {
        spec.no: fact_body(spec.no, facts, header) for spec in specs if spec.has_fact
    }
    if timing is not None:
        timing.mark("fact_assembly")

    interpretation_parts: dict[int, str] = {}
    for spec in specs:
        if not spec.has_interpretation:
            continue
        text = speaker.interpret(spec, facts).strip()
        if not text:
            raise MissingSegmentError(f"第 {spec.no} 段（{spec.title}）的解读为空")
        interpretation_parts[spec.no] = f"{INTERPRETATION_MARK}{text}"
    if timing is not None:
        timing.mark("interpretation")

    for spec in specs:
        parts = []
        if spec.no in fact_parts:
            parts.append(fact_parts[spec.no])
        if spec.no in interpretation_parts:
            parts.append(interpretation_parts[spec.no])
        bodies[spec.no] = ("\n\n".join(part for part in parts if part.strip()), sources_for(spec.no, facts))

    segments = build_segments(bodies, nos=form.segments)
    if timing is not None:
        timing.mark("validation")

    return ReportContent(
        match_id=facts.match.id,
        kind=form.kind,
        version=version,
        generated_at=generated_at,
        fact_layer_hash=header.fact_layer_hash,
        llm_state=header.llm_state,
        meta=report_meta(facts, trigger_game_no=trigger_game_no),
        segments=tuple(segments),
    )
