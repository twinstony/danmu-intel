"""LLM 解读层测试：注入缝（正常 / 幻觉 / 超时 / 报错）、成本硬闸、全局降级、降级不静默。

注入缝在**调用点**（`ChatClient`）：四种假返回值覆盖验收标准的四种情形，
全程不连外网（NFR-GA-4）。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

import pytest

from danmu_intel.common.credentials import CredentialError
from danmu_intel.common.notifications import recent as recent_events
from danmu_intel.pipeline import collect_facts
from danmu_intel.report.forms import LLM_STATE_LLM, LLM_STATE_RULE
from danmu_intel.report.interpreter import RuleInterpreter
from danmu_intel.report.llm import alerts, ledger as llm_ledger
from danmu_intel.report.llm.client import LLMReply, LLMTimeout, LLMUnavailable
from danmu_intel.report.llm.cost import DAILY_LIMIT_CNY, MATCH_LIMIT_CNY
from danmu_intel.report.llm.interpreter import (
    API_KEY_NAME,
    MIN_CALL_S,
    LLMInterpreter,
    facts_json,
    interpreter_for,
)
from danmu_intel.report.llm.prompts import load_prompt_set
from danmu_intel.report.rule_render import interpretation_text
from danmu_intel.report.segments import INTERPRETATION_SEGMENTS, SPECS_BY_NO

FAKE_KEY = "sk-" + "interp" * 6
OK_TEXT = "从弹幕看，G1 的讨论最集中（55 条），这只是注意力层面的观察。"


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@dataclass
class FakeClient:
    """假 LLM：按脚本返回 `LLMReply` 或抛异常；记录每次调用的入参。"""

    model: str = "deepseek-v4-flash"
    script: list[object] = field(default_factory=list)
    calls: list[dict[str, object]] = field(default_factory=list)
    clock: FakeClock | None = None
    seconds_per_call: float = 0.0
    echo_text: str | None = None  # 从提示词里读出段号，回一段合规的解读

    def complete(self, *, system: str, user: str, timeout_s: float | None = None) -> LLMReply:
        self.calls.append({"system": system, "user": user, "timeout_s": timeout_s})
        if self.clock is not None and self.seconds_per_call:
            self.clock.advance(self.seconds_per_call)
        if self.script:
            item = self.script.pop(0)
        elif self.echo_text is not None:
            spec_no = int(re.search(r"\*\*(\d+) 号段", user).group(1))  # type: ignore[union-attr]
            item = reply(segment_payload(spec_no, f"第 {spec_no} 段：{self.echo_text}"))
        else:
            item = reply(OK_TEXT)
        if isinstance(item, Exception):
            raise item
        return item  # type: ignore[return-value]


def reply(text: str, *, prompt_tokens=4000, completion_tokens=400, cache_hit=3000) -> LLMReply:
    return LLMReply(
        text=text,
        model="deepseek-v4-flash",
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cache_hit_tokens=cache_hit,
        latency_ms=1200,
    )


def segment_payload(no: int, text: str) -> str:
    return json.dumps({"segments": {str(no): text}}, ensure_ascii=False)


@pytest.fixture
def facts(ledger):
    return collect_facts(ledger.conn, ledger.match_id, data_root=ledger.data_root)


def build(conn, client, **kwargs) -> LLMInterpreter:
    return LLMInterpreter(conn=conn, client=client, prompt_set=load_prompt_set(), **kwargs)


def run_all(interpreter: LLMInterpreter, facts, nos=INTERPRETATION_SEGMENTS) -> dict[int, str]:
    return {no: interpreter.interpret(SPECS_BY_NO[no], facts) for no in nos}


# —— 正常路径 ——


def test_happy_path_uses_the_llm_text_and_marks_llm(ledger, facts):
    client = FakeClient(echo_text=OK_TEXT)
    interpreter = build(ledger.conn, client)

    texts = run_all(interpreter, facts)

    assert interpreter.state == LLM_STATE_LLM
    assert interpreter.note == ""
    assert all(OK_TEXT in text for text in texts.values())
    assert len(client.calls) == len(INTERPRETATION_SEGMENTS)
    assert interpreter.calls == len(INTERPRETATION_SEGMENTS)

    rows = llm_ledger.calls_for_match(ledger.conn, ledger.match_id)
    assert {row.outcome for row in rows} == {llm_ledger.OUTCOME_OK}
    assert {row.prompt_version for row in rows} == {"v1"}
    assert {row.segment_no for row in rows} == set(INTERPRETATION_SEGMENTS)
    assert all(row.prompt_tokens == 4000 and row.completion_tokens == 400 for row in rows)
    assert interpreter.spent_cny == pytest.approx(sum(row.cost_cny for row in rows))


def test_prompt_carries_only_the_fact_layer_and_the_single_segment_key(ledger, facts):
    client = FakeClient(script=[reply(segment_payload(3, OK_TEXT))])
    build(ledger.conn, client).interpret(SPECS_BY_NO[3], facts)

    call = client.calls[0]
    assert "不得引入新事实" in str(call["system"])
    assert facts_json(facts) in str(call["user"])
    assert '"segments": {"3"' in str(call["user"]).replace("\n", " ")
    assert call["timeout_s"] == pytest.approx(25.0)  # 单次调用 25 秒


def test_cost_is_recorded_per_call_and_below_the_gate(ledger, facts):
    client = FakeClient(echo_text=OK_TEXT)
    interpreter = build(ledger.conn, client)
    run_all(interpreter, facts)

    totals = llm_ledger.spend(ledger.conn, ledger.match_id, now_ms=llm_ledger.calls_for_match(ledger.conn, ledger.match_id)[0].created_at)
    assert 0 < totals.match_cny < MATCH_LIMIT_CNY
    assert interpreter.spent_cny == pytest.approx(totals.match_cny)


# —— 幻觉：重试 1 次，再失败降级 ——


def test_hallucination_is_retried_once_and_then_degrades(ledger, facts):
    fake = segment_payload(3, "从弹幕看，T1 以 3:0 取胜，Faker 稳定。")
    client = FakeClient(script=[reply(fake), reply(fake)])
    interpreter = build(ledger.conn, client)

    text = interpreter.interpret(SPECS_BY_NO[3], facts)

    assert len(client.calls) == 2  # 重试 1 次
    assert text == interpretation_text(3, facts)  # 规则直出兜底
    assert interpreter.state == LLM_STATE_RULE
    assert "第 3 段重试后仍不合格" in interpreter.note
    assert "引入事实层之外的" in interpreter.note and "比分" in interpreter.note
    # 降级原因会进公开页面：不能把模型编造的比分/名字搬到页面上
    assert "3:0" not in interpreter.note and "Faker" not in interpreter.note

    rows = llm_ledger.calls_for_match(ledger.conn, ledger.match_id)
    assert "Faker" in (rows[0].reason or "")  # 原文只进账本
    assert [row.outcome for row in rows] == [llm_ledger.OUTCOME_REJECTED, llm_ledger.OUTCOME_REJECTED]
    assert "3:0" in (rows[0].reason or "") and "Faker" in (rows[0].reason or "")
    assert rows[0].cost_cny > 0  # 被拒的调用照样花了钱，账本要如实记
    # 第二次调用带上了纠正说明
    assert "上一次输出被拒" in str(client.calls[1]["user"])


def test_hallucination_followed_by_a_clean_retry_keeps_llm_state(ledger, facts):
    client = FakeClient(
        script=[reply(segment_payload(3, "比分 3:0，Faker 稳定。")), reply(segment_payload(3, OK_TEXT))]
    )
    interpreter = build(ledger.conn, client)

    text = interpreter.interpret(SPECS_BY_NO[3], facts)

    assert text == OK_TEXT
    assert interpreter.state == LLM_STATE_LLM and interpreter.note == ""
    assert [row.outcome for row in llm_ledger.calls_for_match(ledger.conn, ledger.match_id)] == [
        llm_ledger.OUTCOME_REJECTED,
        llm_ledger.OUTCOME_OK,
    ]


def test_broken_json_contract_is_retried_and_then_degrades(ledger, facts):
    client = FakeClient(script=[reply("这不是 JSON"), reply(segment_payload(9, OK_TEXT))])
    interpreter = build(ledger.conn, client)
    # 第二次回了 9 号段，但当前请求的是 3 号段 → 键集不符 → 降级
    text = interpreter.interpret(SPECS_BY_NO[3], facts)
    assert text == interpretation_text(3, facts)
    assert interpreter.state == LLM_STATE_RULE
    rows = llm_ledger.calls_for_match(ledger.conn, ledger.match_id)
    assert [row.outcome for row in rows] == [llm_ledger.OUTCOME_REJECTED, llm_ledger.OUTCOME_REJECTED]
    assert "不是 JSON" in (rows[0].reason or "")


def test_extra_segment_key_is_refused(ledger, facts):
    payload = json.dumps({"segments": {"3": OK_TEXT, "4": OK_TEXT}}, ensure_ascii=False)
    client = FakeClient(script=[reply(payload), reply(payload)])
    interpreter = build(ledger.conn, client)
    assert interpreter.interpret(SPECS_BY_NO[3], facts) == interpretation_text(3, facts)
    assert "段号不符" in (llm_ledger.calls_for_match(ledger.conn, ledger.match_id)[0].reason or "")


def test_identity_leak_is_refused(ledger, facts):
    leak = facts.all_lines[0].event.user_hash
    client = FakeClient(script=[reply(segment_payload(3, f"观众 {leak} 在带节奏。"))] * 2)
    interpreter = build(ledger.conn, client)
    assert interpreter.interpret(SPECS_BY_NO[3], facts) == interpretation_text(3, facts)
    assert interpreter.state == LLM_STATE_RULE


# —— 超时 / 报错：不重试，直接降级 ——


def test_timeout_degrades_without_retrying(ledger, facts):
    client = FakeClient(script=[LLMTimeout("LLM 调用超时（25 秒）")])
    interpreter = build(ledger.conn, client)

    text = interpreter.interpret(SPECS_BY_NO[3], facts)

    assert len(client.calls) == 1  # 超时不重试（快报的 2 分钟比多试一次重要）
    assert text == interpretation_text(3, facts)
    assert interpreter.state == LLM_STATE_RULE
    assert "调用超时" in interpreter.note
    rows = llm_ledger.calls_for_match(ledger.conn, ledger.match_id)
    assert [row.outcome for row in rows] == [llm_ledger.OUTCOME_TIMEOUT]
    assert rows[0].cost_cny == 0 and rows[0].reason


def test_api_error_degrades_without_retrying(ledger, facts):
    client = FakeClient(script=[LLMUnavailable("LLM 接口不可达（URLError）")])
    interpreter = build(ledger.conn, client)

    text = interpreter.interpret(SPECS_BY_NO[3], facts)

    assert len(client.calls) == 1
    assert text == interpretation_text(3, facts)
    assert "调用失败" in interpreter.note
    assert [row.outcome for row in llm_ledger.calls_for_match(ledger.conn, ledger.match_id)] == [
        llm_ledger.OUTCOME_ERROR
    ]


def test_offline_report_still_produces_every_segment(ledger, facts):
    """断网也要有完整报告：每段都降级，且连丢三次后不再尝试（全局降级）。"""
    client = FakeClient(script=[LLMUnavailable("断网")] * len(INTERPRETATION_SEGMENTS))
    interpreter = build(ledger.conn, client)

    texts = run_all(interpreter, facts)

    assert interpreter.state == LLM_STATE_RULE
    assert all(text.strip() for text in texts.values())
    assert len(client.calls) == llm_ledger.DEGRADE_AFTER_FAILURES
    assert "全局降级" in interpreter.note


# —— 成本硬闸 ——


def seed_spend(ledger, *, cost: float, match_id: int | None = None, created_at: int | None = None) -> None:
    llm_ledger.record(
        ledger.conn,
        match_id=ledger.match_id if match_id is None else match_id,
        segment_no=1,
        model="deepseek-v4-flash",
        prompt_version="v1",
        outcome=llm_ledger.OUTCOME_OK,
        created_at=created_at or 1_790_064_400_000,
        cost_cny=cost,
    )


def test_match_cost_gate_blocks_before_calling_and_alerts(ledger, facts):
    seed_spend(ledger, cost=MATCH_LIMIT_CNY)
    client = FakeClient()
    interpreter = build(ledger.conn, client)

    texts = run_all(interpreter, facts)

    assert client.calls == []  # 调用前就闸住了
    assert interpreter.state == LLM_STATE_RULE
    assert "单场成本已达" in interpreter.note
    assert all(text.strip() for text in texts.values())
    assert interpreter.spent_cny == 0

    events = alerts_rows(ledger)
    assert len(events) == 1  # 一份报告只报一次，不按段刷
    assert events[0].kind == alerts.COST_GATE and events[0].severity == "warning"
    assert events[0].payload["limit_kind"] == "match"
    assert events[0].payload["match_id"] == ledger.match_id


def test_daily_cost_gate_blocks_before_calling(ledger, facts):
    """当日 ¥10 是跨场累计的：今天别的比赛花超了，这一场也不能再调。"""
    seed_spend(ledger, cost=DAILY_LIMIT_CNY, match_id=999, created_at=int(time.time() * 1000))
    client = FakeClient()
    interpreter = build(ledger.conn, client)

    interpreter.interpret(SPECS_BY_NO[3], facts)

    assert client.calls == []
    assert "当日成本已达" in interpreter.note
    events = alerts_rows(ledger)
    assert events[0].payload["limit_kind"] == "day"
    assert events[0].payload["spent_day_cny"] >= DAILY_LIMIT_CNY
    assert events[0].payload["spent_match_cny"] == 0


def test_gated_calls_are_recorded_once_as_gated(ledger, facts):
    seed_spend(ledger, cost=MATCH_LIMIT_CNY)
    interpreter = build(ledger.conn, FakeClient())
    run_all(interpreter, facts)
    rows = llm_ledger.calls_for_match(ledger.conn, ledger.match_id)
    gated = [row for row in rows if row.outcome == llm_ledger.OUTCOME_GATED]
    assert len(gated) == 1
    assert gated[0].cost_cny == 0 and gated[0].segment_no is None


def test_gate_limits_are_configurable(ledger, facts):
    seed_spend(ledger, cost=0.01)
    client = FakeClient(script=[reply(segment_payload(3, OK_TEXT))])
    interpreter = build(ledger.conn, client, match_limit=0.005)
    assert interpreter.interpret(SPECS_BY_NO[3], facts) == interpretation_text(3, facts)
    assert client.calls == []


# —— 全局降级（连续 3 次失败）——


def seed_failures(ledger, count: int = llm_ledger.DEGRADE_AFTER_FAILURES) -> None:
    for _ in range(count):
        llm_ledger.record(
            ledger.conn,
            match_id=ledger.match_id,
            segment_no=3,
            model="deepseek-v4-flash",
            prompt_version="v1",
            outcome=llm_ledger.OUTCOME_TIMEOUT,
            created_at=1_790_064_400_000,
            reason="attempt 1：超时",
        )


def test_consecutive_failures_degrade_globally_without_calling(ledger, facts):
    seed_failures(ledger)
    client = FakeClient(echo_text=OK_TEXT)
    interpreter = build(ledger.conn, client)

    texts = run_all(interpreter, facts)

    assert client.calls == []
    assert interpreter.state == LLM_STATE_RULE
    assert "连续 3 次调用失败" in interpreter.note
    assert all(text.strip() for text in texts.values())
    events = alerts_rows(ledger)
    assert len(events) == 1
    assert events[0].kind == alerts.UNAVAILABLE and events[0].severity == "critical"


def test_the_report_itself_can_reach_the_degradation_threshold(ledger, facts):
    """同一份报告里连丢三次超时，后续段不再尝试调用。"""
    client = FakeClient(script=[LLMTimeout("超时")] * len(INTERPRETATION_SEGMENTS))
    interpreter = build(ledger.conn, client)

    interpreter.interpret(SPECS_BY_NO[2], facts)
    interpreter.interpret(SPECS_BY_NO[3], facts)
    interpreter.interpret(SPECS_BY_NO[4], facts)
    assert len(client.calls) == 3

    interpreter.interpret(SPECS_BY_NO[6], facts)
    assert len(client.calls) == 3  # 第四次不再调用
    assert "全局降级" in interpreter.note
    assert [event.kind for event in alerts_rows(ledger)] == [alerts.UNAVAILABLE]


def test_a_successful_call_recovers_the_health(ledger, facts):
    seed_failures(ledger, count=2)
    client = FakeClient(script=[reply(segment_payload(3, OK_TEXT))])
    interpreter = build(ledger.conn, client)
    assert interpreter.interpret(SPECS_BY_NO[3], facts) == OK_TEXT
    assert not llm_ledger.is_degraded(llm_ledger.recent_outcomes(ledger.conn))


# —— 解读阶段预算 ——


def test_interpreter_stops_calling_when_the_stage_budget_is_gone(ledger, facts):
    clock = FakeClock()
    client = FakeClient(
        script=[reply(segment_payload(2, OK_TEXT)), reply(segment_payload(3, OK_TEXT))],
        clock=clock,
        seconds_per_call=4.0,
    )
    interpreter = build(ledger.conn, client, clock=clock, budget_ms=5000)

    texts = run_all(interpreter, facts)

    assert len(client.calls) == 1  # 第一次花掉 4 秒，剩余不足 3 秒就不再发起调用
    assert texts[2] == OK_TEXT  # 第一段拿到了 LLM 文本
    assert "预算" in interpreter.note and "规则直出" in interpreter.note
    assert interpreter.state == LLM_STATE_RULE
    assert MIN_CALL_S == 3.0 and interpreter.budget_ms == 5000


def test_call_timeout_never_exceeds_the_remaining_budget(ledger, facts):
    clock = FakeClock()
    client = FakeClient(script=[reply(segment_payload(3, OK_TEXT))], clock=clock, seconds_per_call=10.0)
    interpreter = build(ledger.conn, client, clock=clock, budget_ms=20_000)
    interpreter.interpret(SPECS_BY_NO[3], facts)
    assert client.calls[0]["timeout_s"] == pytest.approx(20.0)
    interpreter.interpret(SPECS_BY_NO[4], facts)
    assert client.calls[1]["timeout_s"] == pytest.approx(10.0)


# —— 生产入口 ——


def test_interpreter_for_without_credentials_falls_back_and_says_why(ledger, monkeypatch):
    monkeypatch.delenv(API_KEY_NAME, raising=False)
    interpreter = interpreter_for(ledger.conn)
    assert isinstance(interpreter, RuleInterpreter)
    assert interpreter.state == LLM_STATE_RULE
    assert API_KEY_NAME in interpreter.note


def test_interpreter_for_reads_credentials_from_the_env_file(ledger, data_root, monkeypatch):
    monkeypatch.delenv(API_KEY_NAME, raising=False)
    target = data_root / ".env"
    target.write_text(f"{API_KEY_NAME}={FAKE_KEY}\n", encoding="utf-8")
    target.chmod(0o600)

    interpreter = interpreter_for(ledger.conn)

    assert isinstance(interpreter, LLMInterpreter)
    assert interpreter.client.api_key == FAKE_KEY
    assert interpreter.state == LLM_STATE_LLM
    assert interpreter.prompt_set.version == "v1"


def test_interpreter_for_refuses_inscure_env_file(ledger, data_root, monkeypatch):
    monkeypatch.delenv(API_KEY_NAME, raising=False)
    target = data_root / ".env"
    target.write_text(f"{API_KEY_NAME}={FAKE_KEY}\n", encoding="utf-8")
    target.chmod(0o644)

    interpreter = interpreter_for(ledger.conn)

    assert isinstance(interpreter, RuleInterpreter)
    assert "权限不安全" in interpreter.note
    assert FAKE_KEY not in interpreter.note


def test_interpreter_for_refuses_unknown_model(ledger, data_root, monkeypatch):
    target = data_root / ".env"
    target.write_text(f"{API_KEY_NAME}={FAKE_KEY}\nDEEPSEEK_MODEL=gpt-hallucination\n", encoding="utf-8")
    target.chmod(0o600)
    monkeypatch.delenv(API_KEY_NAME, raising=False)

    interpreter = interpreter_for(ledger.conn)

    assert isinstance(interpreter, RuleInterpreter)
    assert "未登记价格" in interpreter.note


def test_interpreter_for_honours_model_and_base_url_overrides(ledger, data_root, monkeypatch):
    target = data_root / ".env"
    target.write_text(
        f"{API_KEY_NAME}={FAKE_KEY}\nDEEPSEEK_MODEL=deepseek-v4-pro\n"
        "DEEPSEEK_BASE_URL=https://example.test/v1\n",
        encoding="utf-8",
    )
    target.chmod(0o600)
    monkeypatch.delenv(API_KEY_NAME, raising=False)

    interpreter = interpreter_for(ledger.conn)

    assert isinstance(interpreter, LLMInterpreter)
    assert interpreter.client.model == "deepseek-v4-pro"
    assert interpreter.client.endpoint == "https://example.test/v1/chat/completions"


def test_credential_error_message_never_contains_the_key(data_root):
    from danmu_intel.common.credentials import require_secret

    target = data_root / ".env"
    target.write_text(f"OTHER_KEY={FAKE_KEY}\n", encoding="utf-8")
    target.chmod(0o600)
    with pytest.raises(CredentialError) as excinfo:
        require_secret(API_KEY_NAME, path=target)
    assert FAKE_KEY not in str(excinfo.value)


def alerts_rows(ledger):
    """本场的解读层报警（成本闸 / 全局降级）。"""
    return [item for item in recent_events(ledger.conn, match_id=ledger.match_id) if item.kind in alerts.KINDS]


def test_alert_rejects_an_unknown_kind(ledger):
    with pytest.raises(ValueError, match="未知的解读层报警类型"):
        alerts.alert(ledger.conn, "not_a_kind", match_id=1, severity="warning")


def test_alerts_are_written_with_the_match_id_and_payload(ledger):
    alerts.alert(
        ledger.conn,
        alerts.UNAVAILABLE,
        match_id=7,
        severity="critical",
        detail={"consecutive_failures": 3},
        timestamp=1_790_064_400_000,
    )
    event = recent_events(ledger.conn, match_id=7)[0]
    assert event.kind == alerts.UNAVAILABLE and event.severity == "critical"
    assert event.payload == {"match_id": 7, "consecutive_failures": 3}
    assert event.state == "pending" and event.created_at == 1_790_064_400_000


def test_unpriced_model_is_refused_before_any_call(ledger, facts):
    """直接构造解释器（绕过生产入口）时，价格表也要在调用前把关。"""
    client = FakeClient(model="gpt-hallucination-9000")
    interpreter = build(ledger.conn, client)

    text = interpreter.interpret(SPECS_BY_NO[3], facts)

    assert client.calls == []
    assert text == interpretation_text(3, facts)
    assert "模型未登记价格" in interpreter.note
    assert interpreter.state == LLM_STATE_RULE
