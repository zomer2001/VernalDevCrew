"""本机 Git 项目的独立工作副本、单次任务传输和专用交付分支。"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
import subprocess
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import quote, urlencode, urlsplit

from autodev import gitops
from autodev.dag import path_allowed


def clone_source(source: Path, workspace: Path, branch: str) -> None:
    """只导入选定的已提交版本。工作副本没有指向用户仓库的默认推送目标。"""
    if gitops.git(["check-ref-format", "--branch", branch], source, check=False).returncode:
        raise ValueError("所选 Git 分支名称无效")
    gitops.git(["clone", "--no-local", "--no-tags", "--single-branch", "--branch", branch,
                "--", str(source), str(workspace)], workspace.parent)
    validate_checkout(workspace)
    gitops.git(["remote", "remove", "origin"], workspace)


def validate_checkout(workspace: Path) -> None:
    if (workspace / ".autodev").exists() or (workspace / ".autodev").is_symlink():
        raise ValueError("源分支已跟踪 .autodev 平台状态目录，请先从分支中移除")
    for name in (".gitignore", ".opencode", "inputs", "docs", "AGENTS.md"):
        path = workspace / name
        paths = [path, *path.rglob("*")] if path.is_dir() and not path.is_symlink() else [path]
        if any(p.is_symlink() for p in paths):
            raise ValueError(f"平台需要写入 {name}，该路径不能含有符号链接")


class LeaseConflict(gitops.GitError):
    pass


def compare_url(url, base, branch):
    parts = urlsplit(url or "")
    if parts.scheme not in ("https", "http") or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        return None
    separator = "/-/compare/" if "gitlab" in parts.hostname else "/compare/"
    return url.rstrip("/").removesuffix(".git") + separator + quote(base, safe="") + "..." + quote(branch, safe="")


def publish(source: Path, workspace: Path, project: str, milestone: str, commit: str, *, branch: str = "", expected: str = "") -> str:
    branch = branch or f"vernal/{project}/{milestone}"
    ref = f"refs/heads/{branch}"
    gitops.git(["check-ref-format", ref], workspace)
    current = gitops.git(["show-ref", "--verify", "--hash", ref], source, check=False)
    if current.returncode == 0 and current.stdout.strip() == commit:
        return branch
    def current_sha():
        result = gitops.git(["show-ref", "--verify", "--hash", ref], source, check=False)
        return result.stdout.strip() if result.returncode == 0 else ""
    if current_sha() != expected:
        raise LeaseConflict(f"交付分支 {branch} 的当前提交与租约不符")
    try:
        gitops.git(["push", "--no-verify", f"--force-with-lease={ref}:{expected}", "--", str(source),
                    f"{commit}:{ref}"], workspace)
    except gitops.GitError:
        if current_sha() not in (expected, commit):
            raise LeaseConflict(f"交付期间分支 {branch} 被修改") from None
        if current_sha() != commit:
            raise
    return branch


def prepare_snapshot(worktree: Path, base: str, directory: Path | None = None) -> str:
    """发送无父节点的提交，只包含此任务的文件树，不传仓库历史。"""
    tree = gitops.head_sha(worktree, f"{base}^{{tree}}")
    env = {**os.environ, "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
           "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z"}
    result = subprocess.run(["git", "-c", "user.name=Vernal", "-c", "user.email=vernal@localhost",
                             "commit-tree", tree, "-m", "Vernal task baseline"],
                            cwd=worktree, env=env, capture_output=True, text=True, check=True)
    snapshot = result.stdout.strip()
    if directory is None:
        return snapshot
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{snapshot}.pack"
    if not target.exists():
        with NamedTemporaryFile(dir=directory, delete=False) as tmp:
            try:
                subprocess.run(["git", "pack-objects", "--stdout", "--revs"], cwd=worktree,
                               input=(snapshot + "\n").encode(), stdout=tmp, check=True)
                tmp.flush()
                Path(tmp.name).replace(target)
            finally:
                Path(tmp.name).unlink(missing_ok=True)
    return snapshot


def restore_command(url: str, project: str, snapshot: str, signature: str) -> str:
    query = urlencode({"project": project, "sig": signature})
    endpoint = f"{url.rstrip('/')}/vernal-api/git-snapshots/{snapshot}.pack?{query}"
    # 重试同一节点时复用这次任务的工作区。
    return (
        'if previous=$(git -C vernal-source rev-parse --verify refs/vernal/base 2>/dev/null); then '
        f'test "$previous" = {snapshot}; else '
        f"git init -q vernal-source && curl -fsS {shlex.quote(endpoint)} -o vernal-base.pack && "
        "git -C vernal-source index-pack --stdin < vernal-base.pack && "
        f"git -C vernal-source checkout -q --detach {snapshot} && "
        f"git -C vernal-source update-ref refs/vernal/base {snapshot}; fi"
    )


# 使用 ASCII Git 二进制补丁保留二进制、删除、执行位。摘要同时检出被截断的产物。
PACKAGE_PATCH = """set -eu
git -C vernal-source add -A
git -C vernal-source diff --cached --binary --full-index --no-color --no-ext-diff --no-textconv refs/vernal/base -- > vernal-changes.patch
test -s vernal-changes.patch
if command -v python3 >/dev/null 2>&1; then
  python3 - <<'PYTHON'
