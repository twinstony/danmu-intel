"""灰信号测试（需求 §6.5 的 6 条硬约束逐条守住）。

关键用例（issue #7 验收标准）：**单人单时段刷屏 → 不产出灰信号**。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from danmu_intel.common import paths
from danmu_intel.common.config import StatsConfig
from danmu_intel.stats.basic import RawLine
from danmu_intel.stats.gray import (
    GRAY_STATUSES,
    IDENTITY_FIELDS,
    STATUS_CANDIDATE,
    STATUS_DISCARDED,
    GraySignal,
    contains_identity,
    evaluate_gray_signals,
    reportable,
)

from conftest import REL_PATH, make_event

BASE = 1_790_064_000_000
CONFIG = StatsConfig()
GRAY_MODULE = Path(paths.repo_root()) / "src" / "danmu_intel" / "stats" / "gray.py"


def code_source() -> str:
    """灰信号模块的**代码部分**（去掉模块 docstring）：硬约束测试对代码扫描，不对注释扫描。"""
    tree = ast.parse(GRAY_MODULE.read_text(encoding="utf-8"))
    body = tree.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    return "\n".join(ast.unparse(node) for node in body)


def line(ts: int, text: str, *, user: str = "u1", line_no: int = 1) -> RawLine:
    return RawLine(REL_PATH, line_no, make_event(ts, text=text, user=user))


def crowd(keyword: str = "假赛", *, users: int = 4, windows: int = 3, per: int = 3) -> list[RawLine]:
    """多人多时段的聚集：`windows` 个时段 × `users` 人 × 每人 `per` 条。"""
    result: list[RawLine] = []
    index = 0
    for window in range(windows):
        for user in range(users):
            for _ in range(per):
                index += 1
                result.append(
                    line(
                        BASE + window * CONFIG.gray_window_ms + user * 1_000,
                        f"{keyword} 啊 {index}",
                        user=f"u{user}",
                        line_no=index,
                    )
                )
    return result


def test_single_user_single_window_is_not_a_gray_signal():
    """单人单时段刷屏 → 不产出灰信号（需求 §6.5 第 4 条）。"""
    spam = [line(BASE + index * 100, "假赛吧", user="u1", line_no=index + 1) for index in range(30)]
    signals = evaluate_gray_signals(spam, config=CONFIG)
    assert len(signals) == 1
    only = signals[0]
    assert only.status == STATUS_DISCARDED
    assert only.hit_count == 30 >= CONFIG.gray_min_hits, "命中次数够，但独立用户/时段不够"
    assert only.distinct_users == 1
    assert only.window_count == 1
    assert only.reason and "未达证据门槛" in only.reason
    assert reportable(signals) == (), "不达门槛的灰信号不得进报告"


def test_multi_user_multi_window_becomes_candidate():
    signals = evaluate_gray_signals(crowd(), config=CONFIG)
    assert len(signals) == 1
    candidate = signals[0]
    assert candidate.status == STATUS_CANDIDATE
    assert candidate.category == "cheat_suspicion"
    assert candidate.category_label == "比赛公正性讨论聚集"
    assert (candidate.hit_count, candidate.distinct_users, candidate.window_count) == (36, 4, 3)
    assert candidate.samples and len(candidate.samples) == CONFIG.gray_sample_size
    assert candidate.reason is None
    assert reportable(signals) == (candidate,)


def test_gate_needs_all_three_thresholds():
    one_window_many_users = crowd(windows=1)
    assert reportable(evaluate_gray_signals(one_window_many_users, config=CONFIG)) == ()
    few_users = crowd(users=2)
    assert reportable(evaluate_gray_signals(few_users, config=CONFIG)) == ()
    below_hits = crowd(users=4, windows=3, per=1)  # 12 条 ≥ 5，但每窗口每人 1 条
    assert reportable(evaluate_gray_signals(below_hits, config=CONFIG))
    tight = StatsConfig(gray_min_hits=100)
    assert reportable(evaluate_gray_signals(crowd(), config=tight)) == ()


def test_samples_are_time_ordered_and_traceable():
    signals = evaluate_gray_signals(crowd(), config=CONFIG)
    samples = signals[0].samples
    assert [sample.ts for sample in samples] == sorted(sample.ts for sample in samples)
    assert all(sample.rel_path == REL_PATH and sample.line_no > 0 for sample in samples)
    assert all("假赛" in sample.text for sample in samples)


def test_output_never_carries_identity():
    """需求 §6.5 第 2 条：产出物结构上装不下身份字段。"""
    signals = evaluate_gray_signals(crowd(), config=CONFIG)
    payload = [signal.as_dict() for signal in signals]
    keys = set(payload[0]) | {key for sample in payload[0]["samples"] for key in sample}
    assert keys.isdisjoint(IDENTITY_FIELDS)
    assert json.dumps(payload, ensure_ascii=False, sort_keys=True)
    assert not contains_identity(json.dumps(payload), {"u1", "u2", "u3", "u4"})
    assert contains_identity("用户 u2 发言", {"u2"}) == ["u2"]


def test_status_machine_has_no_accusatory_states():
    """需求 §6.5 第 1 条：状态机里只有风险提示的三态，没有「确认违规」这类结论。"""
    assert GRAY_STATUSES == ("candidate", "escalated", "discarded")
    for signal in evaluate_gray_signals(crowd(), config=CONFIG):
        assert signal.status in GRAY_STATUSES
    for forbidden in ("cheat_", "accused", "guilty", "确认违规", "认定作弊", "作弊者"):
        assert forbidden not in code_source()


def test_module_provides_no_export_interface():
    """需求 §6.5 第 5 条：不得用于勒索/威胁/交易 → 不提供任何对外导出接口。

    灰信号只进库与报告页；模块内不得出现写文件能力（`open` / `write_text` / `dump`）。
    """
    for forbidden in ("open(", "write_text", "write_bytes", "json.dump", "to_csv", "export"):
        assert forbidden not in code_source()
    import danmu_intel.stats.gray as gray

    exported = [name for name in dir(gray) if not name.startswith("_")]
    assert [name for name in exported if "export" in name.lower()] == []


def test_configurable_keywords_and_empty_input():
    assert evaluate_gray_signals([], config=CONFIG) == ()
    only_betting = StatsConfig(gray_keywords=(("盘口", "betting"),), gray_min_hits=2, gray_min_users=2, gray_min_windows=2)
    signals = evaluate_gray_signals(crowd("盘口", users=2, windows=2, per=1), config=only_betting)
    assert len(signals) == 1
    assert signals[0].category == "betting" and signals[0].category_label == "盘口讨论聚集"
    assert GraySignal(
        category="betting", keyword="盘口", hit_count=1, distinct_users=1, window_count=1,
        samples=(), status=STATUS_DISCARDED, reason=None,
    ).as_dict()["samples"] == []
