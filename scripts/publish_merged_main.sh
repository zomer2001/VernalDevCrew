#!/usr/bin/env bash
set -euo pipefail

# 本次合并的发布入口，在用户终端执行。可重复执行，不强制推送。
publish_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
primary_root="${VERNAL_PRIMARY_CHECKOUT:-$(cd "$publish_root/../.." && pwd)}"
publish_remote='https://github.com/zomer2001/VernalDevCrew.git'
publish_python="${VERNAL_REVIEW_PYTHON:-python3}"
publish_check_only=0
if [[ "${1:-}" == '--check-only' && "$#" == 1 ]]; then
  publish_check_only=1
elif [[ "$#" != 0 ]]; then
  echo '用法：bash scripts/publish_merged_main.sh [--check-only]' >&2
  exit 1
fi

for publish_dir in "$publish_root" "$primary_root"; do
  if [[ "$(git -C "$publish_dir" branch --show-current)" != main ]]; then
    echo "目录不在 main 分支，已停止：$publish_dir" >&2
    exit 1
  fi
  if [[ "$(git -C "$publish_dir" remote get-url origin)" != "$publish_remote" ]]; then
    echo "远端不是本次指定仓库，已停止：$publish_dir" >&2
    exit 1
  fi
done
if [[ -n "$(git -C "$publish_root" status --porcelain)" ]]; then
  echo '合并目录有未提交修改，已停止，不自动提交。' >&2
  exit 1
fi
publish_head="$(git -C "$publish_root" rev-parse HEAD)"
for publish_ref in a824cc6 7666a1d 7fe0ecb 3418914; do
  git -C "$publish_root" merge-base --is-ancestor "$publish_ref" "$publish_head"
done

# 只将已经逐字节纳入历史基线的工作区变化保存到 stash。
# 任何后来新增的独立修改都会中止，不覆盖用户的新工作。
check_primary() {
"$publish_python" - "$publish_root" "$primary_root" <<'PY'
import os
from pathlib import Path
import subprocess
import sys

source, primary = map(Path, sys.argv[1:])
def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args])
source_head = git(source, "rev-parse", "HEAD").strip().decode()
primary_head = git(primary, "rev-parse", "HEAD").strip().decode()
subprocess.run(["git", "-C", str(source), "merge-base", "--is-ancestor", primary_head, source_head], check=True)
rows = git(primary, "status", "--porcelain=v1", "-z", "--untracked-files=all").split(b"\0")
count = 0
for row in rows:
    if not row:
        continue
    if b"R" in row[:2] or b"C" in row[:2]:
        raise SystemExit("主目录出现重命名或复制操作，请先审阅，未改变任何文件。")
    rel = os.fsdecode(row[3:])
    path = primary / rel
    saved = subprocess.run(["git", "-C", str(source), "show", f"a74f735:{rel}"], capture_output=True)
    if saved.returncode or not path.is_file() or path.is_symlink() or path.read_bytes() != saved.stdout:
        raise SystemExit(f"发现未纳入原基线的新修改，已停止：{rel}")
    mode = git(source, "ls-tree", "a74f735", "--", rel).split()[0]
    if bool(path.stat().st_mode & 0o111) != (mode == b"100755"):
        raise SystemExit(f"发现新的可执行权限变化，已停止：{rel}")
    count += 1
print(f"本地检查通过：main 包含所有目标分支，主目录有 {count} 项已归档基线变化。")
PY
}
check_primary

if [[ "$publish_check_only" == 1 ]]; then
  echo '仅完成本地只读检查；尚未读取远端、备份、更新主目录或推送。'
  exit 0
fi

git -C "$publish_root" fetch origin
for publish_ref in origin/main origin/stable/261126 origin/codex/standard-delivery-p0-p1-20261126; do
  if ! git -C "$publish_root" merge-base --is-ancestor "$publish_ref" "$publish_head"; then
    echo "远端分支有尚未审阅的新提交，已停止：$publish_ref" >&2
    exit 1
  fi
done

git -C "$primary_root" fetch "$publish_root" main:refs/remotes/standard-delivery/main
check_primary
if [[ -n "$(git -C "$primary_root" status --porcelain)" ]]; then
  git -C "$primary_root" stash push --include-untracked -m 'backup: source before main integration 20261126'
  echo "已保存主目录备份：$(git -C "$primary_root" rev-parse refs/stash)"
fi
git -C "$primary_root" merge --ff-only --no-stat refs/remotes/standard-delivery/main
if [[ "$(git -C "$primary_root" rev-parse HEAD)" != "$publish_head" || -n "$(git -C "$primary_root" status --porcelain)" ]]; then
  echo '主目录更新后的状态不符合预期，已停止推送；备份保留。' >&2
  exit 1
fi

git -C "$publish_root" push origin "$publish_head:refs/heads/main"
publish_actual="$(git -C "$publish_root" ls-remote origin refs/heads/main | awk '{print $1}')"
if [[ "$publish_actual" != "$publish_head" ]]; then
  echo '推送后的远端版本与本次提交不同，请检查并发更新。' >&2
  exit 1
fi
echo "远端 main 已核验：$publish_head"
git -C "$primary_root" fetch origin main
echo '主目录和远端 main 均已更新。原工作区备份保留在主目录 git stash 中，无需重新应用。'
