"""原始弹幕的 6 个月归档：压缩迁归档根（NAS 挂载点），索引行改指向归档件。

需求 §7.10 / NFR-D-1..4、AC-17；设计 §5.3；ADR-0021。

一次归档（`run`）对每个**在线期已过**的落盘文件做四件事，任一步对不上就停在那一步：

1. 校验在线文件的内容摘要 == `danmu_segments.sha256`（采集时的封存值）—— 采集之后
   被改过就**不归档**（宁可留着可疑的在线件，也不给它背一个假封存值）；
2. 压成 `archive/<platform>/<yyyy-mm-dd>/<room_id>-<hh>.jsonl.zst`（先写 `.part`
   再 `os.replace`，NAS 上跑到一半不会留下截断的「归档件」）；
3. 校验归档件**解压后**的内容摘要仍等于封存值（这就是 AC-17 的「可核验」），
   并记下归档件自身字节的摘要 `archive_sha256`；
4. 删在线文件、把索引行的 `rel_path` 改指向归档件（`archived_at` / `archive_sha256`
   一起写）。`sha256`（内容摘要）**不变** —— 报告的溯源引用拿它比对。

**归档只碰 `danmu_segments` 与 `audit_log`**：切片/统计/报告/订单/会员/审计长期不删
（NFR-D-2 管的只是原始弹幕）。统计明细 90 天 → 汇总入 `stats_daily` 是 T10 的
`site-stats --prune`，本模块不重复实现；两边合起来是完整的数据生命周期。

异常一律不静默：索引里的在线件找不到、摘要不一致、`raw/` 里超期却**没进索引**的文件，
都进 `ArchiveRun.anomalies` 并让命令非零退出 —— 由人看一眼再决定，而不是悄悄删掉
或悄悄留在在线盘上。
"""

from __future__ import annotations

import calendar
import hashlib
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from danmu_intel.common import audit, evidence, paths

RETENTION_MONTHS = 6  # 在线保留期（需求 Q-4：先定 6 个月，自采集之日算起）
ARCHIVE_RUN = "archive.run"
_ONLINE_SEGMENT = re.compile(
    r"^raw/(?P<platform>[^/]+)/(?P<day>\d{4}-\d{2}-\d{2})/(?P<name>[^/]+)\.jsonl$"
)


@dataclass(frozen=True, slots=True)
class Segment:
    """`danmu_segments` 的一行（证据索引）。"""

    id: int
    rel_path: str
    sha256: str
    msg_count: int
    archived_at: int | None = None

    @property
    def archived(self) -> bool:
        return self.archived_at is not None


@dataclass(frozen=True, slots=True)
class Anomaly:
    """归档路程上「需要人看一眼」的事实。"""

    rel_path: str
    reason: str

    def __str__(self) -> str:
        return f"{self.rel_path}：{self.reason}"


@dataclass(frozen=True, slots=True)
class ArchivedFile:
    online_rel_path: str
    archive_rel_path: str
    msg_count: int
    sha256: str  # 内容摘要（封存值，归档前后不变）
    archive_sha256: str  # 归档件自身字节的摘要
    online_bytes: int
    archive_bytes: int

    @property
    def ratio(self) -> float:
        return self.online_bytes / self.archive_bytes if self.archive_bytes else 0.0


@dataclass(frozen=True, slots=True)
class Plan:
    """这一次归档的到期集合（不碰盘、不压文件）。"""

    cutoff: date
    due: tuple[Segment, ...]
    anomalies: tuple[Anomaly, ...]


@dataclass(frozen=True, slots=True)
class ArchiveRun:
    cutoff: date
    root: Path
    archived_at: int
    due: int
    archived: tuple[ArchivedFile, ...]
    anomalies: tuple[Anomaly, ...]

    @property
    def online_bytes(self) -> int:
        return sum(item.online_bytes for item in self.archived)

    @property
    def archive_bytes(self) -> int:
        return sum(item.archive_bytes for item in self.archived)

    @property
    def ratio(self) -> float:
        return self.online_bytes / self.archive_bytes if self.archive_bytes else 0.0

    def summary(self) -> str:
        head = f"归档根 {self.root}（保留期截止 {self.cutoff.isoformat()}）"
        if not self.archived and not self.anomalies:
            return f"{head}：没有到期的原始记录"
        line = (
            f"{head}：到期 {self.due} 个文件 → 已归档 {len(self.archived)} 个"
            f"（在线 {self.online_bytes} 字节 → 归档件 {self.archive_bytes} 字节"
            f"，{self.ratio:.1f}×）"
        )
        if self.anomalies:
            line += f"｜异常 {len(self.anomalies)} 项（见下）"
        return line


