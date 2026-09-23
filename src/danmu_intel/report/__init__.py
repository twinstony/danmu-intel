"""报告层（设计 §10）：三形态段集 + 事实·解读分层 + 发布钩子。

- `segments.py`：§6.6 的十一段定义（段号/标题/段性质标记）
- `forms.py`：三形态（段集 + 触发方式 + 时限）与时效预算表
- `facts.py`：事实层快照、取材范围收窄、`fact_layer_hash`
- `rule_render.py`：规则直出的事实正文与解读正文（每段的来源引用也在这里）
- `interpreter.py`：解读层注入缝（协议 + 规则直出兜底）
- `assemble.py`：事实层 + 解读层 → 段级 `content_json`
- `html.py`：`content_json` → 静态页
- `publish.py`：发布检查（缺解读段即拒绝）+ 版本账本
"""
