"""解读层的 LLM 实现（T6；设计 §10.3/§10.4、ADR-0003、ADR-0014）。

模块分工：

| 模块 | 管什么 |
|---|---|
| `prompts.py` | 提示词版本与受约束输入（`prompts/interpretation/<版本>/`） |
| `client.py` | DeepSeek 客户端（25 秒超时、错误分类、可注入的 HTTP 传输层） |
| `verify.py` | 反幻觉后置校验（允许集 = 模型看到的那份输入本身） |
| `cost.py` | 价格表与成本硬闸判定（纯函数） |
| `ledger.py` | `llm_calls` 账本：写一行、累计、从账本推导全局降级 |
| `alerts.py` | 降级/成本闸的报警出口（`notifications`，投递属 T11） |
| `interpreter.py` | 组装以上各件，实现 T5 的 `Interpreter` 协议 |

组装与渲染层不认识这里的任何东西：它们只认 `Interpreter` 协议（`state` + `interpret`）。
"""
