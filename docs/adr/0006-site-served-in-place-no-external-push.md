# ADR-0006：站点本机直出（废弃外部站点推送与 Deploy Key）

- **状态**：已采纳
- **日期**：2026-09-22
- **决策者**：用户

## 背景

蓝本发布链路为：VPS 生成页面 → `tools/commit_site_pages.sh` 用 Deploy Key
（私钥 `/root/.ssh/github_deploy`）把页面 commit 进独立的**站点仓库** `site_repo`
→ push 到 GitHub → 由 **GitHub Pages / Vercel** 对外提供站点；对外 API
（`/api/track`、`/api/lead`、`/api/verify-member`）另由 Vercel 项目
`danmu-intel-api.vercel.app` 提供。

该链路引入 **1 个额外仓库 + 1 对长期密钥 + 2 个外部服务**，代价与风险：

1. **密钥面**：一对长期有效的 SSH 私钥常驻服务器，是最高价值攻击目标。
2. **代码游离**：`verify-member` 的实现**不在仓库内**（全仓库 `grep` 只命中调用方
   `tools/add_paywall.py`），付费链路一半代码无法 review / 无法测试。
3. **网络脆弱**：push 依赖 GitHub 可达性，本项目多次实测 TLS 握手失败需重试。
4. **发布延迟**：push + 远端构建，链路长、失败点分散。

用户在 2026-09-22 明确指示：

> 「不要将生成好的页面再推到其它站点，而是当前服务器内部解决页面部署问题，
> 不再依赖其它站点」

> 注：PRD 原文已把发布目标写成「GitHub Pages（danmupulse.com）**或 nginx 直出**」
> ——本 ADR 即选定「直出」这一支并彻底落实。

## 决策

**取消站点仓库、GitHub Pages、Vercel 与 Deploy Key，站点与 API 全部由当前服务器承载。**

### 1. 承载架构

| 层 | 组件 | 职责 |
|---|---|---|
| 入口 | `caddy.service` | 唯一对外入口：TLS（自动 Let's Encrypt）+ 静态直出 + `/api/*` 反代 |
| 应用 | `danmu-api.service` | `ThreadingHTTPServer` **只监听 `127.0.0.1:8080`**，承载 4 组路由 |
| 内容 | `site/` 静态根 | 发布产物，由 Caddy `file_server` 直出 |

### 2. 发布方式：原子 rename

生成到 `site/.staging/` → 审计通过 → `mv site site.prev`（留上一版）→
`mv site/.staging site`（同文件系统内原子换入）→ 自检 → 失败则回滚 `site.prev`。

### 3. 对外 API 前缀不变

Caddy 用 `handle /api/*` + `uri strip_prefix /api` 反代到本机服务。浏览器侧仍是
`/api/track`、`/api/lead`、`/api/verify-member` —— **前端契约零破坏**，且从「跨域 + 硬编码
外部域名」变为**同源相对路径**。蓝本 `tools/add_paywall.py` 的
`fetch("/api/verify-member")` 无需修改即可兼容。

### 4. 选型：Caddy 而非 nginx

| 维度 | Caddy（选定） | nginx + certbot |
|---|---|---|
| HTTPS | 内置 ACME，自动申请 + **自动续期** | 需 certbot + timer + reload 钩子 |
| 配置量 | 6 行 | 2 个 server 块 + 证书路径 + 续期脚本 |
| 反代 / 静态 | `reverse_proxy` / `file_server` | `proxy_pass` / `root` |

理由：把「证书半夜过期」这个最易失效的环节变成零配置，符合 AGENTS.md「最简实现」。
nginx 作为等价替代记录在 §5.7.1（团队更熟 nginx 时可切换）。

### 5. Deploy Key 处置

**彻底删除**。服务器上 `/root/.ssh/github_deploy` 与站点仓库 `site_repo` 一并下线；
长期密钥只剩 `DEEPSEEK_API_KEY` 与 Telegram Bot Token 两个。私钥内容不留档、不入库。

## 后果

**收益**

- 付费链路 4 个路由 100% 进仓库，可 review、可单测。
- 外部部署依赖归零（1 仓库 + 2 服务 + 1 密钥 → 0）。
- 发布延迟从「push + 远端构建」降到**原子 rename（毫秒级）**，远优于 PRD 的 ≤1 分钟。
- 攻击面收敛：API 服务不对公网监听，外部只能经 Caddy 443。
- 付费与访客数据全部留在本机，不出境。

**代价（明确接受）**

- 自扛 TLS、可用性与带宽：无 CDN、无边缘缓存、无平台级 DDoS 兜底；本机宕机 = 站点宕机。
- 证书续期依赖 Caddy ACME，**必须有续期失败告警**。
- 静态页从本机出，受 VPS 带宽约束（用 `encode gzip zstd` + 长缓存头缓解）。
- **DNS 是切换最大坑**：`danmupulse.com` 的 A/AAAA 记录必须从 GitHub Pages / Vercel
  改指向 VPS，否则 Caddy 无法通过 ACME 域名校验。

**边界**：后续若需加速，可在前面加 Cloudflare **仅作加速层**，不引入部署依赖。

## 验证

- [ ] 站点由本机 Caddy 直出，全链路零外部部署依赖（无站点仓库 / GitHub Pages / Vercel / Deploy Key）
- [ ] `grep -rn 'vercel\|site_repo\|github_deploy' src/ deploy/ config/` = 0 命中
- [ ] `ss -tlnp` 显示 API 只监听 `127.0.0.1:8080`，443 由 Caddy 持有
- [ ] 发布为原子 rename；自检失败自动回滚到 `site.prev` 并有告警
- [ ] 发布到可访问 ≤10 秒
- [ ] HTTPS 证书自动续期成功，续期失败有告警
- [ ] `.gitignore` 覆盖 `site/`、`site.prev/`、`*.pem`、`id_*`
- [ ] `danmupulse.com` A/AAAA 已指向 VPS（部署前置检查项）
