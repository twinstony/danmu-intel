"""端到端：一场含 3 个节点（G1/G2 已完成、G3 进行中）的比赛上产出三种报告形态。

对应 issue #8 的验收标准：

- 三种形态分别在 **2 / 10 / 15 分钟**预算内产出（NFR-T-1/2/3）；
- 段号/标题/顺序与需求 §6.6 逐字一致，段性质正确；
- 赛中快报**只发布已完成节点**的段落（进行中的 G3 不进正文）；
- 报告事实与原始记录**逐项核对**（AC-1）；
- 每次发布后，来源引用全部可解析（文件存在 + SHA256 匹配）；
- 缺解读段 / 溯源不可解析时**拒绝发布**（AC-16）。

全程不连外网（NFR-GA-4）。
"""

from __future__ import annotations

import json

import pytest

from danmu_intel.cli import main
from danmu_intel.common.events import iter_events
from danmu_intel.common.sources import verify
from danmu_intel.pipeline import generate_and_publish, verify_sources
from danmu_intel.report.forms import (
    FORMS,
    LIVE_BRIEF_SEGMENTS,
    form_of,
)
from danmu_intel.report.html import parse_sources
from danmu_intel.report.segments import SEGMENTS

GENERATED_AT = 1_790_064_400_000
KINDS = ("live_brief", "full", "review")


def publish(ledger, kind: str, **kwargs):
    return generate_and_publish(
        ledger.conn,
        ledger.match_id,
        kind=kind,
        data_root=ledger.data_root,
        generated_at=GENERATED_AT,
        **kwargs,
    )


def mark_match_ended(ledger) -> None:
    ledger.conn.execute(
        "UPDATE matches SET state='ended', ended_at=?, official_result=? WHERE id=?",
        (
            ledger.windows[ledger.in_progress_game][1],
            json.dumps({"score": "2:1"}, ensure_ascii=False),
            ledger.match_id,
        ),
    )
    ledger.conn.commit()


def publish_all_three(ledger):
    """赛中发快报，赛后发完整版与复盘版（issue #8 验收场景）。"""
    brief = publish(
        ledger,
        "live_brief",
        completed_games=ledger.completed_games,
        trigger_game_no=ledger.in_progress_game - 1,
    )
    mark_match_ended(ledger)
    return brief, publish(ledger, "full"), publish(ledger, "review")


def test_three_forms_publish_within_their_deadlines(three_game_ledger, site_root):
    ledger = three_game_ledger
    _, full, review = publish_all_three(ledger)

    assert [form_of(kind).deadline_ms for kind in KINDS] == [120_000, 600_000, 900_000]
    for result in (publish(ledger, "live_brief", completed_games=ledger.completed_games), full, review):
        form = form_of(result.kind)
        assert result.timing["deadline_ms"] == form.deadline_ms
        assert result.timing["within_deadline"] is True
        assert result.timing["elapsed_ms"] <= form.deadline_ms
        assert result.path == site_root / "matches" / str(ledger.match_id) / f"{result.kind}.html"
        assert result.path.exists()
        assert all(item.passed or not item.blocking for item in result.checks)
        assert verify_sources(ledger.match_id, kind=result.kind, data_root=ledger.data_root) == []


def test_every_form_publishes_its_declared_segment_set(three_game_ledger, site_root):
    ledger = three_game_ledger
    brief, full, review = publish_all_three(ledger)

    assert [segment.no for segment in brief.content.segments] == list(LIVE_BRIEF_SEGMENTS)
    assert [segment.no for segment in full.content.segments] == list(range(11))
    assert [segment.no for segment in review.content.segments] == list(range(11))
    # 段号/标题/顺序与需求 §6.6 逐字一致
    for segment in full.content.segments:
        assert (segment.no, segment.title, segment.nature) == (
            SEGMENTS[segment.no].no,
            SEGMENTS[segment.no].title,
            SEGMENTS[segment.no].nature,
        )

    brief_html = brief.path.read_text(encoding="utf-8")
    full_html = full.path.read_text(encoding="utf-8")
    assert brief_html.count('<section class="seg ') == len(LIVE_BRIEF_SEGMENTS)
    assert full_html.count('<section class="seg ') == 11
    assert brief_html.count("预测验证") == 0, "快报不发布需要终局对照的段"
    assert "预测验证" in full_html
    # 事实段与解读段在页面上可区分
    assert 'class="seg seg--fact"' in full_html and 'class="seg seg--interpretation"' in full_html
    assert 'class="kind kind--fact-gray">事实（风险提示）' in full_html


