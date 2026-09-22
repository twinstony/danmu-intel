"""发布钩子与报告版本账本（需求 §6.8 第 4 项、AC-16，设计 §11.2 的额外加固检查）。

发布前逐项检查，**任一项不通过即拒绝发布**（不写页面；`reports` 行记 `failed` 留痕）：

1. `segments_complete`：形态段集齐备（缺段 / 空段 / 多了本形态不发布的段都不行）；
2. `interpretation_present`：**解读段不得缺失**（AC-16）—— 解读是报告的核心内容，
   不得省略、不得以纯数据替代，且必须显式标注为解读（§6.9 第 2 条）；
3. `sources_resolvable`：报告引用的每个来源都可解析（文件存在 + 行范围可读 + SHA256
   匹配）—— 事实层溯源性在发布时刻的守卫；
4. `within_deadline`：形态时限（NFR-T）对照实测耗时。**超时不阻断发布**（NFR-T 末句：
   准确性优先），但记进检查结果，绝不静默。

一份报告的修改以**新版本**形式发布（FR-C4-9）：同场同形态的版本号递增、旧行不覆盖，
`fact_layer_hash` 记录每个版本依据的事实层（设计 §10.3）。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from typing import Mapping

from danmu_intel.common import paths
from danmu_intel.common.sources import SourceRef, compute_digest, file_digest, resolve
from danmu_intel.report.assemble import ReportContent
from danmu_intel.report.forms import ReportForm, Timing, form_of
from danmu_intel.report.html import render_report_html
from danmu_intel.report.rule_render import INTERPRETATION_MARK
from danmu_intel.report.segments import SPECS_BY_NO

STATE_PUBLISHED = "published"
STATE_FAILED = "failed"


class PublishRefused(RuntimeError):
    """发布检查未通过 —— 报告不得上线。"""

    def __init__(self, failures: tuple["CheckResult", ...]) -> None:
        self.failures = failures
        detail = "；".join(f"{item.label}：{item.detail}" for item in failures)
        super().__init__(f"发布被拒绝（{len(failures)} 项未通过）：{detail}")


@dataclass(frozen=True, slots=True)
class CheckResult:
    key: str
    label: str
    passed: bool
    detail: str
    blocking: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "passed": self.passed,
            "detail": self.detail,
            "blocking": self.blocking,
        }


@dataclass(frozen=True, slots=True)
class PublishResult:
    report_id: int
    kind: str
    version: int
    path: Path
    content: ReportContent
    checks: tuple[CheckResult, ...]
    timing: dict[str, object]


@dataclass(frozen=True, slots=True)
class StoredReport:
    """`reports` 表的一行（不含 content_json，避免列表命令背上一堆正文）。"""

    id: int
    match_id: int
    game_no: int | None
    kind: str
    version: int
    generated_at: int
    state: str
    fact_layer_hash: str
    llm_state: str
    path: str | None


# —— 发布检查（逐项纯函数）——


def check_segments_complete(content: ReportContent, form: ReportForm) -> CheckResult:
    expected = list(form.segments)
    actual = [segment.no for segment in content.segments]
    empty = [segment.no for segment in content.segments if not segment.body.strip()]
    if actual != expected:
        return CheckResult(
            "segments_complete",
            "段集完整",
            False,
            f"{form.label} 应发布段 {expected}，实际为 {actual}",
        )
    if empty:
        return CheckResult(
            "segments_complete", "段集完整", False, f"空段：{','.join(map(str, empty))}"
        )
    return CheckResult("segments_complete", "段集完整", True, f"{len(actual)} 段齐备且均非空")


def check_interpretation_present(content: ReportContent, form: ReportForm) -> CheckResult:
    required = [no for no in form.segments if SPECS_BY_NO[no].has_interpretation]
    present = {segment.no: segment for segment in content.segments}
    missing = [no for no in required if no not in present or not present[no].body.strip()]
    unmarked = [
        no for no in required if no in present and INTERPRETATION_MARK not in present[no].body
    ]
    if missing:
        return CheckResult(
            "interpretation_present",
            "解读段齐备",
            False,
            f"缺解读段：{','.join(map(str, missing))}（需求 §6.9：解读不可省略，AC-16）",
        )
    if unmarked:
        return CheckResult(
            "interpretation_present",
            "解读段齐备",
            False,
            f"解读段未标注为解读：{','.join(map(str, unmarked))}（需求 §6.9 第 2 条）",
        )
    return CheckResult(
        "interpretation_present",
        "解读段齐备",
        True,
        f"{len(required)} 个解读段齐备且已标注（{','.join(map(str, required))}）",
    )


def _unique_sources(content: ReportContent) -> list[SourceRef]:
    seen: dict[tuple[str, int, int, str], SourceRef] = {}
    for segment in content.segments:
        for ref in segment.sources:
            seen.setdefault((ref.rel_path, ref.line_start, ref.line_end, ref.sha256), ref)
    return list(seen.values())


def check_sources_resolvable(
    content: ReportContent,
    *,
    data_root: Path | None = None,
    seals: Mapping[str, str] | None = None,
) -> CheckResult:
    """每个来源文件存在、行范围可读，且文件与**采集时封存的 SHA256** 一致。

    「封存哈希」来自 `danmu_segments`（采集会话落盘时记下的整文件摘要）。它是证据的
    锚点：组装报告时现算的区间摘要永远等于当前文件，只有拿封存值对比才查得出
    「原始记录被改动或追加」（AC-1 / AC-17 的溯源性守卫）。
    """
    refs = _unique_sources(content)
    problems: list[str] = []
    for ref in refs:
        path = resolve(ref, data_root=data_root)
        if not path.exists():
            problems.append(f"{ref.rel_path} 文件不存在")
            continue
        try:
            current = compute_digest(path, ref.line_start, ref.line_end)
        except (OSError, ValueError) as exc:
            problems.append(f"{ref.rel_path} 第 {ref.line_start}–{ref.line_end} 行不可读（{exc}）")
            continue
        if current != ref.sha256:
            problems.append(f"{ref.rel_path} 第 {ref.line_start}–{ref.line_end} 行在组装之后被改动")
        recorded = (seals or {}).get(ref.rel_path)
        if recorded is not None and file_digest(path) != recorded:
            problems.append(f"{ref.rel_path} 与采集时封存的 SHA256 不一致（原始记录被改动或追加）")
    if problems:
        return CheckResult(
            "sources_resolvable",
            "来源可解析",
            False,
            f"{len(problems)} 项来源无法复核（文件缺失 / 行范围越界 / SHA256 不匹配）："
            + "；".join(problems[:3]),
        )
    return CheckResult(
        "sources_resolvable",
        "来源可解析",
        True,
        f"{len(refs)} 项来源全部可复核（文件 + 行范围 + 封存 SHA256，含事实层逐项）",
    )


def check_deadline(content: ReportContent, form: ReportForm, timing: Timing | None) -> CheckResult:
    if timing is None:
        return CheckResult(
            "within_deadline", "时限预算", True, "本次未计时（未注入计时器）", blocking=False
        )
    report = timing.as_dict(form)
    over = report["over_budget_stages"]
    detail = (
        f"实测 {report['elapsed_ms']}ms ≤ 时限 {form.deadline_ms}ms（{form.label}）"
        if report["within_deadline"]
        else f"实测 {report['elapsed_ms']}ms 超过时限 {form.deadline_ms}ms（{form.label}）"
    )
    if over:
        detail += f"；超阶段预算：{'、'.join(str(stage) for stage in over)}"
    return CheckResult("within_deadline", "时限预算", bool(report["within_deadline"]), detail, blocking=False)


def run_checks(
    content: ReportContent,
    *,
    timing: Timing | None = None,
    data_root: Path | None = None,
    seals: Mapping[str, str] | None = None,
) -> tuple[CheckResult, ...]:
    form = form_of(content.kind)
    return (
        check_segments_complete(content, form),
        check_interpretation_present(content, form),
        check_sources_resolvable(content, data_root=data_root, seals=seals),
        check_deadline(content, form, timing),
    )


# —— 报告版本账本 ——


def next_version(conn: sqlite3.Connection, match_id: int, kind: str) -> int:
    row = conn.execute(
        "SELECT MAX(version) AS v FROM reports WHERE match_id=? AND kind=?", (match_id, kind)
    ).fetchone()
    return int(row["v"] or 0) + 1


def _insert(
    conn: sqlite3.Connection,
    content: ReportContent,
    *,
    state: str,
    path: str | None,
    game_no: int | None,
    checks: tuple[CheckResult, ...],
    timing: Timing | None,
) -> int:
    form = form_of(content.kind)
    cursor = conn.execute(
        """
        INSERT INTO reports(match_id, game_no, kind, version, generated_at, state, content_json,
                            fact_layer_hash, llm_state, path, checks_json, timing_json)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            content.match_id,
            game_no,
            content.kind,
            content.version,
            content.generated_at,
            state,
            content.to_json(),
            content.fact_layer_hash,
            content.llm_state,
            path,
            json.dumps([item.as_dict() for item in checks], ensure_ascii=False),
            json.dumps(timing.as_dict(form) if timing is not None else {}, ensure_ascii=False),
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def list_reports(conn: sqlite3.Connection, match_id: int) -> list[StoredReport]:
    rows = conn.execute(
        "SELECT id, match_id, game_no, kind, version, generated_at, state, fact_layer_hash,"
        " llm_state, path FROM reports WHERE match_id=? ORDER BY kind, version",
        (match_id,),
    ).fetchall()
    return [
        StoredReport(
            id=int(row["id"]),
            match_id=int(row["match_id"]),
            game_no=row["game_no"],
            kind=row["kind"],
            version=int(row["version"]),
            generated_at=int(row["generated_at"]),
            state=row["state"],
            fact_layer_hash=row["fact_layer_hash"],
            llm_state=row["llm_state"],
            path=row["path"],
        )
        for row in rows
    ]


