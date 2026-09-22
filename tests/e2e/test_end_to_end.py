"""端到端：一条条命令跑通「采集 → 落盘 → 采集切片 → 统计 → 十一段静态页」。

**全程不连外网**：平台数据用录制帧回放（NFR-GA-4）。对应 issue #4 的验收标准：
JSONL 路径与字段符合契约、行数与采集计数一致、页含全部十一段且标题与 §6.6 逐字一致、
每项事实的来源可复核、删统计后能重算、全库零命中凭据。
"""

from __future__ import annotations

import json

import pytest

from danmu_intel.cli import main
from danmu_intel.common import paths
from danmu_intel.common.db import open_db
from danmu_intel.common.events import JSONL_FIELDS, count_lines, iter_events
from danmu_intel.pipeline import collect_facts, metrics_snapshot, rebuild_metrics, verify_sources
from danmu_intel.report.html import parse_sources
from danmu_intel.report.segments import SEGMENTS
from tools.check_no_secrets import scan_tree

from conftest import load_huya_fixture
from tests.contract.test_adapter_contract import ReplayTransport

PAGE = '"lProfileRoom":660000,"lYyid":1486578378,"lChannelId":1346609715,"lSubChannelId":1346609715,"eLiveStatus":2,"sNick":"样例主播","sRoomName":"标题","sGameFullName":"英雄联盟"'


@pytest.fixture
def replayed(request, monkeypatch):
    """把虎牙适配器接到录制帧上（不连网）。"""
    from danmu_intel.collect.huya import HuyaAdapter

    frames = [bytes.fromhex(record["frame_hex"]) for record in load_huya_fixture() if record["kind"] == "danmaku"]
    adapter = HuyaAdapter(transport=ReplayTransport([frames]))
    monkeypatch.setattr("danmu_intel.collect.ADAPTERS", {"huya": adapter})
    monkeypatch.setattr("danmu_intel.collect.adapter.RECONNECT_BACKOFF_S", (60.0,))

    async def fake_page(room_id: str) -> str:
        return PAGE

    monkeypatch.setattr("danmu_intel.collect.huya.fetch_page", fake_page)
    return len(frames)


def test_worst_case_chain_from_collect_to_page(replayed, data_root, site_root, capsys):
    collected = replayed

    # ① 先登记比赛，再一条命令采集（采集必须挂在一场比赛上）
    assert main(["match", "add", "--league", "LPL", "--team-a", "iG", "--team-b", "LNG",
                 "--state", "ended", "--official-result", '{"score":"2:0"}']) == 0
    capsys.readouterr()
    assert main(["collect", "--url", "https://www.huya.com/660000", "--seconds", "2",
                 "--match-id", "1"]) == 0
    collect_out = capsys.readouterr().out
    assert f"共 {collected} 条弹幕" in collect_out

    # ② 登记的落盘路径与字段符合契约，行数与采集计数一致
    raw_files = sorted((data_root / "raw" / "huya").rglob("*.jsonl"))
    assert len(raw_files) == 1
    raw = raw_files[0]
    rel_path = paths.rel_to_data(raw)
    assert rel_path.startswith("raw/huya/")
    assert count_lines(raw) == collected
    events = [event for _, event in iter_events(raw)]
    assert len(events) == collected
    assert all(list(event.to_json()) == list(JSONL_FIELDS) for event in events)
    assert all(event.room_id == "660000" for event in events)
    assert [event.ts for event in events] == sorted(event.ts for event in events)
    # 不落明文身份
    assert "样例用户" not in raw.read_text(encoding="utf-8")
    assert len({event.user_hash for event in events}) > 1

    # ③ 人工指定这一局的起止（覆盖刚采到的这段）
    start = min(event.ts for event in events)
    end = max(event.ts for event in events) + 1
    match_id = 1
    assert main(["slice", "--match-id", str(match_id), "--game-no", "1",
                 "--start-ms", str(start), "--end-ms", str(end)]) == 0
    assert main(["slice", "--match-id", str(match_id), "--game-no", "1",
                 "--start-ms", str(start), "--end-ms", str(end)]) == 0  # 同上边界可重复执行
    capsys.readouterr()

    # ④ 基础统计 + ⑤ 规则直出十一段静态页
    assert main(["stats", "--match-id", str(match_id)]) == 0
    stats_out = capsys.readouterr().out
    assert f"G1：{collected} 条" in stats_out
    assert main(["render", "--match-id", str(match_id)]) == 0
    capsys.readouterr()

    page = site_root / "matches" / f"{match_id}.html"
    html = page.read_text(encoding="utf-8")
    for spec in SEGMENTS:
        assert f'id="seg-{spec.no}"' in html
        assert f"{spec.no}</span> {spec.title}" in html
    assert html.count('<section class="seg ') == 11

    # ⑥ 页面上每一项事实的来源都可复核（文件 + 行范围 + SHA256）
    refs = parse_sources(html)
    assert refs, "页面上必须能取到来源引用"
    assert all(ref.rel_path == rel_path for ref in refs)
    assert verify_sources(match_id, data_root=data_root) == []

    # ⑦ AC-13：删掉统计结果后，仅凭原始记录 + 切片能重算出同样的统计
    assert main(["rebuild", "--match-id", str(match_id)]) == 0
    assert "AC-13 通过" in capsys.readouterr().out

    # ⑧ 全库零命中可动用资产凭据（AC-12）
    assert scan_tree(paths.repo_root()) == []
    assert scan_tree(data_root) == []


