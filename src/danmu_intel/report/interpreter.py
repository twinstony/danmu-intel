"""解读层调用点（设计 §10.3 的注入缝）。

解读层**只以事实层为输入**：`interpret(spec, facts)` 拿到的 `facts` 就是这份报告的
事实层（已按形态的取材范围收窄），组装层用 `facts.fact_layer_hash` 给这份输入留
指纹并写进 `reports.fact_layer_hash` —— 解读可回溯到它当时依据的事实层。

本票**不接真 LLM**（T6 才接 DeepSeek）：缺省实现是规则直出，`llm_state` 如实标为
`rule_fallback`（ADR-0003：降级不静默）。T6 只需实现同一个 `interpret` 方法并把
`state` 标成 `llm`，组装与渲染层一行不改。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from danmu_intel.report.facts import MatchFacts
from danmu_intel.report.rule_render import interpretation_text
from danmu_intel.report.segments import SegmentSpec

LLM_STATE_LLM = "llm"
LLM_STATE_RULE = "rule_fallback"


class Interpreter(Protocol):
    """解读层实现：给定段定义与事实层，产出该段的解读正文。"""

    state: str

    def interpret(self, spec: SegmentSpec, facts: MatchFacts) -> str: ...


@dataclass(frozen=True, slots=True)
class RuleInterpreter:
    """规则直出兜底（ADR-0003 的 D9）：按时限准时发布，质量降级并如实标注。"""

    state: str = LLM_STATE_RULE

    def interpret(self, spec: SegmentSpec, facts: MatchFacts) -> str:
        return interpretation_text(spec.no, facts)


def ensure_interpreter(interpreter: Interpreter | None) -> Interpreter:
    return RuleInterpreter() if interpreter is None else interpreter
