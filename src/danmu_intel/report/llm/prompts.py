"""受约束提示词（版本化于 `prompts/`，设计 §10.3 / ADR-0003 / ADR-0014）。

提示词分三件，按版本放在 `prompts/interpretation/<版本>/`：

- `system.md`：纪律（不引新事实、不指控、不点名、必须标为分析、输出 JSON 契约）；
- `user.md`：一次调用的骨架，`string.Template` 的 `$name` 占位（提示词里会出现 JSON
  花括号，不能用 `str.format`）；
- `segments.md`：**每个解读段**一段写法，小节标题格式 `## <段号> <段标题>`。缺一个段、
  标题与段定义对不上，都直接报错——段集变了必须改提示词，不允许静默沿用旧提示词写新段。

版本号是代码里的常量 `PROMPT_VERSION`，每次调用把版本记进 `llm_calls`，
因此"这句话是哪版提示词生成的"可回溯（报告 → 账本 → 提示词文件）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Mapping

from danmu_intel.common import paths
from danmu_intel.report.segments import INTERPRETATION_SEGMENTS, SPECS_BY_NO, SegmentSpec

PROMPT_VERSION = "v1"
PROMPTS_DIRNAME = "prompts"
SECTION_RE = re.compile(r"^##\s+(\d+)\s+(.+?)\s*$")


class PromptError(ValueError):
    """提示词缺失或与段定义对不上——必须修提示词，不许绕过。"""


@dataclass(frozen=True, slots=True)
class SegmentGuidance:
    """一个解读段的写法（标题必须与段定义逐字一致）。"""

    no: int
    title: str
    body: str


@dataclass(frozen=True, slots=True)
class PromptSet:
    """一个版本的完整提示词。"""

    version: str
    system: str
    user_template: Template
    guidance: Mapping[int, SegmentGuidance]

    def guidance_for(self, spec: SegmentSpec) -> SegmentGuidance:
        try:
            return self.guidance[spec.no]
        except KeyError:
            raise PromptError(
                f"提示词 {self.version} 缺少第 {spec.no} 段（{spec.title}）的写法小节"
            ) from None


def prompts_dir(version: str = PROMPT_VERSION) -> Path:
    return paths.repo_root() / PROMPTS_DIRNAME / "interpretation" / version


def parse_segment_guidance(text: str) -> dict[int, SegmentGuidance]:
    """把 `segments.md` 解析成 `段号 → 写法`。"""
    guidance: dict[int, SegmentGuidance] = {}
    current: tuple[int, str] | None = None
    buffer: list[str] = []
    for line in text.splitlines():
        match = SECTION_RE.match(line)
        if match:
            if current is not None:
                guidance[current[0]] = SegmentGuidance(current[0], current[1], "\n".join(buffer).strip())
            current = (int(match.group(1)), match.group(2))
            buffer = []
            continue
        if current is not None:
            buffer.append(line)
    if current is not None:
        guidance[current[0]] = SegmentGuidance(current[0], current[1], "\n".join(buffer).strip())
    empty = sorted(no for no, item in guidance.items() if not item.body)
    if empty:
        raise PromptError(f"提示词里这些段的小节是空的：{','.join(map(str, empty))}")
    return guidance


def load_prompt_set(version: str = PROMPT_VERSION, *, directory: Path | None = None) -> PromptSet:
    """读一个版本的提示词，并校验它与段定义对得上。"""
    root = directory or prompts_dir(version)
    try:
        system = (root / "system.md").read_text(encoding="utf-8").strip()
        user_template = Template((root / "user.md").read_text(encoding="utf-8").strip())
        guidance = parse_segment_guidance((root / "segments.md").read_text(encoding="utf-8"))
    except OSError as exc:
        raise PromptError(f"提示词版本 {version} 读不到（{root}）：{exc}") from None
    if not system:
        raise PromptError(f"提示词版本 {version} 的 system.md 是空的")

    missing = [no for no in INTERPRETATION_SEGMENTS if no not in guidance]
    if missing:
        raise PromptError(
            f"提示词版本 {version} 缺解读段的写法：{','.join(map(str, missing))}"
            f"（解读段：{','.join(map(str, INTERPRETATION_SEGMENTS))}）"
        )
    extra = sorted(set(guidance) - set(INTERPRETATION_SEGMENTS))
    if extra:
        raise PromptError(f"提示词版本 {version} 多了不属于解读段的小节：{','.join(map(str, extra))}")
    for no in INTERPRETATION_SEGMENTS:
        expected = SPECS_BY_NO[no].title
        if guidance[no].title != expected:
            raise PromptError(
                f"提示词版本 {version} 第 {no} 段的标题是「{guidance[no].title}」，"
                f"段定义是「{expected}」"
            )
    return PromptSet(
        version=version, system=system, user_template=user_template, guidance=guidance
    )


def render_user(
    prompt_set: PromptSet,
    spec: SegmentSpec,
    *,
    facts_json: str,
    correction: str = "",
) -> str:
    """一次调用的用户消息：写法 + 段定义 + 事实层 JSON + （重试时的）纠正说明。"""
    return prompt_set.user_template.substitute(
        guidance=prompt_set.guidance_for(spec).body,
        segment_no=spec.no,
        segment_title=spec.title,
        segment_nature=spec.nature,
        facts_json=facts_json,
        correction=correction,
    ).strip()
