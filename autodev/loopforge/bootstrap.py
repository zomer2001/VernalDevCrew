"""Asset-as-Code：向 LoopForge 毛坯底座幂等注入 Vernal 标准研发角色、编码规则与交付 SOP。"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from autodev.assets import builtin_mcp_templates, load_builtin_skills
from autodev.config import Settings
from autodev.local_git import PACKAGE_PATCH
from autodev.server.project_launcher import choose_preferred_harness_from_runtimes, merge_runner_runtimes
from autodev.loopforge.client import LoopForgeClient, LoopForgeError

# 任务卡 → 交付泳道（lane）：契约 / 前端 / 修复 / 后端（默认）
LANES = ("backend", "frontend", "contract", "bugfix")
_FRONTEND_SUFFIXES = (".html", ".css", ".scss", ".tsx", ".jsx", ".vue", ".svelte")


def skill_asset_id(project_id: str, slug: str) -> str:
    return f"{project_id}-skill-{slug}"


def lane_workflow_id(project_id: str, mode_key: str, lane: str) -> str:
    """backend 泳道沿用原有默认 SOP id，其余泳道追加 -<lane> 后缀。"""
    base = f"{project_id}-vernal-{'file' if mode_key == 'files' else 'git'}"
    return f"{base}-sop" if lane == "backend" else f"{base}-{lane}-sop"


def classify_lane(role: str, allowed_paths: list[str] | None, title: str = "") -> str:
    paths = [p.lower() for p in (allowed_paths or [])]
    text = f"{role} {title}".lower()
    if any(w in text for w in ("bugfix", "修复", "缺陷", "hotfix")):
        return "bugfix"
    if role == "contract" or (paths and all(p.startswith("contracts/") or p.endswith((".json", ".md")) for p in paths)):
        return "contract"
    if paths and any(p.endswith(_FRONTEND_SUFFIXES) or p.startswith(("web/", "ui/", "frontend/", "public/")) for p in paths):
        return "frontend"
    return "backend"

PACKAGE_FILES_SCRIPT = """\
import json, os, pathlib

SKIP = {"__pycache__", ".pytest_cache", ".DS_Store", "node_modules", ".git", ".autodev", ".venv", "vernal-files.json", "review-report.md", "handoff-brief.json"}
root = pathlib.Path(".").resolve()
baseline_path = root / ".vernal-baseline.json"
baseline = {}
if baseline_path.is_file():
    try:
        baseline = json.loads(baseline_path.read_text("utf-8"))
    except Exception:
        baseline = {}

out_path = root / "vernal-files.json"
explicit = None
if out_path.is_file():
    try:
        parsed = json.loads(out_path.read_text("utf-8"))
        if isinstance(parsed, dict) and isinstance(parsed.get("files"), dict) and parsed["files"]:
            explicit = parsed["files"]
    except Exception:
        explicit = None

if explicit is None:
    current = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP and not d.startswith(".")]
        pdir = pathlib.Path(dirpath)
        for fn in filenames:
            if fn in SKIP or fn.startswith("."):
                continue
            fp = pdir / fn
            if fp.is_symlink() or not fp.is_file():
                continue
            rel = fp.relative_to(root).as_posix()
            current[rel] = fp.read_text("utf-8")
    changes = {}
    for k, v in current.items():
        if baseline.get(k) != v:
            changes[k] = v
    for k in baseline:
        if k not in current:
            changes[k] = None
    out_path.write_text(json.dumps({"files": changes}, ensure_ascii=False, indent=2), "utf-8")
else:
    out_path.write_text(json.dumps({"files": explicit}, ensure_ascii=False, indent=2), "utf-8")

final_data = json.loads(out_path.read_text("utf-8"))
if not isinstance(final_data.get("files"), dict) or not final_data["files"]:
    raise SystemExit("vernal-files.json 为空：任务未产生任何有效文件变更")
print(f"已打包 {len(final_data['files'])} 个交付文件至 vernal-files.json")
"""

RECORD_GIT_MR_SCRIPT = """\
set -eu
sha="$(git rev-parse HEAD)"
printf '# Vernal Git 交付记录\\n\\n- Commit: %s\\n- Branch: %s\\n' "$sha" "${AGENTFLOW_WORK_BRANCH:-HEAD}" > "$AGENTFLOW_WORKSPACE/mr-body.md"
"""

VALIDATE_PLAN_JSON_SCRIPT = (Path(__file__).parents[1] / "speckit" / "validate_output.py").read_text("utf-8")

PACKAGE_FILES_NODE_SCRIPT = """\
const fs = require("fs");
const path = require("path");
const SKIP = new Set(["__pycache__", ".pytest_cache", ".DS_Store", "node_modules", ".git", ".autodev", ".venv", "vernal-files.json", "review-report.md", "handoff-brief.json"]);
const root = process.cwd();
let baseline = {};
if (fs.existsSync(".vernal-baseline.json")) {
  try { baseline = JSON.parse(fs.readFileSync(".vernal-baseline.json", "utf8")); } catch {}
}
let explicit = null;
if (fs.existsSync("vernal-files.json")) {
  try {
    const parsed = JSON.parse(fs.readFileSync("vernal-files.json", "utf8"));
    if (parsed && typeof parsed.files === "object" && Object.keys(parsed.files).length) explicit = parsed.files;
  } catch {}
}
let outFiles = explicit;
if (!outFiles) {
  const current = {};
  const walk = (dir) => {
    for (const ent of fs.readdirSync(dir, { withFileTypes: true })) {
      if (SKIP.has(ent.name) || ent.name.startsWith(".")) continue;
      const full = path.join(dir, ent.name);
      if (ent.isSymbolicLink()) continue;
      if (ent.isDirectory()) walk(full);
      else if (ent.isFile()) {
        const rel = path.relative(root, full).split(path.sep).join("/");
        current[rel] = fs.readFileSync(full, "utf8");
      }
    }
  };
  walk(root);
  outFiles = {};
  for (const [k, v] of Object.entries(current)) if (baseline[k] !== v) outFiles[k] = v;
  for (const k of Object.keys(baseline)) if (!(k in current)) outFiles[k] = null;
}
if (!outFiles || !Object.keys(outFiles).length) {
  console.error("vernal-files.json 为空：任务未产生任何有效文件变更");
  process.exit(1);
}
fs.writeFileSync("vernal-files.json", JSON.stringify({ files: outFiles }, null, 2), "utf8");
console.log(`已打包 ${Object.keys(outFiles).length} 个交付文件至 vernal-files.json`);
"""

VALIDATE_PLAN_JSON_NODE_SCRIPT = (Path(__file__).parents[1] / "speckit" / "validate_output.cjs").read_text("utf-8")

def _dual_shell_script(py_src: str, node_src: str) -> str:
    return (
        "set -eu\n"
        "if command -v python3 >/dev/null 2>&1; then\n"
        "  python3 - <<'__VERNAL_PY__'\n"
        f"{py_src.rstrip()}\n"
        "__VERNAL_PY__\n"
        "elif command -v node >/dev/null 2>&1; then\n"
        "  node - <<'__VERNAL_NODE__'\n"
        f"{node_src.rstrip()}\n"
        "__VERNAL_NODE__\n"
        "else\n"
        '  echo "宿主机缺少 python3 或 node 运行时" >&2\n'
        "  exit 1\n"
        "fi\n"
    )


PACKAGE_FILES_SHELL_SCRIPT = _dual_shell_script(PACKAGE_FILES_SCRIPT, PACKAGE_FILES_NODE_SCRIPT)
VALIDATE_PLAN_JSON_SHELL_SCRIPT = _dual_shell_script(VALIDATE_PLAN_JSON_SCRIPT, VALIDATE_PLAN_JSON_NODE_SCRIPT)


VALIDATE_STEERING_SCRIPT = """\
import json, pathlib

