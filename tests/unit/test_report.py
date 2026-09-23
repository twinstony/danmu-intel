"""报告层测试：十一段与需求 §6.6 逐字一致、缺段不得生成、事实/解读分层、来源可复核。"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from danmu_intel.common import paths
from danmu_intel.pipeline import collect_facts
from danmu_intel.report.assemble import build_content
from danmu_intel.report.facts import fact_layer_hash, scope_facts
from danmu_intel.report.forms import ReportScope, form_of
from danmu_intel.common import paywall
from danmu_intel.report.html import NATURE_CLASSES, nature_class, parse_sources, render_report_html
from danmu_intel.report.rule_render import INTERPRETATION_MARK, format_ts
from danmu_intel.report.segments import (
    INTERPRETATION_SEGMENTS,
    KIND_FACT,
    KIND_INTERPRETATION,
    SEGMENTS,
    MissingSegmentError,
    build_segments,
)

REQUIREMENTS = Path(paths.repo_root()) / "docs" / "requirements" / "DANMU_INTEL_REQUIREMENTS.md"
GENERATED_AT = 1_790_064_400_000


def parse_requirements_section_66() -> list[tuple[int, str, str]]:
    """从需求文档 §6.6 的表格里解析出 (段号, 标题, 内容性质)。"""
    text = REQUIREMENTS.read_text(encoding="utf-8")
    section = text.split("### 6.6 报告的分段结构", 1)[1].split("### 6.7", 1)[0]
    rows: list[tuple[int, str, str]] = []
    for line in section.splitlines():
        match = re.match(r"^\|\s*(\d+)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*$", line)
        if match:
            rows.append((int(match.group(1)), match.group(2), match.group(3)))
    return rows


def content_of(ledger, kind: str = "full", *, scope: ReportScope | None = None, **kwargs):
    facts = collect_facts(ledger.conn, ledger.match_id, data_root=ledger.data_root)
    if scope is not None:
        facts = scope_facts(facts, scope)
    return build_content(
        facts, form=form_of(kind), version=1, generated_at=GENERATED_AT, **kwargs
    )


def test_segments_match_requirements_verbatim():
    """段号、标题、顺序与需求 §6.6 逐字一致（NFR-Q-2 的回归防线）。"""
    expected = parse_requirements_section_66()
    assert len(expected) == 11, "需求文档 §6.6 应当正好有 11 段"
    assert [(spec.no, spec.title) for spec in SEGMENTS] == [(no, title) for no, title, _ in expected]
    assert [spec.no for spec in SEGMENTS] == list(range(11))
    assert [title for _, title, _ in expected] == [
        "比赛信息",
        "结果总览",
        "逐局复盘",
        "队伍画像",
        "人员画像",
        "灰信号汇总",
        "联赛规律与版本",
        "预测验证",
        "盘口讨论",
        "情报含义与后续观察点",
        "数据与溯源",
    ]


def test_segment_kinds_match_requirements():
    """段性质（需求 §6.6「内容性质」列）逐字一致；标记只有 fact / interpretation。"""
    expected = {no: nature for no, _, nature in parse_requirements_section_66()}
    assert {spec.no: spec.nature for spec in SEGMENTS} == expected
    assert expected[2] == "事实 + 解读" and expected[5] == "事实（风险提示）"
    for spec in SEGMENTS:
        assert set(spec.kinds) <= {KIND_FACT, KIND_INTERPRETATION}
    kinds = {spec.no: spec.kinds for spec in SEGMENTS}
    assert kinds[0] == (KIND_FACT,)
    assert kinds[3] == (KIND_INTERPRETATION,)
    # 「事实 + 解读」的段两种标记并存
    assert kinds[2] == (KIND_FACT, KIND_INTERPRETATION)
    assert kinds[7] == (KIND_FACT, KIND_INTERPRETATION)
    # 「事实（风险提示）」是事实标记 + 限定说明
    assert kinds[5] == (KIND_FACT,) and SEGMENTS[5].note == "风险提示"
    assert INTERPRETATION_SEGMENTS == (2, 3, 4, 6, 7, 8, 9)


def test_build_segments_rejects_missing_segment():
    bodies = {no: ("正文", []) for no in range(11)}
    del bodies[4]
    with pytest.raises(MissingSegmentError, match="缺段：4"):
        build_segments(bodies)


def test_build_segments_rejects_empty_body():
    bodies = {no: ("正文", []) for no in range(11)}
    bodies[7] = ("   ", [])
    with pytest.raises(MissingSegmentError, match="空段"):
        build_segments(bodies)


def test_build_segments_rejects_unknown_segment():
    bodies = {no: ("正文", []) for no in range(11)}
    bodies[11] = ("多余", [])
    with pytest.raises(MissingSegmentError, match="未定义的段号：11"):
        build_segments(bodies)


def test_build_segments_rejects_segment_outside_the_form():
    bodies = {no: ("正文", []) for no in range(11)}
    with pytest.raises(MissingSegmentError, match="不发布的段号：0"):
        build_segments(bodies, nos=(1, 2))


def test_format_ts():
    assert format_ts(None) == "未登记"
    assert format_ts(0).startswith("1970-01-01")


def test_build_content_covers_all_segments(ledger):
    content = content_of(ledger)
    assert [segment.no for segment in content.segments] == list(range(11))
    assert all(segment.body.strip() for segment in content.segments)
    assert all(segment.sources for segment in content.segments), "每段都要有可复核的来源"
    assert content.kind == "full" and content.version == 1
    assert content.generated_at == GENERATED_AT
    assert content.llm_state == "rule_fallback"
    assert len(content.fact_layer_hash) == 64
    assert content.meta["match_title"] == "iG vs LNG"
    assert content.meta["danmu_count"] == 65


def test_report_marks_interpretation_segments(ledger):
    content = content_of(ledger)
    for segment in content.segments:
        if segment.has_interpretation:
            assert INTERPRETATION_MARK in segment.body, f"第 {segment.no} 段必须显式标注为解读"
    # 纯解读段不得缺失，也不得以纯数据替代
    for no in (3, 4, 6, 8, 9):
        assert len(content.segment(no).body) > 40


def test_report_facts_use_slice_and_metrics(ledger):
    content = content_of(ledger)
    assert "iG vs LNG" in content.segment(0).body
    assert "取材范围" in content.segment(0).body
    assert "共 65 条弹幕" in content.segment(0).body
    assert "小局切片：本报告覆盖 2 局" in content.segment(1).body
    assert "G1" in content.segment(1).body and "G2" in content.segment(1).body
    assert "峰值窗口" in content.segment(2).body
    assert "灰信号" in content.segment(5).body and "不出现指控性结论" in content.segment(5).body
    source_body = content.segment(10).body
    assert "SHA256" in source_body and ledger.rel_path in source_body
    assert "报告形态：full｜版本：v1" in source_body
    assert content.fact_layer_hash in source_body


def test_report_has_no_user_identity(ledger):
    content = content_of(ledger)
    text = "\n".join(segment.body for segment in content.segments)
    assert "样例主播" not in text  # 主播名不进正文（只记录在 rooms 表）
    assert "user-" not in text  # user_hash 只用于去重计数，永不展示


def test_content_json_round_trip(ledger):
    content = content_of(ledger)
    again = type(content).from_dict(json.loads(content.to_json()))
    assert again == content
    assert again.segment(0).sources == content.segment(0).sources


def test_fact_layer_hash_tracks_its_input(ledger):
    facts = collect_facts(ledger.conn, ledger.match_id, data_root=ledger.data_root)
    first = fact_layer_hash(facts)
    assert first == fact_layer_hash(facts), "同输入必然同指纹"
    scoped = scope_facts(facts, ReportScope(completed_games=(1,)))
    assert fact_layer_hash(scoped) != first, "覆盖范围变了，事实层指纹必须变"
    ledger.conn.execute("DELETE FROM slices WHERE game_no=2")
    ledger.conn.commit()
    assert fact_layer_hash(collect_facts(ledger.conn, ledger.match_id, data_root=ledger.data_root)) != first


def test_nature_class_covers_every_requirement_nature():
    natures = {spec.nature for spec in SEGMENTS}
    assert natures == set(NATURE_CLASSES)
    assert nature_class("事实（风险提示）") == "fact-gray"
    assert nature_class("未知性质") == "fact"


def test_render_report_html_contains_all_segments_and_sources(ledger):
    content = content_of(ledger)
    html = render_report_html(content, visibility=paywall.VISIBILITY_PUBLIC)
    assert html.startswith("<!DOCTYPE html>")
    assert html.count('<section class="seg ') == 11
    for spec in SEGMENTS:
        assert f'id="seg-{spec.no}"' in html
        assert f"{spec.no}</span> {spec.title}" in html
    assert html.count("SHA256") >= 11
    assert "viewport" in html
    assert "<script" not in html  # 不加载任何脚本
    assert "http://" not in html and "https://" not in html  # 不引用任何外部资源
    assert "事实" in html and "解读" in html
    assert content.fact_layer_hash in html
    assert len(parse_sources(html)) > 0


def test_render_report_html_escapes_content(ledger):
    ledger.conn.execute(
        "UPDATE matches SET team_a=?, team_b=? WHERE id=?", ("<script>", "LNG&Co", ledger.match_id)
    )
    ledger.conn.commit()
    html = render_report_html(content_of(ledger), visibility=paywall.VISIBILITY_PUBLIC)
    assert "&lt;script&gt;" in html
    assert "LNG&amp;Co" in html
    assert "<script>" not in html