def test_live_brief_covers_only_completed_nodes(three_game_ledger, site_root):
    """issue #8 范围第 3 条：赛中快报只发布已完成节点的段落子集。"""
    ledger = three_game_ledger
    brief, full, review = publish_all_three(ledger)

    assert brief.content.meta["covered_games"] == [1, 2]
    assert brief.content.meta["excluded_games"] == [ledger.in_progress_game]
    assert brief.content.meta["danmu_count"] == 24 + 12
    brief_text = "\n".join(segment.body for segment in brief.content.segments)
    assert "G3 弹幕" not in brief_text and "G3：" not in brief_text, "进行中的节点不进快报正文"
    assert "未纳入本报告的节点：G3" in brief.content.segment(0).body
    assert "另有 1 局进行中" in brief.content.segment(1).body

    assert full.content.meta["covered_games"] == [1, 2, 3]
    assert full.content.meta["excluded_games"] == []
    assert full.content.meta["danmu_count"] == 24 + 12 + 8
    assert "G3：" in full.content.segment(1).body
    assert review.content.meta["danmu_count"] == 24 + 12 + 8


def test_report_facts_match_the_raw_records(three_game_ledger, site_root):
    """AC-1：报告事实零错误 —— 逐项对着原始记录重算。"""
    ledger = three_game_ledger
    brief, full, _ = publish_all_three(ledger)
    events = [event for _, event in iter_events(ledger.data_root / ledger.rel_path)]
    assert len(events) == 24 + 12 + 8

    def count(no: int) -> int:
        start, end = ledger.windows[no]
        return sum(1 for event in events if start <= event.ts < end)

    def speakers(no: int) -> int:
        start, end = ledger.windows[no]
        return len({event.user_hash for event in events if start <= event.ts < end})

    brief_overview = brief.content.segment(1).body
    for no in ledger.completed_games:
        assert f"G{no}：" in brief_overview
        assert f"弹幕 {count(no)} 条" in brief_overview
        assert f"独立发言者 {speakers(no)} 人" in brief_overview
    assert f"共 {count(1) + count(2)} 条弹幕" in brief.content.segment(0).body
    assert brief.content.meta["danmu_count"] == count(1) + count(2)

    full_overview = full.content.segment(1).body
    for no in (1, 2, 3):
        assert f"弹幕 {count(no)} 条" in full_overview
        assert f"独立发言者 {speakers(no)} 人" in full_overview
    assert f"共 {len(events)} 条弹幕" in full.content.segment(0).body
    assert full.content.meta["danmu_count"] == len(events)
    # 官方比分只在赛后形态出现，且与登记值一致
    assert "官方结果：2:1" in full.content.segment(0).body
    assert "官方结果：未回填" in brief.content.segment(0).body


