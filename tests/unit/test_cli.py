"""命令行测试：每个子命令都能跑通，并且失败路径给非零退出码。"""

from __future__ import annotations

import json

import pytest

from danmu_intel.cli import build_parser, main
from danmu_intel.common import paths
from danmu_intel.common.db import open_db
from danmu_intel.common.events import count_lines

from conftest import BASE_TS, REL_PATH, load_fixture


def test_parser_requires_subcommand():
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_match_add_and_get(conn, capsys):
    assert main(["match", "add", "--league", "LPL", "--team-a", "iG", "--team-b", "LNG",
                 "--state", "ended", "--official-result", '{"score":"2:0"}']) == 0
    assert "已登记比赛 #1" in capsys.readouterr().out
    row = conn.execute("SELECT * FROM matches").fetchone()
    assert row["league"] == "LPL" and json.loads(row["official_result"]) == {"score": "2:0"}


def test_slice_and_stats_and_report_and_rebuild_and_verify(ledger, site_root, capsys):
    match_id = ledger.match_id
    assert main(["slice", "--match-id", str(match_id), "--game-no", "1",
                 "--start-ms", str(BASE_TS), "--end-ms", str(BASE_TS + 300_000)]) == 0
    capsys.readouterr()

    assert main(["stats", "--match-id", str(match_id)]) == 0
    out = capsys.readouterr().out
    assert "G1：55 条" in out and "G2：10 条" in out and "算法版本" in out

    assert main(["report", "--match-id", str(match_id), "--kind", "full"]) == 0
    report_out = capsys.readouterr().out
    assert "已发布完整版 v1" in report_out
    page = site_root / "matches" / str(match_id) / "full.html"
    assert page.exists()

    assert main(["reports", "--match-id", str(match_id)]) == 0
    assert "full v1｜published" in capsys.readouterr().out

    assert main(["rebuild", "--match-id", str(match_id)]) == 0
    assert "AC-13 通过" in capsys.readouterr().out

    assert main(["verify-sources", "--match-id", str(match_id), "--kind", "full"]) == 0
    assert "全部来源校验通过" in capsys.readouterr().out


def test_verify_sources_fails_loudly(ledger, site_root, capsys):
    main(["report", "--match-id", str(ledger.match_id), "--kind", "full"])
    capsys.readouterr()
    path = ledger.data_root / REL_PATH
    path.write_text(path.read_text(encoding="utf-8").replace("弹幕 1", "弹幕 X"), encoding="utf-8")
    assert main(["verify-sources", "--match-id", str(ledger.match_id), "--kind", "full"]) == 1
    assert "来源校验失败" in capsys.readouterr().err


def test_verify_sources_without_page(ledger, site_root, capsys):
    assert main(["verify-sources", "--match-id", str(ledger.match_id), "--kind", "full"]) == 2
    assert "页面尚未生成" in capsys.readouterr().err


def test_reports_command_without_reports(data_root, conn, capsys):
    main(["match", "add", "--league", "LPL", "--team-a", "iG", "--team-b", "LNG"])
    capsys.readouterr()
    assert main(["reports", "--match-id", "1"]) == 0
    assert "还没有发布过报告" in capsys.readouterr().out


def test_report_command_rejects_unknown_kind(ledger, capsys):
    assert main(["report", "--match-id", str(ledger.match_id), "--kind", "hourly"]) == 2
    assert "未注册的报告形态" in capsys.readouterr().err


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

    frames = [bytes.fromhex(r["frame_hex"]) for r in load_fixture("huya") if r["kind"] == "danmaku"]
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
    assert "没有待投递事件" in capsys.readouterr().out


def test_boundaries_resolves_and_records_conflict(ledger, capsys):
    """切片引擎命令：官方优先级最高，冲突照样打印出来。"""
    ledger.conn.execute(
        "UPDATE matches SET official_result=? WHERE id=?",
        (json.dumps({"score": "2:0", "games": [{"game_no": 1, "start_ms": BASE_TS,
                                                "end_ms": BASE_TS + 300_000}]}), ledger.match_id),
    )
    ledger.conn.commit()
    capsys.readouterr()
    assert main(["boundaries", "--match-id", str(ledger.match_id),
                 "--report-window", f"1:{BASE_TS + 60_000}:{BASE_TS + 300_000}"]) == 0
    out = capsys.readouterr().out
    assert "已写入 G1" in out and "边界来源 official" in out
    assert "冲突：" in out and "report_window" in out
    rows = ledger.conn.execute("SELECT * FROM slices ORDER BY game_no").fetchall()
    assert [(row["game_no"], row["boundary_source"]) for row in rows] == [(1, "official"), (2, "manual")]


