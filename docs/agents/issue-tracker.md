# Issue tracker: GitHub

本仓库的 issue 与 spec 存于 **GitHub issues**，所有操作都用 `gh` CLI 完成。

## 约定

- **创建 issue**：`gh issue create --title "..." --body "..."`。多行正文用 heredoc。
- **读取 issue**：`gh issue view <number> --comments`，用 `jq` 过滤评论，同时读取 labels。
- **列出 issue**：`gh issue list --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'`，配合 `--label` 与 `--state` 过滤。
- **评论 issue**：`gh issue comment <number> --body "..."`
- **打 / 去标签**：`gh issue edit <number> --add-label "..."` / `--remove-label "..."`
- **关闭**：`gh issue close <number> --comment "..."`

仓库从 `git remote -v` 推断 — 在 clone 内运行 `gh` 会自动完成。

## Pull request 是否作为 triage 入口

**PRs as a request surface: no.**（若本仓库把外部 PR 也当作 feature request，可改为 `yes`；`/triage` 会读取该标记。）

设为 `yes` 时，PR 使用与 issue 相同的标签与状态，改用 `gh pr` 等价命令：
- **读 PR**：`gh pr view <number> --comments`，diff 用 `gh pr diff <number>`。
- **列出待 triage 的外部 PR**：`gh pr list --state open --json number,title,body,labels,author,authorAssociation,comments`，只保留 `authorAssociation` 为 `CONTRIBUTOR` / `FIRST_TIME_CONTRIBUTOR` / `NONE` 的（丢弃 `OWNER` / `MEMBER` / `COLLABORATOR`）。
- **评论 / 标签 / 关闭**：`gh pr comment`、`gh pr edit --add-label`/`--remove-label`、`gh pr close`。

GitHub 的 issue 与 PR 共用一套编号，裸写 `#42` 可能是其中任意一种 — 先用 `gh pr view 42` 解析，失败再回退 `gh issue view 42`。

## 当 skill 说「publish to the issue tracker」

创建一个 GitHub issue。

## 当 skill 说「fetch the relevant ticket」

运行 `gh issue view <number> --comments`。

## Wayfinding 操作

供 `/wayfinder` 使用。**map** 是单个 issue，其 **child** issue 作为 ticket。

- **Map**：一个带 `wayfinder:map` 标签的 issue，承载 Notes / Decisions-so-far / Fog 正文。`gh issue create --label wayfinder:map`。
- **子 ticket**：链接到 map 的 issue（用 GitHub sub-issue：`gh api` 调 sub-issues 端点）。未开启 sub-issue 时，把子 ticket 写进 map 正文的任务清单，并在其正文顶部加 `Part of #<map>`。标签：`wayfinder:<type>`（`research` / `prototype` / `grilling` / `task`）。被认领后，ticket 指派给执行 dev。
- **Blocking**：用 GitHub **原生 issue 依赖**（规范、UI 可见）。用 `gh api --method POST repos/<owner>/<repo>/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>` 加边，其中 `<blocker-db-id>` 是阻塞者的数字 **database id**（`gh api repos/<owner>/<repo>/issues/<n> --jq .id`，**不是** `#number` 或 `node_id`）。GitHub 在 `issue_dependencies_summary.blocked_by` 里报告（仅未关闭的 blocker — 即实时的门禁）。依赖不可用时就回退为在子 ticket 正文顶部写 `Blocked by: #<n>, #<n>`。当所有 blocker 关闭时，该 ticket 解除阻塞。
- **Frontier 查询**：列出 map 的未关闭子 ticket（`gh issue list --state open`，限定到 map 的 sub-issue / 任务清单），去掉仍有未关闭 blocker（`issue_dependencies_summary.blocked_by > 0`，或 `Blocked by` 行里有未关闭 issue）或已有 assignee 的；按 map 顺序取第一个。
- **Claim**：`gh issue edit <n> --add-assignee @me` — 本会话的第一次写入。
- **Resolve**：`gh issue comment <n> --body "<answer>"`，然后 `gh issue close <n>`，再把一个 context 指针（gist + 链接）追加到 map 的 Decisions-so-far。