def cutoff_date(*, months: int = RETENTION_MONTHS, today: date | None = None) -> date:
    """在线保留期的截止日：今天往回推 `months` 个月（按日历月，日号超出时取当月最后一天）。"""
    moment = today or date.today()
    total = moment.year * 12 + (moment.month - 1) - months
    year, month = divmod(total, 12)
    last_day = calendar.monthrange(year, month + 1)[1]
    return date(year, month + 1, min(moment.day, last_day))


def segment_day(rel_path: str) -> date:
    """从在线地址里读采集日（`raw/<platform>/<yyyy-mm-dd>/…`）。"""
    match = _ONLINE_SEGMENT.match(rel_path)
    if match is None:
        raise ValueError(f"不是原始弹幕落盘路径：{rel_path}")
    return date.fromisoformat(match.group("day"))


def segments(conn: sqlite3.Connection) -> tuple[Segment, ...]:
    rows = conn.execute(
        "SELECT id, rel_path, sha256, msg_count, archived_at FROM danmu_segments ORDER BY rel_path"
    ).fetchall()
    return tuple(
        Segment(
            id=int(row["id"]),
            rel_path=row["rel_path"],
            sha256=row["sha256"],
            msg_count=int(row["msg_count"]),
            archived_at=row["archived_at"],
        )
        for row in rows
    )


def plan(conn: sqlite3.Connection, *, cutoff: date, data_root: Path | None = None) -> Plan:
    """到期集合：索引里仍在线的、采集日早于截止日的记录。

    顺手把两类「对不上」的事实挑出来（不静默）：索引行不是原始弹幕落盘路径，
    以及 `raw/` 下超期却**没进索引**的文件 —— 后者归档也留不下可核验的溯源，
    删掉又可能丢证据，因此交给人处置。
    """
    root = data_root or paths.data_dir()
    due: list[Segment] = []
    anomalies: list[Anomaly] = []
    indexed: set[str] = set()
    for segment in segments(conn):
        indexed.add(evidence.online_rel_path(segment.rel_path))
        if segment.archived:
            continue
        try:
            day = segment_day(segment.rel_path)
        except ValueError as exc:
            anomalies.append(Anomaly(segment.rel_path, f"索引行无法判定归档期（{exc}）"))
            continue
        if day < cutoff:
            due.append(segment)

    for path in sorted((root / "raw").glob("*/*/*.jsonl")):
        rel_path = path.relative_to(root).as_posix()
        if rel_path in indexed:
            continue
        try:
            day = segment_day(rel_path)
        except ValueError:
            continue
        if day < cutoff:
            anomalies.append(
                Anomaly(rel_path, "超期但不在索引里（未封存）：归档它留不下可核验的溯源")
            )
    return Plan(cutoff=cutoff, due=tuple(due), anomalies=tuple(anomalies))


class ArchiveRefused(Exception):
    """一个个文件的归档被拒绝（原因进 `ArchiveRun.anomalies`，在线文件保留）。"""

    def __init__(self, rel_path: str, reason: str) -> None:
        super().__init__(f"{rel_path}：{reason}")
        self.rel_path = rel_path
        self.reason = reason


def _device(path: Path) -> int:
    return path.stat().st_dev


def _require_archive_root(root: Path, *, data_root: Path, allow_same_disk: bool) -> None:
    """归档根必须是**独立挂载点**（NAS），否则「迁出在线范围」只是换个目录名。

    NAS 没挂上时 `<data>/archive` 根本不存在；本地演练/测试用 `allow_same_disk` 明说
    「就写本地」，免得悄悄把两年弹幕压进同一块盘还reporting成功。
    """
    if not root.exists():
        if not allow_same_disk:
            raise ValueError(
                f"归档根不存在：{root}（NAS 共享没挂上？本机演练用 --allow-same-disk）"
            )
        root.mkdir(parents=True, exist_ok=True)
        return
    if not allow_same_disk and _device(root) == _device(data_root):
        raise ValueError(
            f"归档根与数据根在同一磁盘：{root}（归档要迁到 NAS 挂载点；"
            "本机演练用 --allow-same-disk）"
        )


def _archive_one(
    conn: sqlite3.Connection, segment: Segment, *, data_root: Path, moment: int
) -> ArchivedFile:
    online = evidence.resolve(segment.rel_path, data_root=data_root)
    if not online.exists():
        raise ArchiveRefused(segment.rel_path, "在线文件缺失（索引里仍在线的记录找不到文件）")
    if evidence.content_sha256(online) != segment.sha256:
        raise ArchiveRefused(segment.rel_path, "在线文件与封存摘要不一致（拒绝归档）")

    archive_rel_path = evidence.archive_rel_path(segment.rel_path)
    artifact = evidence.resolve(archive_rel_path, data_root=data_root)
    online_bytes = online.stat().st_size
    archive_bytes = evidence.compress_file(online, artifact)
    if evidence.content_sha256(artifact) != segment.sha256:
        artifact.unlink(missing_ok=True)
        raise ArchiveRefused(segment.rel_path, "归档件解压后与封存摘要不一致（已丢弃归档件）")

    artifact_sha256 = evidence.stored_sha256(artifact)
    online.unlink()
    conn.execute(
        """
        UPDATE danmu_segments SET rel_path=?, archived_at=?, archive_sha256=? WHERE id=?
        """,
        (archive_rel_path, moment, artifact_sha256, segment.id),
    )
    conn.commit()
    return ArchivedFile(
        online_rel_path=segment.rel_path,
        archive_rel_path=archive_rel_path,
        msg_count=segment.msg_count,
        sha256=segment.sha256,
        archive_sha256=artifact_sha256,
        online_bytes=online_bytes,
        archive_bytes=archive_bytes,
    )


