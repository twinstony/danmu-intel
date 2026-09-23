"""统计全集（设计 §9.1；需求 FR-C3-1）。

在 `stats/basic.py` 的基础上补齐 C3 要求的全部指标：

| 指标 | 键 | 算法出处 |
|---|---|---|
| 弹幕总量 / 独立发言者 | `danmu_total` / `distinct_users` | §9.1 |
| 密度曲线（60s 窗 / 30s 步） | `density_curve` | §9.1 |
| 峰值 | `peak` | §9.1 |
| 低谷 | `trough` | FR-C3-1「峰值与低谷时刻」 |
| 比分 / 小局结果 | `score` | §9.1（官方为准，差异必记录） |
| 击杀时间轴 | `kill_timeline` | §9.1（官方优先，缺则弹幕抽取） |
| 中立指标（双方提及量对比） | `neutral` | FR-C3-1「双方各项指标对比」+ 中立纪律 |

**中立**的含义：只做计数，不做褒贬、不做归因。双方提及量是「多少人提了谁」，
不是「谁表现好」。

纯函数层：不读时钟、不读网络、不读全局状态（设计 §9 铁律）。
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Sequence

from danmu_intel.common import official
from danmu_intel.slice.manual import SliceWindow
from danmu_intel.stats.basic import WINDOW_MS, RawLine, compute, coverage_span, peak, select

#: 比分提及：`2:0` / `2：0` / `2比0`（一位或两位数字）。
SCORE_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[:：比]\s*(\d{1,2})(?!\d)")

#: 击杀/关键事件词法（击杀轴在缺官方事件序列时的抽取依据）。
KILL_LEXICON = ("一血", "单杀", "双杀", "三杀", "四杀", "五杀", "团灭", "击杀", "超神", "偷家")

#: 指标值里的样本条数上限（取证够用即可，避免指标行无限膨胀）。
SAMPLE_SIZE = 5


def trough(points: Sequence[dict[str, int]]) -> dict[str, Any] | None:
    """低谷时刻：密度曲线里最低的窗口（并列取最早）。全程为零则无意义，返回 None。"""
    if not points or max(point["count"] for point in points) <= 0:
        return None
    lowest = min(point["count"] for point in points)
    holder = next(point for point in points if point["count"] == lowest)
    return {
        "t_start": holder["t_start"],
        "t_end": holder["t_start"] + WINDOW_MS,
        "count": lowest,
        "method": "minimum",
    }


def score(mentions: Sequence[dict[str, Any]], official_result: dict[str, Any] | None, *, game_no: int) -> dict[str, Any]:
    """比分/小局结果：官方为准，弹幕信号交叉校验，不一致时记录差异（设计 §9.1）。"""
    official_score, scope = official.score_for_game(official_result, game_no)
    consensus: str | None = None
    first_ts: int | None = None
    if mentions:
        counts = Counter(mention["score"] for mention in mentions)
        top = max(counts.values())
        consensus = min((value for value, count in counts.items() if count == top), key=lambda value: (
            next(mention["ts"] for mention in mentions if mention["score"] == value)
        ))
        first_ts = next(mention["ts"] for mention in mentions if mention["score"] == consensus)
    consistent = bool(official_score and consensus and official_score == consensus)
    discrepancy: str | None = None
    if official_score and consensus and not consistent:
        discrepancy = f"弹幕多数意见 {consensus} 与官方 {official_score} 不一致，以官方为准（差异留档）"
    elif official_score and not consensus:
        discrepancy = "弹幕中没有可比对的比分信息，仅以官方为准"
    elif consensus and not official_score:
        discrepancy = f"官方比分未回填，弹幕多数意见 {consensus} 仅供参考"
    return {
        "official": official_score,
        "official_scope": scope,  # game|match|missing
        "danmu_consensus": consensus,
        "consistent": consistent,
        "discrepancy": discrepancy,
        "mentions": len(mentions),
        "first_ts": first_ts,
        "samples": [
            {
                "ts": mention["ts"],
                "text": mention["text"],
                "score": mention["score"],
                "rel_path": mention["rel_path"],
                "line_no": mention["line_no"],
            }
            for mention in mentions[:SAMPLE_SIZE]
        ],
    }


def score_mentions(lines: Sequence[RawLine]) -> list[dict[str, Any]]:
    """抽取弹幕里的比分提及（纯词法，不做意图判断）。"""
    found: list[dict[str, Any]] = []
    for line in lines:
        for match in SCORE_RE.finditer(line.event.text):
            found.append(
                {
                    "ts": line.event.ts,
                    "score": f"{int(match.group(1))}:{int(match.group(2))}",
                    "text": line.event.text,
                    "rel_path": line.rel_path,
                    "line_no": line.line_no,
                }
            )
    return sorted(found, key=lambda item: (item["ts"], item["line_no"]))


def _side_of(text: str, side_names: dict[str, str]) -> str:
    for side in (official.SIDE_TEAM_A, official.SIDE_TEAM_B):
        name = side_names.get(side)
        if name and name in text:
            return side
    return official.SIDE_NEUTRAL


def kill_timeline(
    lines: Sequence[RawLine],
    official_result: dict[str, Any] | None,
    *,
    side_names: dict[str, str],
) -> dict[str, Any]:
    """击杀时间轴：官方事件序列优先；缺则弹幕信号抽取，并标注来源（设计 §9.1）。"""
    official_events = official.kills(official_result)
    if official_events:
        return {
            "source": "official",
            "events": [
                {"ts": event["ts"], "side": event["side"], "note": event["note"], "source": "official"}
                for event in official_events
            ],
        }
    events = [
        {
            "ts": line.event.ts,
            "side": _side_of(line.event.text, side_names),
            "note": line.event.text,
            "source": "danmu_signal",
            "rel_path": line.rel_path,
            "line_no": line.line_no,
        }
        for line in lines
        if any(word in line.event.text for word in KILL_LEXICON)
    ]
    events.sort(key=lambda event: (event["ts"], event["line_no"]))
    return {"source": "danmu_signal" if events else "none", "events": events}


def neutral(lines: Sequence[RawLine], *, side_names: dict[str, str]) -> dict[str, Any]:
    """中立指标：双方提及量对比 + 中立弹幕量。只计数，不褒贬、不归因。"""
    buckets: dict[str, list[RawLine]] = {official.SIDE_TEAM_A: [], official.SIDE_TEAM_B: [], official.SIDE_NEUTRAL: []}
    for line in lines:
        buckets[_side_of(line.event.text, side_names)].append(line)
    span = coverage_span(lines)
    return {
        "side_mentions": {
            side: {
                "name": side_names.get(side),
                "count": len(bucket),
                "distinct_users": len({line.event.user_hash for line in bucket}),
            }
            for side, bucket in buckets.items()
        },
        "danmu_total": len(lines),
        "distinct_users": len({line.event.user_hash for line in lines}),
        "coverage": span,
    }


def compute_game(
    lines: Sequence[RawLine],
    window: SliceWindow,
    *,
    official_result: dict[str, Any] | None = None,
    side_names: dict[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    """某小局的统计全集（可直接落 `metrics`）。"""
    names = side_names or {}
    scoped = select(lines, window)
    metrics = compute(scoped, window)
    points = metrics["density_curve"]["points"]
    metrics["trough"] = trough(points) or {}
    metrics["score"] = score(score_mentions(scoped), official_result, game_no=window.game_no)
    metrics["kill_timeline"] = kill_timeline(scoped, official_result, side_names=names)
    metrics["neutral"] = neutral(scoped, side_names=names)
    return metrics


def observed_until(lines: Sequence[RawLine]) -> int | None:
    """观测终点：最后一条原始记录的时刻 +1ms。没有任何记录时返回 None。"""
    if not lines:
        return None
    return max(line.event.ts for line in lines) + 1


def peak_count(metrics: dict[str, Any]) -> int:
    top = metrics.get("peak") or {}
    return int(top.get("count", 0)) if top else 0


__all__ = [
    "KILL_LEXICON",
    "SCORE_RE",
    "compute_game",
    "kill_timeline",
    "neutral",
    "observed_until",
    "peak",
    "peak_count",
    "score",
    "score_mentions",
    "trough",
]
