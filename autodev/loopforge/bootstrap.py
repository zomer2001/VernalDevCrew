"""Asset-as-Code：向 LoopForge 毛坯底座幂等注入 Vernal 标准研发角色、编码规则与交付 SOP。"""

from __future__ import annotations

import copy
from typing import Any

from autodev.assets import builtin_mcp_templates, load_builtin_skills
from autodev.config import Settings
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

SKIP = {"__pycache__", ".pytest_cache", ".DS_Store", "node_modules", ".git", ".autodev", ".venv", "vernal-files.json", "review-report.md"}
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

VALIDATE_PLAN_JSON_SCRIPT = """\
import json, pathlib, sys

root = pathlib.Path(".").resolve()
out_path = root / "vernal-result.json"
if not out_path.is_file():
    raise SystemExit("缺少 vernal-result.json：请将满足 JSON Schema 的唯一 JSON 对象写入当前目录的 vernal-result.json")

raw = out_path.read_text("utf-8").strip()
try:
    data = json.loads(raw)
except Exception as exc:
    raise SystemExit(f"vernal-result.json 不是合法 JSON：{exc}")

if not isinstance(data, dict) or not data:
    raise SystemExit("vernal-result.json 必须是非空 JSON 对象（object）")

schema_path = root / ".vernal-schema.json"
if schema_path.is_file():
    try:
        schema = json.loads(schema_path.read_text("utf-8"))
        req = schema.get("required") or []
        missing = [k for k in req if k not in data]
        if missing:
            raise SystemExit(f"vernal-result.json 缺少必填字段：{', '.join(missing)}")
    except SystemExit:
        raise
    except Exception:
        pass

out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
print(f"vernal-result.json 校验通过（{len(data)} 个顶层字段）")
"""