import base64
import hashlib
import json
from pathlib import Path

patch = Path("vernal-changes.patch").read_bytes()
artifact = {"sha256": hashlib.sha256(patch).hexdigest(), "patch": base64.b64encode(patch).decode()}
Path("vernal-patch.json").write_text(json.dumps(artifact))
PYTHON
else
  node <<'NODE'
const fs = require("fs");
const crypto = require("crypto");
const patch = fs.readFileSync("vernal-changes.patch");
const sha256 = crypto.createHash("sha256").update(patch).digest("hex");
fs.writeFileSync("vernal-patch.json", JSON.stringify({sha256, patch: patch.toString("base64")}));
NODE
fi
if git -C vernal-source remote get-url vernal-task >/dev/null 2>&1; then
  tree=$(git -C vernal-source write-tree)
  result=$(git -C vernal-source -c user.name=Vernal -c user.email=vernal@localhost commit-tree "$tree" -p refs/vernal/base -m "Vernal task changes")
  git -C vernal-source push vernal-task "$result:refs/heads/results/$result"
fi"""


def apply_patch(worktree: Path, base: str, artifact: str, allowed: list[str], protected: list[str]) -> str:
    data = json.loads(artifact)
    if not isinstance(data, dict) or not isinstance(data.get("patch"), str):
        raise ValueError("Git 交付缺少补丁")
    patch = base64.b64decode(data["patch"], validate=True)
    if not patch or hashlib.sha256(patch).hexdigest() != data.get("sha256"):
        raise ValueError("Git 交付补丁为空或不完整")
    # 仅操作平台专用任务工作树。每次从基线重放，也支持在验证前意外中止后的重试。
    gitops.reset_hard(worktree, base)
    gitops.git(["clean", "-fdx"], worktree)
    apply = subprocess.run(["git", "apply", "--index", "--binary", "-"], cwd=worktree,
                           input=patch, capture_output=True)
    if apply.returncode:
        raise ValueError(f"Git 补丁与任务基线不匹配：{apply.stderr.decode(errors='replace')}")
    names = gitops.git(["diff", "--cached", "--no-renames", "--name-only", "-z"], worktree).stdout
    paths = [p for p in names.split("\0") if p]
    if not paths or any(not path_allowed(p, allowed) or path_allowed(p, protected) for p in paths):
        raise ValueError("Git 交付为空或包含允许范围外的路径")
    gitops.git(["commit", "-m", "Vernal: apply reviewed task changes"], worktree)
    return gitops.head_sha(worktree)
