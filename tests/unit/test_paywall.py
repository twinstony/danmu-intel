"""付费墙判定与付费正文出口（需求 §6.7 / §6.10、AC-2）。

判定只由比赛状态机驱动，**永不**看文件名 / 路径 / 报告形态 / 发布日期（ADR-0009）。
"""

from __future__ import annotations

import pytest

from danmu_intel.common import paywall
from danmu_intel.pipeline import generate_and_publish
from danmu_intel.publish.access import latest_version, report_content
from danmu_intel.report.publish import PublishRefused

GENERATED_AT = 1_790_064_400_000
FORM_KINDS = ("live_brief", "full", "review")


def test_visibility_is_driven_by_the_state_machine_only():
    assert paywall.visibility("ended") == paywall.VISIBILITY_PUBLIC
    for state in ("scheduled", "live", "between_games", "aborted"):
        assert paywall.visibility(state) == paywall.VISIBILITY_PAID
    assert paywall.is_paid("live") and not paywall.is_paid("ended")
    with pytest.raises(TypeError):
        paywall.visibility()  # 没有别的入参，也就不可能按文件名判定


def test_every_form_of_a_live_match_is_paid(three_game_ledger):
    """「永不按文件名/路径判定」：同一形态、同一路径，唯一变量是比赛状态。"""
    ledger = three_game_ledger
    for kind in FORM_KINDS:
        kwargs = {"completed_games": ledger.completed_games} if kind == "live_brief" else {}
        result = generate_and_publish(
            ledger.conn, ledger.match_id, kind=kind, data_root=ledger.data_root,
            generated_at=GENERATED_AT, **kwargs,
        )
        assert result.visibility == paywall.VISIBILITY_PAID
        html = result.path.read_text(encoding="utf-8")
        assert paywall.PAYWALL_MARK in html
        assert "G1 弹幕" not in html, "付费页不得出现任何正文"


def test_ended_match_pages_are_public_without_touching_files(three_game_ledger):
    ledger = three_game_ledger
    result = generate_and_publish(
        ledger.conn, ledger.match_id, kind="full", data_root=ledger.data_root, generated_at=GENERATED_AT
    )
    assert result.visibility == paywall.VISIBILITY_PAID
    ledger.conn.execute("UPDATE matches SET state='ended' WHERE id=?", (ledger.match_id,))
    ledger.conn.commit()
    republished = generate_and_publish(
        ledger.conn, ledger.match_id, kind="full", data_root=ledger.data_root, generated_at=GENERATED_AT
    )
    assert republished.visibility == paywall.VISIBILITY_PUBLIC
    assert paywall.PAYWALL_MARK not in republished.path.read_text(encoding="utf-8")


def test_access_requires_credential_while_the_match_is_running(three_game_ledger):
    ledger = three_game_ledger
    result = generate_and_publish(
        ledger.conn, ledger.match_id, kind="live_brief", data_root=ledger.data_root,
        completed_games=ledger.completed_games, generated_at=GENERATED_AT,
    )
    assert latest_version(ledger.conn, ledger.match_id, "live_brief") == result.version

    with pytest.raises(paywall.PaidAccessDenied):
        report_content(ledger.conn, ledger.match_id, "live_brief")
    content = report_content(ledger.conn, ledger.match_id, "live_brief", credential_verified=True)
    assert [segment.no for segment in content.segments] == [no for no in range(11) if no != 7]

    ledger.conn.execute("UPDATE matches SET state='ended' WHERE id=?", (ledger.match_id,))
    ledger.conn.commit()
    assert report_content(ledger.conn, ledger.match_id, "live_brief").version == result.version


def test_access_needs_a_published_report(three_game_ledger):
    ledger = three_game_ledger
    ledger.conn.execute("UPDATE matches SET state='ended' WHERE id=?", (ledger.match_id,))
    ledger.conn.commit()
    with pytest.raises(LookupError):
        report_content(ledger.conn, ledger.match_id, "review")


def test_failed_versions_do_not_become_the_latest(three_game_ledger):
    """失败的版本进账本但不进读者视野：最新版只看 `state='published'`。"""
    ledger = three_game_ledger
    first = generate_and_publish(
        ledger.conn, ledger.match_id, kind="live_brief", data_root=ledger.data_root,
        completed_games=ledger.completed_games, generated_at=GENERATED_AT,
    )
    raw = ledger.data_root / ledger.rel_path
    raw.write_text(raw.read_text(encoding="utf-8").replace("G1 弹幕 5", "G1 弹幕 X"), encoding="utf-8")
    with pytest.raises(PublishRefused):
        generate_and_publish(
            ledger.conn, ledger.match_id, kind="live_brief", data_root=ledger.data_root,
            completed_games=ledger.completed_games, generated_at=GENERATED_AT,
        )
    states = [
        (row["version"], row["state"])
        for row in ledger.conn.execute(
            "SELECT version, state FROM reports WHERE match_id=? AND kind='live_brief' ORDER BY version",
            (ledger.match_id,),
        )
    ]
    assert states == [(first.version, "published"), (first.version + 1, "failed")]
    assert latest_version(ledger.conn, ledger.match_id, "live_brief") == first.version
