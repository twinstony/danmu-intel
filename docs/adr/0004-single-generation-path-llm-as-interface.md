# ADR-0004：收敛生成路径为唯一权威 —— 程序固化 + LLM 只走接口

- **状态**：已采纳
- **日期**：2026-09-21
- **决策者**：用户

## 背景

蓝本仓库并存**三条互相打架的情报生成路径**：

| 路径 | 实现 | 状态 |
|---|---|---|
| A｜Codex 会话 | `tools/vps_intel_pipeline.py::run_codex_report()` → `codex exec` 读 `/root/.codex/skills/intel-report/SKILL.md` | 旧主力，仍保留 |
| B｜程序直连 DeepSeek | `tools/generate_intel_report.py` + `llm_client.py` + `prompts/` | 2026-08-30 新建，**已建未接管** |
| C｜规则直出零 token | `tools/render_fast_intel.py` | 2026-08-30「极简极省」，**当前默认** |

三路输出质量与结构不一致。PRD §2.2 点名此为「四类失效模式」之四：**格式混乱（多套模板并存，缺乏唯一权威）**。

同时，用户关心的「抓取弹幕后台要手动调 LLM」的根因在于 `tools/vps_intel_pipeline.py:47`：

```python
USE_LLM = False   # 2026-08-30 极简极省（用户定稿）
```

即：**这不是「没实现」，而是被主动关掉以省 token**。需要完整版时必须人工改代码为 `True`。

## 决策

**收敛为唯一权威路径：程序固化 + LLM 只走接口。**

1. **删除多路径并存**：不保留 Codex 会话路径，不做兼容层、不做 fallback（遵循 AGENTS.md「不保留向后兼容」）。
2. **LLM 调用接口化**：`src/danmu_intel/refine/llm_client.py` 直连 DeepSeek（OpenAI 兼容协议），提示词固定为 `prompts/` 下 5 份文件（`report_full` / `report_game` / `report_pre` / `report_live` / `intel_asset`）。
3. **LLM 只当打字员**：结构、标准、校验、模板全在程序里；LLM 仅接收「规则层统计底料 + ≤60 条代表弹幕样本」做判断性提炼，输出必须过校验门禁。
4. **全自动**：生成环节无人干预（与蓝本 `USE_LLM=False` 的根本差异）。

## 后果

- **正向**：输出结构唯一，可回归测试；换 LLM 供应商不影响质量；满足 PRD §16.2「全链路自动化」与 §16.3「输出结构唯一」验收。
- **负向**：失去「零 token 规则直出」这一省钱逃生舱 → 由成本控制措施补偿（样本量上限、重试 ≤3 次、usage 记录、阈值告警）。
- **约束**：成本必须可观测 —— 每次调用记录 `input_tokens` / `output_tokens` / `cost_cny` 到 `runtime/llm_usage.jsonl`，目标单页 ≈0.05 元。
- **约束**：校验门禁是质量底线，不可绕过（结构门禁 / 事实层四道闸 / 来源门禁 / 终局四信号）。
