"""领域模型：多角色 / 多研发组 / 滚动规划契约与严格 JSON Schema 生成器。"""

from __future__ import annotations

import copy
import json
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class TaskStatus(StrEnum):
    BLOCKED = "BLOCKED"
    READY = "READY"
    IN_PROGRESS = "IN_PROGRESS"
    VERIFYING = "VERIFYING"
    REVIEWING = "REVIEWING"
    APPROVED = "APPROVED"
    FUSED = "FUSED"
    CANCELLED = "CANCELLED"
    MERGED = "MERGED"


ACTIVE_TASK_STATUSES = {TaskStatus.IN_PROGRESS, TaskStatus.VERIFYING, TaskStatus.REVIEWING}


class MilestoneStatus(StrEnum):
    PENDING = "PENDING"
    INTEGRATING = "INTEGRATING"
    AWAITING_MERGE = "AWAITING_MERGE"
    MERGED = "MERGED"
    FAILED = "FAILED"


class ProjectStatus(StrEnum):
    CREATED = "CREATED"
    INGESTED = "INGESTED"
    SPECIFIED = "SPECIFIED"      # PM 已完成产品规格补全
    PLANNED = "PLANNED"          # 立项蓝图 / 规划已完成，等待人工确认
    APPROVED = "APPROVED"        # 已批准，可自动滚动运行
    RUNNING = "RUNNING"
    STALLED = "STALLED"
    COMPLETED = "COMPLETED"


TaskRole = Literal["bootstrap", "contract", "coder", "integrator", "tester"]

ROLE_LABELS: dict[str, str] = {
    "pm": "产品经理 · PM",
    "architect": "契约与异构架构师 · Architect",
    "qa_lead": "QA 质量总监 · QA Lead",
    "director": "研发总监 · Director",
    "planner": "架构规划 · Planner",
    "bootstrap": "基线搭建 · Bootstrap",
    "contract": "契约架构 · Contract",
    "coder": "模块开发 · Coder",
    "integrator": "网关装配 · Integrator",
    "tester": "系统测试 · Tester",
    "reviewer": "独立审查 · Reviewer",
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------- 1. 多智能体架构评审圆桌（Architecture Council） ----------

class CouncilOpinion(_Strict):
    headline: str
    proposals: list[str]
    concerns: list[str]
    questions: list[str]
    recommended_sandboxes: list[str]


class CouncilADR(_Strict):
    adr_id: str
    title: str
    decision: str
    rationale: str
    affected_squads: list[str]


class CouncilConsensus(_Strict):
    executive_summary: str
    resolved_debates: list[str]
    open_risks: list[str]
    contract_strategy: str
    adrs: list[CouncilADR]
    spec_markdown: str


# ---------- 1B. 产品经理输出：PRD 规格补全 ----------

class ProductSpec(_Strict):
    product_vision: str
    core_workflows: list[str]
    edge_cases: list[str]
    error_conventions: list[str]
    global_acceptance_redlines: list[str]
    spec_markdown: str


# ---------- 2. 经典单次规划 & 原子任务定义 ----------

class PlanModule(_Strict):
    name: str
    path: str
    description: str


class PlanMilestone(_Strict):
    milestone_id: str
    title: str
    goal: str
    integration_cmd: str


class PlanTask(_Strict):
    task_id: str
    milestone_id: str
    squad: str = ""
    module: str
    role: TaskRole
    title: str
    description: str
    acceptance_criteria: list[str]
    depends_on: list[str]
    doc_slice_ids: list[str]
    allowed_paths: list[str]
    verify_cmds: list[str]
    priority: int


class ProjectPlan(_Strict):
    summary: str
    tech_stack: str
    agents_md: str
    modules: list[PlanModule]
    milestones: list[PlanMilestone]
    tasks: list[PlanTask]


# ---------- 3. 研发总监输出：立项蓝图（分组 + 路线图 + 首轮任务） ----------

class SquadSpec(_Strict):
    squad_id: str
    name: str
    mission: str
    owned_paths: list[str]
    roles: list[str]
    sandbox_profile: str = "generic-docker"


class BlueprintMilestone(_Strict):
    milestone_id: str
    title: str
    goal: str
    target_squads: list[str]
    key_deliverables: list[str]
    integration_cmd: str


class ProjectBlueprint(_Strict):
    summary: str
    tech_stack: str
    agents_md: str
    squads: list[SquadSpec]
    milestones: list[BlueprintMilestone]
    initial_tasks: list[PlanTask]


# ---------- 4. 研发总监输出：下一里程碑滚动拆单 ----------

class MilestoneWavePlan(_Strict):
    milestone_id: str
    wave_notes: str
    tasks: list[PlanTask]


# ---------- 5. 研发总监输出：熔断 / 契约变更自动仲裁 ----------

class ContractPatchSpec(_Strict):
    title: str
    description: str
    allowed_paths: list[str]
    verify_cmds: list[str]
    acceptance_criteria: list[str]


class DirectorDecision(_Strict):
    action: Literal["STEER_RETRY", "PATCH_CONTRACT", "ESCALATE_HUMAN"]
    diagnosis: str
    steer_note: str
    contract_patch: ContractPatchSpec


# ---------- 6. 编码汇报与独立审查结论 ----------

class WorkerReport(_Strict):
    status: Literal["DONE", "NEED_CONTRACT_UPDATE", "BLOCKED_BY_SPEC"]
    summary: str
    files_changed: list[str]
    tests_added: list[str]
    notes: str


class ReviewIssue(_Strict):
    file: str
    line: int
    problem: str
    fix: str


class CriterionCheck(_Strict):
    criterion: str
    met: bool
    evidence: str


class ReviewVerdict(_Strict):
    verdict: Literal["PASS", "NEEDS_FIX", "ESCALATE_CONTRACT"]
    summary: str
    criteria_check: list[CriterionCheck]
    blocking_issues: list[ReviewIssue]


SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "council_opinion": CouncilOpinion,
    "council_consensus": CouncilConsensus,
    "prd_spec": ProductSpec,
    "project_plan": ProjectPlan,
    "project_blueprint": ProjectBlueprint,
    "milestone_wave": MilestoneWavePlan,
    "director_decision": DirectorDecision,
    "worker_report": WorkerReport,
    "review_verdict": ReviewVerdict,
}


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """生成扁平、严格的 JSON Schema：内联 $ref，所有对象 additionalProperties=false 且全部字段 required。"""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, list):
            return [resolve(n) for n in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            name = node["$ref"].rsplit("/", 1)[-1]
            return resolve(copy.deepcopy(defs[name]))
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in ("title", "default"):
                continue
            if key == "properties":
                out[key] = {pk: resolve(pv) for pk, pv in value.items()}
            else:
                out[key] = resolve(value)
        if out.get("type") == "object" and "properties" in out:
            out["additionalProperties"] = False
            out["required"] = list(out["properties"].keys())
        return out

    return resolve(schema)


def write_schemas(directory: Path) -> dict[str, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for name, model in SCHEMA_MODELS.items():
        path = directory / f"{name}.schema.json"
        path.write_text(json.dumps(strict_json_schema(model), ensure_ascii=False, indent=2), "utf-8")
        paths[name] = path
    return paths
