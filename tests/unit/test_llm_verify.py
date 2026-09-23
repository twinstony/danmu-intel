"""反幻觉后置校验：新数字/新比分/新名称/身份标识必须被拦下，合规文本必须放行。

一条额外的硬要求：**规则直出兜底的文本也必须过同一个校验**（AC-16 的"解读段里每个
数字都能在事实层找到来源"不能只对 LLM 成立）——因此本文件对降级路径的每一段都跑一遍。
"""

from __future__ import annotations

import pytest

from danmu_intel.pipeline import collect_facts
from danmu_intel.report.facts import scope_facts
from danmu_intel.report.forms import ReportScope
from danmu_intel.report.llm.verify import (
    VIOLATION_IDENTITY,
    VIOLATION_NAME,
    VIOLATION_NUMBER,
    VIOLATION_PERCENT,
    VIOLATION_SCORE,
    allowed_facts,
    correction_note,
    describe,
    mask_structural,
    verify_text,
)
from danmu_intel.report.rule_render import interpretation_text
from danmu_intel.report.segments import INTERPRETATION_SEGMENTS


@pytest.fixture
def facts(ledger):
    return collect_facts(ledger.conn, ledger.match_id, data_root=ledger.data_root)


def kinds(violations):
    return {item.kind for item in violations}


def test_fact_layer_numbers_and_tokens_are_allowed(facts):
    allowed = allowed_facts(facts)
    assert "55" in allowed.numbers  # G1 的弹幕条数
    assert "2:0" in allowed.scores  # 官方比分
    for token in ("iG", "LNG", "LPL", "huya", "raw", "game_no"):
        assert allowed.allows_token(token), token
    assert not allowed.allows_token("Faker")
    assert any(":" in form for form in allowed.scores)  # 时间戳的 HH:MM 写法


def test_clean_text_passes(facts):
    text = (
        "从弹幕看，G1 的讨论最集中（55 条），官方结果是 2:0，"
        "本报告覆盖 2 个小局，边界来源是人工指定。这只是注意力层面的观察。"
    )
    assert verify_text(text, facts) == ()


def test_rule_fallback_texts_pass_the_same_check(facts):
    """降级路径的每一段解读都必须能过校验（否则 AC-16 只对 LLM 成立）。"""
    for no in INTERPRETATION_SEGMENTS:
        text = interpretation_text(no, facts)
        assert verify_text(text, facts) == (), f"第 {no} 段的规则直出文本引入了新事实"


def test_rule_fallback_texts_pass_the_check_without_any_peak(facts):
    """没有峰值那条分支也要能过校验（"均值+3σ"这种写法会带出事实层外的数字）。"""
    from dataclasses import replace

    no_peak = replace(
        facts, games=tuple(replace(game, metrics={**game.metrics, "peak": {}}) for game in facts.games)
    )
    for no in INTERPRETATION_SEGMENTS:
        text = interpretation_text(no, no_peak)
        assert verify_text(text, no_peak) == (), f"第 {no} 段的规则直出文本引入了新事实"


def test_a_fabricated_score_is_refused(facts):
    violations = verify_text("官方结果应是 3:0，iG 轻松取胜。", facts)
    assert kinds(violations) == {VIOLATION_SCORE}
    assert describe(violations) == "比分「3:0」"


def test_score_is_compared_as_a_whole_not_digit_by_digit(facts):
    """事实层里既有 1 也有 0（条数、时间），拆开比对就等于没查。"""
    allowed = allowed_facts(facts)
    assert "1" in allowed.numbers and "0" in allowed.numbers
    violations = verify_text("比分 1:0", facts)
    assert kinds(violations) == {VIOLATION_SCORE}, violations


def test_a_fabricated_number_is_refused(facts):
    violations = verify_text("本局弹幕共 999 条，讨论极其热烈。", facts)
    assert kinds(violations) == {VIOLATION_NUMBER}
    assert "999" in describe(violations)


def test_a_fabricated_percentage_is_refused(facts):
    violations = verify_text("讨论量比上一局增长 63%。", facts)
    assert kinds(violations) == {VIOLATION_PERCENT}


def test_a_fabricated_player_name_is_refused(facts):
    violations = verify_text("从弹幕看，Faker 这一局发挥稳定。", facts)
    assert kinds(violations) == {VIOLATION_NAME}
    assert "Faker" in describe(violations)


def test_a_fabricated_team_name_is_refused(facts):
    violations = verify_text("从弹幕看，T1 的节奏更好。", facts)
    assert kinds(violations) == {VIOLATION_NAME}


def test_identity_markers_are_refused(facts):
    """解读文本里出现任何 user_hash 立即作废（需求 §6.5 第 2 条）。"""
    user_hash = facts.all_lines[0].event.user_hash
    assert kinds(verify_text(f"有观众（{user_hash}）在带节奏。", facts)) == {VIOLATION_IDENTITY}
    assert describe(verify_text(f"有观众（{user_hash}）在带节奏。", facts)) == f"身份标识「{user_hash}」"


def test_structural_references_are_masked(facts):
    masked = mask_structural("第 3 段与第 10 段相互印证")
    assert "3" not in masked and "10" not in masked
    assert verify_text("详见第 10 段的取材范围与 G2 的逐局数据。", facts) == ()
    # 屏蔽只针对结构编号：同样一个数字出现在事实断言里仍然要查
    assert VIOLATION_NUMBER in kinds(verify_text("第 10 段说本局有 999 条弹幕。", facts))


def test_timestamps_may_be_written_out(facts):
    game = facts.games[0]
    from danmu_intel.report.rule_render import format_ts

    text = f"G1 起于 {format_ts(game.window.start_ms)}，讨论在 {format_ts(game.window.end_ms)} 前后收束。"
    assert verify_text(text, facts) == ()


def test_clock_form_is_not_mistaken_for_a_score(facts):
    from danmu_intel.report.rule_render import format_ts

    clock = format_ts(facts.games[0].window.start_ms).split(" ")[1]
    assert verify_text(f"开赛时刻 {clock}（本地时间）。", facts) == ()
    assert kinds(verify_text("开赛时刻 07:77（本地时间）。", facts)) >= {VIOLATION_SCORE}


def test_unknown_latin_word_is_refused_but_roles_are_allowed(facts):
    assert kinds(verify_text("从弹幕看，KDA 与 MVP 无法统计。", facts)) == set()
    assert kinds(verify_text("从弹幕看，Chovy 的节奏更好。", facts)) == {VIOLATION_NAME}


def test_correction_note_lists_the_violations(facts):
    violations = verify_text("比分 3:0，Faker 稳定。", facts)
    note = correction_note(violations)
    assert "3:0" in note and "Faker" in note
    assert "只能使用 JSON 中出现过的数字与名称" in note


def test_verification_uses_the_scoped_fact_layer(three_game_ledger):
    """赛中快报的事实层只覆盖已完成节点：未覆盖节点的数字不得被当成"事实层里的"。"""
    facts = collect_facts(three_game_ledger.conn, three_game_ledger.match_id, data_root=three_game_ledger.data_root)
    scoped = scope_facts(facts, ReportScope(completed_games=(1, 2), trigger_game_no=2))
    assert "8" in allowed_facts(facts).numbers  # G3 的 8 条在全量事实层里
    assert "8" not in allowed_facts(scoped).numbers  # 快报不覆盖 G3，因此不算事实
