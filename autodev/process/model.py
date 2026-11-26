"""流程阶段与门禁模型定义（WP-0）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

GateKind = Literal["human_approval", "ai_review", "command", "quality", "ci", "trace_coverage"]

StageId = Literal[
    "requirements",
    "architecture",
    "detailed_design",
    "implementation",
    "code_review",
    "testing",
    "integration_delivery",
]

StageState = Literal["NOT_STARTED", "IN_PROGRESS", "WAITING_GATE", "PASSED", "FAILED", "SKIPPED"]


class GateDef(BaseModel):
    id: str
    name: str
    kind: GateKind
    scope: Literal["project", "milestone", "task"] = "project"
    blocking: bool = True
    approver_roles: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)


class StageDef(BaseModel):
    id: StageId
    name: str
    enabled: bool = True
    required_artifacts: list[str] = Field(default_factory=list)
    gates: list[GateDef] = Field(default_factory=list)


class ProcessTemplate(BaseModel):
    id: str
    name: str
    description: str
    stages: list[StageDef]
    review_policy: Literal["off", "milestone", "risk", "all"] = "off"
    review_checklist: list[str] = Field(default_factory=list)
    quality: dict[str, float] = Field(default_factory=dict)
    deliver: dict[str, Any] = Field(default_factory=dict)


class StageView(BaseModel):
    id: StageId
    name: str
    enabled: bool
    status: StageState
    pending_count: int = 0
    required_artifacts: list[dict[str, Any]] = Field(default_factory=list)
    gates: list[dict[str, Any]] = Field(default_factory=list)
