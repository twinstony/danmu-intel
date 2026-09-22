"""命令行测试：每个子命令都能跑通，并且失败路径给非零退出码。"""

from __future__ import annotations

import json

import pytest

from danmu_intel.cli import build_parser, main
from danmu_intel.common import paths
from danmu_intel.common.db import open_db
from danmu_intel.common.events import count_lines

from conftest import BASE_TS, REL_PATH, load_huya_fixture


def test_parser_requires_subcommand():
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_match_add_and_get(conn, capsys):
    assert main(["match", "add", "--league", "LPL", "--team-a", "iG", "--team-b", "LNG",
                 "--state", "ended", "--official-result", '{"score":"2:0"}']) == 0
    assert "已登记比赛 #1" in capsys.readouterr().out
    row = conn.execute("SELECT * FROM matches").fetchone()
    assert row["league"] == "LPL" and json.loads(row["official_result"]) == {"score": "2:0"}


def test_slice_and_stats_and_render_and_rebuild_and_verify(ledger, site_root, capsys):
    match_id = ledger.match_id
    assert main(["slice", "--match-id", str(match_id), "--game-no", "1",
                 "--start-ms", str(BASE_TS), "--end-ms", str(BASE_TS + 300_000)]) == 0
    capsys.readouterr()

    assert main(["stats", "--match-id", str(match_id)]) == 0
    out = capsys.readouterr().out
    assert "G1：55 条" in out and "G2：10 条" in out and "算法版本" in out

    assert main(["render", "--match-id", str(match_id)]) == 0
    assert "已生成静态页" in capsys.readouterr().out
    page = site_root / "matches" / f"{match_id}.html"
    assert page.exists()

    assert main(["rebuild", "--match-id", str(match_id)]) == 0
    assert "AC-13 通过" in capsys.readouterr().out

    assert main(["verify-sources", "--match-id", str(match_id)]) == 0
    assert "全部来源校验通过" in capsys.readouterr().out


def test_verify_sources_fails_loudly(ledger, site_root, capsys):
    main(["render", "--match-id", str(ledger.match_id)])
    path = ledger.data_root / REL_PATH
    path.write_text(path.read_text(encoding="utf-8").replace("弹幕 1", "弹幕 X"), encoding="utf-8")
    assert main(["verify-sources", "--match-id", str(ledger.match_id)]) == 1
    assert "来源校验失败" in capsys.readouterr().err


def test_verify_sources_without_page(ledger, site_root, capsys):
    assert main(["verify-sources", "--match-id", str(ledger.match_id)]) == 2
    assert "页面尚未生成" in capsys.readouterr().err


def test_cli_reports_errors(conn, capsys):
    assert main(["stats", "--match-id", "999"]) == 2
    assert "错误" in capsys.readouterr().err
    assert main(["slice", "--match-id", "1", "--game-no", "1", "--start-ms", "5", "--end-ms", "5"]) == 2
    assert "切片起止非法" in capsys.readouterr().err


def test_cli_verbose_sets_logging(conn, capsys):
    assert main(["--verbose", "match", "add", "--league", "LEC", "--team-a", "G2", "--team-b", "FNC"]) == 0
    assert "已登记比赛" in capsys.readouterr().out


def test_collect_command_with_replay_adapter(data_root, monkeypatch, capsys):
    from tests.contract.test_adapter_contract import ReplayTransport
    from danmu_intel.collect.huya import HuyaAdapter

    frames = [bytes.fromhex(r["frame_hex"]) for r in load_huya_fixture() if r["kind"] == "danmaku"]
    adapter = HuyaAdapter(transport=ReplayTransport([frames]))
    monkeypatch.setattr("danmu_intel.collect.ADAPTERS", {"huya": adapter})
    monkeypatch.setattr("danmu_intel.collect.adapter.RECONNECT_BACKOFF_S", (60.0,))

    async def fake_page(room_id: str) -> str:
        return '"lProfileRoom":660000,"lYyid":1,"lChannelId":2,"lSubChannelId":2,"eLiveStatus":2,"sNick":"样例主播"'

    monkeypatch.setattr("danmu_intel.collect.huya.fetch_page", fake_page)

    assert main(["match", "add", "--league", "LPL", "--team-a", "iG", "--team-b", "LNG"]) == 0
    capsys.readouterr()
    assert main(["collect", "--url", "https://www.huya.com/660000", "--seconds", "2", "--match-id", "1"]) == 0
    out = capsys.readouterr().out
    assert "共 33 条弹幕" in out
    assert "落盘：" in out and "SHA256" in out

    raw_files = list((paths.data_dir() / "raw" / "huya").rglob("*.jsonl"))
    assert len(raw_files) == 1
    assert count_lines(raw_files[0]) == 33

    conn = open_db(paths.db_path())
    try:
        assert conn.execute("SELECT COUNT(*) AS n FROM danmu_segments").fetchone()["n"] == 1
    finally:
        conn.close()


