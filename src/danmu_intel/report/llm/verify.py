"""反幻觉后置校验（设计 §10.3、需求 §6.9 第 1 条、AC-16）。

提示词里的纪律是"请别编"，这里是**硬拦**：从解读文本里抽出**数字、比分、百分比、名称**，
逐个比对事实层；只要出现事实层里没有的，就算新事实，该段作废（重试一次，再失败降级）。

允许集的定义只有一句话：**模型看到的那份输入本身**（`fact_layer_payload` 的规范 JSON）。
凡是不在这份 JSON 里出现过的数字/名称，模型都无从"从事实层推出"——那就是它自己编的。
两处必要的放宽，都是同一个值的确定性改写，不引入新信息：

1. 事实层 JSON 里时间戳的格式化写法（`2026-09-22 16:00:00` 是 `1790064000123` 的另一种写法）；
2. **结构编号**（`第 3 段`、`G2`）在抽取前屏蔽掉——它们是段号与局号，不是事实断言。

三处刻意收紧：

- 比分（`2:0`）与百分比（`63%`）**按整体**比对，不允许拆成普通数字：
  否则事实层里任意出现过的 `3` 和 `0` 会让编造的 `3:0` 蒙混过关；
- 名称按"这个拉丁词是否出现在输入 JSON 里"判定（大小写不敏感），
  因此 "iG"/"LPL" 放行、"Faker"/"T1" 拦下；角色与技术缩写（MVP/KDA/BP 等）另有一张
  与任何具体人无关的白名单；
- 任何 `user_hash` 出现在解读文本里立即作废（需求 §6.5 第 2 条在解读层的延续）。

**已知局限（不假装能拦）**：中文昵称/中文队名无法在无 NER 的情况下识别；事实层里没有
花名册，因此中文人名本就不该出现——这条靠提示词纪律与人工抽检兜底（ADR-0014）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterable, Sequence

from danmu_intel.report.facts import MatchFacts, fact_layer_payload
from danmu_intel.report.rule_render import format_ts
from danmu_intel.stats.gray import contains_identity

#: 结构编号（不是事实断言）：段号引用与局号标签，抽取前屏蔽。
STRUCTURAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"第\s*\d+\s*段"),
    re.compile(r"\bG\s*\d+\b"),
)

#: 比分：`2:0`。两头都不挨数字/冒号，因此 "16:00:00" 这类时刻不会被当成比分。
SCORE_RE = re.compile(r"(?<![\d:])\d{1,3}\s*[:：]\s*\d{1,3}(?![\d:])")
PERCENT_RE = re.compile(r"\d+(?:\.\d+)?\s*%")
NUMBER_RE = re.compile(r"(?<![\d.])\d+(?:\.\d+)?(?![\d.])")
LATIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]+")

#: 允许出现的角色/技术词（大写比较）：不是新事实，也与任何具体人无关。
ALLOWED_TOKENS: frozenset[str] = frozenset(
    {"ADC", "AI", "APC", "API", "BP", "GG", "HTML", "ID", "JSON", "JUG", "KDA", "LLM", "MID",
     "MVP", "OK", "SHA256", "SUP", "TOP", "TS", "URL"}
)

VIOLATION_NUMBER = "number"
VIOLATION_SCORE = "score"
VIOLATION_PERCENT = "percent"
VIOLATION_NAME = "name"
VIOLATION_IDENTITY = "identity"

LABELS = {
    VIOLATION_NUMBER: "数字",
    VIOLATION_SCORE: "比分",
    VIOLATION_PERCENT: "百分比",
    VIOLATION_NAME: "名称",
    VIOLATION_IDENTITY: "身份标识",
}


@dataclass(frozen=True, slots=True)
class Violation:
    """一处新事实：`kind` 是类别，`value` 是原文里的写法。"""

    kind: str
    value: str

    def describe(self) -> str:
        return f"{LABELS.get(self.kind, self.kind)}「{self.value}」"


@dataclass(frozen=True, slots=True)
class AllowedFacts:
    """事实层里出现过的东西（解读层唯一可信的词汇表）。"""

    numbers: frozenset[str]
    scores: frozenset[str]
    percents: frozenset[str]
    corpus: str  # 事实层规范 JSON（大写）：判断一个拉丁词是否"出现在输入里"

    def allows_token(self, token: str) -> bool:
        upper = token.upper()
        return upper in ALLOWED_TOKENS or upper in self.corpus


def _walk_numbers(value: object, out: set[str]) -> None:
    """递归收集规范 JSON 里的数字写法。"""
    if isinstance(value, bool):
        return
    if isinstance(value, int):
        out.add(str(value))
        return
    if isinstance(value, float):
        out.add(str(value))
        if value.is_integer():
            out.add(str(int(value)))
        return
    if isinstance(value, dict):
        for item in value.values():
            _walk_numbers(item, out)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _walk_numbers(item, out)


def _timestamp_forms(ms: int) -> set[str]:
    """一个毫秒值的确定性改写：日期、时刻、`HH:MM`、`YYYY-MM` 与各自的数字分量。"""
    text = format_ts(ms)
    date, _, clock = text.partition(" ")
    forms = {text, date, clock, clock[:5], date[:7]}
    forms.update(date.split("-"))
    forms.update(clock.split(":"))
    forms.discard("")
    return forms


#: 毫秒时间戳的取值范围（1973 – 2286 年）：事实层里落在这个区间的整数都是时间。
EPOCH_MS_MIN = 100_000_000_000
EPOCH_MS_MAX = 10_000_000_000_000


def _walk_epochs(value: object, out: set[str]) -> None:
    """递归收集事实层里所有像毫秒时间戳的整数，并加上它们的格式化写法。"""
    if isinstance(value, bool):
        return
    if isinstance(value, int):
        if EPOCH_MS_MIN <= value < EPOCH_MS_MAX:
            out |= _timestamp_forms(value)
        return
    if isinstance(value, dict):
        for item in value.values():
            _walk_epochs(item, out)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _walk_epochs(item, out)


def _timestamp_forms_of(facts: MatchFacts) -> set[str]:
    """事实层里所有时间值的格式化写法（峰值时刻、样本时间、切片边界…一个都不漏）。"""
    forms: set[str] = set()
    _walk_epochs(fact_layer_payload(facts), forms)
    return forms


def allowed_facts(facts: MatchFacts) -> AllowedFacts:
    """从事实层算出允许集：**模型看到的那份 JSON** + 时间戳的确定性改写。"""
    payload = fact_layer_payload(facts)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    numbers: set[str] = set()
    _walk_numbers(payload, numbers)
    time_forms = _timestamp_forms_of(facts)
    numbers |= time_forms
    scores = {_normalize_score(match.group(0)) for match in SCORE_RE.finditer(canonical)}
    scores |= {form for form in time_forms if ":" in form}
    percents = {_normalize_percent(match.group(0)) for match in PERCENT_RE.finditer(canonical)}
    return AllowedFacts(
        numbers=frozenset(numbers),
        scores=frozenset(scores),
        percents=frozenset(percents),
        corpus=canonical.upper(),
    )


def _normalize_score(value: str) -> str:
    return re.sub(r"\s+", "", value.replace("：", ":"))


def _normalize_percent(value: str) -> str:
    return re.sub(r"\s+", "", value)


def _normalize_number(value: str) -> str:
    text = value.strip()
    return text[:-2] if text.endswith(".0") else text


def mask_structural(text: str) -> str:
    """屏蔽结构编号（段号引用、局号标签）——它们不是事实断言。"""
    for pattern in STRUCTURAL_PATTERNS:
        text = pattern.sub(" ", text)
    return text


def verify_text(text: str, facts: MatchFacts) -> tuple[Violation, ...]:
    """逐项校验解读文本；返回空元组即通过。"""
    allowed = allowed_facts(facts)
    violations: list[Violation] = []

    leaked = contains_identity(text, {line.event.user_hash for line in facts.all_lines})
    violations.extend(Violation(VIOLATION_IDENTITY, value) for value in leaked)
    for value in leaked:  # 身份标识已单独作废，不重复按"名称"再报一次
        text = text.replace(value, " ")

    masked = mask_structural(text)
    for value in sorted({_normalize_score(m.group(0)) for m in SCORE_RE.finditer(masked)}):
        if value not in allowed.scores:
            violations.append(Violation(VIOLATION_SCORE, value))
    for value in sorted({_normalize_percent(m.group(0)) for m in PERCENT_RE.finditer(masked)}):
        if value not in allowed.percents:
            violations.append(Violation(VIOLATION_PERCENT, value))

    remainder = masked
    for pattern in (SCORE_RE, PERCENT_RE):
        remainder = pattern.sub(" ", remainder)
    for value in sorted({_normalize_number(m.group(0)) for m in NUMBER_RE.finditer(remainder)}):
        if value not in allowed.numbers:
            violations.append(Violation(VIOLATION_NUMBER, value))

    for token in sorted({match.group(0) for match in LATIN_RE.finditer(remainder)}):
        if not allowed.allows_token(token):
            violations.append(Violation(VIOLATION_NAME, token))
    return tuple(violations)


def describe(violations: Sequence[Violation]) -> str:
    """违规项的中文说明（进账本的 reason 与重试提示词）。"""
    return "、".join(item.describe() for item in violations) if violations else ""


def correction_note(violations: Iterable[Violation]) -> str:
    """重试时回灌给模型的纠正说明：指出它引用了事实层里没有的东西。"""
    return (
        "上一次输出被拒：出现了事实层 JSON 里没有的 "
        + "、".join(item.describe() for item in violations)
        + "。只能使用 JSON 中出现过的数字与名称；没有依据的内容请直接说明"
        "“事实层未提供”，不要补数字、不要补名字。\n"
    )
