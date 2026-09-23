"""端到端：站点产物 → 7 项检查 → 原子发布 → 结束转公开 → 秒级回滚（issue #10 的验收）。

对应 issue #10 的验收标准：

- 检查不通过时**线上仍是上一版**且站点可用（AC-8）；
- 回滚后线上版本 == 上一版（有可验证版本标识）；
- 比赛标记结束后该场**全部**页面自动转公开（AC-2）；
- **静态页面拿不到付费正文**（像 `curl` 一个页面那样直接读字节）；
- 断网可跑：发布器注入假 Vercel 客户端。
"""

from __future__ import annotations

import json

import pytest

from danmu_intel.cli import main
from danmu_intel.common import paywall
from danmu_intel.pipeline import generate_and_publish
from danmu_intel.publish.release import (
    STATE_FAILED,
    current_release,
    list_releases,
    site_version,
)
from danmu_intel.publish.site import report_page_path

GENERATED_AT = 1_790_064_400_000


def publish_brief(ledger) -> None:
    generate_and_publish(
        ledger.conn,
        ledger.match_id,
        kind="live_brief",
        completed_games=ledger.completed_games,
        data_root=ledger.data_root,
        generated_at=GENERATED_AT,
    )


def test_publish_no_deploy_then_ended_flips_everything_public(three_game_ledger, site_root, capsys):
    """CLI 全流程（本地模式）：发布 → 比赛结束 → 自动转公开。"""
    ledger = three_game_ledger
    publish_brief(ledger)

    assert main(["publish", "--no-deploy", "--reason", "首次上线"]) == 0
    out = capsys.readouterr().out
    assert "已发布 v1" in out
    assert f"付费内容（不进静态产物）的比赛：#{ledger.match_id}" in out
    assert "检查｜全站导航唯一：通过" in out
    assert "检查｜付费墙正确：通过" in out
    assert "检查｜来源引用可达：通过" in out

    # 像 curl 一样直接读静态页：付费正文拿不到
    for kind in ("live_brief",):
        body = (site_root / report_page_path(ledger.match_id, kind)).read_text(encoding="utf-8")
        assert paywall.PAYWALL_MARK in body
        assert "取材范围" not in body
        assert "解读，非事实" not in body
    index = (site_root / "index.html").read_text(encoding="utf-8")
    assert "会员（付费）" in index
    assert site_version(site_root)["version"] == 1

    # 状态机写入 → 自动再发布公开版（无人工干预）
    assert main(["match", "set-state", "--match-id", str(ledger.match_id), "--state", "ended",
                 "--no-deploy"]) == 0
    out = capsys.readouterr().out
    assert "状态：live → ended" in out
    assert "已自动再发布公开版 v2" in out
    assert site_version(site_root)["version"] == 2
    public = (site_root / report_page_path(ledger.match_id, "live_brief")).read_text(encoding="utf-8")
    assert paywall.PAYWALL_MARK not in public
    assert "取材范围" in public

    assert main(["releases"]) == 0
    listing = capsys.readouterr().out
    assert "v2｜live" in listing and "v1｜superseded" in listing and "← 线上" in listing


def test_publish_dry_run_reports_a_defect_without_touching_the_site(three_game_ledger, site_root, capsys):
    ledger = three_game_ledger
    publish_brief(ledger)
    assert main(["publish", "--no-deploy"]) == 0
    capsys.readouterr()
    before = {path: path.read_bytes() for path in site_root.rglob("*") if path.is_file()}

    row = ledger.conn.execute(
        "SELECT id, content_json FROM reports WHERE kind='live_brief' AND state='published'"
    ).fetchone()
    payload = json.loads(row["content_json"])
    payload["segments"] = [item for item in payload["segments"] if item["no"] != 9]
    ledger.conn.execute(
        "UPDATE reports SET content_json=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), row["id"])
    )
    ledger.conn.commit()

    assert main(["publish", "--dry-run"]) == 1
    err = capsys.readouterr().err
    assert "不会发布" in err
    after = {path: path.read_bytes() for path in site_root.rglob("*") if path.is_file()}
    assert after == before, "试运行不许动产物"