def test_published_pages_freeze_resolvable_sources(three_game_ledger, site_root):
    ledger = three_game_ledger
    brief, full, review = publish_all_three(ledger)

    for result in (brief, full, review):
        page_refs = parse_sources(result.path.read_text(encoding="utf-8"))
        assert page_refs, f"{result.kind} 页面上必须能取到来源引用"
        assert {ref.rel_path for ref in page_refs} == {ledger.rel_path}
        assert all(verify(ref, data_root=ledger.data_root) for ref in page_refs)
        content_refs = {
            (ref.rel_path, ref.line_start, ref.line_end, ref.sha256)
            for segment in result.content.segments
            for ref in segment.sources
        }
        assert {
            (ref.rel_path, ref.line_start, ref.line_end, ref.sha256) for ref in page_refs
        } <= content_refs

    # 快报的引用只覆盖已完成节点的行范围，完整版覆盖全文件
    brief_refs = parse_sources(brief.path.read_text(encoding="utf-8"))
    assert max(ref.line_end for ref in brief_refs) == 24 + 12
    full_refs = parse_sources(full.path.read_text(encoding="utf-8"))
    assert max(ref.line_end for ref in full_refs) == 24 + 12 + 8

    # 原始记录被改动后，每个形态都能当场发现（校验的是产物里冻结的哈希）
    raw = ledger.data_root / ledger.rel_path
    raw.write_text(raw.read_text(encoding="utf-8").replace("G1 弹幕 5", "G1 弹幕 X"), encoding="utf-8")
    for kind in KINDS:
        failed = verify_sources(ledger.match_id, kind=kind, data_root=ledger.data_root)
        assert failed, f"{kind} 必须能发现原始记录被改过"
        assert {ref.rel_path for ref in failed} == {ledger.rel_path}


def test_cli_publishes_three_forms_and_lists_versions(three_game_ledger, site_root, capsys):
    match_id = three_game_ledger.match_id
    assert main([
        "report", "--match-id", str(match_id), "--kind", "live_brief",
        "--completed-game", "1", "--completed-game", "2", "--trigger-game", "2",
    ]) == 0
    out = capsys.readouterr().out
    assert "已发布赛中快报 v1" in out
    assert "段集完整：通过" in out and "解读段齐备：通过" in out
    assert "来源可解析：通过" in out and "时限预算：通过" in out
    assert "解读层 rule_fallback" in out

    mark_match_ended(three_game_ledger)
    assert main(["report", "--match-id", str(match_id), "--kind", "full"]) == 0
    assert "已发布完整版 v1" in capsys.readouterr().out
    assert main(["report", "--match-id", str(match_id), "--kind", "review"]) == 0
    assert "已发布复盘版 v1" in capsys.readouterr().out

    assert main(["reports", "--match-id", str(match_id)]) == 0
    listing = capsys.readouterr().out
    for kind in KINDS:
        assert f"{kind} v1｜published" in listing
    assert "live_brief v1｜published｜节点 G2" in listing

    for kind in KINDS:
        assert (site_root / "matches" / str(match_id) / f"{kind}.html").exists()
        assert main(["verify-sources", "--match-id", str(match_id), "--kind", kind]) == 0
        assert "全部来源校验通过" in capsys.readouterr().out


def test_cli_refuses_a_live_brief_without_completed_nodes(three_game_ledger, capsys):
    match_id = three_game_ledger.match_id
    assert main(["report", "--match-id", str(match_id), "--kind", "live_brief"]) == 2
    assert "必须声明已完成节点" in capsys.readouterr().err


def test_cli_refuses_to_publish_when_evidence_changed(three_game_ledger, site_root, capsys):
    match_id = three_game_ledger.match_id
    assert main(["report", "--match-id", str(match_id), "--kind", "full"]) == 0
    capsys.readouterr()

    raw = three_game_ledger.data_root / three_game_ledger.rel_path
    raw.write_text(raw.read_text(encoding="utf-8").replace("G2 弹幕 1", "G2 弹幕 X"), encoding="utf-8")

    assert main(["report", "--match-id", str(match_id), "--kind", "full"]) == 1
    err = capsys.readouterr().err
    assert "发布被拒绝" in err and "来源可解析" in err

    rows = three_game_ledger.conn.execute(
        "SELECT version, state FROM reports WHERE kind='full' ORDER BY version"
    ).fetchall()
    assert [(row["version"], row["state"]) for row in rows] == [(1, "published"), (2, "failed")]


def test_all_forms_are_declared_with_a_trigger_and_a_deadline():
    assert {form.trigger for form in FORMS} == {"node_end", "match_end"}
    assert form_of("live_brief").segments == LIVE_BRIEF_SEGMENTS
    with pytest.raises(ValueError):
        form_of("recap")
