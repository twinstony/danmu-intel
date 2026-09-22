"""基础规则统计（设计 §9）。

**铁律：本层全部是纯函数** —— 输入（事件列表 + 切片），输出（指标数值）。
不读时钟、不读网络、不读全局状态。这条铁律买到三件事：可重算（AC-13）、
可测试（覆盖率载体）、可并行。
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from danmu_intel.common.events import DanmuEvent
from danmu_intel.slice.manual import SliceWindow

ALGO_VERSION = "1.0.0"
WINDOW_MS = 60_000  # 滑窗宽度：60s
STEP_MS = 30_000  # 滑窗步长：30s
PEAK_SIGMA = 3.0  # 峰值判定：count > mean + 3σ
PEAK_ABSOLUTE = 40  # 峰值判定：count > 绝对阈值
SAMPLE_SIZE = 5  # 峰值窗口内容摘要条数
FLOAT_DIGITS = 6  # ADR-0002：浮点入库统一保留 6 位小数


@dataclass(frozen=True, slots=True)
class RawLine:
    """一条原始弹幕 + 它的取证坐标（文件 + 行号）。"""

    rel_path: str
    line_no: int
    event: DanmuEvent


def in_window(line: RawLine, window: SliceWindow) -> bool:
    return window.start_ms <= line.event.ts < window.end_ms


def select(lines: list[RawLine], window: SliceWindow) -> list[RawLine]:
    return [line for line in lines if in_window(line, window)]


def _round(value: float) -> float:
    return round(value, FLOAT_DIGITS)


def density_curve(timestamps: list[int], start_ms: int, end_ms: int) -> list[dict[str, int]]:
    """密度曲线：窗口 60s、步长 30s 的滑窗逐窗计数（设计 §9.1）。"""
    if end_ms <= start_ms:
        return []
    ordered = sorted(timestamps)
    points: list[dict[str, int]] = []
    cursor = start_ms
    index = 0
    total = len(ordered)
    while cursor < end_ms:
        window_end = cursor + WINDOW_MS
        while index < total and ordered[index] < cursor:
            index += 1
        count = 0
        scan = index
        while scan < total and ordered[scan] < window_end:
            count += 1
            scan += 1
        points.append({"t_start": cursor, "count": count})
        cursor += STEP_MS
    return points


def peak(points: list[dict[str, int]]) -> dict[str, object] | None:
    """峰值：取密度曲线里的最高窗口，需满足 `> mean + 3σ` 或 `> 绝对阈值`。"""
    if not points:
        return None
    counts = [point["count"] for point in points]
    best = max(counts)
    if best <= 0:
        return None
    mean = statistics.fmean(counts)
    sigma = statistics.pstdev(counts)
    threshold = _round(mean + PEAK_SIGMA * sigma)
    if best <= threshold and best <= PEAK_ABSOLUTE:
        return None
    holder = next(point for point in points if point["count"] == best)
    return {
        "t_start": holder["t_start"],
        "t_end": holder["t_start"] + WINDOW_MS,
        "count": best,
        "mean": _round(mean),
        "sigma": _round(sigma),
        "threshold": threshold,
        "method": "mean+3sigma" if best > threshold else "absolute",
    }


def total(lines: list[RawLine]) -> dict[str, int]:
    return {"count": len(lines)}


def distinct_users(lines: list[RawLine]) -> dict[str, int]:
    return {"count": len({line.event.user_hash for line in lines})}


def compute(lines: list[RawLine], window: SliceWindow) -> dict[str, dict[str, object]]:
    """某小局的全部基础统计。返回 `metric_key -> value`（可 JSON 序列化）。"""
    scoped = select(lines, window)
    timestamps = [line.event.ts for line in scoped]
    curve = density_curve(timestamps, window.start_ms, window.end_ms)
    top = peak(curve)
    values: dict[str, dict[str, object]] = {
        "danmu_total": total(scoped),
        "distinct_users": distinct_users(scoped),
        "density_curve": {"window_ms": WINDOW_MS, "step_ms": STEP_MS, "points": curve},
        "peak": top if top is not None else {},
    }
    if top is not None:
        values["peak"]["sample"] = peak_sample(scoped, top)
    return values


def peak_sample(scoped: list[RawLine], top: dict[str, object]) -> list[dict[str, object]]:
    """峰值窗口内容摘要：取样若干条原文 + 取证坐标（设计 §9.1）。

    按时间排序后取样，保证与输入顺序无关（可重算性）。
    """
    start = int(top["t_start"])  # type: ignore[arg-type]
    end = int(top["t_end"])  # type: ignore[arg-type]
    inside = sorted(
        (line for line in scoped if start <= line.event.ts < end),
        key=lambda line: (line.event.ts, line.line_no),
    )
    return [
        {"rel_path": line.rel_path, "line_no": line.line_no, "ts": line.event.ts, "text": line.event.text}
        for line in inside[:SAMPLE_SIZE]
    ]


def coverage_span(lines: list[RawLine]) -> dict[str, int | None]:
    if not lines:
        return {"first_ts": None, "last_ts": None}
    timestamps = [line.event.ts for line in lines]
    return {"first_ts": min(timestamps), "last_ts": max(timestamps)}