def test_page_still_complete_when_no_peak(replayed, data_root, site_root, capsys):
    """没有显著峰值时也必须产出完整十一段（缺段不得生成）。"""
    assert main(["match", "add", "--league", "LPL", "--team-a", "iG", "--team-b", "LNG"]) == 0
    assert main(["collect", "--url", "https://www.huya.com/660000", "--seconds", "2",
                 "--match-id", "1"]) == 0
    capsys.readouterr()
    match_id = 1
    raw = next((data_root / "raw" / "huya").rglob("*.jsonl"))
    rel = paths.rel_to_data(raw)
    times = [event.ts for _, event in iter_events(data_root / rel)]

    assert main(["slice", "--match-id", str(match_id), "--game-no", "1",
                 "--start-ms", str(min(times)), "--end-ms", str(max(times) + 1)]) == 0
    assert main(["render", "--match-id", str(match_id)]) == 0
    capsys.readouterr()
    html = (site_root / "matches" / f"{match_id}.html").read_text(encoding="utf-8")
    assert html.count('<section class="seg ') == 11
    assert "无显著峰值" in html
    assert verify_sources(match_id, data_root=data_root) == []
    metrics = json.loads(
        json.dumps({"sections": html.count('<section class="seg ')})
    )
    assert metrics["sections"] == 11


