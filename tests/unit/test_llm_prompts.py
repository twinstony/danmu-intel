"""提示词版本与受约束输入：段覆盖、标题一致、占位符替换、缺段必须报错。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from danmu_intel.pipeline import collect_facts
from danmu_intel.report.facts import fact_layer_payload
from danmu_intel.report.llm.prompts import (
    PROMPTS_DIRNAME,
    PROMPT_VERSION,
    PromptError,
    load_prompt_set,
    parse_segment_guidance,
    prompts_dir,
    render_user,
)
from danmu_intel.report.segments import INTERPRETATION_SEGMENTS, SPECS_BY_NO


def test_v1_prompt_set_covers_every_interpretation_segment():
    prompt_set = load_prompt_set()
    assert prompt_set.version == PROMPT_VERSION == "v1"
    assert sorted(prompt_set.guidance) == sorted(INTERPRETATION_SEGMENTS)
    for no in INTERPRETATION_SEGMENTS:
        assert prompt_set.guidance[no].title == SPECS_BY_NO[no].title
        assert prompt_set.guidance_for(SPECS_BY_NO[no]).body
    assert "{" in prompt_set.system and "}" in prompt_set.system  # 输出契约写在 system 里


def test_prompt_files_live_under_the_repo_prompts_dir():
    directory = prompts_dir()
    assert directory.is_dir()
    assert directory.parent.parent.name == PROMPTS_DIRNAME
    for name in ("system.md", "user.md", "segments.md"):
        assert (directory / name).is_file()


def test_system_prompt_states_the_hard_rules():
    system = load_prompt_set().system
    for phrase in ("不得引入新事实", "指控", "不点名", "JSON"):
        assert phrase in system


def test_guidance_mentions_each_segment_specific_discipline():
    guidance = load_prompt_set().guidance
    assert "归因" in guidance[2].body  # 逐局复盘：不做胜负归因
    assert "不得" in guidance[3].body  # 队伍画像：不下结论
    assert "哈希" in guidance[4].body  # 人员画像：不落明文身份
    assert "规律" in guidance[6].body  # 联赛规律
    assert "追认" in guidance[7].body  # 预测验证
    assert "操纵" in guidance[8].body  # 盘口讨论：不暗示操纵
    assert "缺口" in guidance[9].body  # 观察点


def test_missing_segment_guidance_is_refused(tmp_path):
    source = prompts_dir()
    for name in ("system.md", "user.md", "segments.md"):
        (tmp_path / name).write_text((source / name).read_text(encoding="utf-8"), encoding="utf-8")
    text = (tmp_path / "segments.md").read_text(encoding="utf-8")
    # 抹掉 6 号段的小节标题（其正文并入上一节）
    (tmp_path / "segments.md").write_text(
        text.replace("## 6 联赛规律与版本\n", ""), encoding="utf-8"
    )
    with pytest.raises(PromptError, match="缺解读段的写法：6"):
        load_prompt_set("v1", directory=tmp_path)


def test_guidance_for_a_non_interpretation_segment_is_refused(tmp_path):
    prompt_set = load_prompt_set()
    with pytest.raises(PromptError, match="缺少第 0 段"):
        prompt_set.guidance_for(SPECS_BY_NO[0])


def test_title_mismatch_is_refused(tmp_path):
    source = prompts_dir()
    for name in ("system.md", "user.md", "segments.md"):
        (tmp_path / name).write_text((source / name).read_text(encoding="utf-8"), encoding="utf-8")
    text = (tmp_path / "segments.md").read_text(encoding="utf-8")
    (tmp_path / "segments.md").write_text(
        text.replace("## 3 队伍画像", "## 3 队伍介绍"), encoding="utf-8"
    )
    with pytest.raises(PromptError, match="标题是「队伍介绍」"):
        load_prompt_set("v1", directory=tmp_path)


def test_extra_section_is_refused(tmp_path):
    source = prompts_dir()
    for name in ("system.md", "user.md", "segments.md"):
        (tmp_path / name).write_text((source / name).read_text(encoding="utf-8"), encoding="utf-8")
    text = (tmp_path / "segments.md").read_text(encoding="utf-8")
    (tmp_path / "segments.md").write_text(text + "\n## 1 结果总览\n这不是解读段。\n", encoding="utf-8")
    with pytest.raises(PromptError, match="多了不属于解读段的小节：1"):
        load_prompt_set("v1", directory=tmp_path)


def test_empty_section_is_refused(tmp_path):
    source = prompts_dir()
    for name in ("system.md", "user.md", "segments.md"):
        (tmp_path / name).write_text((source / name).read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "segments.md").write_text("## 3 队伍画像\n", encoding="utf-8")
    with pytest.raises(PromptError, match="小节是空的：3"):
        load_prompt_set("v1", directory=tmp_path)


def test_missing_version_is_refused(tmp_path):
    with pytest.raises(PromptError, match="读不到"):
        load_prompt_set("v9", directory=tmp_path)


def test_empty_system_prompt_is_refused(tmp_path):
    source = prompts_dir()
    for name in ("user.md", "segments.md"):
        (tmp_path / name).write_text((source / name).read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "system.md").write_text("   \n", encoding="utf-8")
    with pytest.raises(PromptError, match="system.md 是空的"):
        load_prompt_set("v1", directory=tmp_path)


def test_parse_segment_guidance_ignores_preamble():
    parsed = parse_segment_guidance(
        "# 标题\n前言不算任何段。\n\n## 3 队伍画像\n不下结论。\n\n## 9 情报含义与后续观察点\n讲缺口。\n"
    )
    assert sorted(parsed) == [3, 9]
    assert parsed[3].title == "队伍画像" and parsed[3].body == "不下结论。"


def test_render_user_fills_every_placeholder(ledger):
    """用户消息 = 写法 + 段定义 + 事实层 JSON；事实层 JSON 与哈希同源。"""
    facts = collect_facts(ledger.conn, ledger.match_id, data_root=ledger.data_root)
    facts_json = json.dumps(fact_layer_payload(facts), ensure_ascii=False, sort_keys=True)
    prompt_set = load_prompt_set()
    spec = SPECS_BY_NO[3]

    message = render_user(prompt_set, spec, facts_json=facts_json)
    assert "$" not in message
    assert "3 号段「队伍画像」" in message
    assert prompt_set.guidance[3].body in message
    assert facts_json in message
    assert '"segments": {"3"' in message.replace("\n", " ")

    retried = render_user(prompt_set, spec, facts_json=facts_json, correction="上一次输出被拒：……")
    assert "上一次输出被拒" in retried


def test_prompt_files_are_not_the_place_for_secrets():
    """提示词是纯纪律与结构说明：不该出现任何看起来像凭据的赋值。"""
    for path in sorted(Path(prompts_dir()).glob("*.md")):
        text = path.read_text(encoding="utf-8")
        for marker in ("API_KEY", "api_key", "token=", "sk-"):
            assert marker not in text, f"{path.name} 不该出现 {marker}"