root = pathlib.Path(".").resolve()
out_path = root / "steering-plan.json"
if not out_path.is_file():
    raise SystemExit("缺少 steering-plan.json：请将满足约定格式的转向变更单写入当前目录 steering-plan.json")

try:
    data = json.loads(out_path.read_text("utf-8").strip())
except Exception as exc:
    raise SystemExit(f"steering-plan.json 不是合法 JSON：{exc}")

if not isinstance(data, dict) or not isinstance(data.get("summary"), str) or not data["summary"].strip():
    raise SystemExit("steering-plan.json 必须是非空对象且包含非空字符串 summary")

for list_key in ("decisions_upsert", "decisions_revoke", "reopen_tasks", "cancel_tasks", "new_tasks"):
    val = data.get(list_key)
    if val is not None and not isinstance(val, list):
        raise SystemExit(f"steering-plan.json 字段 {list_key} 必须是数组")
    data.setdefault(list_key, [])

summary_md = root / "steering-summary.md"
if not summary_md.is_file() or not summary_md.read_text("utf-8").strip():
    lines = [f"# 评审与转向变更摘要\\n\\n{data['summary'].strip()}\\n"]
    if data["decisions_upsert"]:
        lines.append("## 新增/更新项目规矩")
        for d in data["decisions_upsert"]:
            if isinstance(d, dict):
                lines.append(f"- **{d.get('decision_id', 'D-NEW')} {d.get('title', '')}**：{d.get('rule_text', '')}")
    if data["reopen_tasks"]:
        lines.append("\\n## 局部退回重做任务")
        for r in data["reopen_tasks"]:
            if isinstance(r, dict):
                lines.append(f"- **{r.get('task_id', '')}**：{r.get('reason', '')}")
    if data["new_tasks"]:
        lines.append("\\n## 新增/重排后续任务")
        for t in data["new_tasks"]:
            if isinstance(t, dict):
                lines.append(f"- **{t.get('title', '')}**（{t.get('role', 'coder')}）")
    summary_md.write_text("\\n".join(lines) + "\\n", "utf-8")

