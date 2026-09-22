"""端到端：一条条命令跑通「采集 → 落盘 → 人工切片 → 统计 → 完整版报告发布」。

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
from danmu_intel.pipeline import verify_sources
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

    # ④ 基础统计 + ⑤ 规则直出十一段报告并发布
    assert main(["stats", "--match-id", str(match_id)]) == 0
    stats_out = capsys.readouterr().out
    assert f"G1：{collected} 条" in stats_out
    assert main(["report", "--match-id", str(match_id), "--kind", "full"]) == 0
    capsys.readouterr()

    page = site_root / "matches" / str(match_id) / "full.html"
    html = page.read_text(encoding="utf-8")
    for spec in SEGMENTS:
        assert f'id="seg-{spec.no}"' in html
        assert f"{spec.no}</span> {spec.title}" in html
    assert html.count('<section class="seg ') == 11

    # ⑥ 页面上每一项事实的来源都可复核（文件 + 行范围 + SHA256）
    refs = parse_sources(html)
    assert refs, "页面上必须能取到来源引用"
    assert all(ref.rel_path == rel_path for ref in refs)
    assert verify_sources(match_id, kind="full", data_root=data_root) == []

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
    assert main(["report", "--match-id", str(match_id), "--kind", "full"]) == 0
    capsys.readouterr()
    html = (site_root / "matches" / str(match_id) / "full.html").read_text(encoding="utf-8")
    assert html.count('<section class="seg ') == 11
    assert "无显著峰值" in html
    assert verify_sources(match_id, kind="full", data_root=data_root) == []
    metrics = json.loads(
        json.dumps({"sections": html.count('<section class="seg ')})
    )
    assert metrics["sections"] == 11
