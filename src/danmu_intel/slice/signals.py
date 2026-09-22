"""弹幕信号复核（需求 §6.3 第 2 优先级 / 设计 §8.1 第 2 优先级）。

小局边界可以从弹幕里看出来，但**必须复核**：设计 §8.1 明文要求候选边界
**需 ≥2 类相互独立信号支持**。三类独立信号：

| 类别 | 含义 | 独立性 |
|---|---|---|
| `lexical_start` / `lexical_end` | 开局/收局语义的词法模式 | 看「说了什么」 |
| `density_shift` | 密度骤变（滑窗计数显著高于均值） | 看「说了多少」 |
| `score_mention` | 出现比分信息 | 看「提到了比分」 |

只有一句话语命中的候选**不得**当作边界（设计原话：不得凭印象划分的一方就是要拦住
「几个人喊了一句『开始了』」这种信号）。复核不通过时留 `note` 说明为什么不用它。

纯函数层：不读时钟、不读库、不写盘。
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any, Sequence

from danmu_intel.common.config import StatsConfig
from danmu_intel.common.lexicon import END_LEXICON, START_LEXICON, hits
from danmu_intel.stats.basic import RawLine, density_curve
from danmu_intel.stats.full import score_mentions

#: 三类独立信号（设计 §8.1）。
BOUNDARY_SIGNAL_KINDS = ("lexical_start", "lexical_end", "density_shift", "score_mention")

DIRECTION_START = "start"
DIRECTION_END = "end"
DIRECTION_UNKNOWN = "unknown"

#: 同一句话被刷屏的判定间隔（≤15s 视为同一句）。
UTTERANCE_GAP_MS = 15_000

#: 密度骤变的判定：窗口计数 ≥ 邻近窗口计数中位数的 3 倍，且 ≥ `DENSITY_MIN_HITS` 条。
#: 用**局部基线**（邻近 5 分钟）而非全局均值："骤变"是相对上下文的变化，
#: 全局均值会被整场比赛的常态流量抬高而把边界冲掉。
DENSITY_RATIO = 3.0
DENSITY_BASELINE_MS = 300_000
DENSITY_MIN_HITS = 3


@dataclass(frozen=True, slots=True)
class SignalMoment:
    """一个信号时刻。"""

    kind: str
    at_ms: int
    hits: int
    evidence: dict[str, Any]


@dataclass(frozen=True, slots=True)
class BoundaryClaim:
    """复核后的候选边界（`verified=False` 时 `note` 说明为什么不能用）。"""

    direction: str
    at_ms: int
    kinds: tuple[str, ...]
    verified: bool
    hits: int
    note: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "at_ms": self.at_ms,
            "kinds": list(self.kinds),
            "verified": self.verified,
            "hits": self.hits,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class DanmuWindow:
    """由弹幕信号复核得到的小局窗口（两端边界都通过 ≥2 类复核）。"""

    game_no: int
    start_ms: int
    end_ms: int
    kinds: tuple[str, ...]
    evidence: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "game_no": self.game_no,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "kinds": list(self.kinds),
            "evidence": self.evidence,
        }


def _cluster(timestamps: Sequence[int], gap_ms: int) -> list[tuple[int, int, int]]:
    """把时间点按「相邻间隔 ≤ gap」聚成若干段，返回 `(起始, 结束, 条数)`。"""
    clusters: list[tuple[int, int, int]] = []
    for ts in sorted(timestamps):
        if clusters and ts - clusters[-1][1] <= gap_ms:
            start, _, count = clusters[-1]
            clusters[-1] = (start, ts, count + 1)
            continue
        clusters.append((ts, ts, 1))
    return clusters


def lexical_moments(lines: Sequence[RawLine]) -> list[SignalMoment]:
    moments: list[SignalMoment] = []
    for kind, lexicon in (("lexical_start", START_LEXICON), ("lexical_end", END_LEXICON)):
        matched = hits(lines, lexicon)
        for start, end, count in _cluster([line.event.ts for line in matched], UTTERANCE_GAP_MS):
            moments.append(
                SignalMoment(
                    kind=kind,
                    at_ms=start,
                    hits=count,
                    evidence={"last_ms": end, "cluster_ms": UTTERANCE_GAP_MS},
                )
            )
    return moments


def density_moments(lines: Sequence[RawLine], *, at_least: int = DENSITY_MIN_HITS) -> list[SignalMoment]:
    """密度骤变：窗口计数明显高于邻近窗口（需求 §6.3「密度骤变」）。"""
    if not lines:
        return []
    timestamps = [line.event.ts for line in lines]
    points = density_curve(timestamps, min(timestamps), max(timestamps) + 1)
    if not points:
        return []
    found: list[SignalMoment] = []
    for index, point in enumerate(points):
        neighbours = [
            other["count"]
            for position, other in enumerate(points)
            if position != index and abs(other["t_start"] - point["t_start"]) <= DENSITY_BASELINE_MS
        ]
        baseline = statistics.median(neighbours) if neighbours else 0
        count = point["count"]
        if count < at_least or count <= DENSITY_RATIO * baseline:
            continue
        found.append(
            SignalMoment(
                kind="density_shift",
                at_ms=point["t_start"],
                hits=count,
                evidence={
                    "count": count,
                    "baseline_median": baseline,
                    "ratio": DENSITY_RATIO,
                    "baseline_ms": DENSITY_BASELINE_MS,
                },
            )
        )
    return found


def score_moments(lines: Sequence[RawLine]) -> list[SignalMoment]:
    return [
        SignalMoment(
            kind="score_mention",
            at_ms=mention["ts"],
            hits=1,
            evidence={"score": mention["score"], "rel_path": mention["rel_path"], "line_no": mention["line_no"]},
        )
        for mention in score_mentions(lines)
    ]


def moments(lines: Sequence[RawLine], *, config: StatsConfig) -> tuple[SignalMoment, ...]:
    """抽取全部信号时刻（三类独立信号），顺序固定以便重算。"""
    collected = [*lexical_moments(lines), *density_moments(lines), *score_moments(lines)]
    return tuple(sorted(collected, key=lambda moment: (moment.at_ms, moment.kind)))


def review(candidates: Sequence[SignalMoment], *, config: StatsConfig) -> tuple[BoundaryClaim, ...]:
    """复核：以**词法锚点**为中心聚拢独立信号，≥`verify_min_kinds` 类才算通过。"""
    all_moments = list(candidates)
    anchors = [moment for moment in all_moments if moment.kind in ("lexical_start", "lexical_end")]
    claims: list[BoundaryClaim] = []
    for anchor_kind in ("lexical_start", "lexical_end"):
        typed = [moment for moment in anchors if moment.kind == anchor_kind]
        for start, end, _ in _cluster([moment.at_ms for moment in typed], config.boundary_cluster_ms):
            grouped = [moment for moment in typed if start <= moment.at_ms <= end]
            claims.append(_claim(all_moments, anchor_kind, start, end, grouped, config))
    return tuple(sorted(claims, key=lambda claim: (claim.at_ms, claim.direction)))


def _claim(
    all_moments: Sequence[SignalMoment],
    anchor_kind: str,
    start: int,
    end: int,
    anchors: Sequence[SignalMoment],
    config: StatsConfig,
) -> BoundaryClaim:
    window_start = start - config.boundary_cluster_ms
    window_end = end + config.boundary_cluster_ms
    supporting = {
        moment.kind
        for moment in all_moments
        if window_start <= moment.at_ms <= window_end
    }
    kinds = tuple(sorted(supporting))
    direction = DIRECTION_START if anchor_kind == "lexical_start" else DIRECTION_END
    count = sum(anchor.hits for anchor in anchors)
    if len(kinds) < config.verify_min_kinds:
        return BoundaryClaim(
            direction=direction,
            at_ms=start,
            kinds=kinds,
            verified=False,
            hits=count,
            note=(
                f"复核不通过：候选边界只得到 {len(kinds)} 类信号支持"
                f"（{'、'.join(kinds) or '无'}），设计 §8.1 要求 ≥{config.verify_min_kinds} 类独立信号"
            ),
        )
    return BoundaryClaim(
        direction=direction,
        at_ms=start,
        kinds=kinds,
        verified=True,
        hits=count,
        note=None,
    )


def detect_danmu_windows(lines: Sequence[RawLine], *, config: StatsConfig) -> tuple[DanmuWindow, ...]:
    """从弹幕信号得出小局窗口：起点/终点都必须通过复核，按时间顺序编号。"""
    claims = [claim for claim in review(moments(lines, config=config), config=config) if claim.verified]
    windows: list[DanmuWindow] = []
    pending_start: BoundaryClaim | None = None
    for claim in claims:
        if claim.direction == DIRECTION_START:
            if pending_start is None:
                pending_start = claim
            continue
        if pending_start is None:
            continue  # 没有起点就先看到终点：不足以成局，丢弃
        if claim.at_ms > pending_start.at_ms:
            windows.append(
                DanmuWindow(
                    game_no=len(windows) + 1,
                    start_ms=pending_start.at_ms,
                    end_ms=claim.at_ms,
                    kinds=tuple(sorted(set(pending_start.kinds) | set(claim.kinds))),
                    evidence={
                        "start": pending_start.as_dict(),
                        "end": claim.as_dict(),
                    },
                )
            )
        pending_start = None
    return tuple(windows)