def test_boundaries_dry_run_writes_nothing(ledger, capsys):
    ledger.conn.execute("DELETE FROM slices")
    ledger.conn.commit()
    capsys.readouterr()
    assert main(["boundaries", "--match-id", str(ledger.match_id), "--dry-run",
                 "--report-window", f"1:{BASE_TS}:{BASE_TS + 10_000}"]) == 0
    assert "试算 G1" in capsys.readouterr().out
    assert ledger.conn.execute("SELECT COUNT(*) AS n FROM slices").fetchone()["n"] == 0


def test_boundaries_without_any_candidate(ledger, capsys):
    ledger.conn.execute("DELETE FROM slices")
    ledger.conn.commit()
    capsys.readouterr()
    assert main(["boundaries", "--match-id", str(ledger.match_id)]) == 0
    assert "没有可用的小局边界候选" in capsys.readouterr().out


def test_boundaries_rejects_bad_report_window(ledger, capsys):
    capsys.readouterr()
    assert main(["boundaries", "--match-id", str(ledger.match_id), "--report-window", "1:2"]) == 2
    assert "格式应为" in capsys.readouterr().err


def test_final_and_gray_and_config_commands(ledger, capsys):
    capsys.readouterr()
    assert main(["final", "--match-id", str(ledger.match_id)]) == 0
    out = capsys.readouterr().out
    assert "终局判定：live" in out and "本场没有任何一类独立信号成立" in out

    assert main(["gray", "--match-id", str(ledger.match_id)]) == 0
    out = capsys.readouterr().out
    assert "没有达到门槛的灰信号" in out
    assert "灰信号门槛（config）：命中 ≥5 次" in out
    assert "不提供对外导出" in out

    assert main(["config"]) == 0
    assert "gray_min_users = 3" in capsys.readouterr().out

    assert main(["config", "--set", "gray_min_users=7", "--actor", "管理员"]) == 0
    out = capsys.readouterr().out
    assert "已更新统计门槛" in out and "gray_min_users = 7" in out
    assert ledger.conn.execute("SELECT COUNT(*) AS n FROM audit_log WHERE action='config.update'").fetchone()["n"] == 1

    assert main(["config", "--set", "不存在的键=1"]) == 2
    assert "未知的统计配置项" in capsys.readouterr().err
    assert main(["config", "--set", "bad-format"]) == 2
    assert "格式应为 key=value" in capsys.readouterr().err


def test_gray_command_reports_discarded_with_reason(data_root, conn, capsys):
    from danmu_intel.common.matches import create_match
    from danmu_intel.slice.manual import add_manual_slice
    from conftest import BASE_TS as BASE, REL_PATH as REL, make_event, write_jsonl

    events = [make_event(BASE + index * 100, text="假赛吧", user="u1") for index in range(20)]
    digest = write_jsonl(data_root / REL, events)
    match_id = create_match(conn, league="LPL", team_a="iG", team_b="LNG", state="ended")
    conn.execute(
        "INSERT INTO rooms(platform, room_id, url, discovered_by) VALUES('huya', '660000', 'u', 'manual')"
    )
    conn.execute(
        "INSERT INTO room_sessions(room_id, match_id, pid, started_at, state) VALUES(1, ?, 1, ?, 'exited')",
        (match_id, BASE),
    )
    conn.execute(
        "INSERT INTO danmu_segments(room_session_id, rel_path, sha256, first_ts, last_ts, msg_count, sealed_at) "
        "VALUES(1, ?, ?, ?, ?, ?, ?)",
        (REL, digest, events[0].ts, events[-1].ts, len(events), BASE),
    )
    conn.commit()
    add_manual_slice(conn, match_id=match_id, game_no=1, start_ms=BASE, end_ms=BASE + 60_000)
    capsys.readouterr()
    assert main(["gray", "--match-id", str(match_id)]) == 0
    out = capsys.readouterr().out
    assert "没有达到门槛的灰信号" in out
    assert "已作废：假赛——未达证据门槛" in out
    assert "u1" not in out, "命令行输出里也不得出现身份标识"


# —— 链上监听（T8）——


class FakeChainClient:
    """假的供应商客户端：只实现 CLI 真正用到的两个成员。"""

    def __init__(self, ledger, transfers=None, failure=None):
        self.ledger = ledger
        self._transfers = list(transfers or [])
        self._failure = failure
        self.calls = 0

    def _serve(self):
        self.calls += 1
        if self._failure is not None:
            raise self._failure
        return list(self._transfers)

    def transfers(self, address, *, from_block=0):
        return self._serve()

    def signatures(self, address, *, until=None):
        self._serve()
        return []


