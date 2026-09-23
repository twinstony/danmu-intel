"""T4 流水线集成测试：统计全集/终局判定/灰信号落库 + 报告渲染的硬约束。

覆盖 issue #7 的四条验收标准：

- 每条切片有 `boundary_source`，冲突时有 `conflict_note`（切片引擎单测 + 这里落库后复查）
- 人工修正必须带理由，落审计（`algo_version` 随之递增）
- 删掉 `metrics` 后仅凭原始记录 + 切片 + 配置可重算出**完全一致**的统计（AC-13）
- 灰信号：单人单时段刷屏不产出；渲染输出中零用户名
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from danmu_intel.common import audit
from danmu_intel.common.config import StatsConfig, save_stats_config
from danmu_intel.common.matches import create_match
from danmu_intel.pipeline import (
    clear_metrics,
    collect_facts,
    generate_and_publish,
    metrics_snapshot,
    rebuild_metrics,
    write_gray_signals,
    write_metrics,
)
from danmu_intel.slice.manual import add_manual_slice
from danmu_intel.stats.basic import ALGO_VERSION
from danmu_intel.stats.gray import STATUS_CANDIDATE, STATUS_DISCARDED

from conftest import make_event, write_jsonl

BASE = 1_790_064_000_000
REL = "raw/huya/2026-09-22/777777-16.jsonl"
GAME_SPAN = 1_200_000


def _ledger(data_root, conn, *, spam_users: list[str] | None = None, identity_trap: bool = False):
    """造一场「峰值 → 长期静默 → 收局」的账本，让 3 类独立信号同时成立。"""
    events = [make_event(BASE + index * 1_000, text=f"热闹 {index}") for index in range(40)]
    events.append(make_event(BASE + 900_000, text="比分 2:0"))
    events.append(make_event(BASE + 1_000_000, text="官宣了"))
    events.append(make_event(BASE + 1_010_000, text="官宣了"))
    events.append(make_event(BASE + 1_150_000, text="收尾"))
    for user in spam_users or []:
        for index, window in enumerate(range(3)):
            events.append(
                make_event(BASE + window * 300_000 + index * 1_000, text="假赛吧", user=user)
            )
    if identity_trap:
        # 故意把某个身份哈希做成弹幕原文：渲染层必须拦住这种泄漏
        events.append(make_event(BASE + 1_150_001, text="假赛", user="假赛"))
    digest = write_jsonl(data_root / REL, events)
    match_id = create_match(
        conn,
        league="LPL",
        team_a="iG",
        team_b="LNG",
        state="ended",
        ended_at=BASE + 1_000_000,  # 官方宣告早于观测结束，反转窗口走得完
        official_result={"score": "2:0"},
    )
    conn.execute(
        "INSERT INTO rooms(platform, room_id, url, streamer, discovered_by, is_live, last_seen_at) "
        "VALUES('huya', '777777', 'https://www.huya.com/777777', '样例主播', 'manual', 1, ?)",
        (BASE,),
    )
    room_id = conn.execute("SELECT id FROM rooms").fetchone()["id"]
    conn.execute(
        "INSERT INTO room_sessions(room_id, match_id, pid, started_at, ended_at, state, last_msg_at) "
        "VALUES(?, ?, 1, ?, ?, 'exited', ?)",
        (room_id, match_id, BASE, BASE + GAME_SPAN, BASE + 1_150_000),
    )
    session_id = conn.execute("SELECT id FROM room_sessions").fetchone()["id"]
    conn.execute(
        "INSERT INTO danmu_segments(room_session_id, rel_path, sha256, first_ts, last_ts, msg_count, sealed_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?)",
        (session_id, REL, digest, events[0].ts, events[-1].ts, len(events), BASE + GAME_SPAN),
    )
    conn.commit()
    add_manual_slice(conn, match_id=match_id, game_no=1, start_ms=BASE, end_ms=BASE + GAME_SPAN)
    return match_id


@pytest.fixture
def t4_ledger(data_root, conn):
    match_id = _ledger(data_root, conn, spam_users=["u1", "u2", "u3"])
    return conn, data_root, match_id


# --------------------------------------------------------------------------- #
# 统计全集 + 终局判定
# --------------------------------------------------------------------------- #


def test_facts_include_full_metrics_final_signal_and_gray_signals(t4_ledger):
    conn, data_root, match_id = t4_ledger
    facts = collect_facts(conn, match_id, data_root=data_root)

    judgement = facts.final_judgement
    assert judgement.verdict == "final", judgement.reason
    assert set(judgement.kinds) == {"traffic_drop", "score_confirmed", "announcement"}
    assert judgement.decided_at_ms == judgement.satisfied_at_ms + 120_000

    metrics = facts.games[0].metrics
    assert metrics["danmu_total"]["count"] == 53
    assert metrics["score"]["consistent"] is True
    assert metrics["kill_timeline"]["events"] == []
    assert metrics["neutral"]["side_mentions"]["team_a"]["count"] == 0
    assert metrics["trough"]["count"] == 0

    signals = facts.gray_signals
    assert [signal.status for signal in signals] == [STATUS_CANDIDATE]
    assert (signals[0].hit_count, signals[0].distinct_users, signals[0].window_count) == (9, 3, 3)
    assert facts.reportable_gray_signals == signals


def test_single_user_spam_produces_no_reportable_gray_signal(data_root, conn):
    match_id = _ledger(data_root, conn, spam_users=["only-me"])
    facts = collect_facts(conn, match_id, data_root=data_root)
    assert [signal.status for signal in facts.gray_signals] == [STATUS_DISCARDED]
    assert facts.reportable_gray_signals == ()
    assert "未达证据门槛" in facts.gray_signals[0].reason


def test_metrics_store_snapshot_covers_final_and_gray(t4_ledger):
    conn, data_root, match_id = t4_ledger
    facts = collect_facts(conn, match_id, data_root=data_root)
    write_metrics(conn, facts)
    snapshot = dict(((game_no, key), value) for game_no, key, value in metrics_snapshot(conn, match_id))
    final_value = json.loads(snapshot[(None, "final_signal")])
    assert final_value["verdict"] == "final" and final_value["decided_at_ms"]
    gray_value = json.loads(snapshot[(None, "gray_signals")])
    assert gray_value["candidate_count"] == 1
    assert gray_value["candidates"][0]["samples"][0]["text"].startswith("假赛")
    assert gray_value["discarded"] == []


def test_gray_signals_are_persisted_with_samples_and_status(t4_ledger):
    conn, data_root, match_id = t4_ledger
    facts = collect_facts(conn, match_id, data_root=data_root)
    assert write_gray_signals(conn, facts, now=BASE) == 1
    row = conn.execute("SELECT * FROM gray_signals").fetchone()
    assert row["match_id"] == match_id
    assert row["status"] == STATUS_CANDIDATE and row["reason"] is None
    assert row["category"] == "cheat_suspicion" and row["keyword"] == "假赛"
    assert (row["hit_count"], row["distinct_users"], row["window_count"]) == (9, 3, 3)
    samples = json.loads(row["samples_json"])
    assert samples and {"ts", "text", "rel_path", "line_no"} == set(samples[0])
    assert "user_hash" not in row["samples_json"]

    # 重算是幂等的（不叠加重复行）
    write_gray_signals(conn, facts, now=BASE + 1)
    assert conn.execute("SELECT COUNT(*) AS n FROM gray_signals").fetchone()["n"] == 1


def test_gray_signal_persistence_requires_samples(t4_ledger):
    conn, data_root, match_id = t4_ledger
    facts = collect_facts(conn, match_id, data_root=data_root)
    broken = dataclasses.replace(facts.gray_signals[0], samples=())
    with pytest.raises(ValueError, match="必须附样本"):
        write_gray_signals(conn, dataclasses.replace(facts, gray_signals=(broken,)))


def test_escalated_gray_signal_survives_recompute(t4_ledger):
    conn, data_root, match_id = t4_ledger
    facts = collect_facts(conn, match_id, data_root=data_root)
    write_gray_signals(conn, facts, now=BASE)
    conn.execute("UPDATE gray_signals SET status='escalated'")
    conn.commit()
    write_gray_signals(conn, facts, now=BASE + 10)
    rows = conn.execute("SELECT status FROM gray_signals").fetchall()
    assert [row["status"] for row in rows] == ["escalated", "candidate"]


def test_thresholds_come_from_config_table(data_root, conn):
    match_id = _ledger(data_root, conn, spam_users=["u1", "u2", "u3"])
    save_stats_config(conn, actor="管理员", changes={"gray_min_users": 9})
    facts = collect_facts(conn, match_id, data_root=data_root)
    assert facts.gray_signals[0].status == STATUS_DISCARDED
    assert "9" in (facts.gray_signals[0].reason or "")
    assert isinstance(facts.stats_config, StatsConfig)


# --------------------------------------------------------------------------- #
# AC-13：删统计后可重算（含人工修正后的版本）
# --------------------------------------------------------------------------- #


def test_rebuild_is_identical_and_covers_t4_metrics(t4_ledger):
    conn, data_root, match_id = t4_ledger
    facts = collect_facts(conn, match_id, data_root=data_root)
    write_metrics(conn, facts)
    before = metrics_snapshot(conn, match_id, algo_version=facts.algo_version)
    assert before

    clear_metrics(conn, match_id)  # 删掉**全部**统计结果
    assert metrics_snapshot(conn, match_id) == []
    assert rebuild_metrics(conn, match_id, data_root=data_root) is True
    assert metrics_snapshot(conn, match_id, algo_version=ALGO_VERSION) == before


def test_manual_correction_keeps_old_version_and_recomputes_new(t4_ledger):
    conn, data_root, match_id = t4_ledger
    facts = collect_facts(conn, match_id, data_root=data_root)
    write_metrics(conn, facts)
    assert facts.algo_version == ALGO_VERSION

    # 人工修正：官方时间回填（边界改 30 秒）→ 审计 + 版本递增 + 重算
    add_manual_slice(
        conn,
        match_id=match_id,
        game_no=1,
        start_ms=BASE,
        end_ms=BASE + GAME_SPAN + 30_000,
        override_by="管理员",
        override_reason="官方时间回填",
        override_at=BASE + 2_000_000,
    )
    corrected = collect_facts(conn, match_id, data_root=data_root)
    assert corrected.algo_version == f"{ALGO_VERSION}+ov1"
    write_metrics(conn, corrected)

    versions = {row["algo_version"] for row in conn.execute("SELECT algo_version FROM metrics")}
    assert versions == {ALGO_VERSION, f"{ALGO_VERSION}+ov1"}, "旧版本不删（设计 §9.3）"
    assert any(
        window.boundary_source == "manual" and window.override_reason == "官方时间回填"
        for window in [game.window for game in corrected.games]
    )
    assert audit.count(conn, action=audit.SLICE_OVERRIDE, target_prefix=f"match:{match_id}/") == 1
    assert rebuild_metrics(conn, match_id, data_root=data_root) is True


# --------------------------------------------------------------------------- #
# 渲染层的灰信号硬约束
# --------------------------------------------------------------------------- #


def _publish_full(conn, match_id, data_root):
    """走发布钩子发一份完整版，返回页面正文（报告层消费 T4 统计产物的唯一入口）。"""
    return generate_and_publish(
        conn, match_id, kind="full", data_root=data_root
    ).path.read_text(encoding="utf-8")


def test_report_renders_samples_without_any_identity(t4_ledger, site_root):
    conn, data_root, match_id = t4_ledger
    html = _publish_full(conn, match_id, data_root)
    assert "灰信号汇总" in html
    assert "样本：" in html and "原文「假赛吧」" in html
    assert "不构成对任何个人或队伍的任何指控" in html
    assert "不出现指控性结论" in html
    for user_hash in {event.user_hash for event in
                      [line.event for line in collect_facts(conn, match_id, data_root=data_root).all_lines]}:
        assert user_hash not in html, "渲染输出中零用户名（需求 §6.5 第 2 条）"


def test_render_guard_blocks_identity_leak(data_root, conn, site_root):
    match_id = _ledger(data_root, conn, spam_users=["u1", "u2", "u3"], identity_trap=True)
    with pytest.raises(ValueError, match="禁止输出任何身份标识"):
        generate_and_publish(conn, match_id, kind="full", data_root=data_root)


def test_render_marks_not_judged_and_reports_gate(t4_ledger, site_root, monkeypatch):
    conn, data_root, match_id = t4_ledger
    # 把门槛提到不可能达到 → 灰信号全部作废，报告要如实说「未达门槛」
    save_stats_config(conn, actor="管理员", changes={"gray_min_hits": 999})
    html = _publish_full(conn, match_id, data_root)
    assert "未产出达到门槛的灰信号" in html
    assert "未达证据门槛" not in html or "已按纪律作废" in html


def test_render_without_correction_shows_manual_boundary_label(t4_ledger, site_root):
    conn, data_root, match_id = t4_ledger
    html = _publish_full(conn, match_id, data_root)
    assert "边界来源：人工指定 1 局" in html
    assert "终局判定：已终局" in html
    assert "比分：官方 2:0" in html
