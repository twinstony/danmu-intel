"""官方数据契约（`matches.official_result`，设计 §5.1）。

设计 §8.1 把「官方」列为切片边界的第 1 优先级、§9.1 把官方数据列为比分与击杀轴的首选来源；
官方数据源本身仍是设计 §20 O8 的开放项，所以 T4 把它的**形状**先定下来，人工登记与
将来的抓取器共用同一个 JSON：

```json
{
  "score": "2:0",                              -- 场次比分（可选）
  "games": [                                   -- 小局列表（可选；官方边界来源）
    {"game_no": 1, "start_ms": 1758451200000, "end_ms": 1758453000000, "score": "1:0"}
  ],
  "kills": [                                   -- 官方事件序列（可选；击杀轴首选来源）
    {"ts": 1758451260000, "side": "team_a", "note": "一血"}
  ]
}
```

字段全部可选：缺哪一项，就由弹幕信号补（需求 §6.3 第 2 优先级）或如实标注「未回填」——
**不猜**。
"""

from __future__ import annotations

from typing import Any

SIDE_TEAM_A = "team_a"
SIDE_TEAM_B = "team_b"
SIDE_NEUTRAL = "neutral"
SIDES = (SIDE_TEAM_A, SIDE_TEAM_B, SIDE_NEUTRAL)


def _payload(official_result: dict[str, Any] | None) -> dict[str, Any]:
    return official_result if isinstance(official_result, dict) else {}


def match_score(official_result: dict[str, Any] | None) -> str | None:
    score = _payload(official_result).get("score")
    return str(score) if score else None


def games(official_result: dict[str, Any] | None) -> tuple[dict[str, Any], ...]:
    """官方小局列表（按 `game_no` 排序；缺字段的条目直接跳过 —— 官方数据不做残缺补全）。"""
    entries = _payload(official_result).get("games") or []
    cleaned = [
        {
            "game_no": int(entry["game_no"]),
            "start_ms": int(entry["start_ms"]),
            "end_ms": int(entry["end_ms"]),
            "score": str(entry["score"]) if entry.get("score") else None,
        }
        for entry in entries
        if isinstance(entry, dict) and {"game_no", "start_ms", "end_ms"} <= set(entry)
    ]
    return tuple(sorted(cleaned, key=lambda item: item["game_no"]))


def game(official_result: dict[str, Any] | None, game_no: int) -> dict[str, Any] | None:
    for entry in games(official_result):
        if entry["game_no"] == game_no:
            return entry
    return None


def score_for_game(official_result: dict[str, Any] | None, game_no: int) -> tuple[str | None, str]:
    """该小局的官方比分与口径：`("1:0", "game")` / `("2:0", "match")` / `(None, "missing")`。"""
    entry = game(official_result, game_no)
    if entry is not None and entry["score"]:
        return entry["score"], "game"
    overall = match_score(official_result)
    if overall:
        return overall, "match"
    return None, "missing"


def kills(official_result: dict[str, Any] | None) -> tuple[dict[str, Any], ...]:
    """官方事件序列（击杀轴首选来源）；缺字段的条目跳过。"""
    entries = _payload(official_result).get("kills") or []
    cleaned = [
        {
            "ts": int(entry["ts"]),
            "side": str(entry.get("side") or SIDE_NEUTRAL),
            "note": str(entry.get("note") or ""),
        }
        for entry in entries
        if isinstance(entry, dict) and "ts" in entry
    ]
    return tuple(sorted(cleaned, key=lambda item: item["ts"]))
