"""领域模型：多角色 / 多研发组 / 滚动规划契约与严格 JSON Schema 生成器。"""

from __future__ import annotations

import copy
import json
import re
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TaskStatus(StrEnum):
    BLOCKED = "BLOCKED"
    READY = "READY"
    IN_PROGRESS = "IN_PROGRESS"
    VERIFYING = "VERIFYING"
    REVIEWING = "REVIEWING"
    AWAITING_HUMAN_REVIEW = "AWAITING_HUMAN_REVIEW"
    APPROVED = "APPROVED"
    FUSED = "FUSED"
    CANCELLED = "CANCELLED"
    MERGED = "MERGED"


ACTIVE_TASK_STATUSES = {TaskStatus.IN_PROGRESS, TaskStatus.VERIFYING, TaskStatus.REVIEWING}


class MilestoneStatus(StrEnum):
    PENDING = "PENDING"
    INTEGRATING = "INTEGRATING"
    CI_RUNNING = "CI_RUNNING"
    PUBLISHING = "PUBLISHING"
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


class HandoffBrief(_Strict):
    delivered: str
    integration: str
    boundaries: str
    verification: str

    @model_validator(mode="after")
    def complete_brief(self):
        if any(not value.strip() for value in self.model_dump().values()):
            raise ValueError("交接便签的四个部分均须填写；未执行验证应明确说明")
        return self


class ClarificationItem(_Strict):
    question_key: str = Field(pattern=r"^Q-[0-9]{2,}$", description="稳定问题编号，例如 Q-01、Q-02；新问题不得复用已有编号")
    category: Literal["scope", "interface", "acceptance", "constraint", "non_goal"] = "scope"
    severity: Literal["BLOCKING", "DEFAULTED"] = "BLOCKING"
    question: str = Field(min_length=1, pattern=r"\S")
    options: list[str] = []
    default_option: str = ""
    rationale: str = ""

    @model_validator(mode="after")
    def valid_question(self):
        if not re.fullmatch(r"Q-[0-9]{2,}", self.question_key) or not self.question.strip():
            raise ValueError("澄清问题必须具有稳定编号和非空问题")
        if len(set(self.options)) != len(self.options) or any(not s.strip() for s in self.options):
            raise ValueError("澄清选项不得重复或为空")
        if self.default_option and self.default_option not in self.options:
            raise ValueError("默认选项必须属于选项列表")
        if self.severity == "DEFAULTED" and (not self.default_option or not self.rationale.strip()):
            raise ValueError("低风险默认选择必须有明确选项和依据")
        return self


class SpecFinding(_Strict):
    severity: Literal["CRITICAL", "WARNING", "INFO"]
    message: str
    requirement_ids: list[str] = []


class SpecStepResult(_Strict):
    step: Literal["constitution", "specify", "clarify", "checklist", "plan", "tasks", "analyze"]
    content: str
    clarifications: list[ClarificationItem] = []
    findings: list[SpecFinding] = []
    handoff_brief: HandoffBrief


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
    requirements: list[RequirementItem] = []
    nfrs: list[RequirementItem] = []


# ---------- 1B. 产品经理输出：PRD / SRS 规格补全 ----------

class RequirementItem(_Strict):
    req_id: str
    title: str
    statement: str
    priority: Literal["P0", "P1", "P2"] = "P1"
    acceptance: list[str] = []
    feature_id: str = ""

    @model_validator(mode="after")
    def validate_identity(self):
        if not self.req_id.strip() or self.req_id != self.req_id.strip():
            raise ValueError("需求编号不得为空或带首尾空格")
        if self.feature_id and (not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", self.feature_id) or ":" in self.req_id):
            raise ValueError("feature_id 必须是单层范围标识，req_id 不得重复携带范围")
        return self

    @property
    def identity(self) -> str:
        return f"{self.feature_id}:{self.req_id}" if self.feature_id else self.req_id


class ProductSpec(_Strict):
    product_vision: str
    core_workflows: list[str]
    edge_cases: list[str]
    error_conventions: list[str]
    global_acceptance_redlines: list[str]
    spec_markdown: str
    requirements: list[RequirementItem] = []
    nfrs: list[RequirementItem] = []

    @model_validator(mode="after")
    def unique_requirement_ids(self):
        seen = set()
        for item in self.requirements + self.nfrs:
            if not item.req_id.strip() or item.identity in seen:
                raise ValueError(f"需求编号为空或重复：{item.identity}")
            seen.add(item.identity)
        return self


# ---------- 1C. 架构师 / 研发组输出：详细设计规格（Detailed Design） ----------

class DesignInterface(_Strict):
    name: str
    signature: str
    inputs: str
    outputs: str
    errors: list[str]
    req_ids: list[str]


class DesignTestCase(_Strict):
    tc_id: str
    req_ids: list[str]
    kind: Literal["unit", "integration", "e2e"]
    description: str


class DetailedDesign(_Strict):
    squad_id: str
    milestone_id: str
    responsibilities: list[str]
    interfaces: list[DesignInterface]
    data_models: list[str]
    sequences_mermaid: list[str]
    error_handling: list[str]
    test_cases: list[DesignTestCase]
    design_markdown: str


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
    requirement_ids: list[str] = []
    design_refs: list[str] = []


class SpecTaskDraft(_Strict):
    task_key: str
    package_key: str
    milestone_id: str = "M1"
    squad: str = "core"
    module: str
    role: TaskRole = "coder"
    title: str
    description: str
    acceptance_criteria: list[str]
    requirement_ids: list[str]
    depends_on: list[str] = []
    allowed_paths: list[str]
    verify_cmds: list[str]


class CompiledWorkPackage(_Strict):
    task_key: str
    source_task_keys: list[str]
    feature_id: str
    spec_rev_id: int
    spec_sha: str
    constitution_sha: str
    plan_sha: str
    tasks_sha: str
    contract_sha: str
    asset_versions: dict[str, str]
    task: PlanTask
    compiled_task_hash: str = ""


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
    handoff_brief: HandoffBrief | None = None


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
    handoff_brief: HandoffBrief | None = None


SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "council_opinion": CouncilOpinion,
    "council_consensus": CouncilConsensus,
    "prd_spec": ProductSpec,
    "detailed_design": DetailedDesign,
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
