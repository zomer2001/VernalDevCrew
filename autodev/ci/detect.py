"""CI 模板自动探测与测试命令合成（WP-3：尊重项目既有 workflows，仅生成独立 vernal-ci.yml）。"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from autodev.db import Milestone, Task
from autodev.models import TaskStatus

CI_ASSETS_DIR = Path(__file__).resolve().parents[1] / "assets" / "ci"


def detect_ci_template_kind(cwd: Path) -> Literal["python", "node", "generic"]:
    if (cwd / "pyproject.toml").is_file() or any(cwd.glob("requirements*.txt")) or (cwd / "setup.py").is_file():
        return "python"
    if (cwd / "package.json").is_file():
        return "node"
    return "generic"


def synthesize_test_cmds(ms: Milestone, tasks: list[Task]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in [ms.integration_cmd, *(cmd for t in tasks if t.status != TaskStatus.CANCELLED for cmd in t.verify_cmds)]:
        cleaned = (raw or "").strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            ordered.append(cleaned)
    return ordered or ["true"]


def render_vernal_ci_workflow(
    cwd: Path,
    ms: Milestone,
    tasks: list[Task],
    *,
    callback_url: str = "",
) -> str:
    kind = detect_ci_template_kind(cwd)
    tpl_text = (CI_ASSETS_DIR / f"{kind}.yml").read_text("utf-8")
    cmds = synthesize_test_cmds(ms, tasks)
    indented = "\n          ".join(cmds)
    return tpl_text.replace("{{ test_cmds }}", indented).replace("{{ callback_url }}", callback_url)


def write_ci_workflow_non_destructive(cwd: Path, workflow_yaml: str) -> Path:
    """仅写入 `.gitea/workflows/vernal-ci.yml`，绝不覆盖项目原有的其它 workflow 文件。"""
    wf_dir = cwd / ".gitea" / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    target = wf_dir / "vernal-ci.yml"
    target.write_text(workflow_yaml, "utf-8")
    return target


def extract_workflow_steps(workflow_yaml: str) -> list[dict[str, str]]:
    """从渲染后的 Gitea/GitHub Actions workflow YAML 中无损解析出各步骤的字面 `run:` 脚本与 `if:` 条件。"""
    steps: list[dict[str, str]] = []
    lines = workflow_yaml.splitlines()
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)
        if stripped.startswith("- name:"):
            name = stripped.split(":", 1)[1].strip().strip('"').strip("'")
            step_if = ""
            run_lines: list[str] = []
            i += 1
            while i < n:
                sub = lines[i]
                sub_stripped = sub.lstrip(" ")
                sub_indent = len(sub) - len(sub_stripped)
                if sub_stripped and sub_indent <= indent:
                    break
                if sub_stripped.startswith("if:"):
                    step_if = sub_stripped.split(":", 1)[1].strip()
                    i += 1
                elif sub_stripped.startswith("run: |"):
                    block_base: int | None = None
                    i += 1
                    while i < n:
                        bline = lines[i]
                        bstripped = bline.lstrip(" ")
                        bindent = len(bline) - len(bstripped)
                        if bstripped and bindent <= sub_indent:
                            break
                        if bstripped and block_base is None:
                            block_base = bindent
                        if not bstripped:
                            run_lines.append("")
                        else:
                            run_lines.append(bline[block_base:] if block_base is not None else bstripped)
                        i += 1
                else:
                    i += 1
            steps.append({"name": name, "if": step_if, "run": "\n".join(run_lines).rstrip() + "\n"})
        else:
            i += 1
    return steps
