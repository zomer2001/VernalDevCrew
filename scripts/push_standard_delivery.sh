#!/usr/bin/env bash
set -euo pipefail

publish_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$publish_root"
publish_branch='codex/standard-delivery-p0-p1-20261126'
publish_remote='https://github.com/zomer2001/VernalDevCrew.git'

if [[ "$(git branch --show-current)" != "$publish_branch" ]]; then
  echo '当前分支不匹配，已停止；请在标准交付发布目录运行本脚本。' >&2
  exit 1
fi
if [[ "$(git remote get-url origin)" != "$publish_remote" ]]; then
  echo '远端地址与本次发布目标不一致，已停止。' >&2
  exit 1
fi
if [[ -n "$(git status --porcelain)" ]]; then
  echo '发布目录出现未提交修改，请先检查，脚本不会自动提交。' >&2
  exit 1
fi

git fetch origin
if git show-ref --verify --quiet refs/remotes/origin/main; then
  if ! git merge-base --is-ancestor refs/remotes/origin/main HEAD; then
    echo '远端 main 含本分支尚未合并的提交。本次仍只发布功能分支，合并时需处理这些差异。'
  fi
fi
git push --set-upstream origin "HEAD:refs/heads/$publish_branch"
echo '已推送标准交付功能分支。main 保持现状。'
echo '比较页面：https://github.com/zomer2001/VernalDevCrew/compare/main...codex/standard-delivery-p0-p1-20261126?expand=1'
