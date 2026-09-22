#!/usr/bin/env bash
# 把凭据闸门装进 git 钩子目录（幂等）。用 `git rev-parse --git-path hooks`
# 而不是拼 .git/hooks —— 在 git worktree 里 .git 是一个文件。
set -euo pipefail
repo_root="$(git rev-parse --show-toplevel)"
hooks_dir="$(git rev-parse --git-path hooks)"
mkdir -p "$hooks_dir"
install -m 0755 "$repo_root/deploy/hooks/pre-commit" "$hooks_dir/pre-commit"
echo "已安装 pre-commit 凭据检查：$hooks_dir/pre-commit"
