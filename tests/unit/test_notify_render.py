"""通知文本：三段式（级别 + 事件名 + 主语 / 细节 / 入库时刻）。"""

from __future__ import annotations

import re

import pytest

from danmu_intel.common.notifications import Notification
from danmu_intel.notify.render import KIND_LABELS, MAX_DETAIL_CHARS, format_message

from conftest import BASE_TS


def make(kind: str, *, severity: str = "warning", payload: dict | None = None) -> Notification:
    return Notification(
        id=1,
        kind=kind,
        severity=severity,
        payload=payload if payload is not None else {},
        created_at=BASE_TS,
        state="pending",
    )


def test_collection_event_renders_as_three_lines():
    text = format_message(
        make(
            "process_exit",
            severity="critical",
            payload={"match_id": 1, "platform": "huya", "room_id": "660000", "exit_code": -9},
        )
    )

    headline, detail, stamp = text.splitlines()
    assert headline == "【严重】采集进程退出｜比赛 #1｜huya/660000"
    assert detail == "exit_code=-9"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", stamp)


def test_every_known_kind_has_a_chinese_label():
    assert all(label and not label.startswith("{") for label in KIND_LABELS.values())
    assert KIND_LABELS["drop_rate_high"] == "落盘丢包率超阈"
    assert KIND_LABELS["billing_member_sweep_failed"] == "会员到期批处理失败"


def test_unknown_kind_is_printed_as_is():
    text = format_message(make("brand.new.kind", payload={}))
    assert text.startswith("【注意】brand.new.kind")


def test_recovery_notification_is_labelled():
    text = format_message(
        make(
            "chain_rate_limited.resolved",
            severity="info",
            payload={"provider": "polygonscan", "resolved": True},
        )
    )

    assert text.startswith("【提示】供应商限速（已恢复）｜供应商 polygonscan")
    assert "resolved" not in text  # 恢复标记本身不重复进细节


def test_recovery_label_follows_the_kind_suffix_not_the_payload_flag():
    """抑制与渲染看同一个信号（kind 后缀），两处不许各有一套判据。"""
    text = format_message(make("chain_rate_limited", payload={"provider": "polygonscan", "resolved": True}))
    assert text.startswith("【注意】供应商限速｜供应商 polygonscan")


def test_order_and_provider_become_the_subject():
    text = format_message(make("billing_payment_short", payload={"order_ref": "DM1A2B", "shortage_units": 5}))
    assert text.splitlines()[0] == "【注意】订单待补款｜订单 DM1A2B"
    assert text.splitlines()[1] == "shortage_units=5"


def test_release_failure_without_other_subject_fields_falls_back_to_the_release_number():
    text = format_message(make("release.failed", severity="critical", payload={"release": 3, "reason": "检查不过"}))
    assert text.splitlines()[0] == "【严重】发布失败｜发布批次 #3"
    assert text.splitlines()[1] == "reason=检查不过"


def test_room_only_payload_uses_the_room_as_subject():
    text = format_message(make("stalled", payload={"room_id": "660000"}))
    assert text.splitlines()[0] == "【注意】断流重连｜房间 660000"


def test_match_only_payload_has_no_room_part():
    text = format_message(make("llm_cost_gate", severity="critical", payload={"match_id": 7, "spent_match_cny": 0.31}))
    assert text.startswith("【严重】解读层成本超闸｜比赛 #7")
    assert text.splitlines()[1] == "spent_match_cny=0.31"


def test_nested_values_are_json_encoded_and_none_is_skipped():
    text = format_message(
        make("release.failed", payload={"failed_checks": ["a", "b"], "detail": None, "hint": ""})
    )
    assert 'failed_checks=["a", "b"]' in text
    assert "detail" not in text and "hint" not in text


def test_long_detail_is_truncated_to_keep_the_message_sendable():
    text = format_message(make("release.failed", payload={"reason": "很长的原因" * 200}))
    detail = text.splitlines()[1]
    assert len(detail) == MAX_DETAIL_CHARS
    assert detail.endswith("…")


@pytest.mark.parametrize("severity,label", [("info", "提示"), ("warning", "注意"), ("critical", "严重")])
def test_severity_labels(severity, label):
    assert format_message(make("disk_low", severity=severity, payload={})).startswith(f"【{label}】")
