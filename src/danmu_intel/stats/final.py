"""终局判定（需求 §6.4 / 设计 §9.2）。

规则（需求原文）：**至少 3 类相互独立的信号同时成立**，**且此后 2 分钟内未出现反转**
（如官方改判、比赛恢复），才可判「已终局」。设计补一句纪律：**宁可不判，不可误判** ——
不确定时保持 `live`。

四类信号（互不依赖，各自有独立的证据来源）：

| 类别 | 判定 | 需求出处 |
|---|---|---|
| `end_burst` | 终结类弹幕高密度聚集并持续 ≥2 分钟 | §6.4 ① |
| `score_confirmed` | 比分与官方结果核对一致 | §6.4 ② |
| `traffic_drop` | 弹幕流量降至峰值一成以下并持续 ≥5 分钟 | §6.4 ③ |
| `announcement` | 官方渠道或主播明确宣布比赛结束 | §6.4 ④ |

**反转窗口**：首次满足 3 类后开 120s 计时器。期间任一**当时成立的信号失效**，
或出现 `official_revision` / `resumed`（官方改判 / 比赛恢复）→ 撤销，并留下
「曾判定、已撤销」的事实（`satisfied_at_ms` + `reversal`）。

本模块与 `stats/basic.py`、`stats/full.py` 一样是**纯函数层**：不读时钟、不读网络、
不读全局状态；`observed_until_ms` 由调用方从原始记录推出，`config` 由调用方注入。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from danmu_intel.common.config import StatsConfig
from danmu_intel.stats.basic import STEP_MS, WINDOW_MS, RawLine, density_curve

#: 四类独立信号（需求 §6.4 ①②③④）。
SIGNAL_KINDS = ("announcement", "end_burst", "score_confirmed", "traffic_drop")

#: 反转信号（需求 §6.4 末句：官方改判、比赛恢复）。
REVERSAL_KINDS = ("official_revision", "resumed")

SIGNAL_LABELS = {
    "end_burst": "终结类弹幕高密度聚集（≥2 分钟）",
    "score_confirmed": "比分与官方结果核对一致",
    "traffic_drop": "弹幕流量降至峰值一成以下（≥5 分钟）",
    "announcement": "官方或主播明确宣布比赛结束",
    "official_revision": "官方改判",
    "resumed": "比赛恢复",
}

VERDICT_FINAL = "final"
VERDICT_LIVE = "live"
VERDICT_REVOKED = "revoked"

#: 终结类弹幕词法模式（需求 §6.4 ①「终结类弹幕」）。
END_LEXICON = ("结束", "GG", "gg", "恭喜", "赢了", "输了", "收官", "拿下", "再见", "终局")

#: 宣告类词法模式（需求 §6.4 ④「官方渠道或主播明确宣布」）。
ANNOUNCE_LEXICON = ("官宣", "宣布", "下播", "本场结束", "比赛结束")


@dataclass(frozen=True, slots=True)
class SignalFact:
    """一类信号在一段时间内成立（`end_ms=None` 表示观测结束时仍成立）。"""

    kind: str
    start_ms: int
    end_ms: int | None
    evidence: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": SIGNAL_LABELS.get(self.kind, self.kind),
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "evidence": self.evidence,
        }


@dataclass(frozen=True, slots=True)
class Reversal:
    kind: str
    at_ms: int
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "label": SIGNAL_LABELS.get(self.kind, self.kind),
                "at_ms": self.at_ms, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class FinalJudgement:
    verdict: str  # final | live | revoked
    satisfied_at_ms: int | None
    decided_at_ms: int | None
    kinds: tuple[str, ...]
    reason: str
    reversal: Reversal | None = None

    @property
    def is_final(self) -> bool:
        return self.verdict == VERDICT_FINAL

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "satisfied_at_ms": self.satisfied_at_ms,
            "decided_at_ms": self.decided_at_ms,
            "kinds": list(self.kinds),
            "reason": self.reason,
            "reversal": self.reversal.as_dict() if self.reversal else None,
        }


def active_kinds(facts: Iterable[SignalFact], at_ms: int) -> set[str]:
    """在 `at_ms` 这一刻成立的信号类别（只看四类独立信号，反转信号不算成立）。"""
    return {
        fact.kind
        for fact in facts
        if fact.kind in SIGNAL_KINDS and fact.start_ms <= at_ms and (fact.end_ms is None or at_ms < fact.end_ms)
    }


def _first_satisfied(facts: Sequence[SignalFact], min_kinds: int) -> int | None:
    """最早出现「≥min_kinds 类独立信号同时成立」的时刻。"""
    candidates = sorted({fact.start_ms for fact in facts if fact.kind in SIGNAL_KINDS})
    for moment in candidates:
        if len(active_kinds(facts, moment)) >= min_kinds:
            return moment
    return None


def judge_final(
    facts: Iterable[SignalFact],
    *,
    observed_until_ms: int,
    config: StatsConfig | None = None,
) -> FinalJudgement:
    """按需求 §6.4 判定是否终局。返回 `final` / `live` / `revoked` 三选一。"""
    conf = config or StatsConfig()
    ordered = tuple(sorted(facts, key=lambda fact: (fact.start_ms, fact.kind)))
    satisfied_at = _first_satisfied(ordered, conf.min_signal_kinds)
    if satisfied_at is None:
        return FinalJudgement(
            verdict=VERDICT_LIVE,
            satisfied_at_ms=None,
            decided_at_ms=None,
            kinds=(),
            reason=f"独立信号不足 {conf.min_signal_kinds} 类，不判终局（宁可不判，不可误判）",
        )

    kinds = tuple(sorted(active_kinds(ordered, satisfied_at)))
    deadline = satisfied_at + conf.reversal_window_ms

    for fact in ordered:
        if fact.kind not in kinds:
            continue
        if fact.end_ms is not None and fact.end_ms < deadline:
            return _revoked(
                satisfied_at, kinds, Reversal(fact.kind, fact.end_ms, f"{SIGNAL_LABELS[fact.kind]}在反转窗口内失效"),
                conf,
            )
    for fact in ordered:
        if fact.kind in REVERSAL_KINDS and satisfied_at <= fact.start_ms < deadline:
            return _revoked(
                satisfied_at,
                kinds,
                Reversal(fact.kind, fact.start_ms, f"反转窗口内出现{SIGNAL_LABELS[fact.kind]}（{fact.evidence.get('detail', '')}）"),
                conf,
            )

    if observed_until_ms < deadline:
        return FinalJudgement(
            verdict=VERDICT_LIVE,
            satisfied_at_ms=satisfied_at,
            decided_at_ms=None,
            kinds=kinds,
            reason=(
                f"已满足 {len(kinds)} 类独立信号，但观测只到 {observed_until_ms}，"
                f"反转窗口未走完，暂不判终局"
            ),
        )
    return FinalJudgement(
        verdict=VERDICT_FINAL,
        satisfied_at_ms=satisfied_at,
        decided_at_ms=deadline,
        kinds=kinds,
        reason=(
            f"{len(kinds)} 类独立信号同时成立（{'、'.join(SIGNAL_LABELS[kind] for kind in kinds)}），"
            f"且 {conf.reversal_window_ms // 1000} 秒内无反转"
        ),
    )


def _revoked(satisfied_at: int, kinds: tuple[str, ...], reversal: Reversal, conf: StatsConfig) -> FinalJudgement:
    return FinalJudgement(
        verdict=VERDICT_REVOKED,
        satisfied_at_ms=satisfied_at,
        decided_at_ms=None,
        kinds=kinds,
        reason=(
            f"曾满足 {len(kinds)} 类独立信号（{satisfied_at}），但 {conf.reversal_window_ms // 1000} 秒内出现反转："
            f"{reversal.detail}，撤销判定"
        ),
        reversal=reversal,
    )


# --------------------------------------------------------------------------- #
# 信号抽取（同样是纯函数：只吃事件 + 官方数据 + 配置）
# --------------------------------------------------------------------------- #


def _run(
    points: Sequence[dict[str, int]], predicate, *, min_ms: int
) -> tuple[int, int | None, int] | None:
    """在密度曲线的**连续窗口**里找最长的一段满足 `predicate` 且时长 ≥ `min_ms`。

    返回 `(start_ms, end_ms | None, 命中条数)`；到达曲线末尾仍成立则 `end_ms=None`
    （表示信号在观测结束时依然成立）。
    """
    best: tuple[int, int | None, int, int] | None = None  # start, end, hits, duration
    for candidate in _runs(points, predicate):
        start = candidate[0]["t_start"]
        last_index = candidate[-1]["t_start"]
        end = None if candidate[-1] is points[-1] else last_index + WINDOW_MS
        duration = (last_index + WINDOW_MS) - start
        if duration < min_ms:
            continue
        hits = sum(point["count"] for point in candidate)
        if best is None or duration > best[3]:
            best = (start, end, hits, duration)
    return None if best is None else (best[0], best[1], best[2])


def _runs(points: Sequence[dict[str, int]], predicate) -> list[list[dict[str, int]]]:
    """把曲线切成「连续满足条件」的若干段（逐窗口相邻）。"""
    runs: list[list[dict[str, int]]] = []
    current: list[dict[str, int]] = []
    for point in points:
        if predicate(point["count"]):
            current.append(point)
            continue
        if current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    return runs


def end_burst_fact(lines: Sequence[RawLine], *, observed_until_ms: int, config: StatsConfig) -> SignalFact | None:
    """需求 §6.4 ①：终结类弹幕高密度聚集并持续不短于 2 分钟。"""
    hits = [line.event.ts for line in lines if _matches(line.event.text, END_LEXICON)]
    if not hits:
        return None
    points = density_curve(hits, min(hits), observed_until_ms)
    found = _run(points, lambda count: count >= config.end_burst_min_hits, min_ms=config.end_burst_min_ms)
    if found is None:
        return None
    start, end, total = found
    return SignalFact(
        kind="end_burst",
        start_ms=start,
        end_ms=end,
        evidence={
            "hits": total,
            "window_ms": WINDOW_MS,
            "step_ms": STEP_MS,
            "min_ms": config.end_burst_min_ms,
            "min_hits_per_window": config.end_burst_min_hits,
        },
    )


def traffic_drop_fact(lines: Sequence[RawLine], *, observed_until_ms: int, config: StatsConfig) -> SignalFact | None:
    """需求 §6.4 ③：弹幕流量降至峰值的一成以下并持续不短于 5 分钟。"""
    if not lines:
        return None
    timestamps = [line.event.ts for line in lines]
    start = min(timestamps)
    points = density_curve(timestamps, start, observed_until_ms)
    top = max((point["count"] for point in points), default=0)
    if top <= 0:
        return None
    limit = top * config.silence_ratio
    found = _run(points, lambda count: count < limit, min_ms=config.silence_min_ms)
    if found is None:
        return None
    window_start, window_end, total = found
    return SignalFact(
        kind="traffic_drop",
        start_ms=window_start,
        end_ms=window_end,
        evidence={
            "peak_count": top,
            "peak_ratio": config.silence_ratio,
            "min_ms": config.silence_min_ms,
            "count": total,
        },
    )


def score_confirmed_fact(score: dict[str, Any]) -> SignalFact | None:
    """需求 §6.4 ②：比分与官方结果核对一致（来源：`stats/full.compute_game` 的 `score` 指标）。"""
    if not score.get("official") or not score.get("consistent"):
        return None
    return SignalFact(
        kind="score_confirmed",
        start_ms=int(score["first_ts"]),
        end_ms=None,
        evidence={
            "official": score["official"],
            "danmu_consensus": score["danmu_consensus"],
            "mentions": score["mentions"],
        },
    )


def announcement_fact(
    lines: Sequence[RawLine],
    *,
    official_ended_at: int | None,
    config: StatsConfig,
) -> SignalFact | None:
    """需求 §6.4 ④：官方渠道或主播明确宣布比赛结束。

    官方渠道：已登记的比赛结束时间（官方数据源接入前的登记口，设计 §20 O8）。
    主播宣告：宣告词在 ≤2 分钟（`boundary_cluster_ms`）内出现 ≥2 次 —— 单条可能是误读。
    """
    if official_ended_at is not None:
        return SignalFact(
            kind="announcement",
            start_ms=official_ended_at,
            end_ms=None,
            evidence={"channel": "official", "detail": "比赛结束时间已由官方数据登记"},
        )
    hits = [line.event.ts for line in lines if _matches(line.event.text, ANNOUNCE_LEXICON)]
    if len(hits) < 2:
        return None
    ordered = sorted(hits)
    for index in range(len(ordered) - 1):
        window = [ts for ts in ordered if ordered[index] <= ts <= ordered[index] + config.boundary_cluster_ms]
        if len(window) >= 2:
            return SignalFact(
                kind="announcement",
                start_ms=window[0],
                end_ms=window[-1] + 1,
                evidence={"channel": "danmu", "hits": len(window), "detail": "宣告词在 2 分钟内多次出现"},
            )
    return None


def _matches(text: str, lexicon: Sequence[str]) -> bool:
    return any(word in text for word in lexicon)


def collect_signal_facts(
    lines: Sequence[RawLine],
    *,
    score: dict[str, Any],
    official_ended_at: int | None,
    observed_until_ms: int,
    config: StatsConfig,
    extra: Sequence[SignalFact] = (),
) -> tuple[SignalFact, ...]:
    """抽取全部信号事实（四类独立信号 + 调用方注入的反转信号）。"""
    facts: list[SignalFact] = [fact for fact in extra]
    for candidate in (
        end_burst_fact(lines, observed_until_ms=observed_until_ms, config=config),
        traffic_drop_fact(lines, observed_until_ms=observed_until_ms, config=config),
        score_confirmed_fact(score),
        announcement_fact(lines, official_ended_at=official_ended_at, config=config),
    ):
        if candidate is not None:
            facts.append(candidate)
    return tuple(sorted(facts, key=lambda fact: (fact.start_ms, fact.kind)))
