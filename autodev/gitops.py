"""Git 与 git worktree 子进程封装：仓库初始化、工作树隔离、越界检测回滚、提交与合并。"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

GITIGNORE = """\
# AutoDev 状态目录（数据库、工作树、日志）
.autodev/
# 常见构建与缓存产物
__pycache__/
*.pyc
.pytest_cache/
.venv/
node_modules/
target/
dist/
build/
.DS_Store
"""


class GitError(RuntimeError):
    pass


def git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} 失败 (cwd={cwd}):\n{proc.stderr.strip() or proc.stdout.strip()}")
    return proc


def ensure_repo(root: Path, base_branch: str) -> None:
    """初始化目标仓库：git init、提交人配置、.gitignore 与初始提交。"""
    if not (root / ".git").exists():
        git(["init", "-b", base_branch], root)
    if not git(["config", "user.email"], root, check=False).stdout.strip():
        git(["config", "user.email", "autodev@localhost"], root)
    if not git(["config", "user.name"], root, check=False).stdout.strip():
        git(["config", "user.name", "AutoDev"], root)
    gi = root / ".gitignore"
    if not gi.exists():
        gi.write_text(GITIGNORE, "utf-8")
    elif ".autodev/" not in gi.read_text("utf-8"):
        gi.write_text(gi.read_text("utf-8").rstrip("\n") + "\n.autodev/\n", "utf-8")
    has_head = git(["rev-parse", "--verify", "HEAD"], root, check=False).returncode == 0
    if not has_head:
        git(["add", "-A"], root)
        git(["commit", "--allow-empty", "-m", "autodev: init workspace"], root)
        git(["branch", "-M", base_branch], root)


def current_branch(cwd: Path) -> str:
    return git(["symbolic-ref", "--short", "-q", "HEAD"], cwd, check=False).stdout.strip()


def head_sha(cwd: Path, ref: str = "HEAD") -> str:
    return git(["rev-parse", ref], cwd).stdout.strip()


def branch_exists(root: Path, branch: str) -> bool:
    return git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], root, check=False).returncode == 0


def is_clean(cwd: Path) -> bool:
    return not git(["status", "--porcelain"], cwd).stdout.strip()


def commit_paths(
    root: Path, paths: list[str], message: str, author_name: str | None = None, author_email: str | None = None
) -> str | None:
    git(["add", "--", *paths], root)
    if not git(["diff", "--cached", "--name-only"], root).stdout.strip():
        return None
    author_args = []
    if author_name and author_email:
        author_args = ["-c", f"user.name={author_name}", "-c", f"user.email={author_email}"]
    git([*author_args, "commit", "-m", message], root)
    return head_sha(root)


# ---------- worktree ----------

def create_worktree(root: Path, path: Path, branch: str, base_ref: str) -> None:
    if path.exists() and (path / ".git").exists():
        return
    if path.exists():
        shutil.rmtree(path)
    git(["worktree", "prune"], root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if branch_exists(root, branch):
        git(["worktree", "add", str(path), branch], root)
    else:
        git(["worktree", "add", "-b", branch, str(path), base_ref], root)


def remove_worktree(root: Path, path: Path) -> None:
    if path.exists():
        git(["worktree", "remove", "--force", str(path)], root, check=False)
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    git(["worktree", "prune"], root, check=False)


def delete_branch(root: Path, branch: str) -> None:
    if branch_exists(root, branch):
        git(["branch", "-D", branch], root)


# ---------- 改动检测与回滚 ----------

def changed_files(cwd: Path) -> list[str]:
    """工作区相对 HEAD 的全部新增 / 修改 / 删除 / 未跟踪文件。"""
    out = git(["status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd).stdout
    entries = out.split("\0")
    files: list[str] = []
    i = 0
    while i < len(entries):
        entry = entries[i]
        if not entry:
            i += 1
            continue
        xy, path = entry[:2], entry[3:]
        files.append(path)
        if xy[0] in "RC" and i + 1 < len(entries):
            files.append(entries[i + 1])
            i += 2
        else:
            i += 1
    return sorted(set(files))


def _tracked_in_head(cwd: Path, path: str) -> bool:
    return git(["cat-file", "-e", f"HEAD:{path}"], cwd, check=False).returncode == 0


def revert_paths(cwd: Path, paths: list[str]) -> None:
    """把指定文件恢复到 HEAD 状态：已跟踪的还原内容，新增的直接删除。"""
    for p in paths:
        if _tracked_in_head(cwd, p):
            git(["checkout", "HEAD", "--", p], cwd)
            continue
        git(["rm", "--cached", "-q", "--ignore-unmatch", "--", p], cwd, check=False)
        target = cwd / p
        if target.is_file() or target.is_symlink():
            target.unlink()
        parent = target.parent
        while parent != cwd and parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent


def commit_all(
    cwd: Path, message: str, author_name: str | None = None, author_email: str | None = None
) -> str | None:
    git(["add", "-A"], cwd)
    if not git(["diff", "--cached", "--name-only"], cwd).stdout.strip():
        return None
    author_args = []
    if author_name and author_email:
        author_args = ["-c", f"user.name={author_name}", "-c", f"user.email={author_email}"]
    git([*author_args, "commit", "-q", "-m", message], cwd)
    return head_sha(cwd)


def commits_since(cwd: Path, base: str) -> int:
    return int(git(["rev-list", "--count", f"{base}..HEAD"], cwd).stdout.strip() or 0)


def diff_since(cwd: Path, base: str, max_chars: int = 80_000) -> str:
    text = git(["diff", f"{base}..HEAD"], cwd).stdout
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n... [diff 过长，已截断，原长度 {len(text)} 字符]"
    return text


def commit_diff(cwd: Path, base: str, target: str, max_chars: int = 80_000) -> str:
    proc = git(["diff", f"{base}..{target}"], cwd, check=False)
    text = proc.stdout if proc.returncode == 0 else ""
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n... [diff 过长，已截断，原长度 {len(text)} 字符]"
    return text


def working_diff(cwd: Path, base: str, max_chars: int = 80_000) -> str:
    """工作区相对基线的实时完整 diff：包含已提交、已暂存、未暂存以及新建未跟踪文本文件。"""
    parts: list[str] = []
    tracked = git(["diff", base], cwd, check=False).stdout
    if tracked.strip():
        parts.append(tracked.rstrip("\n"))
    untracked_out = git(["ls-files", "--others", "--exclude-standard"], cwd, check=False).stdout
    for rel in sorted(line.strip() for line in untracked_out.splitlines() if line.strip()):
        f = cwd / rel
        if not f.is_file() or f.stat().st_size > 200_000:
            continue
        try:
            content = f.read_text("utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        lines = content.splitlines()
        body = "\n".join(f"+{ln}" for ln in lines)
        parts.append(f"diff --git a/{rel} b/{rel}\nnew file\n--- /dev/null\n+++ b/{rel}\n@@ -0,0 +1,{len(lines)} @@\n{body}")
    text = "\n\n".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n... [diff 过长，已截断，原长度 {len(text)} 字符]"
    return text


def diff_stat(cwd: Path, base: str) -> str:
    return git(["diff", "--stat", f"{base}..HEAD"], cwd).stdout.strip()


def working_diff_stat(cwd: Path, base: str) -> str:
    return git(["diff", "--stat", base], cwd, check=False).stdout.strip()


# ---------- 合并 ----------

def merge(cwd: Path, ref: str, message: str) -> tuple[bool, str]:
    proc = git(["merge", "--no-ff", "--no-edit", "-m", message, ref], cwd, check=False)
    if proc.returncode == 0:
        return True, proc.stdout
    conflicts = git(["diff", "--name-only", "--diff-filter=U"], cwd, check=False).stdout.strip()
    git(["merge", "--abort"], cwd, check=False)
    return False, f"合并 {ref} 冲突：{conflicts or proc.stderr.strip() or proc.stdout.strip()}"


def fast_forward(root: Path, ref: str, message: str) -> None:
    if git(["merge", "--ff-only", ref], root, check=False).returncode == 0:
        return
    ok, out = merge(root, ref, message)
    if not ok:
        raise GitError(out)


def has_remote(cwd: Path, remote: str = "origin") -> bool:
    return git(["remote", "get-url", remote], cwd, check=False).returncode == 0


def push_branch(cwd: Path, branch: str, remote: str = "origin") -> tuple[bool, str]:
    proc = git(["push", remote, branch], cwd, check=False)
    if proc.returncode == 0:
        return True, proc.stdout.strip()
    return False, (proc.stderr or proc.stdout).strip()


def reset_hard(cwd: Path, ref: str) -> None:
    git(["reset", "--hard", ref], cwd)


def is_ancestor(cwd: Path, sha: str, ref: str = "HEAD") -> bool:
    return git(["merge-base", "--is-ancestor", sha, ref], cwd, check=False).returncode == 0


def list_repo_files(cwd: Path, limit: int = 300) -> list[str]:
    out = git(["ls-files"], cwd, check=False).stdout.splitlines()
    files = [f.strip() for f in out if f.strip()]
    if len(files) > limit:
        return files[:limit] + [f"... (共 {len(files)} 个已跟踪文件)"]
    return files


def tree_fingerprint(cwd: Path, paths: list[str], ref: str = "HEAD") -> str:
    """受保护目录（契约）在某一提交上的内容指纹。"""
    out = git(["ls-tree", "-r", ref, "--", *paths], cwd, check=False).stdout if paths else ""
    return hashlib.sha256(out.encode()).hexdigest()[:16]