def run(
    conn: sqlite3.Connection,
    *,
    actor: str,
    cutoff: date,
    data_root: Path | None = None,
    allow_same_disk: bool = False,
    now_ms: int | None = None,
) -> ArchiveRun:
    """执行一次归档（幂等：已归档的行不再出现在到期集合里，重跑只处理新到期的）。"""
    root_path = data_root or paths.data_dir()
    root = paths.archive_dir(data_root=root_path)
    plan_ = plan(conn, cutoff=cutoff, data_root=root_path)
    moment = int(time.time() * 1000) if now_ms is None else now_ms
    archived: list[ArchivedFile] = []
    anomalies = list(plan_.anomalies)

    if plan_.due:
        _require_archive_root(root, data_root=root_path, allow_same_disk=allow_same_disk)
        for segment in plan_.due:
            try:
                archived.append(_archive_one(conn, segment, data_root=root_path, moment=moment))
            except ArchiveRefused as exc:
                anomalies.append(Anomaly(exc.rel_path, exc.reason))

    result = ArchiveRun(
        cutoff=cutoff,
        root=root,
        archived_at=moment,
        due=len(plan_.due),
        archived=tuple(archived),
        anomalies=tuple(anomalies),
    )
    if archived or anomalies:
        audit.record(
            conn,
            actor=actor,
            action=ARCHIVE_RUN,
            target=str(root),
            detail={
                "cutoff": cutoff.isoformat(),
                "archived_at": moment,
                "archived": len(archived),
                "due": result.due,
                "online_bytes": result.online_bytes,
                "archive_bytes": result.archive_bytes,
                # 这次归档了哪些范围：两端 + 「按 archived_at 查索引行」这条线索
                "range": [
                    archived[0].online_rel_path if archived else None,
                    archived[-1].online_rel_path if archived else None,
                ],
                "anomalies": [str(item) for item in anomalies],
            },
            ts=moment,
        )
    return result


def verify(conn: sqlite3.Connection, *, data_root: Path | None = None) -> tuple[Anomaly, ...]:
    """复核全部归档件：文件在、归档件自身摘要对得上、解压后内容摘要 == 封存值。"""
    root_path = data_root or paths.data_dir()
    problems: list[Anomaly] = []
    rows = conn.execute(
        "SELECT rel_path, sha256, archive_sha256 FROM danmu_segments WHERE archived_at IS NOT NULL"
    ).fetchall()
    for row in rows:
        path = evidence.resolve(row["rel_path"], data_root=root_path)
        if not path.exists():
            problems.append(Anomaly(row["rel_path"], "归档件缺失"))
            continue
        if row["archive_sha256"] != evidence.stored_sha256(path):
            problems.append(Anomaly(row["rel_path"], "归档件自身摘要不一致（存储/传输损坏）"))
            continue
        if row["sha256"] != evidence.content_sha256(path):
            problems.append(Anomaly(row["rel_path"], "归档件解压后与封存摘要不一致（内容被改）"))
    return tuple(problems)


def retrieve(
    conn: sqlite3.Connection, rel_path: str, *, data_root: Path | None = None
) -> tuple[Path, bytes]:
    """取回一份证据的内容（在线地址或归档地址都认），能对上封存摘要才交出去。

    取回的是**未压缩字节**（与采集时逐字节相同，行号也相同），因此归档件可以
    直接喂给读原始记录的任何路径（统计重算、人工复核、导出）。
    """
    root_path = data_root or paths.data_dir()
    path = evidence.locate(rel_path, data_root=root_path)
    if not path.exists():
        raise LookupError(f"证据文件不存在：{rel_path}（在线与归档位置都没有）")
    content = evidence.read_bytes(path)
    row = conn.execute(
        "SELECT sha256 FROM danmu_segments WHERE rel_path IN (?, ?)",
        (rel_path, evidence.online_rel_path(rel_path)),
    ).fetchone()
    if row is not None and hashlib.sha256(content).hexdigest() != row["sha256"]:
        raise ValueError(f"内容与封存摘要不一致：{rel_path}（拒绝交出可疑证据）")
    return path, content
