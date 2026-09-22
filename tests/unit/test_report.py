"""报告层测试：十一段结构与需求 §6.6 逐字一致、缺段不得生成、来源可复核。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from danmu_intel.common import paths
from danmu_intel.pipeline import collect_facts
from danmu_intel.report.html import render_html
from danmu_intel.report.rule_render import (
    INTERPRETATION_MARK,
    build_report,
    format_ts,
)
from danmu_intel.report.segments import (
    INTERPRETATION_SEGMENTS,
    KIND_FACT,
    KIND_FACT_GRAY,
    KIND_FACT_INTERPRETATION,
    KIND_INTERPRETATION,
    SEGMENTS,
    MissingSegmentError,
    build_segments,
)

REQUIREMENTS = Path(paths.repo_root()) / "docs" / "requirements" / "DANMU_INTEL_REQUIREMENTS.md"


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
    kinds = {no: kind for no, _, kind in parse_requirements_section_66()}
    assert kinds[0] == "事实" and kinds[3] == "解读" and kinds[5] == "事实（风险提示）"
    assert kinds[2] == "事实 + 解读"
    spec_kinds = {spec.no: spec.kind for spec in SEGMENTS}
    assert spec_kinds[0] == KIND_FACT
    assert spec_kinds[2] == KIND_FACT_INTERPRETATION
    assert spec_kinds[3] == KIND_INTERPRETATION
    assert spec_kinds[5] == KIND_FACT_GRAY
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


def test_format_ts():
    assert format_ts(None) == "未登记"
    assert format_ts(0).startswith("1970-01-01")


def test_build_report_covers_all_segments(ledger):
    facts = collect_facts(ledger.conn, ledger.match_id, data_root=ledger.data_root)
    segments = build_report(facts)
    assert [segment.no for segment in segments] == list(range(11))
    assert all(segment.body.strip() for segment in segments)
    assert all(segment.sources for segment in segments), "每段都要有可复核的来源"

    # 缺段无法生成（把某段正文清空即抛错）
    facts_without = facts
    with pytest.raises(MissingSegmentError):
        build_segments({no: ("正文", []) for no in range(10)})
    assert facts_without.match.id == ledger.match_id


def test_report_marks_interpretation_segments(ledger):
    facts = collect_facts(ledger.conn, ledger.match_id, data_root=ledger.data_root)
    segments = build_report(facts)
    for segment in segments:
        if segment.kind in (KIND_INTERPRETATION, KIND_FACT_INTERPRETATION):
            assert INTERPRETATION_MARK in segment.body, f"第 {segment.no} 段必须显式标注为解读"
    # 纯解读段不得缺失，也不得以纯数据替代
    for no in (3, 4, 6, 8, 9):
        assert len(segments[no].body) > 40


def test_report_facts_use_slice_and_metrics(ledger):
    facts = collect_facts(ledger.conn, ledger.match_id, data_root=ledger.data_root)
    segments = build_report(facts)
    assert "iG vs LNG" in segments[0].body
    assert "取材范围" in segments[0].body
    assert "共 65 条弹幕" in segments[0].body
    assert "小局切片：2 局" in segments[1].body
    assert "G1" in segments[1].body and "G2" in segments[1].body
    assert "峰值窗口" in segments[2].body
    assert "灰信号" in segments[5].body and "不出现指控性结论" in segments[5].body
    assert "SHA256" in segments[10].body
    assert ledger.rel_path in segments[10].body


def test_report_has_no_user_identity(ledger):
    facts = collect_facts(ledger.conn, ledger.match_id, data_root=ledger.data_root)
    segments = build_report(facts)
    text = "\n".join(segment.body for segment in segments)
    assert "样例主播" not in text  # 主播名不进正文（T1 只记录在 rooms 表）
    assert "user-" not in text  # user_hash 只用于去重计数，永不展示


def test_render_html_contains_all_segments_and_sources(ledger):
    facts = collect_facts(ledger.conn, ledger.match_id, data_root=ledger.data_root)
    html = render_html(facts)
    assert html.startswith("<!DOCTYPE html>")
    assert html.count('<section class="seg ') == 11
    for spec in SEGMENTS:
        assert f'id="seg-{spec.no}"' in html
        assert f"{spec.no}</span> {spec.title}" in html
    assert html.count("SHA256") >= 11
    assert "viewport" in html and "手机" not in html
    assert "<script" not in html  # 不加载任何脚本
    assert "http://" not in html and "https://" not in html  # 不引用任何外部资源
    assert "事实" in html and "解读" in html


def test_render_html_escapes_content(ledger):
    ledger.conn.execute("UPDATE matches SET team_a=?, team_b=? WHERE id=?", ("<script>", "LNG&Co", ledger.match_id))
    ledger.conn.commit()
    facts = collect_facts(ledger.conn, ledger.match_id, data_root=ledger.data_root)
    html = render_html(facts)
    assert "&lt;script&gt;" in html
    assert "LNG&amp;Co" in html
    assert "<script>" not in html