def test_collect_command_rejects_unknown_platform(data_root, capsys):
    assert main(["collect", "--url", "https://www.huya.com/1", "--platform", "twitch",
                 "--seconds", "1", "--match-id", "1"]) == 2
    assert "未注册的平台适配器" in capsys.readouterr().err


def test_collect_command_requires_registered_match(data_root, capsys):
    assert main(["collect", "--url", "https://www.huya.com/1", "--seconds", "1", "--match-id", "7"]) == 2
    assert "未找到比赛 #7" in capsys.readouterr().err


def test_collect_command_requires_match_id(capsys):
    with pytest.raises(SystemExit):
        main(["collect", "--url", "https://www.huya.com/1", "--seconds", "1"])


# —— T2：多房间监督与可见性 ——


def test_supervise_rejects_duplicate_rooms(data_root, capsys, monkeypatch):
    monkeypatch.setattr("danmu_intel.collect.supervisor.POLL_INTERVAL_S", 0.01)
    main(["match", "add", "--league", "LPL", "--team-a", "iG", "--team-b", "LNG"])
    capsys.readouterr()
    assert main(["supervise", "--match-id", "1", "--room", "https://www.huya.com/660000",
                 "--room", "https://www.huya.com/660000"]) == 2
    assert "重复的直播间" in capsys.readouterr().err


def test_supervise_rejects_unknown_match(data_root, capsys):
    assert main(["supervise", "--match-id", "7", "--room", "https://www.huya.com/660000"]) == 2
    assert "未找到比赛 #7" in capsys.readouterr().err


def test_supervise_runs_three_rooms_and_reports(data_root, capsys, monkeypatch):
    """三个房间并发监督：一房间一子进程，收工后逐个汇报（真子进程替换为假进程）。"""
    from tests.unit.test_supervisor import FakeProcess

    monkeypatch.setattr("danmu_intel.collect.supervisor.POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr("danmu_intel.collect.supervisor.popen", lambda command, env: FakeProcess(pid=4242 + len(command)))

    main(["match", "add", "--league", "LPL", "--team-a", "iG", "--team-b", "LNG"])
    capsys.readouterr()
    code = main([
        "supervise", "--match-id", "1", "--seconds", "0.05",
        "--room", "https://www.huya.com/660000",
        "--room", "https://www.huya.com/323444",
        "--room", "https://www.huya.com/11342412",
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert "开始监督 3 个直播间" in out
    assert out.count("重启 0 次") == 3
    assert out.count("重连 0 次") == 3


def test_health_prints_live_and_dead_rooms(ledger, capsys):
    assert main(["health", "--match-id", str(ledger.match_id)]) == 0
    out = capsys.readouterr().out
    assert "huya/660000" in out
    assert "状态 exited" in out
    assert "重启 0 次" in out and "已收 65 条" in out


def test_health_without_sessions(data_root, capsys):
    main(["match", "add", "--league", "LPL", "--team-a", "iG", "--team-b", "LNG"])
    capsys.readouterr()
    assert main(["health", "--match-id", "1"]) == 0
    assert "还没有采集会话" in capsys.readouterr().out


def test_contribution_prints_per_room(ledger, capsys):
    assert main(["contribution", "--match-id", str(ledger.match_id)]) == 0
    out = capsys.readouterr().out
    assert "huya/660000：65 条｜去重后 65 条（重复 0 条）" in out
    assert "时间跨度 354 秒" in out
    assert "合计：65 条｜去重后 65 条（1 个直播间）" in out


def test_contribution_without_records(data_root, capsys):
    main(["match", "add", "--league", "LPL", "--team-a", "iG", "--team-b", "LNG"])
    capsys.readouterr()
    assert main(["contribution", "--match-id", "1"]) == 0
    assert "还没有落盘记录" in capsys.readouterr().out


def test_events_prints_incidents(data_root, conn, capsys):
    from danmu_intel.collect.incidents import DISK_LOW, emit

    main(["match", "add", "--league", "LPL", "--team-a", "iG", "--team-b", "LNG"])
    emit(conn, DISK_LOW, severity="critical", platform="huya", room_id="660000", match_id=1,
         detail={"free_bytes": 1024})
    capsys.readouterr()

    assert main(["events", "--match-id", "1"]) == 0
    out = capsys.readouterr().out
    assert "disk_low（critical，pending）" in out
    assert "huya/660000" in out and '"free_bytes": 1024' in out


def test_events_reports_empty(data_root, capsys):
    assert main(["events"]) == 0
    assert "没有采集异常事件" in capsys.readouterr().out