def _chain_client(monkeypatch, module_name: str, client) -> None:
    import importlib

    module = importlib.import_module(f"danmu_intel.chain.{module_name}")
    monkeypatch.setattr(module, "client_from_credentials", lambda **kwargs: client)


def _polygon_transfer():
    from danmu_intel.chain.transfer import NATIVE_ASSET, Transfer

    return Transfer(
        network="polygon",
        address="0xabc",
        tx_ref="0xpay1",
        asset=NATIVE_ASSET,
        units=1_000_000_000_000_000_000,
        at_ms=BASE_TS,
        block=100,
    )


def test_chain_usage_prints_quota_and_cursors(conn, capsys):
    from danmu_intel.chain import cursor
    from danmu_intel.chain.quota import POLYGONSCAN, QuotaLedger

    QuotaLedger(conn, POLYGONSCAN).record(calls=3)
    cursor.advance(conn, "polygon", "0xabc", "12345", at_ms=BASE_TS)

    assert main(["chain-usage"]) == 0
    out = capsys.readouterr().out
    assert "polygonscan" in out and "helius" in out
    assert "当日 3 次调用" in out
    assert "免费额度内" in out
    assert "polygon/0xabc：12345" in out


def test_chain_usage_without_any_data(conn, capsys):
    assert main(["chain-usage"]) == 0
    out = capsys.readouterr().out
    assert "当日 0 次调用" in out
    assert "还没有扫过任何地址" in out


def test_chain_watch_requires_an_address(capsys):
    assert main(["chain-watch"]) == 2
    assert "至少要给一个监听地址" in capsys.readouterr().err


def test_chain_watch_without_credentials_is_a_loud_error(conn, capsys):
    assert main(["chain-watch", "--polygon-address", "0xabc", "--once"]) == 2
    assert "POLYGONSCAN_API_KEY" in capsys.readouterr().err


def test_chain_watch_once_prints_transfers(conn, monkeypatch, capsys):
    from danmu_intel.chain.quota import POLYGONSCAN, QuotaLedger

    _chain_client(
        monkeypatch,
        "polygonscan",
        FakeChainClient(QuotaLedger(conn, POLYGONSCAN), transfers=[_polygon_transfer()]),
    )

    assert main(["chain-watch", "--polygon-address", "0xabc", "--once"]) == 0
    out = capsys.readouterr().out
    assert "入账｜polygon/0xabc｜native｜1000000000000000000" in out
    assert "0xpay1" in out
    assert "本轮共看见 1 笔入账" in out


def test_chain_watch_rescan_mode(conn, monkeypatch, capsys):
    from danmu_intel.chain.quota import POLYGONSCAN, QuotaLedger

    _chain_client(
        monkeypatch,
        "polygonscan",
        FakeChainClient(QuotaLedger(conn, POLYGONSCAN), transfers=[_polygon_transfer()]),
    )

    assert main(["chain-watch", "--polygon-address", "0xabc", "--rescan"]) == 0
    out = capsys.readouterr().out
    assert "补扫 1 个地址" in out
    assert "本轮共看见 1 笔入账" in out


def test_chain_watch_loop_mode_stops_at_seconds(conn, monkeypatch, capsys):
    from danmu_intel.chain.quota import POLYGONSCAN, QuotaLedger

    _chain_client(
        monkeypatch,
        "polygonscan",
        FakeChainClient(QuotaLedger(conn, POLYGONSCAN), transfers=[_polygon_transfer()]),
    )

    assert main(["chain-watch", "--polygon-address", "0xabc", "--seconds", "0"]) == 0
    out = capsys.readouterr().out
    assert "开始监听 1 个地址" in out
    assert "本轮共看见 1 笔入账" in out


def test_chain_watch_reports_failures_with_nonzero_exit(conn, monkeypatch, capsys):
    from danmu_intel.chain.provider import RateLimited
    from danmu_intel.chain.quota import POLYGONSCAN, QuotaLedger

    _chain_client(
        monkeypatch,
        "polygonscan",
        FakeChainClient(QuotaLedger(conn, POLYGONSCAN), failure=RateLimited("限速：HTTP 429")),
    )

    assert main(["chain-watch", "--polygon-address", "0xabc", "--once"]) == 1
    captured = capsys.readouterr()
    assert "扫描失败｜polygon/0xabc：限速：HTTP 429" in captured.err
    assert "本轮共看见 0 笔入账（扫描失败 1 个目标）" in captured.out
