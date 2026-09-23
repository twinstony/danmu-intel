"""解读层调用点（设计 §10.3 的注入缝）。

解读层**只以事实层为输入**：`interpret(spec, facts)` 拿到的 `facts` 就是这份报告的
事实层（已按形态的取材范围收窄），组装层用 `facts.fact_layer_hash` 给这份输入留
指纹并写进 `reports.fact_layer_hash` —— 解读可回溯到它当时依据的事实层。

本模块只放**缺省**的规则直出实现（T5 定下的注入缝）：T6 的真 LLM 在
`report/llm/interpreter.py`，它实现同一个 `interpret` 方法并把 `state` 标成 `llm`，
组装与渲染层一行不改。生产入口是 `report.llm.interpreter.interpreter_for(conn)`：
配了凭据就用 LLM，没配就回落到这里的 `RuleInterpreter` **并带上降级原因**
（ADR-0003：降级不静默）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from danmu_intel.report.facts import MatchFacts
from danmu_intel.report.forms import LLM_STATE_LLM, LLM_STATE_RULE
from danmu_intel.report.rule_render import interpretation_text
from danmu_intel.report.segments import SegmentSpec


class Interpreter(Protocol):
    """解读层实现：给定段定义与事实层，产出该段的解读正文。

    `note` 是可选属性：降级的原因（没配凭据 / 成本闸 / 调用失败 / 未过校验）。
    """

    state: str

    def interpret(self, spec: SegmentSpec, facts: MatchFacts) -> str: ...


@dataclass(frozen=True, slots=True)
class RuleInterpreter:
    """规则直出兜底（ADR-0003 的 D9）：按时限准时发布，质量降级并如实标注。

    `note` 是降级的**原因**（未配置凭据 / 模型未登记价格 / 成本闸 / 全局降级），
    进报告第 10 段与页面横幅：降级可以，静默降级不行。
    """

    state: str = LLM_STATE_RULE
    note: str = ""

    def interpret(self, spec: SegmentSpec, facts: MatchFacts) -> str:
        return interpretation_text(spec.no, facts)


def ensure_interpreter(interpreter: Interpreter | None) -> Interpreter:
    return RuleInterpreter() if interpreter is None else interpreter
