"""端到端：数据生命周期（issue #23 / AC-17、NFR-D-1..4、设计 §5.3）。

一条线上跑完四件事，全部离线可重复：

1. 原始弹幕到期（6 个月）压缩迁归档根，`danmu_segments.rel_path` 改指向归档件；
2. 归档**可调取、可核验**：`--verify` 复核校验和，`--retrieve` 取回的内容逐字节等于采集时；
3. 归档**不打断**在线保留期内报告的数据溯源（NFR-D-4）：老页面的引用冻结的是在线地址，
   归档后照样复核得过，发布检查也照旧通过；
4. 长期数据不删：切片/统计/报告/订单/会员/审计不被归档碰；
   统计明细 90 天后汇总入 `stats_daily` 再删（T10 的口径，这里断言它没丢账）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from compression import zstd

from danmu_intel import archive
from danmu_intel.billing import orders, pricing
from danmu_intel.cli import main
from danmu_intel.common import audit, evidence
from danmu_intel.common.matches import create_match
from danmu_intel.pipeline import generate_and_publish
from danmu_intel.site_stats import beacon, daily

from conftest import index_segment, make_event, write_jsonl

GENERATED_AT = 1_790_064_400_000
WALLET = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"
NOW = date.today()
OLD_DAY = (NOW - timedelta(days=200)).isoformat()  # 在线期已过（6 个月之外）
FRESH_DAY = (NOW - timedelta(days=3)).isoformat()  # 在线期内
OLD_REL = f"raw/huya/{OLD_DAY}/660001-16.jsonl"
FRESH_REL = f"raw/huya/{FRESH_DAY}/660002-16.jsonl"
LEDGER_TABLES = ("slices", "metrics", "reports", "orders", "members", "order_payments")


@dataclass
class Seeded:
    match_id: int
    rel_path: str
    start_ms: int
    events: list


def day_start_ms(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").timestamp() * 1000)


def seed_match(conn, data_root, *, day: str, rel_path: str, room_id: str, team_a: str) -> Seeded:
    """一场比赛的最小完整账本：原始记录 + 索引 + 一个小局切片 + 报告。"""
    start = day_start_ms(day)
    events = [make_event(start + i * 1000, text=f"{rel_path} 第 {i} 条") for i in range(12)]
    digest = write_jsonl(data_root / rel_path, events)
    match_id = create_match(
        conn,
        league="LPL",
        team_a=team_a,
        team_b="LNG",
        state="ended",
        official_result={"score": "1:0"},
    )
    index_segment(
        conn, match_id=match_id, rel_path=rel_path, digest=digest, events=events, room_id=room_id
    )
    main(["slice", "--match-id", str(match_id), "--game-no", "1",
          "--start-ms", str(start), "--end-ms", str(start + 12_000)])
    main(["stats", "--match-id", str(match_id)])
    generate_and_publish(conn, match_id, kind="full", data_root=data_root, generated_at=GENERATED_AT)
    return Seeded(match_id=match_id, rel_path=rel_path, start_ms=start, events=events)


def counts(conn) -> dict[str, int]:
    return {table: conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"] for table in LEDGER_TABLES}


def segment_row(conn, online_rel_path: str):
    return conn.execute(
        "SELECT * FROM danmu_segments WHERE rel_path IN (?, ?)",
        (online_rel_path, evidence.archive_rel_path(online_rel_path)),
    ).fetchone()


def seed_ledgers(conn, data_root) -> tuple[Seeded, Seeded]:
    """两场比赛：一场在保留期外（该归档）、一场在保留期内（不该动）。"""
    old = seed_match(conn, data_root, day=OLD_DAY, rel_path=OLD_REL, room_id="660001", team_a="EDG")
    fresh = seed_match(conn, data_root, day=FRESH_DAY, rel_path=FRESH_REL, room_id="660002", team_a="iG")
    pricing.save_billing_config(
        conn,
        actor="管理员",
        changes={
            "solana_address": WALLET,
            "tiers": [{"key": "trial", "label": "试用档", "amount_units": 500_000, "days": 7}],
        },
    )
    orders.create_order(conn, platform="qq", username="12345678", tier="trial", network="solana")
    for day, ip in ((OLD_DAY, "203.0.113.1"), (FRESH_DAY, "203.0.113.2")):
        beacon.record(conn, page="index.html", ip=ip, ts=day_start_ms(day) + 3600_000)
    audit.record(conn, actor="管理员", action="config.update", target="stats", detail={"note": "归档前就在的审计"})
    return old, fresh


def test_archive_moves_expired_segments_and_keeps_everything_else(conn, data_root, capsys):
    old, fresh = seed_ledgers(conn, data_root)
    cutoff = archive.cutoff_date()
    assert OLD_DAY < cutoff.isoformat() <= FRESH_DAY
    before_bytes = (data_root / OLD_REL).read_bytes()
    before_counts = counts(conn)
    before_audit_ids = [entry.id for entry in audit.entries(conn)]

    assert main(["archive", "--allow-same-disk"]) == 0
    out = capsys.readouterr().out
    assert f"保留期截止 {cutoff.isoformat()}" in out
    assert "到期 1 个文件 → 已归档 1 个" in out

    # ① 索引行 rel_path 改指向归档件，内容摘要（封存值）不变
    moved = segment_row(conn, OLD_REL)
    assert moved["rel_path"] == evidence.archive_rel_path(OLD_REL)
    assert moved["archived_at"] is not None and moved["archive_sha256"]
    assert moved["sha256"] == hashlib.sha256(before_bytes).hexdigest()
    assert not (data_root / OLD_REL).exists()
    assert (data_root / moved["rel_path"]).exists()

    # 在线期内的记录一个都没动（NFR-D-4 的前提）
    kept = segment_row(conn, FRESH_REL)
    assert kept["rel_path"] == FRESH_REL and kept["archived_at"] is None
    assert (data_root / FRESH_REL).exists()

    # ② 可核验 + 可调取
    assert main(["archive", "--verify"]) == 0
    assert "全部通过" in capsys.readouterr().out
    out_path = data_root / "取回" / "660001-16.jsonl"
    assert main(["archive", "--retrieve", OLD_REL, "--out", str(out_path)]) == 0
    capsys.readouterr()
    assert out_path.read_bytes() == before_bytes

    # 归档后仍可重算统计（AC-13：删统计 → 仅凭原始记录重算）
    assert main(["rebuild", "--match-id", str(old.match_id)]) == 0
    assert "AC-13 通过" in capsys.readouterr().out

    # ③ 归档不打断报告溯源：老页面（引用冻结的是在线地址）与在线期内的页面都复核得过
    for match_id in (old.match_id, fresh.match_id):
        assert main(["verify-sources", "--match-id", str(match_id), "--kind", "full"]) == 0
        assert "全部来源校验通过" in capsys.readouterr().out

    # 发布闭环照旧：整棵站点树重出，7 项检查（含来源可解析 + 封存摘要加固）全通过
    assert main(["publish", "--no-deploy", "--reason", "归档后复检"]) == 0
    publish_out = capsys.readouterr().out
    assert "检查｜来源引用可达：通过" in publish_out

    # ④ 长期数据不删；审计只增（归档动作本身留痕）
    assert counts(conn) == before_counts
    after_audit_ids = [entry.id for entry in audit.entries(conn)]
    assert set(before_audit_ids) <= set(after_audit_ids)
    runs = audit.entries(conn, action=archive.ARCHIVE_RUN)
    assert len(runs) == 1
    assert runs[0].detail["cutoff"] == cutoff.isoformat()
    assert runs[0].detail["range"] == [OLD_REL, OLD_REL]


def test_stats_detail_rolls_up_after_ninety_days(conn, data_root, capsys):
    """统计原始事件 90 天后汇总入 `stats_daily`（明细删掉，口径不丢；账本不受影响）。"""
    old, fresh = seed_ledgers(conn, data_root)
    before_counts = counts(conn)
    old_summary = daily.summary(conn, OLD_DAY)

    assert main(["site-stats", "--prune", "--retention-days", "90"]) == 0
    capsys.readouterr()

    assert daily.detail_days(conn) == (FRESH_DAY,)  # 到期明细已删，在线期内留着
    assert daily.summary(conn, OLD_DAY).page_views == old_summary.page_views > 0  # 汇总后口径不变
    assert daily.summary(conn, OLD_DAY).unique_visitors == old_summary.unique_visitors
    # 切片/统计/报告/订单/会员/审计与归档无关，一个都没少
    assert counts(conn) == before_counts
    assert old.match_id and fresh.match_id


def test_boundaries_of_the_retention_window(conn, data_root, capsys):
    """归档**只**碰保留期外的：试运行先看清，再执行，在线期内的一份都不动。"""
    old, fresh = seed_ledgers(conn, data_root)
    cutoff = archive.cutoff_date()

    assert main(["archive", "--dry-run", "--cutoff", cutoff.isoformat()]) == 0
    out = capsys.readouterr().out
    assert f"到期｜{OLD_REL}" in out and FRESH_REL not in out
    assert (data_root / OLD_REL).exists()

    assert main(["archive", "--cutoff", cutoff.isoformat(), "--allow-same-disk"]) == 0
    assert (data_root / FRESH_REL).exists()
    assert main(["archive", "--verify"]) == 0
    assert "归档件复核：1 个" in capsys.readouterr().out


def test_archived_evidence_is_still_guarded_by_the_seal(conn, data_root, capsys):
    """归档不许把「封存摘要」这道防线变成空转：往归档件里追加一行照样被拦下。

    追加不影响已有行的行范围摘要（所以「行范围 SHA256」这一层照样通过），
    只有拿**封存值**比对整份内容才查得出来 —— 归档后这条比对仍须生效。
    """
    old, _ = seed_ledgers(conn, data_root)
    assert main(["archive", "--allow-same-disk"]) == 0
    capsys.readouterr()

    artifact = data_root / evidence.archive_rel_path(OLD_REL)
    appended = (
        '{"ts":1,"platform":"huya","room_id":"660001","match_id":1,'
        '"user_hash":"x","text":"归档之后被追加的一行","extra":{}}\n'
    ).encode("utf-8")
    artifact.write_bytes(zstd.compress(evidence.read_bytes(artifact) + appended))

    assert main(["archive", "--verify"]) == 1
    assert "摘要不一致" in capsys.readouterr().err

    assert main(["publish", "--dry-run"]) == 1
    captured = capsys.readouterr()
    assert "来源引用可达：未通过" in captured.out and "封存" in captured.out
    # 报告页面对应的那场比赛仍在（归档没动报告账本）
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM reports WHERE match_id=?", (old.match_id,)
    ).fetchone()["n"] == 1