PACKAGE_FILES_NODE_SCRIPT = """\
const fs = require("fs");
const path = require("path");
const SKIP = new Set(["__pycache__", ".pytest_cache", ".DS_Store", "node_modules", ".git", ".autodev", ".venv", "vernal-files.json", "review-report.md"]);
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

VALIDATE_PLAN_JSON_NODE_SCRIPT = """\
const fs = require("fs");
if (!fs.existsSync("vernal-result.json")) {
  console.error("缺少 vernal-result.json：请将满足 JSON Schema 的唯一 JSON 对象写入当前目录的 vernal-result.json");
  process.exit(1);
}
let data;
try { data = JSON.parse(fs.readFileSync("vernal-result.json", "utf8").trim()); } catch (e) {
  console.error(`vernal-result.json 不是合法 JSON：${e.message}`);
  process.exit(1);
}
if (!data || typeof data !== "object" || Array.isArray(data) || !Object.keys(data).length) {
  console.error("vernal-result.json 必须是非空 JSON 对象（object）");
  process.exit(1);
}
if (fs.existsSync(".vernal-schema.json")) {
  try {
    const schema = JSON.parse(fs.readFileSync(".vernal-schema.json", "utf8"));
    const missing = (schema.required || []).filter((k) => !(k in data));
    if (missing.length) {
      console.error(`vernal-result.json 缺少必填字段：${missing.join(", ")}`);
      process.exit(1);
    }
  } catch {}
}
fs.writeFileSync("vernal-result.json", JSON.stringify(data, null, 2), "utf8");
console.log(`vernal-result.json 校验通过（${Object.keys(data).length} 个顶层字段）`);
"""


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


def build_vernal_agents(project_id: str, settings: Settings) -> list[dict[str, Any]]:
    coder_exec = _executor_id_for(settings.harness_for("coder"))
    qa_exec = _executor_id_for(settings.harness_for("reviewer"))
    coder_model = settings.model_for("coder")
    qa_model = settings.model_for("reviewer")
    rule_id = f"{project_id}-vernal-guard-rule"

    coder: dict[str, Any] = {
        "id": f"{project_id}-vernal-coder",
        "projectId": project_id,
        "name": "Vernal 模块开发工程师",
        "version": 1,
        "description": "负责按架构蓝图、契约与目录领地独立完成子任务实现及自测",
        "executorId": coder_exec,
        "model": coder_model,
        "effort": "high" if coder_exec == "codex" else "",
        "rules": [rule_id],
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
            "5. 结束回合前，若环境中存在 `submit_gate_result` MCP 工具，必须调用一次 `submit_gate_result`（参数 `decision=\"pass\"`, `reason=\"编码与自测通过\"`）提交节点结论。"
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
        "rules": [rule_id],
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
            "3. 将审查结论写入 `review-report.md`（首行写 `# PASS` 或 `# REJECT`，并附上核验依据）。\n"
            "4. 结束回合前，必须调用一次 `submit_gate_result` MCP 工具：若通过传 `decision=\"pass\"`, `reason=\"契约与验收标准全部通过\"`；若不满足验收标准传 `decision=\"rework\"`, `reason=\"...\"`, `actionRequired=\"具体修复项\"`。"
        ),
    }
    available = {s["slug"] for s in load_builtin_skills()}

    def _skills(*slugs: str) -> list[str]:
        return [skill_asset_id(project_id, s) for s in slugs if s in available]

    coder["skills"] = _skills("test-driven-development", "systematic-debugging")
    reviewer["skills"] = _skills("security-review")

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
            "skills": [],
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
                "4. 结束回合前，若环境中存在 `submit_gate_result` MCP 工具，请调用一次 `submit_gate_result`（参数 `decision=\"pass\"`, `reason=\"已产出合法的 vernal-result.json\"`）。"
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
    return [coder, reviewer, frontend, contract, bugfix, planner, planner_pm, planner_qa]


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


async def _sync_workflow_template(
    client: LoopForgeClient,
    wf: dict[str, Any],
    existing_wfs: dict[str, dict[str, Any]],
) -> bool:
    """幂等同步单个 SOP：不存在则新建并发布；已存在则仅升级未改动的旧版 Python 脚本节点，保留用户手工定制。"""
    wid = wf["id"]
    if wid not in existing_wfs:
        saved = await client.put_workflow(wf)
        if saved.get("status") != "published":
            await client.publish_workflow(wid)
        return True
    prev = existing_wfs[wid]
    upgraded = _upgrade_legacy_workflow(prev)
    if upgraded is not None:
        await client.put_workflow(upgraded)
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

    # 1. 规则（幂等 PUT）
    rule = build_vernal_rule(project_id)
    await client.put_rule(rule)

    # 2. 内置开源 Skill / MCP（先于角色注册，角色才能引用）
    builtin = await ensure_builtin_assets(client, project_id)

    # 3. 角色（保留已有 version 避免覆盖冲突）
    existing_agents = {str(a.get("id")): a for a in await client.list_agents(project_id)}
    agent_ids: list[str] = []
    registered = set(builtin["skills"])
    for agent in build_vernal_agents(project_id, settings):
        agent["skills"] = [s for s in agent.get("skills", []) if s in registered]
        prev = existing_agents.get(agent["id"])
        if prev and prev.get("version"):
            agent["version"] = int(prev["version"])
        await client.put_agent(agent)
        agent_ids.append(agent["id"])

    # 4. 标准 SOP 与专项泳道（新建并发布；已存在时仅自动升级未改动的旧版 Python 节点，保留用户手工定制）
    existing_wfs = {str(w.get("id")): w for w in await client.list_workflows(project_id)}
    templates = build_lane_workflows(project_id, sandbox_id=sandbox_id)
    workflow_ids: dict[str, str] = {}
    lane_workflows: dict[str, str] = {}
    for (mode_key, lane), wf in templates.items():
        wid = wf["id"]
        await _sync_workflow_template(client, wf, existing_wfs)
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

    preferred_mode = "files" if settings.collaboration_mode == "files" else "git"
    selected_wf = workflow_ids[preferred_mode]
    # 若当前未显式指定 workflow_id，或之前绑定的是另一模式的默认 Vernal SOP，则自动对齐当前协作模式
    if not settings.loopforge_workflow_id or settings.loopforge_workflow_id in workflow_ids.values():
        settings.loopforge_workflow_id = selected_wf

    # 若当前用户已有在线执行机，自动设为默认执行机，确保 Codex CLI 任务直接路由到本机
    default_runner_id = ""
    try:
        runners = await client.list_runners()
        online = [r for r in runners if isinstance(r, dict) and r.get("status") == "online" and r.get("id")]
        if online:
            default_runner_id = str(online[0]["id"])
            await client.set_default_runner(default_runner_id)
    except Exception:
        pass

    return {
        "project_id": project_id,
        "sandbox_id": sandbox_id,
        "rule_id": rule["id"],
        "agent_ids": agent_ids,
        "workflow_ids": workflow_ids,
        "lane_workflows": lane_workflows,
        "plan_workflow_id": plan_wf_id,
        "plan_workflows": plan_workflows,
        "builtin_skills": builtin["skills"],
        "builtin_mcp": builtin["mcp"],
        "selected_workflow_id": settings.loopforge_workflow_id,
        "default_runner_id": default_runner_id,
    }