def load_content(conn: sqlite3.Connection, match_id: int, kind: str, version: int) -> ReportContent:
    row = conn.execute(
        "SELECT content_json FROM reports WHERE match_id=? AND kind=? AND version=?",
        (match_id, kind, version),
    ).fetchone()
    if row is None:
        raise LookupError(f"未找到报告：比赛 #{match_id} {kind} v{version}")
    return ReportContent.from_dict(json.loads(row["content_json"]))


# —— 发布 ——


def publish(
    conn: sqlite3.Connection,
    content: ReportContent,
    *,
    data_root: Path | None = None,
    timing: Timing | None = None,
    seals: Mapping[str, str] | None = None,
) -> PublishResult:
    """检查 → 渲染 → 落盘 → 记账。检查不过就不上线（AC-16）。

    `seals` 是「落盘文件 → 采集时封存的 SHA256」（`danmu_segments.sha256`），
    来源检查拿它当证据锚点。
    """
    form = form_of(content.kind)
    checks = run_checks(content, timing=timing, data_root=data_root, seals=seals)
    if timing is not None:
        timing.mark("publish_checks")

    failures = tuple(item for item in checks if item.blocking and not item.passed)
    game_no = content.meta.get("trigger_game_no")
    game_no = int(game_no) if isinstance(game_no, int) else None

    if failures:
        _insert(
            conn,
            content,
            state=STATE_FAILED,
            path=None,
            game_no=game_no,
            checks=checks,
            timing=timing,
        )
        raise PublishRefused(failures)

    target = paths.report_page_path(content.match_id, content.kind)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_report_html(content), encoding="utf-8")
    if timing is not None:
        timing.mark("render")

    rel_path = paths.rel_to_site(target)
    report_id = _insert(
        conn,
        content,
        state=STATE_PUBLISHED,
        path=rel_path,
        game_no=game_no,
        checks=checks,
        timing=timing,
    )
    return PublishResult(
        report_id=report_id,
        kind=content.kind,
        version=content.version,
        path=target,
        content=content,
        checks=checks,
        timing=timing.as_dict(form) if timing is not None else {},
    )
