"""词法表（领域词表）。

弹幕是自然语言，规则层对它的唯一抓手是**词法模式 + 独立信号复核**。这里集中放三张表，
供切片层（`slice/signals.py`）与统计层（`stats/final.py`）共用 —— 同一句话在两层里的
含义必须一致，不能各写一份。

词表只标「这句话在说什么」，**不判真假、不判对错**（需求 §6.5 的纪律同样适用于此）。
"""

from __future__ import annotations

from typing import Sequence

#: 开局语义（设计 §8.1：弹幕信号复核的候选边界依据）。
START_LEXICON = (
    "开始了",
    "开赛",
    "开打",
    "开局",
    "第一局",
    "第二局",
    "第三局",
    "进入游戏",
    "选手入场",
    "BP开始",
    "选人开始",
)

#: 收局语义（需求 §6.4 ①「终结类弹幕」；设计 §8.1 收局边界）。
END_LEXICON = ("结束", "GG", "gg", "恭喜", "赢了", "输了", "收官", "拿下", "再见", "终局")

#: 宣告语义（需求 §6.4 ④「官方渠道或主播明确宣布」）。
ANNOUNCE_LEXICON = ("官宣", "宣布", "下播", "本场结束", "比赛结束")


def matches(text: str, lexicon: Sequence[str]) -> bool:
    """`text` 是否命中词表里的任一词。"""
    return any(word in text for word in lexicon)


def hits(lines, lexicon: Sequence[str]) -> list:
    """按时间顺序取出命中词表的原始行（顺序先于分组：可重算的前提）。"""
    return sorted(
        (line for line in lines if matches(line.event.text, lexicon)),
        key=lambda line: (line.event.ts, line.rel_path, line.line_no),
    )
