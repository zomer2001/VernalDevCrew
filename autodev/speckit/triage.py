"""Semantic intake assessment using the existing LoopForge planning SOP."""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from autodev.models import ClarificationItem, HandoffBrief
from . import assets, service


class Prerequisite(BaseModel):
    key: str = Field(pattern=r"^P[0-9]{2,}$")
    description: str = Field(min_length=1)
    evidence_needed: str = Field(min_length=1)


class ExperienceItem(BaseModel):
    description: str = Field(min_length=1)
    core_acceptance: bool = False


class Assessment(BaseModel):
    goal_clear: bool
    scope_clear: bool
    acceptance_clear: bool
    local_impact: bool
    change_type: Literal["new_system", "feature", "bugfix", "refactor", "documentation"]
    risk: Literal["low", "medium", "high"]
    rationale: str = Field(min_length=1)
    questions: list[ClarificationItem] = Field(default_factory=list, max_length=3)
    prerequisites: list[Prerequisite] = Field(default_factory=list, max_length=10)
    experience_items: list[ExperienceItem] = Field(default_factory=list, max_length=10)
    proposed_spec: str = ""
    handoff_brief: HandoffBrief

    @model_validator(mode="after")
    def validate_commitments(self):
        blocking = any(q.severity == "BLOCKING" for q in self.questions)
        if not all((self.goal_clear, self.scope_clear, self.acceptance_clear)) and not blocking:
            raise ValueError("交付承诺仍不清楚时必须提供待确认问题")
        for q in self.questions:
            if q.severity == "DEFAULTED" and q.category in ("scope", "acceptance", "non_goal"):
                raise ValueError("范围与验收承诺不能作为低风险默认值")
        if any(x.core_acceptance for x in self.experience_items):
            raise ValueError("核心验收不能降级为交付后人工体验项")
        if len({p.key for p in self.prerequisites}) != len(self.prerequisites):
            raise ValueError("前置条件编号不能重复")
        return self

    @property
    def clarity_level(self) -> str:
        if any(q.severity == "BLOCKING" for q in self.questions):
            return "NEEDS_CLARIFICATION"
        return "READY_LITE" if self.local_impact and self.risk == "low" else "READY_STANDARD"


class StandardSpecBundle(Assessment):
    constitution_markdown: str = Field(pattern=r"\S")
    proposed_spec: str = Field(pattern=r"\S")


def source_origin(engine) -> str:
    mode = engine.db.meta_get("source_mode") or "scratch"
    return {"scratch": "greenfield", "local_folder": "existing_local", "import_folder": "existing_local",
            "host_link": "existing_local", "git_repo": "existing_git"}.get(mode, "existing_local")


def recommendation(assessment: Assessment, origin: str) -> dict:
    lite = assessment.clarity_level == "READY_LITE"
    return {"clarity_level": assessment.clarity_level, "source_origin": origin,
            "recommended_preset": "vernal-lite" if lite else "vernal-standard",
            "available_preset": "vernal-lite" if lite else "vernal-core",
            "process_template": "quick-prototype" if lite else "standard",
            "overlay": "vernal-existing" if origin != "greenfield" else "",
            "limitation": "" if lite else "当前采用核心预设和标准门禁；完整标准与存量预设、条件式圆桌后续交付。"}


def input_key(engine, mission: dict, *, constitution_rev_id: int | None = None) -> str:
    mission = json.loads(engine.db.meta_get(f"mission:{mission['feature_id']}") or "null") or mission
    rev = service.latest(engine, mission["feature_id"])
    if rev:
        service.assert_source_current(engine, rev)
    constitution = service.latest(engine, "project", "constitution")
    if constitution:
        service.assert_source_current(engine, constitution)
    return assets.digest_json([assets.generation_baseline(engine), mission["revision"], mission["brief"], mission["intent"], source_origin(engine),
                               rev["rev_id"] if rev else 0, constitution_rev_id if constitution_rev_id is not None else (constitution["rev_id"] if constitution else 0),
                               [(s.slice_id, s.content) for s in engine.db.list_slices()],
                               service.list_clarifications(engine, mission["feature_id"])])