out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
print("steering-plan.json 校验通过")
"""

VALIDATE_STEERING_NODE_SCRIPT = """\
const fs = require("fs");
if (!fs.existsSync("steering-plan.json")) {
  console.error("缺少 steering-plan.json：请将满足约定格式的转向变更单写入当前目录 steering-plan.json");
  process.exit(1);
}
let data;
try { data = JSON.parse(fs.readFileSync("steering-plan.json", "utf8").trim()); } catch (e) {
  console.error(`steering-plan.json 不是合法 JSON：${e.message}`);
  process.exit(1);
}
if (!data || typeof data !== "object" || Array.isArray(data) || typeof data.summary !== "string" || !data.summary.trim()) {
  console.error("steering-plan.json 必须是非空对象且包含非空字符串 summary");
  process.exit(1);
}
for (const k of ["decisions_upsert", "decisions_revoke", "reopen_tasks", "cancel_tasks", "new_tasks"]) {
  if (data[k] !== undefined && !Array.isArray(data[k])) {
    console.error(`steering-plan.json 字段 ${k} 必须是数组`);
    process.exit(1);
  }
  if (!data[k]) data[k] = [];
}
if (!fs.existsSync("steering-summary.md") || !fs.readFileSync("steering-summary.md", "utf8").trim()) {
  const lines = [`# 评审与转向变更摘要\\n\\n${data.summary.trim()}\\n`];
  if (data.decisions_upsert.length) {
    lines.push("## 新增/更新项目规矩");
    for (const d of data.decisions_upsert) if (d) lines.push(`- **${d.decision_id || "D-NEW"} ${d.title || ""}**：${d.rule_text || ""}`);
  }
  if (data.reopen_tasks.length) {
    lines.push("\\n## 局部退回重做任务");
    for (const r of data.reopen_tasks) if (r) lines.push(`- **${r.task_id || ""}**：${r.reason || ""}`);
  }
  if (data.new_tasks.length) {
    lines.push("\\n## 新增/重排后续任务");
    for (const t of data.new_tasks) if (t) lines.push(`- **${t.title || ""}**（${t.role || "coder"}）`);
  }
  fs.writeFileSync("steering-summary.md", lines.join("\\n") + "\\n", "utf8");
}
fs.writeFileSync("steering-plan.json", JSON.stringify(data, null, 2), "utf8");
console.log("steering-plan.json 校验通过");
"""

VALIDATE_STEERING_SHELL_SCRIPT = _dual_shell_script(VALIDATE_STEERING_SCRIPT, VALIDATE_STEERING_NODE_SCRIPT)


def review_workflow_id(project_id: str) -> str:
    return f"{project_id}-vernal-review-sop"


def decisions_rule_id(project_id: str) -> str:
    return f"{project_id}-vernal-decisions-rule"


def plan_workflow_id(project_id: str, role: str = "director") -> str:
    r = (role or "director").strip().lower()
    if r == "pm":
        return f"{project_id}-vernal-plan-pm-sop"
    if r in ("qa_lead", "tester", "reviewer"):
        return f"{project_id}-vernal-plan-qa-sop"
    return f"{project_id}-vernal-plan-sop"


def _executor_id_for(harness: str) -> str:
    h = (harness or "codex").strip().lower()
    h = {"claude": "claude-code", "deepseek": "dsh", "deepseek-harness": "dsh"}.get(h, h)
    if h in ("codex", "opencode", "claude-code", "codebuddy", "dsh"):
        return h
    return "codex"


def build_vernal_rule(project_id: str) -> dict[str, Any]:
    return {
        "id": f"{project_id}-vernal-guard-rule",
        "projectId": project_id,
        "name": "Vernal 领地边界与契约门禁规范",
        "description": "严格遵守子任务声明的允许修改路径白名单与必跑验证命令",
        "content": (
            "1. 严格只新增或修改需求正文中「允许修改路径白名单」列出的目录/文件，严禁越界修改其他研发组领地或受保护契约。\n"
            "2. 若采用文件交接模式，先将需求末尾提供的上游文件 JSON 还原到当前工作目录并保存一份 `.vernal-baseline.json`，再在此基础上开发。\n"
            "3. 完成编码后，必须在当前目录逐条运行「必跑验证命令」，确保退出码全部为 0 后再结束回合。\n"
        ),
    }


def build_decisions_rule(
    project_id: str,
    decisions: list[dict[str, Any]],
    *,
    workflow_ids: list[str] | None = None,
    version: int = 1,
    constitution: str = "",
) -> dict[str, Any]:
    """把项目当前所有生效的评审决定编译为 LoopForge 全局 Rule（global=True），由底座自动注入给所有 SOP 节点与对话角色。"""
    active = [d for d in decisions if str(d.get("status") or "ACTIVE") == "ACTIVE"]
    if not active:
        body = "当前项目暂无附加圆桌评审硬规矩，按任务验收标准与路径白名单执行即可。\n"
    else:
        lines = [
            f"# 项目生效工程规矩（评审版本 v{version} · 共 {len(active)} 条硬约束）",
            "以下规矩由项目评审会拍板生效，所有编码实现与独立代码审查必须逐条遵守，违背任意一条必须打回返工：",
            "",
        ]
        for d in active:
            did = d.get("decision_id") or "D"
            title = d.get("title") or ""
            rule_txt = d.get("rule_text") or ""
            paths = d.get("affected_paths") or []
            scope = f"（适用范围：{', '.join(paths)}）" if paths else ""
            lines.append(f"- **[{did}] {title}**{scope}：{rule_txt}")
        body = "\n".join(lines) + "\n"
    if constitution:
        body = "# 项目宪章\n\n" + constitution + "\n\n" + body
    return {
        "id": decisions_rule_id(project_id),
        "projectId": project_id,
        "name": f"Vernal 项目评审生效规矩 (v{version})",
        "description": "圆桌评审与阶段复盘拍板生效的硬性架构/业务规矩，底座全局自动注入所有编码与审查节点",
        "content": body,
        "priority": 100,
        "global": True,
        "workflowIds": list(workflow_ids or []),
    }


async def sync_decisions_rule(
    client: LoopForgeClient,
    project_id: str,
    decisions: list[dict[str, Any]],
    *,
    workflow_ids: list[str] | None = None,
    version: int = 1,
    constitution: str = "",
) -> dict[str, Any]:
    if workflow_ids is None:
        wfs: list[str] = []
        for mk in ("files", "git"):
            for ln in LANES:
                wfs.append(lane_workflow_id(project_id, mk, ln))
        wfs.append(review_workflow_id(project_id))
        workflow_ids = wfs
    rule = build_decisions_rule(project_id, decisions, workflow_ids=workflow_ids, version=version,constitution=constitution)
    return await client.put_rule(rule)


def build_vernal_agents(project_id: str, settings: Settings) -> list[dict[str, Any]]:
    coder_exec = _executor_id_for(settings.harness_for("coder"))
    qa_exec = _executor_id_for(settings.harness_for("reviewer"))
    coder_model = settings.model_for("coder")
    qa_model = settings.model_for("reviewer")
    rule_ids = [f"{project_id}-vernal-guard-rule", decisions_rule_id(project_id)]

    coder: dict[str, Any] = {
        "id": f"{project_id}-vernal-coder",
        "projectId": project_id,
        "name": "Vernal 模块开发工程师",
        "version": 1,
        "description": "负责按架构蓝图、契约与目录领地独立完成子任务实现及自测",
        "executorId": coder_exec,
        "model": coder_model,
        "effort": "high" if coder_exec == "codex" else "",
        "rules": rule_ids,
        "skills": [],
        "mcpServers": [],
        "loop": {
            "enabled": False,
            "maxIterations": 1,
            "exitCondition": "executor_success",
            "onExhausted": "fail",
        },
        "verification": [],
        "prompt": (
            "你是 Vernal 架构研发平台的模块开发工程师（Coder）。\n"
            "请仔细阅读当前需求描述中的【任务目标】【验收标准】【允许修改路径白名单】【必跑验证命令】以及上游上下文：\n"
            "1. 如果需求末尾附带了上游文件交接 JSON，请先用 Python 将这些键值写入当前工作区对应路径，并把原始上游字典存为 `.vernal-baseline.json`。\n"
            "2. 严格在允许修改路径白名单内编写高质量、可运行的代码与测试，不得触碰未授权路径。\n"
            "3. 逐条执行所有「必跑验证命令」并修复任何报错，直到全部通过。\n"
            "4. 若为文件交接模式，将本次新增/修改的文件写入 `vernal-files.json`（格式 `{\"files\": {\"path\": \"content\"}}`）；若为 Git 模式，提交并推送到工作分支。\n"
            "5. 结束回合前，若环境中存在且允许调用 `submit_gate_result` MCP 工具，调用一次 `submit_gate_result`（参数 `decision=\"pass\"`, `reason=\"编码与自测通过\"`）；若被审批策略拒绝，如实记录受阻原因并正常退出，不得伪造终态。"
        ),
    }

    reviewer: dict[str, Any] = {
        "id": f"{project_id}-vernal-reviewer",
        "projectId": project_id,
        "name": "Vernal 独立契约与质量审查官",
        "version": 1,
        "description": "独立复核代码改动是否满足验收标准、契约一致性与路径白名单",
        "executorId": qa_exec,
        "model": qa_model,
        "effort": "high" if qa_exec == "codex" else "",
        "rules": rule_ids,
        "skills": [],
        "mcpServers": [],
        "loop": {
            "enabled": False,
            "maxIterations": 1,
            "exitCondition": "all_gates_pass",
            "onExhausted": "fail",
        },
        "artifactOutputs": [
            {
                "name": "review-report.md",
                "path": "review-report.md",
                "mediaType": "text/markdown",
                "required": True,
            }
        ],
        "verification": [],
        "prompt": (
            "你是 Vernal 独立审查官（Reviewer）。请对照需求中的【验收标准】【允许修改路径白名单】【必跑验证命令】审查当前工作区：\n"
            "1. 复跑需求中的验证命令，核实测试真实通过而非空壳占位。\n"
            "2. 检查改动文件是否全部位于允许修改路径白名单之内，且未破坏冻结契约。\n"
            "文件交付时用本工位的交付清单和复跑结果判定本节点。涉及原地回写的项目，在报告中把原目录验收列为后续平台交付检查。\n"
            "3. 将审查结论写入 `review-report.md`（首行写 `# PASS` 或 `# REJECT`，并附上核验依据）。\n"
            "4. 结束回合前，若允许调用 `submit_gate_result` MCP 工具，调用一次：通过传 `decision=\"pass\"`, `reason=\"契约与验收标准全部通过\"`；不满足传 `decision=\"rework\"`, `reason=\"...\"`, `actionRequired=\"具体修复项\"`。若工具被审批策略拒绝，确保 `review-report.md` 已落盘并如实说明受阻原因。"
        ),
    }
    available = {s["slug"] for s in load_builtin_skills()}

    def _skills(*slugs: str) -> list[str]:
        return [skill_asset_id(project_id, s) for s in slugs if s in available]

    coder["skills"] = _skills("test-driven-development", "systematic-debugging")
    reviewer["skills"] = _skills("security-review")
    reviewer["prompt"] += "\n审查前先读取运行根目录 vernal-review-context.md，核对真实 Diff 的调用方、工单内的详设与需求标记。"

    def _variant(lane: str, name: str, desc: str, skills: list[str], extra: str) -> dict[str, Any]:
        agent = copy.deepcopy(coder)
        agent["id"] = f"{project_id}-vernal-coder-{lane}"
        agent["name"] = name
        agent["description"] = desc
        agent["skills"] = skills
        agent["prompt"] = coder["prompt"] + "\n" + extra
        return agent

    frontend = _variant(
        "frontend",
        "Vernal 前端界面工程师",
        "负责网页界面、交互与小游戏前端，注重审美克制、响应式与键盘/触屏输入",
        _skills("frontend-design", "webapp-testing", "systematic-debugging"),
        "6. 前端专项：界面须在窄屏与桌面均可用，同时支持键盘、鼠标与触屏；使用 ES Module 时用本地 HTTP 服务验证入口，不要依赖 file://。",
    )
    contract = _variant(
        "contract",
        "Vernal 契约与文档工程师",
        "负责接口契约、数据结构、样例与说明文档，强调字段稳定、可被机器校验",
        _skills("systematic-debugging"),
        "6. 契约专项：优先产出可被脚本校验的 schema 与样例；字段一旦冻结不得随意更名，并附一个最小校验命令。",
    )
    bugfix = _variant(
        "bugfix",
        "Vernal 缺陷定位修复工程师",
        "先复现与定位根因，再做最小补丁与回归测试",
        _skills("systematic-debugging", "test-driven-development"),
        "6. 修复专项：先写出能稳定复现问题的最小用例并确认失败，再做最小补丁；不得靠放宽断言或吞异常过关。",
    )
    def _make_planner(agent_id: str, name: str, desc: str, role_key: str) -> dict[str, Any]:
        exec_id = _executor_id_for(settings.harness_for(role_key))
        model_id = settings.model_for(role_key)
        return {
            "id": agent_id,
            "projectId": project_id,
            "name": name,
            "version": 1,
            "description": desc,
            "executorId": exec_id,
            "model": model_id,
            "effort": "high" if exec_id == "codex" else "",
            "rules": [],
            "skills": _skills("spec-writing"),
            "mcpServers": [],
            "loop": {
                "enabled": False,
                "maxIterations": 1,
                "exitCondition": "executor_success",
                "onExhausted": "fail",
            },
            "artifactOutputs": [
                {
                    "name": "vernal-result.json",
                    "path": "vernal-result.json",
                    "mediaType": "application/json",
                    "required": True,
                }
            ],
            "verification": [],
            "prompt": (
                "你是 Vernal 架构规划与圆桌评审专家（只读分析，不直接开发业务代码）。\n"
                "请仔细阅读当前需求正文中的角色设定、背景资料、约束规则与 JSON Schema：\n"
                "1. 若需求正文给出了上下文还原命令，请先在当前工作目录执行该命令还原 `PLAN_PROMPT.md`、`inputs/` 与 `.vernal-schema.json`，再完整阅读 `PLAN_PROMPT.md`。\n"
                "2. 严格按要求的 JSON Schema 生成唯一的 JSON 对象，并以 UTF-8 纯 JSON（不要包含 Markdown 代码块围栏）写入当前目录的 `vernal-result.json`。\n"
                "3. 不要编写或提交无关的业务源码文件。\n"
                "4. 结束回合前，若环境中存在且允许调用 `submit_gate_result` MCP 工具，调用一次 `submit_gate_result`（参数 `decision=\"pass\"`, `reason=\"已产出合法的 vernal-result.json\"`）；若工具被审批策略拒绝，确保 `vernal-result.json` 已落盘并如实说明受阻原因后正常退出。"
            ),
        }

    planner = _make_planner(
        f"{project_id}-vernal-planner",
        "Vernal 架构总监与蓝图规划师",
        "承担研发总监 ADR 拍板、契约架构评审、蓝图拆单与异常仲裁（只读输出 vernal-result.json）",
        "director",
    )
    planner_pm = _make_planner(
        f"{project_id}-vernal-planner-pm",
        "Vernal 圆桌产品经理（PM）",
        "承担圆桌产品视角评审与 PRD 规格梳理，支持独立配置产品角色模型",
        "pm",
    )
    planner_qa = _make_planner(
        f"{project_id}-vernal-planner-qa",
        "Vernal 圆桌 QA 质量总监",
        "承担圆桌验收红线与测试策略评审，支持与研发总监异构模型交叉把关",
        "qa_lead",
    )

    def _make_review_specialist(slug: str, name: str, role_key: str, focus: str) -> dict[str, Any]:
        exec_id = _executor_id_for(settings.harness_for(role_key))
        model_id = settings.model_for(role_key)
        out_file = f"opinions/{slug}.json"
        return {
            "id": f"{project_id}-vernal-review-{slug}",
            "projectId": project_id,
            "name": name,
            "version": 1,
            "description": f"原生并发进展评审专家（输出独立文件 {out_file}，互不阻塞）",
            "executorId": exec_id,
            "model": model_id,
            "effort": "high" if exec_id == "codex" else "",
            "rules": [],
            "skills": [],
            "mcpServers": [],
            "loop": {"enabled": False, "maxIterations": 1, "exitCondition": "executor_success", "onExhausted": "fail"},
            "artifactOutputs": [{"name": f"opinion-{slug}.json", "path": out_file, "mediaType": "application/json", "required": True}],
            "verification": [],
            "prompt": (
                f"你是 Vernal 进展与问题评审圆桌的【{name}】（只读分析，不改业务代码）。\n"
                f"你的评审焦点：{focus}\n"
                "1. 若需求正文给出了上下文还原命令，先在当前目录执行该命令还原评审资料，再阅读需求正文中的【当前进展与议题】。\n"
                f"2. 请确保目录 `opinions/` 存在，并将你的结构化意见以纯 UTF-8 JSON 写入 `{out_file}`，包含字段：\n"
                '   `{"headline": "一句话核心判断", "keep": ["应保留的成果/规矩"], "change": ["建议修改的方向/规矩"], "rework_tasks": ["建议局部退回的任务ID"], "risks": ["关键风险"]}`。\n'
                "3. 若存在且允许调用 `submit_gate_result` MCP 工具，结束前调用 `submit_gate_result(decision=\"pass\", reason=\"已提交评审意见\")`；若被审批策略拒绝，确保意见 JSON 已落盘并如实说明受阻原因。"
            ),
        }

    rev_pm = _make_review_specialist(
        "pm",
        "评审圆桌 · 产品经理",
        "pm",
        "聚焦功能取舍、用户体验方向调整、优先级裁剪与验收口径",
    )
    rev_arch = _make_review_specialist(
        "arch",
        "评审圆桌 · 契约架构师",
        "director",
        "聚焦模块边界、接口契约演进、如何保证改动不破坏已完成模块",
    )
    rev_qa = _make_review_specialist(
        "qa",
        "评审圆桌 · QA 质量总监",
        "qa_lead",
        "聚焦暴露出的缺陷根因、哪张旧任务需要精准退回重测、无界面自动化验证命令自洽性",
    )

    dir_exec = _executor_id_for(settings.harness_for("director"))
    dir_model = settings.model_for("director")
    rev_director: dict[str, Any] = {
        "id": f"{project_id}-vernal-review-director",
        "projectId": project_id,
        "name": "评审圆桌 · 研发总监（生成转向变更单）",
        "version": 1,
        "description": "汇总三方并发评审意见与人类反馈，产出可执行转向变更单 steering-plan.json",
        "executorId": dir_exec,
        "model": dir_model,
        "effort": "high" if dir_exec == "codex" else "",
        "rules": [],
        "skills": [],
        "mcpServers": [],
        "loop": {"enabled": False, "maxIterations": 1, "exitCondition": "executor_success", "onExhausted": "fail"},
        "artifactOutputs": [
            {"name": "steering-plan.json", "path": "steering-plan.json", "mediaType": "application/json", "required": True},
            {"name": "steering-summary.md", "path": "steering-summary.md", "mediaType": "text/markdown", "required": True},
        ],
        "verification": [],
        "prompt": (
            "你是 Vernal 研发总监（Director）。请综合需求正文中的【当前进展、生效规矩与本次议题】、当前工作区 `opinions/pm.json`、`opinions/arch.json`、`opinions/qa.json`（以及人类审批驳回意见，若有），生成一份精确、最小侵入的转向变更单：\n"
            "1. 将纯 JSON 写入 `steering-plan.json`，严格包含以下顶层字段：\n"
            "   - `summary` (string)：用大白话说明本次为什么调整、保留什么、改什么；\n"
            "   - `decisions_upsert` (array)：新增或修订的硬性项目规矩，每项 `{\"decision_id\": \"D-01\", \"title\": \"短标题\", \"rule_text\": \"明确可核验的规矩条文\", \"rationale\": \"原因\", \"affected_paths\": [\"src/\"]}`；\n"
            "   - `decisions_revoke` (array)：需废止的旧规矩 ID 列表，每项 `{\"decision_id\": \"D-01\", \"reason\": \"废止原因\"}`；\n"
            "   - `reopen_tasks` (array)：仅需局部退回重做的已有任务（严禁无故退回已通过且无关的任务！），每项 `{\"task_id\": \"T-003\", \"reason\": \"具体问题与修复指引\"}`；\n"
            "   - `cancel_tasks` (array)：因方向调整需关闭取消的未完成任务 ID 字符串数组，如 `[\"T-006\"]`；\n"
            "   - `new_tasks` (array)：需新增的任务卡，每项 `{\"milestone_id\": \"M1\", \"title\": \"人话标题\", \"description\": \"目标与细节\", \"role\": \"coder\", \"squad\": \"core\", \"depends_on\": [], \"allowed_paths\": [\"src/\"], \"verify_cmds\": [], \"acceptance_criteria\": [\"...\"]}`。\n"
            "2. 同时将面向人类阅读的简明变更说明写入 `steering-summary.md`。\n"
            "3. 若存在且允许调用 `submit_gate_result` MCP 工具，结束前调用 `submit_gate_result(decision=\"pass\", reason=\"已生成转向变更单\")`；若被审批策略拒绝，确保变更单与摘要文件已落盘并如实说明受阻原因。"
        ),
    }

    from autodev.prompts_style import COMMUNICATION_STYLE
    agents = [coder, reviewer, frontend, contract, bugfix, planner, planner_pm, planner_qa, rev_pm, rev_arch, rev_qa, rev_director]
    for agent in agents:
        agent["prompt"] = COMMUNICATION_STYLE + "\n" + agent["prompt"]
    for agent in [coder,reviewer,frontend,contract,bugfix]:
        agent["prompt"] += '\n输出四段交接便签到 handoff-brief.json，字段 delivered、integration、boundaries、verification 均为非空中文字符串。该文件为过程产物，不要放入源码交付清单或 Git 提交。未执行验证须明确说明。'
        agent["artifactOutputs"] = [*agent.get("artifactOutputs",[]),{"name":"handoff-brief.json","path":"handoff-brief.json","mediaType":"application/json","required":False}]
    return agents


def build_vernal_workflows(project_id: str, sandbox_id: str = "local") -> dict[str, dict[str, Any]]:
    coder_id = f"{project_id}-vernal-coder"
    reviewer_id = f"{project_id}-vernal-reviewer"

    file_wf_id = f"{project_id}-vernal-file-sop"
    git_wf_id = f"{project_id}-vernal-git-sop"

    file_wf: dict[str, Any] = {
        "id": file_wf_id,
        "projectId": project_id,
        "name": "Vernal 标准文件闭环交付 SOP",
        "version": 1,
        "status": "published",
        "sandboxProfileId": sandbox_id,
        "metricCategory": "需求开发",
        "sopLane": "后端",
        "description": "Vernal 多智能体架构平台自动装配：模块开发 → 产物打包校验 → 独立质量审查",
        "nodes": [
            {
                "id": "implement",
                "name": "模块编码与自测",
                "type": "agent",
                "metricStage": "development",
                "agentId": coder_id,
                "maxAttempts": 2,
                "position": {"x": 80, "y": 140},
            },
            {
                "id": "package",
                "name": "打包校验交付清单",
                "type": "script",
                "metricStage": "testing",
                "scriptLanguage": "shell",
                "script": PACKAGE_FILES_SHELL_SCRIPT,
                "rejectTo": "implement",
                "onFailure": "reject",
                "maxAttempts": 1,
                "maxReworks": 2,
                "artifactOutputs": [
                    {
                        "name": "vernal-files.json",
                        "path": "vernal-files.json",
                        "mediaType": "application/json",
                        "required": True,
                    }
                ],
                "position": {"x": 360, "y": 140},
            },
            {
                "id": "review",
                "name": "独立契约与测试审查",
                "type": "agent",
                "metricStage": "testing",
                "agentId": reviewer_id,
                "rejectTo": "implement",
                "onFailure": "reject",
                "maxAttempts": 1,
                "maxReworks": 2,
                "artifactOutputs": [
                    {
                        "name": "review-report.md",
                        "path": "review-report.md",
                        "mediaType": "text/markdown",
                        "required": True,
                    }
                ],
                "position": {"x": 640, "y": 140},
            },
        ],
        "edges": [
            {"id": "e1", "source": "implement", "target": "package", "when": "success"},
            {"id": "e2", "source": "package", "target": "review", "when": "success"},
        ],
    }

    git_wf: dict[str, Any] = {
        "id": git_wf_id,
        "projectId": project_id,
        "name": "Vernal 标准 Git 分支交付 SOP",
        "version": 1,
        "status": "published",
        "sandboxProfileId": sandbox_id,
        "metricCategory": "需求开发",
        "sopLane": "后端",
        "description": "Vernal 多智能体架构平台自动装配：分支编码推送 → 提交哈希登记 → 独立质量审查",
        "nodes": [
            {
                "id": "implement",
                "name": "分支编码与推送",
                "type": "agent",
                "metricStage": "development",
                "agentId": coder_id,
                "maxAttempts": 2,
                "position": {"x": 80, "y": 140},
            },
            {
                "id": "record_mr",
                "name": "登记分支提交哈希",
                "type": "script",
                "metricStage": "testing",
                "scriptLanguage": "shell",
                "executionScope": "repositories",
                "script": RECORD_GIT_MR_SCRIPT,
                "rejectTo": "implement",
                "onFailure": "reject",
                "maxAttempts": 1,
                "maxReworks": 2,
                "artifactOutputs": [
                    {
                        "name": "mr-body.md",
                        "path": "mr-body.md",
                        "mediaType": "text/markdown",
                        "required": True,
                    }
                ],
                "position": {"x": 360, "y": 140},
            },
            {
                "id": "review",
                "name": "独立契约与测试审查",
                "type": "agent",
                "metricStage": "testing",
                "agentId": reviewer_id,
                "rejectTo": "implement",
                "onFailure": "reject",
                "maxAttempts": 1,
                "maxReworks": 2,
                "artifactOutputs": [
                    {
                        "name": "review-report.md",
                        "path": "review-report.md",
                        "mediaType": "text/markdown",
                        "required": True,
                    }
                ],
                "position": {"x": 640, "y": 140},
            },
        ],
        "edges": [
            {"id": "e1", "source": "implement", "target": "record_mr", "when": "success"},
            {"id": "e2", "source": "record_mr", "target": "review", "when": "success"},
        ],
    }

    for workflow in (file_wf,git_wf):
        for node in workflow["nodes"]:
            if node["id"] in ("implement","review"):
                node["artifactOutputs"] = [*node.get("artifactOutputs",[]),{"name":"handoff-brief.json","path":"handoff-brief.json","mediaType":"application/json","required":False}]
    return {"files": file_wf, "git": git_wf}


_LANE_LABELS = {"frontend": "前端界面", "contract": "契约文档", "bugfix": "缺陷修复"}


def build_plan_workflow(project_id: str, sandbox_id: str = "local", role: str = "director") -> dict[str, Any]:
    """按圆桌/规划岗位构造两节点只读规划 SOP：起草 → JSON Schema 校验（支持跨模型交叉评审）。"""
    r = (role or "director").strip().lower()
    if r == "pm":
        planner_id = f"{project_id}-vernal-planner-pm"
        wf_name = "Vernal 圆桌产品评审 SOP（PM）"
    elif r in ("qa_lead", "tester", "reviewer"):
        planner_id = f"{project_id}-vernal-planner-qa"
        wf_name = "Vernal 圆桌质量评审 SOP（QA 总监）"
    else:
        planner_id = f"{project_id}-vernal-planner"
        wf_name = "Vernal 架构圆桌与蓝图规划 SOP（总监）"
    return {
        "id": plan_workflow_id(project_id, r),
        "projectId": project_id,
        "name": wf_name,
        "version": 1,
        "status": "published",
        "sandboxProfileId": sandbox_id,
        "metricCategory": "需求开发",
        "sopLane": "产品",
        "description": "Vernal 顶层圆桌评审、蓝图拆单与异常仲裁统一执行流：岗位专家起草 → JSON 契约校验（不合规自动打回重写）",
        "nodes": [
            {
                "id": "draft",
                "name": "圆桌专家 / 总监起草",
                "type": "agent",
                "metricStage": "development",
                "agentId": planner_id,
                "maxAttempts": 2,
                "position": {"x": 80, "y": 140},
            },
            {
                "id": "validate",
                "name": "校验结构化规划 JSON",
                "type": "script",
                "metricStage": "testing",
                "scriptLanguage": "shell",
                "script": VALIDATE_PLAN_JSON_SHELL_SCRIPT,
                "rejectTo": "draft",
                "onFailure": "reject",
                "maxAttempts": 1,
                "maxReworks": 2,
                "artifactOutputs": [
                    {
                        "name": "vernal-result.json",
                        "path": "vernal-result.json",
                        "mediaType": "application/json",
                        "required": True,
                    }
                ],
                "position": {"x": 380, "y": 140},
            },
        ],
        "edges": [
            {"id": "e1", "source": "draft", "target": "validate", "when": "success"},
        ],
    }


def build_plan_workflows(project_id: str, sandbox_id: str = "local") -> dict[str, dict[str, Any]]:
    return {
        role: build_plan_workflow(project_id, sandbox_id=sandbox_id, role=role)
        for role in ("director", "pm", "qa_lead")
    }


def build_review_workflow(project_id: str, sandbox_id: str = "local") -> dict[str, Any]:
    """原生并发「进展评审与转向 SOP」：PM / 架构师 / QA 三分支并行评审 → 总监生成变更单 → 脚本校验 → 原生人工拍板门禁（驳回跳回总监重议）。"""
    return {
        "id": review_workflow_id(project_id),
        "projectId": project_id,
        "name": "Vernal 进展评审与方向调整 SOP",
        "version": 1,
        "status": "published",
        "sandboxProfileId": sandbox_id,
        "metricCategory": "需求开发",
        "sopLane": "产品",
        "description": "支持立项评审与运行中随时拉会复盘：三角色并发评审 → 总监生成规矩与工单变更单 → 人工审批门禁（通过自动同步全局 Rule 与局部返工工单，驳回重议）",
        "nodes": [
            {
                "id": "pm_opinion",
                "name": "产品经理并发评审",
                "type": "agent",
                "metricStage": "clarification",
                "agentId": f"{project_id}-vernal-review-pm",
                "maxAttempts": 2,
                "artifactOutputs": [
                    {"name": "opinion-pm.json", "path": "opinions/pm.json", "mediaType": "application/json", "required": True}
                ],
                "position": {"x": 80, "y": 40},
            },
            {
                "id": "arch_opinion",
                "name": "契约架构师并发评审",
                "type": "agent",
                "metricStage": "clarification",
                "agentId": f"{project_id}-vernal-review-arch",
                "maxAttempts": 2,
                "artifactOutputs": [
                    {"name": "opinion-arch.json", "path": "opinions/arch.json", "mediaType": "application/json", "required": True}
                ],
                "position": {"x": 80, "y": 160},
            },
            {
                "id": "qa_opinion",
                "name": "QA 总监并发评审",
                "type": "agent",
                "metricStage": "clarification",
                "agentId": f"{project_id}-vernal-review-qa",
                "maxAttempts": 2,
                "artifactOutputs": [
                    {"name": "opinion-qa.json", "path": "opinions/qa.json", "mediaType": "application/json", "required": True}
                ],
                "position": {"x": 80, "y": 280},
            },
            {
                "id": "director_plan",
                "name": "研发总监生成转向变更单",
                "type": "agent",
                "metricStage": "development",
                "agentId": f"{project_id}-vernal-review-director",
                "maxAttempts": 2,
                "artifactOutputs": [
                    {"name": "steering-plan.json", "path": "steering-plan.json", "mediaType": "application/json", "required": True},
                    {"name": "steering-summary.md", "path": "steering-summary.md", "mediaType": "text/markdown", "required": True},
                ],
                "position": {"x": 380, "y": 160},
            },
            {
                "id": "validate_plan",
                "name": "校验转向变更单格式",
                "type": "script",
                "metricStage": "testing",
                "scriptLanguage": "shell",
                "script": VALIDATE_STEERING_SHELL_SCRIPT,
                "rejectTo": "director_plan",
                "onFailure": "reject",
                "maxAttempts": 1,
                "maxReworks": 2,
                "artifactOutputs": [
                    {"name": "steering-plan.json", "path": "steering-plan.json", "mediaType": "application/json", "required": True},
                    {"name": "steering-summary.md", "path": "steering-summary.md", "mediaType": "text/markdown", "required": True},
                ],
                "position": {"x": 650, "y": 160},
            },
            {
                "id": "human_gate",
                "name": "拍板确认方向调整",
                "type": "approval",
                "metricStage": "clarification",
                "taskPrompt": "请审阅转向变更单（规矩升版 / 局部返工旧单 / 增删后续任务）：通过后将自动写入 LoopForge 全局规则并局部调整工单；如需改方向请填写意见并驳回。",
                "rejectTo": "director_plan",
                "onFailure": "reject",
                "maxAttempts": 1,
                "maxReworks": 5,
                "position": {"x": 910, "y": 160},
            },
        ],
        "edges": [
            {"id": "e-pm-dir", "source": "pm_opinion", "target": "director_plan", "when": "success"},
            {"id": "e-arch-dir", "source": "arch_opinion", "target": "director_plan", "when": "success"},
            {"id": "e-qa-dir", "source": "qa_opinion", "target": "director_plan", "when": "success"},
            {"id": "e-dir-val", "source": "director_plan", "target": "validate_plan", "when": "success"},
            {"id": "e-val-gate", "source": "validate_plan", "target": "human_gate", "when": "success"},
        ],
    }


def build_lane_workflows(project_id: str, sandbox_id: str = "local") -> dict[tuple[str, str], dict[str, Any]]:
    """在默认（backend）SOP 基础上派生 frontend / contract / bugfix 三条专项泳道，仅替换开发角色。"""
    base = build_vernal_workflows(project_id, sandbox_id=sandbox_id)
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for mode_key, wf in base.items():
        out[(mode_key, "backend")] = wf
        for lane, label in _LANE_LABELS.items():
            variant = copy.deepcopy(wf)
            variant["id"] = lane_workflow_id(project_id, mode_key, lane)
            variant["name"] = f"{wf['name']} · {label}泳道"
            variant["description"] = f"{wf['description']}（{label}专项：挂载对应开源 Skill）"
            for node in variant["nodes"]:
                if node.get("id") == "implement":
                    node["agentId"] = f"{project_id}-vernal-coder-{lane}"
            out[(mode_key, lane)] = variant
    return out


def candidate_upload_url(settings: Settings | None, project_id: str) -> str:
    """Stable 7-day bucketed signed URL so >48KB candidates spill to Vernal disk instead of MySQL TEXT."""
    if not settings or not project_id:
        return ""
    key = settings.project_signing_key()
    if not key:
        return ""
    import time
    from urllib.parse import quote
    from autodev import public_pool
    from autodev.loopforge.bridge import sign_context_sig

    bucket = 86400 * 7
    expiry = (int(time.time()) // bucket + 2) * bucket
    sig = sign_context_sig(key, project_id, f"candidate-upload:{expiry}")
    base = public_pool.context_url(settings).rstrip("/")
    return f"{base}/vernal-api/candidate-upload/{quote(project_id, safe='')}/{expiry}/{sig}"


async def ensure_local_git_workflow(client: LoopForgeClient, project_id: str, lane: str, *, settings: Settings | None = None) -> str:
    """复用对应泳道的角色、沙箱和评审门禁，交付二进制 Git 补丁。"""
    workflows = {w["id"]: w for w in await client.list_workflows(project_id)}
    template_id = lane_workflow_id(project_id, "files", lane)
    template = workflows.get(template_id, {})
    sandbox = template.get("sandboxProfileId") or "local"
    wf = build_lane_workflows(project_id, sandbox)[("files", lane)]
    wf["id"] = f"{project_id}-vernal-local-git-{lane}-sop"
    wf["name"] = f"本机 Git 分支交付 · {lane}"
    wf["description"] = "在独立副本中修改和审查代码，生成可校验的 Git 补丁。"
    implement, package, review = wf["nodes"]
    implement["taskPrompt"] = "先执行工单中的 Git 快照还原命令，在 vernal-source 目录内修改和测试。交付由下一节点打包。"
    review["taskPrompt"] = "进入 vernal-source 对照 refs/vernal/base 审查实际改动并复跑测试，将 review-report.md 写到运行根目录。"
    package["script"] = PACKAGE_PATCH
    package["artifactOutputs"] = [{"name": "vernal-patch.json", "path": "vernal-patch.json",
                                   "mediaType": "application/json", "required": True}]
    await _sync_workflow_template(client, wf, workflows, upload_url=candidate_upload_url(settings, project_id))
    return wf["id"]


async def ensure_builtin_assets(client: LoopForgeClient, project_id: str) -> dict[str, list[str]]:
    """幂等注册内置开源 Skill 与 MCP 目录（失败只降级，不阻断主流程）。"""
    done: dict[str, list[str]] = {"skills": [], "mcp": []}
    try:
        existing = {str(s.get("id")) for s in await client.list_skills(project_id)}
    except LoopForgeError:
        existing = set()
    for sk in load_builtin_skills():
        sid = skill_asset_id(project_id, sk["slug"])
        try:
            await client.put_skill(
                {
                    "id": sid,
                    "name": sk["name"],
                    "projectId": project_id,
                    "description": sk["description"],
                    "content": sk["content"],
                    "files": sk["files"],
                }
            )
            done["skills"].append(sid)
        except LoopForgeError:
            if sid in existing:
                done["skills"].append(sid)
    for mcp in builtin_mcp_templates():
        try:
            mid = f"{project_id}-{mcp['id']}"
            await client.put_mcp_server({**mcp, "id": mid, "projectId": project_id, "scope": "project"})
            done["mcp"].append(mid)
        except LoopForgeError:
            pass
    return done


def _upgrade_legacy_workflow(existing: dict[str, Any]) -> dict[str, Any] | None:
    """识别并升级旧版未改动的纯 Python 脚本节点（保留用户对其它节点、边或自定义脚本的手工定制）。"""
    nodes = existing.get("nodes")
    if not isinstance(nodes, list):
        return None
    updated = copy.deepcopy(existing)
    changed = False
    for node in updated["nodes"]:
        if not isinstance(node, dict) or node.get("type") != "script":
            continue
        lang = str(node.get("scriptLanguage") or "").strip().lower()
        script = str(node.get("script") or "").strip()
        if lang == "python" and node.get("id") == "package" and script == PACKAGE_FILES_SCRIPT.strip():
            node["scriptLanguage"] = "shell"
            node["script"] = PACKAGE_FILES_SHELL_SCRIPT
            changed = True
        elif lang == "python" and node.get("id") == "validate" and script == VALIDATE_PLAN_JSON_SCRIPT.strip():
            node["scriptLanguage"] = "shell"
            node["script"] = VALIDATE_PLAN_JSON_SHELL_SCRIPT
            changed = True
    return updated if changed else None


_NATIVE_METADATA = {"id", "projectId", "version", "revision", "publishedRevision", "status", "createdAt", "updatedAt"}


def _managed_digest(asset, fields):
    import hashlib, json
    def normalize(value):
        if isinstance(value, dict):
            normalized = {k: normalize(v) for k, v in value.items()}
            return {k: v for k, v in normalized.items() if v not in ("", None, [], {}, False)}
        if isinstance(value, list):
            return [normalize(v) for v in value]
        return value
    content = {k: asset.get(k) for k in fields}
    content["description"] = (content.get("description") or "").split("\n[vernal-managed-v", 1)[0]
    return hashlib.sha256(json.dumps(normalize(content), ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def managed_asset(asset):
    fields = sorted(set(asset) - _NATIVE_METADATA)
    sig = _managed_digest(asset, fields)
    return {**asset, "description": (asset.get("description") or "") + f"\n[vernal-managed-v2:{sig}:{','.join(fields)}]"}


def managed_clean(previous, desired):
    import re
    stamp = re.search(r"\n\[vernal-managed-v\d+:([a-f0-9]{64}):([\w,]+)\]$", previous.get("description") or "")
    # Unknown nonempty user-authored fields (e.g. newly enabled hooks) are customizations too.
    fields = set(stamp[2].split(",")) if stamp else set(desired) - _NATIVE_METADATA
    if any(previous[name] for name in set(previous) - fields - _NATIVE_METADATA):
        return False
    if stamp:
        return _managed_digest(previous, fields) == stamp[1]
    return _managed_digest(previous, fields) == _managed_digest(desired, fields)


async def _sync_workflow_template(
    client: LoopForgeClient,
    wf: dict[str, Any],
    existing_wfs: dict[str, dict[str, Any]],
    *,
    upload_url: str = "",
) -> bool:
    """幂等同步单个 SOP：不存在则新建并发布；已存在则仅升级未改动的旧版 Python 脚本节点，保留用户手工定制。"""
    wid = wf["id"]
    from autodev.context_bundle import attach_native_review_context
    legacy_template = wf
    wf = managed_asset(attach_native_review_context(wf, upload_url=upload_url))
    if wid not in existing_wfs:
        saved = await client.put_workflow(wf)
        if saved.get("status") != "published":
            await client.publish_workflow(wid)
        return True
    prev = existing_wfs[wid]
    if "vernal-managed-v" in (prev.get("description") or ""):
        if managed_clean(prev, wf) and prev.get("description") != wf["description"]:
            await client.put_workflow({**wf, "version": prev.get("version", 1), "revision": prev.get("revision", 0)})
            await client.publish_workflow(wid)
            return True
        return False
    upgraded = _upgrade_legacy_workflow(prev)
    if upgraded is None and (managed_clean(prev, legacy_template) or managed_clean(prev, wf)):
        upgraded = wf
    if upgraded is not None:
        updated = {**upgraded, "revision": prev.get("revision", 0)}
        if "sandboxProfileId" in wf:
            updated["sandboxProfileId"] = wf["sandboxProfileId"]
        await client.put_workflow(updated)
        await client.publish_workflow(wid)
        return True
    if prev.get("status") != "published":
        await client.publish_workflow(wid)
    return False


async def ensure_vernal_loopforge_assets(
    client: LoopForgeClient,
    project_id: str,
    settings: Settings,
) -> dict[str, Any]:
    """在指定 LoopForge 项目内幂等创建/更新 Vernal 规则、角色与标准交付 SOP，并返回推荐绑定的 workflow_id。"""
    sandboxes = await client.list_sandboxes()
    sandbox_id = "local"
    if sandboxes:
        ids = [str(s.get("id") or "") for s in sandboxes if s.get("id")]
        if "local" in ids:
            sandbox_id = "local"
        elif ids:
            sandbox_id = ids[0]

    # 1. 规则（幂等 PUT：领地门禁规则 + 评审生效规矩全局规则）
    rule = build_vernal_rule(project_id)
    await client.put_rule(rule)
    all_target_wfs = [lane_workflow_id(project_id, mk, ln) for mk in ("files", "git") for ln in LANES] + [review_workflow_id(project_id)]
    dec_rule_id = decisions_rule_id(project_id)
    raw_rules = await client._req("GET", "/api/rules", params={"projectId": project_id})
    decision = next((r for r in raw_rules if isinstance(r, dict) and r.get("id") == dec_rule_id), None)
    seed = build_decisions_rule(project_id, [], version=1)
    seed["global"] = False
    # Roles require the rule to exist, while a workflow-scoped rule requires published SOPs.
    if decision is None:
        saved = await client.put_rule(seed)
        decision = {**seed, **(saved or {})}
    bind_decision = (not decision.get("workflowIds") and not decision.get("global")
                     and decision.get("content") == seed["content"])

    # 2. 内置开源 Skill / MCP（先于角色注册，角色才能引用）
    builtin = await ensure_builtin_assets(client, project_id)

    # 3. 角色（保留已有 version 避免覆盖冲突）
    existing_agents = {str(a.get("id")): a for a in await client.list_agents(project_id)}
    agent_ids: list[str] = []
    registered = set(builtin["skills"])
    default_runner_id = ""
    settings.runner_harness = ""
    try:
        runners = await client.list_runners()
    except LoopForgeError:
        runners = []
    online = [r for r in runners if isinstance(r, dict) and r.get("status") == "online" and r.get("id")]
    public_route = settings.selected_pool == "public_docker" or settings.execution_pool == "public_docker"
    if public_route:
        from autodev import public_pool
        state = public_pool.status()
        settings.runner_harness = choose_preferred_harness_from_runtimes(state["runtimes"], settings)
        # Bind the generated workflows to an explicit Docker driver and our checked image.
        import hashlib
        import re
        image = state["sandbox_image"]
        if not re.fullmatch(r"[a-zA-Z0-9./:@_-]+", image):
            raise LoopForgeError("公用沙箱镜像名称无效")
        import time
        from autodev.loopforge.bridge import sign_context_sig
        if not re.fullmatch(r"[A-Za-z0-9._-]+", project_id):
            raise LoopForgeError("非法原生项目 ID")
        expiry = (int(time.time()) // 86400 + 7) * 86400
        proof = sign_context_sig(settings.project_signing_key(), project_id, f"model-sandbox:{expiry}")
        profile_source = (f"FROM {image}\nENTRYPOINT []\nENV VERNAL_NATIVE_PROJECT={project_id} VERNAL_NATIVE_EXPIRY={expiry} "
                          f"VERNAL_NATIVE_PROOF={proof}\n")
        image_tag = hashlib.sha256(profile_source.encode()).hexdigest()[:10]
        dockerfile_id = f"{project_id}-vernal-image-{image_tag}"
        sandbox_id = f"{project_id}-vernal-docker-{image_tag}"
        existing = next((row for row in sandboxes if row.get("id") == sandbox_id), {})
        if existing.get("driver") != "docker" or existing.get("dockerfileId") != dockerfile_id:
            await client._req("PUT", "/api/dockerfiles", json_body={
                "id": dockerfile_id, "projectId": project_id, "name": "Vernal 公用沙箱镜像",
                "content": profile_source,
            })
            await client._req("PUT", "/api/sandboxes", json_body={
                "id": sandbox_id, "projectId": project_id, "name": "Vernal 公用 Docker",
                "driver": "docker", "dockerfileId": dockerfile_id,
            })
            verified = next((row for row in await client.list_sandboxes() if row.get("id") == sandbox_id), {})
            if verified.get("driver") != "docker" or verified.get("dockerfileId") != dockerfile_id:
                raise LoopForgeError("LoopForge 尚未确认公用 Docker 沙箱配置")
    elif online:
        from autodev.public_pool import select_runner
        try:
            user_info = await client.auth_current()
        except LoopForgeError:
            user_info = {}
        runner, runtimes = select_runner(settings, online, user_info)
        if runner:
            default_runner_id = str(runner["id"])
            if not runner.get("isDefault"):
                await client.set_default_runner(default_runner_id)
        else:
            runtimes, _ = merge_runner_runtimes({}, settings, runners=online, user_info=user_info, include_pools=False)
        if settings.harness == "auto":
            settings.runner_harness = choose_preferred_harness_from_runtimes(runtimes, settings)
    for agent in build_vernal_agents(project_id, settings):
        agent["skills"] = [s for s in agent.get("skills", []) if s in registered]
        prev = existing_agents.get(agent["id"])
        import json, os
        role = agent["id"].removeprefix(project_id + "-vernal-")
        configured = json.loads(os.environ.get("VERNAL_ROLE_MCP") or "{}").get(role, [])
        agent["mcpServers"] = [mid for mid in builtin["mcp"] if mid in configured or mid.removeprefix(project_id + "-") in configured]
        desired = managed_asset(agent)
        legacy = {**agent, "mcpServers": []}
        if role.startswith("planner"):
            legacy["skills"] = []
        if role == "reviewer":
            legacy["prompt"] = agent["prompt"].split("\n审查前先读取运行根目录", 1)[0]
        if not prev or (prev.get("description") != desired["description"] and
                        (managed_clean(prev, desired) or managed_clean(prev, legacy))):
            await client.put_agent({**desired, "version": (prev or {}).get("version") or 1})
        agent_ids.append(agent["id"])

    # 4. 标准 SOP 与专项泳道（新建并发布；已存在时仅自动升级未改动的旧版 Python 节点，保留用户手工定制）
    existing_wfs = {str(w.get("id")): w for w in await client.list_workflows(project_id)}
    templates = build_lane_workflows(project_id, sandbox_id=sandbox_id)
    workflow_ids: dict[str, str] = {}
    lane_workflows: dict[str, str] = {}
    upload_url = candidate_upload_url(settings, project_id)
    for (mode_key, lane), wf in templates.items():
        wid = wf["id"]
        await _sync_workflow_template(client, wf, existing_wfs, upload_url=upload_url)
        lane_workflows[f"{mode_key}:{lane}"] = wid
        if lane == "backend":
            workflow_ids[mode_key] = wid

    # 4b. 顶层规划与圆桌按角色分流 SOP（director / pm / qa_lead，确保多角色异构模型真实生效）
    plan_workflows: dict[str, str] = {}
    for prole, pwf in build_plan_workflows(project_id, sandbox_id=sandbox_id).items():
        pwid = pwf["id"]
        await _sync_workflow_template(client, pwf, existing_wfs)
        plan_workflows[prole] = pwid
    plan_wf_id = plan_workflows["director"]

    # 4c. 原生并发「进展评审与转向 SOP」（支持立项与运行中随时拉会讨论、原生 approval 拍板与驳回重议）
    rev_wf = build_review_workflow(project_id, sandbox_id=sandbox_id)
    rev_wf_id = rev_wf["id"]
    await _sync_workflow_template(client, rev_wf, existing_wfs)
    if bind_decision:
        await client.put_rule({**decision, "global": True, "workflowIds": all_target_wfs})

    preferred_mode = "files" if settings.collaboration_mode == "files" else "git"
    selected_wf = workflow_ids[preferred_mode]
    # 若当前未显式指定 workflow_id，或之前绑定的是另一模式的默认 Vernal SOP，则自动对齐当前协作模式
    if not settings.loopforge_workflow_id or settings.loopforge_workflow_id in workflow_ids.values():
        settings.loopforge_workflow_id = selected_wf

    return {
        "project_id": project_id,
        "sandbox_id": sandbox_id,
        "rule_id": rule["id"],
        "agent_ids": agent_ids,
        "workflow_ids": workflow_ids,
        "lane_workflows": lane_workflows,
        "plan_workflow_id": plan_wf_id,
        "plan_workflows": plan_workflows,
        "review_workflow_id": rev_wf_id,
        "decisions_rule_id": dec_rule_id,
        "builtin_skills": builtin["skills"],
        "builtin_mcp": builtin["mcp"],
        "selected_workflow_id": settings.loopforge_workflow_id,
        "default_runner_id": default_runner_id,
    }


async def ensure_signoff_workflow(client, project_id):
    wid = project_id + "-vernal-signoff"
    existing = {w["id"]:w for w in await client.list_workflows(project_id)}
    await _sync_workflow_template(client, {
        "id":wid, "projectId":project_id,"name":"候选代码交付签字","version":1,"status":"published",
        "sandboxProfileId":"local",
        "description":"对同一工单开发 SOP 的固定候选与质量证据进行人工签字。",
        "nodes":[{"id":"signoff","name":"指定审批人签字","type":"approval",
                  "taskPrompt":"核对上游开发 SOP 的交付候选和审查报告，作出批准或驳回决定。"}], "edges":[]
    }, existing)
