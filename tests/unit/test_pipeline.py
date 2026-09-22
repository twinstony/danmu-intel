"""流水线测试：写库、静态页、来源复核、AC-13 重算。"""

from __future__ import annotations

import json

import pytest

from danmu_intel.common import paths
from danmu_intel.pipeline import (
    clear_metrics,
    collect_facts,
    load_lines,
    load_segment_facts,
    metrics_snapshot,
    rebuild_metrics,
    render_match_page,
    report_sources,
    verify_sources,
    write_metrics,
)


def test_collect_facts_shape(ledger):
    facts = collect_facts(ledger.conn, ledger.match_id, data_root=ledger.data_root)
    assert facts.match.id == ledger.match_id
    assert len(facts.games) == 2
    assert facts.games[0].window.game_no == 1
    assert facts.games[0].metrics["danmu_total"]["count"] == 55
    assert facts.games[1].metrics["danmu_total"]["count"] == 10
    assert len(facts.all_lines) == 65
    assert facts.platforms == ["huya"]
    assert facts.room_ids == ["660000"]
    assert facts.generated_at > 0


def test_load_lines_is_ordered_and_traceable(ledger):
    lines = load_lines(ledger.conn, ledger.match_id, data_root=ledger.data_root)
    assert [line.line_no for line in lines] == list(range(1, 66))
    assert all(line.rel_path == ledger.rel_path for line in lines)
    assert lines[0].event.ts == ledger.events[0].ts


def test_write_metrics_is_idempotent(ledger):
    facts = collect_facts(ledger.conn, ledger.match_id, data_root=ledger.data_root)
    first = write_metrics(ledger.conn, facts)
    snapshot = metrics_snapshot(ledger.conn, ledger.match_id)
    assert first == len(facts.games) * 4
    assert write_metrics(ledger.conn, facts) == first
    assert metrics_snapshot(ledger.conn, ledger.match_id) == snapshot
    keys = {key for _, key, _ in snapshot}
    assert keys == {"danmu_total", "distinct_users", "density_curve", "peak"}
    row = ledger.conn.execute(
        "SELECT computed_at, algo_version FROM metrics LIMIT 1"
    ).fetchone()
    assert row["algo_version"] == facts.algo_version
    assert row["computed_at"] > 0
    assert json.loads(snapshot[0][2])


def test_clear_metrics(ledger):
    facts = collect_facts(ledger.conn, ledger.match_id, data_root=ledger.data_root)
    write_metrics(ledger.conn, facts)
    clear_metrics(ledger.conn, ledger.match_id)
    assert metrics_snapshot(ledger.conn, ledger.match_id) == []


def test_rebuild_metrics_reproduces_identical_values(ledger):
    """AC-13：删掉统计结果后，仅凭原始记录 + 切片能重算出同样的统计。"""
    facts = collect_facts(ledger.conn, ledger.match_id, data_root=ledger.data_root)
    write_metrics(ledger.conn, facts)
    before = [row[2] for row in metrics_snapshot(ledger.conn, ledger.match_id)]

    assert rebuild_metrics(ledger.conn, ledger.match_id, data_root=ledger.data_root) is True
    assert [row[2] for row in metrics_snapshot(ledger.conn, ledger.match_id)] == before


def test_rebuild_from_empty_metrics_builds_baseline(ledger):
    assert metrics_snapshot(ledger.conn, ledger.match_id) == []
    assert rebuild_metrics(ledger.conn, ledger.match_id, data_root=ledger.data_root) is True
    assert metrics_snapshot(ledger.conn, ledger.match_id)


def test_rebuild_detects_missing_slice(ledger):
    facts = collect_facts(ledger.conn, ledger.match_id, data_root=ledger.data_root)
    write_metrics(ledger.conn, facts)
    ledger.conn.execute("DELETE FROM slices WHERE game_no=2")
    ledger.conn.commit()
    assert rebuild_metrics(ledger.conn, ledger.match_id, data_root=ledger.data_root) is False


def test_render_match_page(ledger, site_root):
    path = render_match_page(ledger.conn, ledger.match_id, data_root=ledger.data_root)
    assert path == site_root / "matches" / f"{ledger.match_id}.html"
    html = path.read_text(encoding="utf-8")
    assert html.count('<section class="seg ') == 11
    assert "比赛信息" in html and "数据与溯源" in html


def test_verify_sources_passes_then_fails_after_tampering(ledger):
    render_match_page(ledger.conn, ledger.match_id, data_root=ledger.data_root)
    assert verify_sources(ledger.match_id, data_root=ledger.data_root) == []
    assert report_sources(ledger.conn, ledger.match_id, data_root=ledger.data_root)

    path = ledger.data_root / ledger.rel_path
    path.write_text(path.read_text(encoding="utf-8").replace("G1 突发 0", "G1 突发 X"), encoding="utf-8")
    failed = verify_sources(ledger.match_id, data_root=ledger.data_root)
    assert failed, "篡改原始记录后来源校验必须失败"
    assert {ref.rel_path for ref in failed} == {ledger.rel_path}


def test_verify_sources_requires_rendered_page(ledger, site_root):
    with pytest.raises(LookupError, match="页面尚未生成"):
        verify_sources(ledger.match_id, data_root=ledger.data_root)


def test_render_match_page_without_slices(ledger, site_root):
    ledger.conn.execute("DELETE FROM slices")
    ledger.conn.commit()
    facts = collect_facts(ledger.conn, ledger.match_id, data_root=ledger.data_root)
    assert facts.games == ()
    html = render_match_page(ledger.conn, ledger.match_id, data_root=ledger.data_root).read_text(encoding="utf-8")
    assert html.count('<section class="seg ') == 11, "没有切片也必须产出十一段（不可缺段）"


def test_load_segment_facts_uses_declared_index(ledger):
    facts = load_segment_facts(ledger.conn, ledger.match_id)
    assert len(facts) == 1
    assert facts[0].rel_path == ledger.rel_path
    assert facts[0].msg_count == 65
    assert len(facts[0].sha256) == 64


def test_render_match_page_requires_existing_match(ledger, site_root):
    with pytest.raises(LookupError):
        render_match_page(ledger.conn, 999, data_root=ledger.data_root)
    assert paths.site_dir() == site_root
