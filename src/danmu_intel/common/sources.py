"""来源引用与 SHA256 校验（需求 FR-C4-7、AC-13/AC-17 的底座）。

每项事实都带来源：**文件路径 + 行范围 + SHA256**。哈希取该行范围（含首尾行、
含行尾换行）的原始字节，因此事后可独立复核「这一段证据有没有被改过」。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from danmu_intel.common import paths


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
    return (data_root or paths.data_dir()) / ref.rel_path


def compute_digest(path: Path, line_start: int, line_end: int) -> str:
    """对 `[line_start, line_end]` 行（含首尾）的原始字节取 SHA256。"""
    if line_start < 1 or line_end < line_start:
        raise ValueError(f"非法行范围：{line_start}-{line_end}")
    lines = path.read_bytes().splitlines(keepends=True)
    if line_end > len(lines):
        raise ValueError(f"行范围超出文件：{line_end} > {len(lines)}")
    return hashlib.sha256(b"".join(lines[line_start - 1 : line_end])).hexdigest()


def make_ref(rel_path: str, line_start: int, line_end: int, *, data_root: Path | None = None) -> SourceRef:
    path = (data_root or paths.data_dir()) / rel_path
    return SourceRef(
        rel_path=rel_path,
        line_start=line_start,
        line_end=line_end,
        sha256=compute_digest(path, line_start, line_end),
    )


def verify(ref: SourceRef, *, data_root: Path | None = None) -> bool:
    """复核来源：文件存在、行范围可读、SHA256 一致。"""
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


def refs_for_lines(rel_path: str, line_numbers: list[int], *, data_root: Path | None = None) -> list[SourceRef]:
    return [
        make_ref(rel_path, start, end, data_root=data_root)
        for start, end in merge_line_numbers(line_numbers)
    ]