def test_t4_chain_boundaries_stats_gray_rebuild(replayed, data_root, site_root, capsys):
    """T4 端到端：切片引擎 → 人工修正留痕 → 统计全集 → 终局判定 → 灰信号 → 静态页 → AC-13。

    对应 issue #7 的验收标准（每条切片有来源、冲突记录、修正留痕、统计可重算、
    灰信号门槛与渲染零身份）。全程不连外网。
    """
    from danmu_intel.common import audit
    from danmu_intel.common.config import load_stats_config
    from danmu_intel.pipeline import write_metrics
    from danmu_intel.stats.basic import ALGO_VERSION

    match_id = 1
    assert main(["match", "add", "--league", "LPL", "--team-a", "iG", "--team-b", "LNG",
                 "--state", "ended", "--official-result", '{"score":"2:0"}']) == 0
    assert main(["collect", "--url", "https://www.huya.com/660000", "--seconds", "2",
                 "--match-id", str(match_id)]) == 0
    capsys.readouterr()

    conn = open_db()
    raw = next((data_root / "raw" / "huya").rglob("*.jsonl"))
    times = [event.ts for _, event in iter_events(raw)]
    start, end = min(times), max(times) + 1
    conn.close()

    # ① 切片引擎：报告窗口作为候选（优先级 3）→ 落库并留来源
    assert main(["boundaries", "--match-id", str(match_id),
                 "--report-window", f"1:{start}:{end}"]) == 0
    assert "已写入 G1" in capsys.readouterr().out
    conn = open_db()
    row = conn.execute("SELECT * FROM slices WHERE match_id=?", (match_id,)).fetchone()
    assert row["boundary_source"] == "report_window" and row["conflict_note"] is None
    assert conn.execute("SELECT COUNT(*) AS n FROM audit_log WHERE action='slice.boundary'").fetchone()["n"] == 1

    # ② 人工修正：必须带理由，落审计，算法版本递增
    assert main(["slice", "--match-id", str(match_id), "--game-no", "1",
                 "--start-ms", str(start - 1_000), "--end-ms", str(end),
                 "--override-by", "管理员", "--override-reason", "对齐官方开赛时间"]) == 0
    assert "人工修正已留痕" in capsys.readouterr().out
    row = conn.execute("SELECT * FROM slices WHERE match_id=?", (match_id,)).fetchone()
    assert row["boundary_source"] == "manual" and row["override_reason"] == "对齐官方开赛时间"
    assert audit.count(conn, action=audit.SLICE_OVERRIDE) == 1

    # ③ 统计全集 + 终局判定 + 灰信号（门槛来自 config 表）
    assert main(["stats", "--match-id", str(match_id)]) == 0
    stats_out = capsys.readouterr().out
    assert f"算法版本 {ALGO_VERSION}+ov1" in stats_out
    assert "终局判定：live" in stats_out and "灰信号：0 项达门槛" in stats_out

    assert main(["final", "--match-id", str(match_id)]) == 0
    assert "终局判定：live" in capsys.readouterr().out
    assert main(["gray", "--match-id", str(match_id)]) == 0
    gray_out = capsys.readouterr().out
    assert "没有达到门槛的灰信号" in gray_out
    assert "样本：" not in gray_out, "没有达门槛的信号就不该有样本行"
    assert "user_hash" not in gray_out
    assert load_stats_config(conn).gray_min_hits == 5

    # ④ 十一段静态页：灰信号段必须带纪律与门槛，且不含任何身份标识
    assert main(["render", "--match-id", str(match_id)]) == 0
    capsys.readouterr()
    html = (site_root / "matches" / f"{match_id}.html").read_text(encoding="utf-8")
    assert html.count('<section class="seg ') == 11
    assert "灰信号汇总" in html and "不构成对任何个人或队伍的任何指控" in html
    assert "边界来源：人工指定 1 局" in html
    user_hashes = {event.user_hash for _, event in iter_events(raw)}
    assert user_hashes and all(value not in html for value in user_hashes)

    # ⑤ AC-13：删掉统计结果后仅凭原始记录 + 切片 + 配置重算出完全一致的统计
    facts = collect_facts(conn, match_id, data_root=data_root)
    write_metrics(conn, facts)
    before = metrics_snapshot(conn, match_id, algo_version=facts.algo_version)
    conn.execute("DELETE FROM metrics")
    conn.commit()
    assert metrics_snapshot(conn, match_id) == []
    assert rebuild_metrics(conn, match_id, data_root=data_root) is True
    assert metrics_snapshot(conn, match_id, algo_version=facts.algo_version) == before
    conn.close()
    assert main(["rebuild", "--match-id", str(match_id)]) == 0
    assert "AC-13 通过" in capsys.readouterr().out

    # ⑥ 来源逐项复核 + 全库零命中凭据
    assert main(["verify-sources", "--match-id", str(match_id)]) == 0
    assert "全部来源校验通过" in capsys.readouterr().out
    assert scan_tree(paths.repo_root()) == []
    assert scan_tree(data_root) == []
