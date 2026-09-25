"""来源引用与 SHA256 校验（需求 FR-C4-7、AC-13/AC-17 的底座）。

每项事实都带来源：**文件路径 + 行范围 + SHA256**。哈希取该行范围（含首尾行、
含行尾换行）的原始字节，因此事后可独立复核「这一段证据有没有被改过」。

归档（T13 / ADR-0021）不改这套语义：文件位置由 `common/evidence.py` 解析 ——
引用里冻结的是**生成那一刻的地址**，证据归档后靠在线 ↔ 归档两个地址的互换找到
归档件，再解压后取行范围摘要，因此同一个引用的 SHA256 在归档前后**都对得上**。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from danmu_intel.common import evidence


@dataclass(frozen=True, slots=True)
class SourceRef:
    rel_path: str  # 相对数据根目录，如 raw/huya/2026-09-22/660000-16.jsonl
    line_start: int
    line_end: int
    sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "rel_path": self.rel_path,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "SourceRef":
        return cls(
            rel_path=str(payload["rel_path"]),
            line_start=int(payload["line_start"]),  # type: ignore[arg-type]
            line_end=int(payload["line_end"]),  # type: ignore[arg-type]
            sha256=str(payload["sha256"]),
        )


def resolve(ref: SourceRef, *, data_root: Path | None = None) -> Path:
    """引用对应的证据文件：在线件不在就看归档件（归档件解压后即原始内容）。"""
    return evidence.locate(ref.rel_path, data_root=data_root)


def compute_digest(path: Path, line_start: int, line_end: int) -> str:
    """对 `[line_start, line_end]` 行（含首尾）的原始字节取 SHA256。"""
    return evidence.line_range_sha256(path, line_start, line_end)


def file_digest(path: Path) -> str:
    """整文件 SHA256 —— 与 `danmu_segments.sha256`（采集时封存的摘要）同口径。

    归档件先解压再取摘要：封存值记的是**内容**，不是压缩后的字节。
    """
    return evidence.content_sha256(path)


def make_ref(rel_path: str, line_start: int, line_end: int, *, data_root: Path | None = None) -> SourceRef:
    path = evidence.locate(rel_path, data_root=data_root)
    return SourceRef(
        rel_path=rel_path,
        line_start=line_start,
        line_end=line_end,
        sha256=compute_digest(path, line_start, line_end),
    )


def verify(ref: SourceRef, *, data_root: Path | None = None) -> bool:
    """复核来源：文件存在（在线或归档件）、行范围可读、SHA256 一致。"""
    try:
        return compute_digest(resolve(ref, data_root=data_root), ref.line_start, ref.line_end) == ref.sha256
    except (OSError, ValueError):
        return False


def merge_line_numbers(line_numbers: list[int]) -> list[tuple[int, int]]:
    """把行号列表合并成连续区间（用于把「一组弹幕」压成最少的来源引用）。"""
    spans: list[tuple[int, int]] = []
    for number in sorted(set(line_numbers)):
        if spans and number == spans[-1][1] + 1:
            spans[-1] = (spans[-1][0], number)
        else:
            spans.append((number, number))
    return spans


def evidence_key(rel_path: str) -> str:
    """同一份证据的**规范地址**（在线那颗）：`seals` 与引用都按它对齐。

    归档把 `danmu_segments.rel_path` 改成了归档件地址（ADR-0002），而报告里冻结的
    是在线地址；两侧都用这个函数归一，封存摘要的比对才不至于悄悄跳过。
    """
    return evidence.online_rel_path(rel_path)


def refs_for_lines(rel_path: str, line_numbers: list[int], *, data_root: Path | None = None) -> list[SourceRef]:
    return [
        make_ref(rel_path, start, end, data_root=data_root)
        for start, end in merge_line_numbers(line_numbers)
    ]
