# 弹幕情报库

把公开弹幕变成可复核的情报报告页。

- 需求：[`docs/requirements/DANMU_INTEL_REQUIREMENTS.md`](docs/requirements/DANMU_INTEL_REQUIREMENTS.md)
- 设计：[`docs/design/ENGINEERING_DESIGN_v2.md`](docs/design/ENGINEERING_DESIGN_v2.md)
- 领域术语：[`CONTEXT.md`](CONTEXT.md)｜架构决策：[`docs/adr/`](docs/adr/)

## 当前能力（T1：采集 → 静态页最薄闭环）

虎牙**单直播间**真实弹幕 → append-only JSONL → 人工指定小局起止 → 基础统计 →
规则直出**十一段**静态页。不含 LLM、付费墙、公网发布、多平台、后台。

## 安装

```bash
pip install -e '.[dev]'
bash deploy/hooks/install.sh     # 装上「提交前凭据检查」钩子
```

## 跑通一遍

```bash
# ① 先登记比赛（采集必须挂在一场比赛上）
danmu-intel match add --league LPL --team-a iG --team-b LNG --state ended \
  --official-result '{"score":"2:0"}'

# ② 采集真实弹幕（seconds 省略则持续采集到 Ctrl-C）
danmu-intel collect --url https://www.huya.com/660000 --seconds 300 --match-id 1

# ③ 人工指定这一局的起止（毫秒时间戳，可从落盘记录里取）
danmu-intel slice --match-id 1 --game-no 1 --start-ms 1790064000123 --end-ms 1790064300123

# ④ 规则统计 → ⑤ 十一段静态页
danmu-intel stats  --match-id 1
danmu-intel render --match-id 1          # → site/matches/1.html

# ⑥ 自检
danmu-intel verify-sources --match-id 1  # 逐项复核来源（文件 + 行范围 + SHA256）
danmu-intel rebuild        --match-id 1  # AC-13：删统计后重算，结果必须逐字节相同
python3 tools/check_no_secrets.py        # AC-12：全库零命中可动用资产凭据
```

## 数据落点

| 内容 | 位置 | 进 git 吗 |
|---|---|---|
| 原始弹幕 JSONL | `~/danmu-intel-data/raw/<platform>/<yyyy-mm-dd>/<room_id>-<hh>.jsonl` | 否 |
| SQLite | `~/danmu-intel-data/db.sqlite3`（WAL） | 否 |
| 用户哈希盐值 | `~/danmu-intel-data/salt`（0600） | 否 |
| 站点产物 | `site/matches/<match_id>.html` | 是 |

`DANMU_INTEL_DATA` / `DANMU_INTEL_SITE` 可覆盖上面两个位置（测试用它指向临时目录）。

`site/matches/1.html` 是 2026-09-22 在本机对虎牙 660000 / 323444 两个直播间做了
5 分钟真实采集后生成的样例产物；它引用的原始记录在采集机的数据目录里，
换一台机器跑 `danmu-intel render` 会用当地数据重新生成。

## 测试

```bash
pytest          # 覆盖率门禁 90%；全程不连外网（平台数据用录制帧回放）
```

平台数据的回放 fixture 由 `tools/record_fixtures.py` 生成：先 `record` 连真实
直播间录原始帧，再 `sanitize` 把身份与原文替换成样例值后写入
`tests/fixtures/huya/frames.jsonl`（录制帧文件本身不进仓库）。

## 边界

- 公开弹幕是唯一数据来源，不使用任何需要突破访问限制的手段。
- 原始记录**不落明文身份**：只存平台用户 ID 的加盐哈希（`user_hash`）。
- 灰信号只作风险提示，不指控、不点名、必须附样本与门槛（需求 §6.5）。
