"""LLM 解读层：受约束调用 → 反幻觉校验 → 重试 → 降级（ADR-0003 / ADR-0014）。

实现 T5 定下的 `Interpreter` 协议（`state` + `interpret`），因此组装与渲染层一行不改
就换上了真 LLM。每次 `interpret` 的完整路径：

1. **调用前判闸**：成本硬闸（单场 ¥0.3 / 当日 ¥10）+ 全局降级（账本里连续 ≥3 次失败）
   → 不调用、记一行 `gated`、报警（一份报告只报一次）、该段规则直出；
2. **调用**：单次超时 25 秒，且不超过解读阶段的剩余预算；
3. **校验输出契约**：JSON 解析 + 段号键集必须正好是本次要求的那一段；
4. **反幻觉校验**：数字/比分/百分比/名称逐个比对事实层，出现新事实即作废；
5. **重试 1 次**：把违规项清单回灌给模型（只对"调通了但内容不合格"重试）；
6. **降级**：仍不合格则该段用规则直出文本，`state='rule_fallback'` 并记下原因。

超时/断网/API 报错**不重试**（快报的 2 分钟比"多试一次"重要），直接该段降级。
任何一次降级都会把整份报告标成 `rule_fallback`（那一段确实是规则文本），
并把原因记进 `note` → 报告第 10 段与页面横幅（降级不静默）。
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from danmu_intel.common.credentials import CredentialError, get_secret, require_secret
from danmu_intel.report.facts import MatchFacts, fact_layer_payload
from danmu_intel.report.forms import LLM_STATE_LLM, LLM_STATE_RULE, STAGE_BUDGET_MS
from danmu_intel.report.interpreter import Interpreter, RuleInterpreter
from danmu_intel.report.llm import alerts, ledger
from danmu_intel.report.llm.client import (
    DEFAULT_BASE_URL,
    DeepSeekClient,
    LLMError,
    LLMProtocolError,
    LLMReply,
    LLMTimeout,
    LLMUnavailable,
    parse_segment_text,
)
from danmu_intel.report.llm.cost import (
    DAILY_LIMIT_CNY,
    DEFAULT_MODEL,
    MATCH_LIMIT_CNY,
    estimate_cost_cny,
    gate,
    price_for,
)
from danmu_intel.report.llm.prompts import PROMPT_VERSION, PromptSet, load_prompt_set, render_user
from danmu_intel.report.llm.verify import correction_note, describe, verify_text
from danmu_intel.report.rule_render import interpretation_text
from danmu_intel.report.segments import SegmentSpec

#: 解读阶段的总预算（设计 §10.2 的预算表；阶段名与 `forms.STAGE_BUDGET_MS` 一致）。
DEFAULT_BUDGET_MS = STAGE_BUDGET_MS["interpretation"]

#: 剩余预算少于这个数就不值得再发起一次调用（该段直接规则直出）。
MIN_CALL_S = 3.0

API_KEY_NAME = "DEEPSEEK_API_KEY"
MODEL_NAME = "DEEPSEEK_MODEL"
BASE_URL_NAME = "DEEPSEEK_BASE_URL"


class ChatClient(Protocol):
    """LLM 调用点注入缝（测试注入 fake：正常 / 幻觉 / 超时 / 报错）。"""

    model: str

    def complete(self, *, system: str, user: str, timeout_s: float | None = None) -> LLMReply: ...


def facts_json(facts: MatchFacts) -> str:
    """模型看到的受约束输入：**只有**事实层的规范 JSON。"""
    return json.dumps(fact_layer_payload(facts), ensure_ascii=False, sort_keys=True, indent=1)


@dataclass
class LLMInterpreter:
    """把 LLM 接进解读层（`Interpreter` 协议的另一半）。"""

    conn: sqlite3.Connection
    client: ChatClient
    prompt_set: PromptSet = field(default_factory=load_prompt_set)
    budget_ms: int = DEFAULT_BUDGET_MS
    match_limit: float = MATCH_LIMIT_CNY
    daily_limit: float = DAILY_LIMIT_CNY
    clock: Callable[[], float] | None = None
    state: str = LLM_STATE_LLM
    calls: int = 0
    spent_cny: float = 0.0
    _reasons: list[str] = field(default_factory=list)
    _alerted: set[str] = field(default_factory=set)
    _deadline: float | None = None
    _gated_recorded: bool = False

    @property
    def note(self) -> str:
        """降级原因（去重、保持发生顺序）：进报告第 10 段与页面横幅。"""
        return "；".join(dict.fromkeys(self._reasons))

    # —— Interpreter 协议 ——

    def interpret(self, spec: SegmentSpec, facts: MatchFacts) -> str:
        text = self._try_llm(spec, facts)
        if text is not None:
            return text
        return interpretation_text(spec.no, facts)

    # —— 内部 ——

    def _now(self) -> float:
        return (self.clock or time.monotonic)()

    def _now_ms(self) -> int:
        return int(time.time() * 1000)

    def _remaining_s(self) -> float:
        if self._deadline is None:
            self._deadline = self._now() + self.budget_ms / 1000
        return self._deadline - self._now()

    def _degrade(self, reason: str) -> None:
        self.state = LLM_STATE_RULE
        if reason and reason not in self._reasons:
            self._reasons.append(reason)

    def _try_llm(self, spec: SegmentSpec, facts: MatchFacts) -> str | None:
        match_id = facts.match.id
        blocked = self._preflight(match_id)
        if blocked is not None:
            self._degrade(blocked)
            return None

        correction = ""
        for attempt in (1, 2):
            remaining = self._remaining_s()
            if remaining < MIN_CALL_S:
                self._degrade(
                    f"解读阶段预算（{self.budget_ms // 1000} 秒）已用尽，第 {spec.no} 段起规则直出"
                )
                return None
            reply = self._call(spec, facts, match_id, attempt, correction, remaining)
            if reply is None:
                return None
            try:
                text = parse_segment_text(reply.text, segment_no=spec.no)
            except LLMProtocolError as exc:
                self._reject(spec, match_id, reply, str(exc), attempt=attempt)
                correction = (
                    "上一次输出不符合约定：必须是 JSON 对象，键正好是本次要求的段号，"
                    "正文非空。请只输出那个 JSON。\n"
                )
                continue
            violations = verify_text(text, facts)
            if not violations:
                self._record(match_id, spec.no, outcome=ledger.OUTCOME_OK, reply=reply)
                return text
            self._reject(
                spec,
                match_id,
                reply,
                f"含事实层之外的内容：{describe(violations)}",
                attempt=attempt,
            )
            correction = correction_note(violations)
        self._degrade(f"第 {spec.no} 段重试后仍含事实层之外的内容，该段规则直出")
        return None

    def _preflight(self, match_id: int) -> str | None:
        """调用前的三道闸：价格表、全局降级、成本硬闸。返回降级原因，或 None 表示可以调用。"""
        try:
            price_for(self.client.model)
        except LookupError as exc:
            return f"模型未登记价格，拒绝调用（{exc}）"
        outcomes = ledger.recent_outcomes(self.conn)
        if ledger.is_degraded(outcomes):
            reason = (
                f"连续 {ledger.consecutive_failures(outcomes)} 次调用失败，"
                "解读能力已全局降级（站点显示降级状态）"
            )
            self._alert_once(
                alerts.UNAVAILABLE,
                match_id=match_id,
                severity="critical",
                detail={"consecutive_failures": ledger.consecutive_failures(outcomes)},
            )
            self._record_gated(match_id, None, reason)
            return reason

        totals = ledger.spend(self.conn, match_id, now_ms=self._now_ms())
        decision = gate(totals, match_limit=self.match_limit, daily_limit=self.daily_limit)
        if not decision.allowed:
            self._alert_once(
                alerts.COST_GATE,
                match_id=match_id,
                severity="warning",
                detail={
                    "limit_kind": decision.limit_kind,
                    "spent_match_cny": round(totals.match_cny, 4),
                    "spent_day_cny": round(totals.day_cny, 4),
                    "reason": decision.reason,
                },
            )
            self._record_gated(match_id, None, decision.reason)
            return decision.reason
        return None

    def _call(
        self,
        spec: SegmentSpec,
        facts: MatchFacts,
        match_id: int,
        attempt: int,
        correction: str,
        remaining_s: float,
    ) -> LLMReply | None:
        user = render_user(
            self.prompt_set, spec, facts_json=facts_json(facts), correction=correction
        )
        try:
            return self.client.complete(
                system=self.prompt_set.system, user=user, timeout_s=remaining_s
            )
        except LLMTimeout as exc:
            self._record(
                match_id, spec.no, outcome=ledger.OUTCOME_TIMEOUT, reason=str(exc), attempt=attempt
            )
            self._degrade(f"第 {spec.no} 段调用超时，该段规则直出")
            return None
        except LLMUnavailable as exc:
            self._record(
                match_id, spec.no, outcome=ledger.OUTCOME_ERROR, reason=str(exc), attempt=attempt
            )
            self._degrade(f"第 {spec.no} 段调用失败（{exc}），该段规则直出")
            return None

    def _reject(
        self, spec: SegmentSpec, match_id: int, reply: LLMReply, reason: str, *, attempt: int = 1
    ) -> None:
        self._record(
            match_id,
            spec.no,
            outcome=ledger.OUTCOME_REJECTED,
            reason=reason,
            attempt=attempt,
            reply=reply,
        )

    def _record(
        self,
        match_id: int,
        segment_no: int | None,
        *,
        outcome: str,
        reason: str = "",
        attempt: int = 1,
        reply: LLMReply | None = None,
    ) -> None:
        cost = 0.0
        prompt_tokens = completion_tokens = cache_hit = latency = 0
        if reply is not None:
            prompt_tokens = reply.prompt_tokens
            completion_tokens = reply.completion_tokens
            cache_hit = reply.cache_hit_tokens
            latency = reply.latency_ms
            cost = estimate_cost_cny(
                self.client.model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cache_hit_tokens=cache_hit,
            )
            self.spent_cny += cost
        self.calls += 1
        ledger.record(
            self.conn,
            match_id=match_id,
            segment_no=segment_no,
            model=self.client.model,
            prompt_version=self.prompt_set.version,
            outcome=outcome,
            created_at=self._now_ms(),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cache_hit_tokens=cache_hit,
            cost_cny=cost,
            latency_ms=latency,
            reason=f"attempt {attempt}：{reason}" if reason else None,
        )

    def _record_gated(self, match_id: int, segment_no: int | None, reason: str) -> None:
        """被闸住也记账（一行就够）：账本要能回答"为什么这一场没花到钱"。"""
        if self._gated_recorded:
            return
        self._gated_recorded = True
        self._record(match_id, segment_no, outcome=ledger.OUTCOME_GATED, reason=reason)

    def _alert_once(self, kind: str, *, match_id: int, severity: str, detail: dict[str, object]) -> None:
        """同一份报告里同一类报警只写一行（避免 7 段刷出 7 条通知）。"""
        if kind in self._alerted:
            return
        self._alerted.add(kind)
        alerts.alert(self.conn, kind, match_id=match_id, severity=severity, detail=detail)


def interpreter_for(conn: sqlite3.Connection, *, clock: Callable[[], float] | None = None) -> Interpreter:
    """生产入口：配了凭据就用 LLM，没配/价格未登记就规则直出（并说明原因，不静默）。

    凭据缺失或模型没登记价格都不是"报错退出"的理由——报告必须按时发布，
    只是解读能力降级，因此返回带 `note` 的规则直出实现。
    """
    try:
        api_key = require_secret(API_KEY_NAME)
    except CredentialError as exc:
        # 页面是公开产物：降级原因里只写"缺什么/怎么修"，不写本机绝对路径
        reason = (
            "凭据文件权限不安全（必须是 0600）"
            if "权限" in str(exc)
            else f"未配置 {API_KEY_NAME}（仓库外数据目录的 .env，0600）"
        )
        return RuleInterpreter(note=reason)
    model = get_secret(MODEL_NAME) or DEFAULT_MODEL
    try:
        price_for(model)
    except LookupError as exc:
        return RuleInterpreter(note=f"模型未登记价格，拒绝调用（{model}）")
    return LLMInterpreter(
        conn=conn,
        client=DeepSeekClient(
            api_key=api_key, model=model, base_url=get_secret(BASE_URL_NAME) or DEFAULT_BASE_URL, clock=clock
        ),
        prompt_set=load_prompt_set(PROMPT_VERSION),
        clock=clock,
    )
