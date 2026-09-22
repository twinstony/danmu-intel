"""发布钩子测试：缺解读段拒绝发布（AC-16）、来源不可解析拒绝发布、版本递增、时限记账。"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace

import pytest

from danmu_intel.pipeline import collect_facts, generate_and_publish
from danmu_intel.report.assemble import ReportContent, build_content
from danmu_intel.report.forms import ReportScope, form_of
from danmu_intel.report.interpreter import LLM_STATE_LLM, LLM_STATE_RULE
from danmu_intel.report.publish import (
    PublishRefused,
    load_content,
    list_reports,
    next_version,
    publish,
    run_checks,
)
from danmu_intel.report.rule_render import INTERPRETATION_MARK
from danmu_intel.report.segments import Segment

GENERATED_AT = 1_790_064_400_000


@dataclass(frozen=True, slots=True)
class FakeInterpreter:
    """LLM 调用点注入缝的假实现：固定解读文本（本票不接真 LLM）。"""

    texts: dict[int, str]
    state: str = LLM_STATE_LLM

    def interpret(self, spec, facts) -> str:
        return self.texts.get(spec.no, f"{spec.title}：假 LLM 的固定解读文本。")


def content_of(ledger, kind: str = "full", *, scope: ReportScope | None = None, **kwargs):
    facts = collect_facts(ledger.conn, ledger.match_id, data_root=ledger.data_root)
    if scope is not None:
        from danmu_intel.report.facts import scope_facts

        facts = scope_facts(facts, scope)
    return build_content(
        facts, form=form_of(kind), version=1, generated_at=GENERATED_AT, **kwargs
    )


def test_injected_llm_text_is_published_and_marked(ledger, site_root):
    """注入缝：解读文本来自被注入的实现，且如实标注 llm_state='llm'。"""
    fake = FakeInterpreter(texts={3: "队伍画像：这是假 LLM 的固定解读文本。"})
    result = generate_and_publish(
        ledger.conn,
        ledger.match_id,
        kind="full",
        interpreter=fake,
        data_root=ledger.data_root,
        generated_at=GENERATED_AT,
    )
    assert result.content.llm_state == LLM_STATE_LLM
    body = result.content.segment(3).body
    assert "假 LLM 的固定解读文本" in body
    assert body.startswith(INTERPRETATION_MARK)
    row = ledger.conn.execute("SELECT llm_state FROM reports").fetchone()
    assert row["llm_state"] == LLM_STATE_LLM
    assert "假 LLM 的固定解读文本" in result.path.read_text(encoding="utf-8")


def test_default_interpreter_is_rule_fallback(ledger, site_root):
    result = generate_and_publish(
        ledger.conn, ledger.match_id, kind="full", data_root=ledger.data_root, generated_at=GENERATED_AT
    )
    assert result.content.llm_state == LLM_STATE_RULE


def test_run_checks_reports_every_item(ledger):
    content = content_of(ledger)
    checks = run_checks(content, data_root=ledger.data_root)
    assert [item.key for item in checks] == [
        "segments_complete",
        "interpretation_present",
        "sources_resolvable",
        "within_deadline",
    ]
    assert all(item.passed for item in checks)
    assert all(item.detail.strip() for item in checks)


def test_publish_refuses_when_an_interpretation_segment_is_missing(ledger, site_root):
    """AC-16：报告缺少解读段落时不得发布。"""
    content = content_of(ledger)
    doctored = replace(
        content, segments=tuple(segment for segment in content.segments if segment.no != 4)
    )
    with pytest.raises(PublishRefused) as excinfo:
        publish(ledger.conn, doctored, data_root=ledger.data_root)
    assert "解读段齐备" in str(excinfo.value)
    assert [item.key for item in excinfo.value.failures] == [
        "segments_complete",
        "interpretation_present",
    ]

    # 没有上线任何页面，且失败留痕（报告行 state=failed）
    assert not (site_root / "matches" / str(ledger.match_id) / "full.html").exists()
    row = ledger.conn.execute("SELECT state, path, checks_json FROM reports").fetchone()
    assert row["state"] == "failed" and row["path"] is None
    checks = {item["key"]: item for item in json.loads(row["checks_json"])}
    assert checks["interpretation_present"]["passed"] is False
    assert "缺解读段：4" in checks["interpretation_present"]["detail"]


def test_publish_refuses_an_unmarked_interpretation_segment(ledger, site_root):
    """解读必须明确标注为分析（需求 §6.9 第 2 条），否则不得发布。"""
    content = content_of(ledger)
    doctored = replace(
        content,
        segments=tuple(
            replace(segment, body="纯数据，没有标注") if segment.no == 3 else segment
            for segment in content.segments
        ),
    )
    with pytest.raises(PublishRefused, match="未标注为解读：3"):
        publish(ledger.conn, doctored, data_root=ledger.data_root)


def test_publish_refuses_when_a_source_cannot_be_resolved(ledger, site_root):
    content = content_of(ledger)
    raw = ledger.data_root / ledger.rel_path
    raw.write_text(raw.read_text(encoding="utf-8").replace("G1 突发 0", "G1 突发 X"), encoding="utf-8")

    with pytest.raises(PublishRefused, match="来源可解析"):
        publish(ledger.conn, content, data_root=ledger.data_root)
    assert not (site_root / "matches" / str(ledger.match_id) / "full.html").exists()


def test_late_report_is_recorded_but_not_blocked(ledger, site_root):
    """时限与准确性冲突时准确性优先（NFR-T）：超时照发，但如实记账。"""
    calls: list[int] = []

    def slow_clock() -> float:
        calls.append(len(calls))
        return 0.0 if len(calls) == 1 else 300.0

    result = generate_and_publish(
        ledger.conn,
        ledger.match_id,
        kind="live_brief",
        data_root=ledger.data_root,
        generated_at=GENERATED_AT,
        clock=slow_clock,
    )
    assert result.path.exists()
    assert result.timing["within_deadline"] is False
    assert result.timing["elapsed_ms"] > form_of("live_brief").deadline_ms
    checks = {item.key: item for item in result.checks}
    assert checks["within_deadline"].passed is False
    assert checks["within_deadline"].blocking is False
    row = ledger.conn.execute("SELECT timing_json FROM reports").fetchone()
    assert json.loads(row["timing_json"])["within_deadline"] is False


def test_publish_versions_increase_and_stay_traceable(ledger, site_root):
    """FR-C4-9：修改以新版本发布，旧版本可追溯。"""
    assert next_version(ledger.conn, ledger.match_id, "live_brief") == 1
    first = generate_and_publish(
        ledger.conn,
        ledger.match_id,
        kind="live_brief",
        completed_games=(1,),
        trigger_game_no=1,
        data_root=ledger.data_root,
        generated_at=GENERATED_AT,
    )
    second = generate_and_publish(
        ledger.conn,
        ledger.match_id,
        kind="live_brief",
        completed_games=(1, 2),
        trigger_game_no=2,
        data_root=ledger.data_root,
        generated_at=GENERATED_AT + 1,
    )
    assert (first.version, second.version) == (1, 2)
    assert first.path == second.path  # 同形态同页面，旧版靠 reports 行留痕
    assert first.content.fact_layer_hash != second.content.fact_layer_hash

    rows = list_reports(ledger.conn, ledger.match_id)
    assert [(row.kind, row.version, row.state) for row in rows] == [
        ("live_brief", 1, "published"),
        ("live_brief", 2, "published"),
    ]
    assert [row.game_no for row in rows] == [1, 2]
    restored = load_content(ledger.conn, ledger.match_id, "live_brief", 1)
    assert restored.fact_layer_hash == first.content.fact_layer_hash
    assert restored.meta["covered_games"] == [1]
    with pytest.raises(LookupError, match="未找到报告"):
        load_content(ledger.conn, ledger.match_id, "live_brief", 9)


def test_publish_result_carries_the_published_content(ledger, site_root):
    result = generate_and_publish(
        ledger.conn, ledger.match_id, kind="review", data_root=ledger.data_root, generated_at=GENERATED_AT
    )
    assert isinstance(result.report_id, int)
    assert [segment.no for segment in result.content.segments] == list(range(11))
    assert result.content.fact_layer_hash in result.path.read_text(encoding="utf-8")


def test_doctored_content_with_a_foreign_segment_is_refused(ledger, site_root):
    """形态段集是固定的：混进本形态不发布的段同样拒绝（结构稳定）。"""
    content = content_of(ledger, "live_brief", scope=ReportScope(completed_games=(1, 2)))
    extra = Segment(
        no=7,
        title="预测验证",
        kinds=("fact", "interpretation"),
        nature="事实 + 解读",
        body="不该出现在快报里的段",
    )
    doctored: ReportContent = replace(content, segments=(*content.segments, extra))
    with pytest.raises(PublishRefused, match="段集完整"):
        publish(ledger.conn, doctored, data_root=ledger.data_root)
