"""统一结构化 Diff 生成器（WP-2：同时支持 git 与 files 两种协作模式）。"""

from __future__ import annotations

import difflib
import re
from typing import TYPE_CHECKING, Any

from autodev import gitops
from autodev.artifacts import FileExchange
from autodev.db import Milestone, Task
from autodev.models import TaskStatus

if TYPE_CHECKING:
    from autodev.engine import Engine

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
MAX_SINGLE_FILE_LINES = 3000


def parse_unified_diff(raw_diff: str) -> dict[str, Any]:
    """将标准 unified diff 解析为结构化文件/hunk/行号视图。"""
    files: list[dict[str, Any]] = []
    cur_file: dict[str, Any] | None = None
    cur_hunk: dict[str, Any] | None = None
    old_ln = 0
    new_ln = 0
    total_add = 0
    total_del = 0

    def _finish_file() -> None:
        if cur_file is None:
            return
        line_cnt = sum(len(h["lines"]) for h in cur_file["hunks"])
        if line_cnt > MAX_SINGLE_FILE_LINES:
            cur_file["too_large"] = True
            cur_file["hunks"] = []

    for line in raw_diff.splitlines():
        if line.startswith("diff --git "):
            _finish_file()
            parts = line.split(" ")
            b_path = parts[-1][2:] if len(parts) >= 4 and parts[-1].startswith("b/") else parts[-1]
            cur_file = {
                "path": b_path,
                "status": "M",
                "additions": 0,
                "deletions": 0,
                "binary": False,
                "too_large": False,
                "hunks": [],
            }
            files.append(cur_file)
            cur_hunk = None
            continue

        if cur_file is None:
            continue

        if line.startswith("new file mode") or line == "--- /dev/null":
            cur_file["status"] = "A"
            continue
        if line.startswith("deleted file mode") or line == "+++ /dev/null":
            cur_file["status"] = "D"
            continue
        if line.startswith("Binary files "):
            cur_file["binary"] = True
            continue
        if line.startswith(("--- ", "+++ ", "index ")):
            if line.startswith("+++ b/"):
                cur_file["path"] = line[6:]
            continue

        m = _HUNK_RE.match(line)
        if m:
            old_ln = int(m.group(1))
            new_ln = int(m.group(3))
            cur_hunk = {"header": line, "lines": []}
            cur_file["hunks"].append(cur_hunk)
            continue

        if cur_hunk is None:
            continue

        if line.startswith("+"):
            cur_file["additions"] += 1
            total_add += 1
            cur_hunk["lines"].append({"type": "add", "old_no": None, "new_no": new_ln, "text": line[1:]})
            new_ln += 1
        elif line.startswith("-"):
            cur_file["deletions"] += 1
            total_del += 1
            cur_hunk["lines"].append({"type": "del", "old_no": old_ln, "new_no": None, "text": line[1:]})
            old_ln += 1
        elif line.startswith(" "):
            cur_hunk["lines"].append({"type": "ctx", "old_no": old_ln, "new_no": new_ln, "text": line[1:]})
            old_ln += 1
            new_ln += 1

    _finish_file()
    return {
        "files": files,
        "stat": {
            "files_changed": len(files),
            "additions": total_add,
            "deletions": total_del,
            "total_lines": total_add + total_del,
        },
        "raw_patch": raw_diff,
    }


def _patch_map_to_unified(patch_map: dict[str, tuple[str | None, str | None]]) -> str:
    blocks: list[str] = []
    for path in sorted(patch_map):
        old_val, new_val = patch_map[path]
        old_lines = (old_val or "").splitlines(keepends=True)
        new_lines = (new_val or "").splitlines(keepends=True)
        blocks.append(f"diff --git a/{path} b/{path}")
        if old_val is None:
            blocks.append("new file mode 100644")
        elif new_val is None:
            blocks.append("deleted file mode 100644")
        blocks.extend(
            ln.rstrip("\n")
            for ln in difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile="/dev/null" if old_val is None else f"a/{path}",
                tofile="/dev/null" if new_val is None else f"b/{path}",
            )
        )
    return "\n".join(blocks)


def task_diff(engine: "Engine", task: Task) -> dict[str, Any]:
    if engine.settings.collaboration_mode == "files":
        exchange = FileExchange(engine.ws, engine.db)
        if not task.artifact_digest:
            return parse_unified_diff("")
        raw_patch_obj = exchange.get(task.artifact_digest) or {}
        # FileExchange.patch 存储格式为 {path: [old_val, new_val]}
        normalized: dict[str, tuple[str | None, str | None]] = {}
        for k, pair in raw_patch_obj.items():
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                normalized[k] = (pair[0], pair[1])
            elif isinstance(pair, str):
                normalized[k] = (None, pair)
        return parse_unified_diff(_patch_map_to_unified(normalized))

    if task.base_sha and task.candidate_commit_sha:
        raw = gitops.git(
            ["diff", f"{task.base_sha}..{task.candidate_commit_sha}"],
            engine.ws.root,
            check=False,
        ).stdout
        return parse_unified_diff(raw)
    if task.worktree_path and task.base_sha:
        from pathlib import Path

        wt = Path(task.worktree_path)
        if wt.exists():
            return parse_unified_diff(gitops.diff_since(wt, task.base_sha))
    return parse_unified_diff("")


def milestone_diff(engine: "Engine", ms: Milestone) -> dict[str, Any]:
    tasks = [
        t for t in engine.db.list_tasks(milestone_id=ms.milestone_id)
        if t.status != TaskStatus.CANCELLED
    ]
    if engine.settings.collaboration_mode == "files":
        exchange = FileExchange(engine.ws, engine.db)
        merged_map: dict[str, tuple[str | None, str | None]] = {}
        for t in tasks:
            if not t.artifact_digest:
                continue
            raw_patch_obj = exchange.get(t.artifact_digest) or {}
            for k, pair in raw_patch_obj.items():
                if isinstance(pair, (list, tuple)) and len(pair) == 2:
                    first_old = merged_map[k][0] if k in merged_map else pair[0]
                    merged_map[k] = (first_old, pair[1])
        return parse_unified_diff(_patch_map_to_unified(merged_map))

    raw_chunks: list[str] = []
    for t in tasks:
        if t.base_sha and t.candidate_commit_sha:
            chunk = gitops.git(
                ["diff", f"{t.base_sha}..{t.candidate_commit_sha}"],
                engine.ws.root,
                check=False,
            ).stdout
            if chunk.strip():
                raw_chunks.append(chunk)
    return parse_unified_diff("\n".join(raw_chunks))
