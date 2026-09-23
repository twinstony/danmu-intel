"""切片引擎（设计 §8.1；需求 §6.2/§6.3 + FR-C2-3/4/5）。

**边界的优先级**（一条切片必须能回答「这个边界凭什么这么定」）：

| 优先级 | 来源 | 判定方式 |
|---|---|---|
| 1 | `official` | 官方赛程/比分数据里的小局起止时间 |
| 2 | `danmu_signal` | 弹幕信号复核（**≥2 类独立信号**，见 `slice/signals.py`） |
| 3 | `report_window` | 已发布报告里记录的时间窗口（回填历史比赛） |
| 4 | `manual` | 人工修正（后台操作，永远记录操作者与理由） |

**优先级与人工修正的关系**（本模块的明确解读，两边文档都满足）：

- 前三者是**证据来源**，冲突时按 1 > 2 > 3 取用（需求 §6.3 末句）。
- `manual` 不是第 4 个证据来源，而是**修正机制**：FR-C2-5 要求「修正后以修正结果为准
  且留痕」。因此一旦存在带留痕的人工修正，它覆盖证据来源；反过来，**自动来源永远不得
  覆盖人工修正过的切片**（`apply_boundaries` 会跳过并留审计）。
- 无论谁胜出，只要多来源给出**不同**边界，差异一律写进 `conflict_note`（设计 §8.1：
  「冲突必记录」，这是「不丢证据」在切片层的落点）。

**人工修正的版本语义**：每次人工修正写一条 `slice.override` 审计；`metrics.algo_version`
按修正次数递增（`1.0.0` → `1.0.0+ov1` → …）。版本号因此**只由输入决定**（审计记录 +
常量），删掉统计结果后重算仍可复现（AC-13）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from danmu_intel.common import audit, official
from danmu_intel.common.config import StatsConfig
from danmu_intel.slice import manual
from danmu_intel.slice.signals import detect_danmu_windows
from danmu_intel.stats.basic import ALGO_VERSION, RawLine

#: 证据来源的优先级（需求 §6.3 的三级；设计 §8.1 的前三行）。
EVIDENCE_PRIORITY = ("official", "danmu_signal", "report_window")

#: 修正来源：不是证据，而是覆盖机制。
CORRECTION_SOURCE = "manual"

#: 完整顺序（含修正），与 `slices.boundary_source` 的取值一致。
BOUNDARY_PRIORITY = (*EVIDENCE_PRIORITY, CORRECTION_SOURCE)


@dataclass(frozen=True, slots=True)
class BoundaryCandidate:
    """一个来源给出的小局边界候选。"""

    source: str
    game_no: int
    start_ms: int
    end_ms: int
    verified: bool = True
    kinds: tuple[str, ...] = ()
    note: str | None = None
    override_by: str | None = None
    override_at: int | None = None
    override_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "game_no": self.game_no,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "verified": self.verified,
            "kinds": list(self.kinds),
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class ResolvedBoundary:
    game_no: int
    start_ms: int
    end_ms: int
    boundary_source: str
    conflict_note: str | None
    candidates: tuple[BoundaryCandidate, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "game_no": self.game_no,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "boundary_source": self.boundary_source,
            "conflict_note": self.conflict_note,
            "candidates": [candidate.as_dict() for candidate in self.candidates],
        }


def candidates_from_official(official_result: dict[str, Any] | None) -> tuple[BoundaryCandidate, ...]:
    """官方小局边界（设计 §8.1 优先级 1）。官方数据无需复核。"""
    return tuple(
        BoundaryCandidate(
            source="official",
            game_no=entry["game_no"],
            start_ms=entry["start_ms"],
            end_ms=entry["end_ms"],
            note="来源：官方赛程/比分数据",
        )
        for entry in official.games(official_result)
    )


def candidates_from_danmu(
    lines: Sequence[RawLine], *, config: StatsConfig | None = None
) -> tuple[BoundaryCandidate, ...]:
    """弹幕信号候选（设计 §8.1 优先级 2）；只返回**通过 ≥2 类独立信号复核**的窗口。"""
    windows = detect_danmu_windows(lines, config=config or StatsConfig())
    return tuple(
        BoundaryCandidate(
            source="danmu_signal",
            game_no=window.game_no,
            start_ms=window.start_ms,
            end_ms=window.end_ms,
            kinds=window.kinds,
            note="来源：弹幕信号复核（≥2 类独立信号）",
        )
        for window in windows
    )


def candidates_from_report_window(
    entries: Iterable[tuple[int, int, int]]
) -> tuple[BoundaryCandidate, ...]:
    """已发布报告窗口（设计 §8.1 优先级 3）；回填历史比赛时由发布库喂进来。"""
    return tuple(
        BoundaryCandidate(
            source="report_window",
            game_no=game_no,
            start_ms=start_ms,
            end_ms=end_ms,
            note="来源：已发布报告记录的时间窗口",
        )
        for game_no, start_ms, end_ms in entries
    )


def _describe(candidates: Sequence[BoundaryCandidate]) -> str:
    return "；".join(
        f"{candidate.source} G{candidate.game_no} [{candidate.start_ms}, {candidate.end_ms}]"
        for candidate in candidates
    )


def _difference(reference: BoundaryCandidate, other: BoundaryCandidate) -> str:
    return f"起点差 {other.start_ms - reference.start_ms}ms、终点差 {other.end_ms - reference.end_ms}ms"


def _pick(candidates: Sequence[BoundaryCandidate]) -> BoundaryCandidate | None:
    """在一个 game_no 的候选里按优先级取用（人工修正优先于证据来源；未通过复核的不用）。"""
    usable = [candidate for candidate in candidates if candidate.verified]
    corrections = [candidate for candidate in usable if candidate.source == CORRECTION_SOURCE]
    if corrections:
        return sorted(corrections, key=lambda candidate: (candidate.override_at or 0, candidate.start_ms))[-1]
    for source in EVIDENCE_PRIORITY:
        for candidate in usable:
            if candidate.source == source:
                return candidate
    return None


def conflict_note_for(candidates: Sequence[BoundaryCandidate], winner: BoundaryCandidate) -> str | None:
    """多来源给出不同边界时，把差异写成人能读的冲突事实；只有一家则返回 None。"""
    usable = [candidate for candidate in candidates if candidate.verified]
    rejected = [candidate for candidate in candidates if not candidate.verified]
    bounds = {(candidate.start_ms, candidate.end_ms) for candidate in usable}
    if len(bounds) <= 1 and not rejected:
        return None
    parts: list[str] = []
    if len(bounds) > 1:
        others = [
            candidate
            for candidate in usable
            if candidate is not winner and (candidate.start_ms, candidate.end_ms) != (winner.start_ms, winner.end_ms)
        ]
        parts.append("冲突：多来源给出不同边界 —— " + _describe(usable))
        parts.append(
            "；".join(
                f"{winner.source} 与 {other.source} 相比：{_difference(winner, other)}" for other in others
            )
        )
    how = (
        "人工修正覆盖证据来源（FR-C2-5：修正后以修正结果为准）"
        if winner.source == CORRECTION_SOURCE
        else f"采用 {winner.source}（优先级最高）"
    )
    parts.append(f"{how}，最终 {winner.source} [{winner.start_ms}, {winner.end_ms}]")
    parts.extend(f"未采用：{candidate.source} —— {candidate.note}" for candidate in rejected)
    return "。".join(part for part in parts if part)


def resolve(candidates: Iterable[BoundaryCandidate]) -> tuple[ResolvedBoundary, ...]:
    """按 `game_no` 裁决边界，并把冲突事实记进 `conflict_note`。"""
    grouped: dict[int, list[BoundaryCandidate]] = {}
    for candidate in candidates:
        if candidate.start_ms >= candidate.end_ms:
            raise ValueError(
                f"切片起止非法：{candidate.source} G{candidate.game_no} {candidate.start_ms} >= {candidate.end_ms}"
            )
        grouped.setdefault(candidate.game_no, []).append(candidate)
    resolved: list[ResolvedBoundary] = []
    for game_no in sorted(grouped):
        group = grouped[game_no]
        winner = _pick(group)
        if winner is None:
            continue
        resolved.append(
            ResolvedBoundary(
                game_no=game_no,
                start_ms=winner.start_ms,
                end_ms=winner.end_ms,
                boundary_source=winner.source,
                conflict_note=conflict_note_for(group, winner),
                candidates=tuple(group),
            )
        )
    return tuple(resolved)


def algo_version(conn: sqlite3.Connection, match_id: int) -> str:
    """该场统计的算法版本：基础版本 + 人工修正次数（每修正一次递增一级）。"""
    corrections = audit.count(conn, action=audit.SLICE_OVERRIDE, target_prefix=f"match:{match_id}/")
    return ALGO_VERSION if corrections == 0 else f"{ALGO_VERSION}+ov{corrections}"


def collect_candidates(
    *,
    official_result: dict[str, Any] | None,
    lines: Sequence[RawLine],
    report_windows: Iterable[tuple[int, int, int]] = (),
    config: StatsConfig | None = None,
) -> tuple[BoundaryCandidate, ...]:
    """把三个证据来源的候选汇总在一起（顺序固定，便于重算与比对）。"""
    candidates = [
        *candidates_from_official(official_result),
        *candidates_from_danmu(lines, config=config),
        *candidates_from_report_window(report_windows),
    ]
    return tuple(
        sorted(
            candidates,
            key=lambda candidate: (
                candidate.game_no,
                EVIDENCE_PRIORITY.index(candidate.source),
                candidate.start_ms,
            ),
        )
    )


def apply_boundaries(
    conn: sqlite3.Connection,
    match_id: int,
    resolutions: Iterable[ResolvedBoundary],
    *,
    actor: str = "boundary-engine",
) -> tuple[ResolvedBoundary, ...]:
    """把裁决结果写进 `slices`。人工修正走留痕路径；自动来源不得覆盖人工修正。"""
    applied: list[ResolvedBoundary] = []
    for resolved in resolutions:
        target = f"match:{match_id}/game:{resolved.game_no}"
        existing = conn.execute(
            "SELECT * FROM slices WHERE match_id=? AND game_no=?", (match_id, resolved.game_no)
        ).fetchone()
        human_fixed = existing is not None and existing["override_at"] is not None
        if resolved.boundary_source == CORRECTION_SOURCE:
            candidate = next(
                candidate for candidate in resolved.candidates if candidate.source == CORRECTION_SOURCE
            )
            if not candidate.override_by or not candidate.override_reason:
                raise ValueError("人工修正必须填写 override_by 与 override_reason（FR-C2-5 留痕）")
            manual.add_manual_slice(
                conn,
                match_id=match_id,
                game_no=resolved.game_no,
                start_ms=resolved.start_ms,
                end_ms=resolved.end_ms,
                note=resolved.conflict_note,
                override_by=candidate.override_by,
                override_reason=candidate.override_reason,
                override_at=candidate.override_at,
            )
            applied.append(resolved)
            continue
        if human_fixed:
            audit.record(
                conn,
                actor=actor,
                action=audit.SLICE_BOUNDARY,
                target=target,
                detail={
                    "outcome": "skipped",
                    "reason": "该小局已有人工修正，自动来源不得覆盖",
                    "source": resolved.boundary_source,
                },
            )
            continue
        _write_auto(conn, match_id, resolved)
        audit.record(
            conn,
            actor=actor,
            action=audit.SLICE_BOUNDARY,
            target=target,
            detail={
                "outcome": "applied",
                "source": resolved.boundary_source,
                "start_ms": resolved.start_ms,
                "end_ms": resolved.end_ms,
                "conflict_note": resolved.conflict_note,
            },
        )
        applied.append(resolved)
    return tuple(applied)


def _write_auto(conn: sqlite3.Connection, match_id: int, resolved: ResolvedBoundary) -> None:
    conn.execute(
        """
        INSERT INTO slices(match_id, game_no, start_ms, end_ms, boundary_source, conflict_note)
        VALUES(?, ?, ?, ?, ?, ?)
        ON CONFLICT(match_id, game_no) DO UPDATE SET
          start_ms=excluded.start_ms, end_ms=excluded.end_ms,
          boundary_source=excluded.boundary_source, conflict_note=excluded.conflict_note
        """,
        (
            match_id,
            resolved.game_no,
            resolved.start_ms,
            resolved.end_ms,
            resolved.boundary_source,
            resolved.conflict_note,
        ),
    )
    conn.commit()


def resolve_match(
    conn: sqlite3.Connection,
    match_id: int,
    *,
    official_result: dict[str, Any] | None,
    lines: Sequence[RawLine],
    report_windows: Iterable[tuple[int, int, int]] = (),
    config: StatsConfig | None = None,
    apply: bool = True,
    actor: str = "boundary-engine",
) -> tuple[ResolvedBoundary, ...]:
    """从三个证据来源裁决该场全部小局边界；`apply=True` 时落库。"""
    resolutions = resolve(
        collect_candidates(
            official_result=official_result, lines=lines, report_windows=report_windows, config=config
        )
    )
    if not apply:
        return resolutions
    return apply_boundaries(conn, match_id, resolutions, actor=actor)