def test_failed_publish_keeps_the_previous_site_usable(three_game_ledger, site_root, capsys):
    """AC-8：检查不通过 → 拒绝发布且站点保持可用。"""
    ledger = three_game_ledger
    publish_brief(ledger)
    assert main(["publish", "--no-deploy"]) == 0
    capsys.readouterr()
    before = {path: path.read_bytes() for path in site_root.rglob("*") if path.is_file()}

    row = ledger.conn.execute(
        "SELECT id, content_json FROM reports WHERE kind='live_brief' AND state='published'"
    ).fetchone()
    payload = json.loads(row["content_json"])
    payload["segments"] = payload["segments"][:-1]
    ledger.conn.execute(
        "UPDATE reports SET content_json=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), row["id"])
    )
    ledger.conn.commit()

    assert main(["publish", "--no-deploy"]) == 1
    captured = capsys.readouterr()
    assert "发布被拒绝" in captured.err and "站点保持上一版可用" in captured.err

    after = {path: path.read_bytes() for path in site_root.rglob("*") if path.is_file()}
    assert after == before
    assert site_version(site_root)["version"] == 1
    assert current_release(ledger.conn).version == 1
    assert [item.state for item in list_releases(ledger.conn)][0] == STATE_FAILED

    # 报警入口（FR-C5-9）：待投递事件里有这一条
    assert main(["events"]) == 0
    assert "release.failed" in capsys.readouterr().out


def test_rollback_needs_a_deployment(three_game_ledger, site_root, capsys):
    """本地模式没有部署记录 → 回滚如实报错（不假装成功）。"""
    ledger = three_game_ledger
    publish_brief(ledger)
    assert main(["publish", "--no-deploy"]) == 0
    capsys.readouterr()
    assert main(["rollback", "--no-deploy"]) == 2
    err = capsys.readouterr().err
    assert "没有部署标识" in err or "无法回滚" in err


def test_auto_republish_failure_does_not_roll_back_the_state_write(
    three_game_ledger, site_root, capsys
):
    """状态已写入 `ended`，但再发布被检查拦下：如实报错，不回退状态机（线上仍旧版）。"""
    ledger = three_game_ledger
    publish_brief(ledger)
    assert main(["publish", "--no-deploy"]) == 0
    capsys.readouterr()

    row = ledger.conn.execute(
        "SELECT id, content_json FROM reports WHERE kind='live_brief' AND state='published'"
    ).fetchone()
    payload = json.loads(row["content_json"])
    payload["segments"] = [item for item in payload["segments"] if item["no"] != 9]
    ledger.conn.execute(
        "UPDATE reports SET content_json=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), row["id"])
    )
    ledger.conn.commit()

    assert main(["match", "set-state", "--match-id", str(ledger.match_id), "--state", "ended",
                 "--no-deploy"]) == 1
    captured = capsys.readouterr()
    assert "状态：live → ended" in captured.out
    assert "自动再发布没做成" in captured.err
    state = ledger.conn.execute(
        "SELECT state FROM matches WHERE id=?", (ledger.match_id,)
    ).fetchone()["state"]
    assert state == "ended", "状态机的写入是事实，不因发布失败而回退"
    assert site_version(site_root)["version"] == 1


def test_publish_without_vercel_credentials_says_what_to_do(
    three_game_ledger, site_root, capsys, monkeypatch
):
    """没有凭据时如实报错并指路 --no-deploy；测试环境里先摘掉真实凭据（断网可跑）。"""
    for name in ("VERCEL_TOKEN", "VERCEL_PROJECT_ID"):
        monkeypatch.delenv(name, raising=False)
    ledger = three_game_ledger
    publish_brief(ledger)
    assert main(["publish"]) == 2
    err = capsys.readouterr().err
    assert "VERCEL_TOKEN" in err and "--no-deploy" in err


def test_publish_commands_are_documented(capsys):
    from danmu_intel.cli import build_parser

    usage = build_parser().format_help()
    for command in ("publish", "releases", "rollback"):
        assert command in usage
    assert "原子发布" in usage and "秒级回滚" in usage
    with pytest.raises(SystemExit):
        main(["match", "--help"])
    assert "set-state" in capsys.readouterr().out
