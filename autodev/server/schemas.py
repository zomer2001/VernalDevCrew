"""Vernal HTTP API 请求载荷模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class PlanRequest(BaseModel):
    extra_instructions: str = ""
    mode: str | None = None


class CouncilInterjectRequest(BaseModel):
    actor: str = "owner"
    note: str = ""
    refresh_blueprint: bool = True


class RuntimeConfigRequest(BaseModel):
    collaboration_mode: str | None = None
    runtime_backend: str | None = None
    loopforge_url: str | None = None
    loopforge_token: str | None = None
    loopforge_project_id: str | None = None
    loopforge_workflow_id: str | None = None
    loopforge_repository_id: str | None = None


class AgentConfigRequest(BaseModel):
    native_executor_user_id: int | None = None
    native_approver_user_id: int | None = None
    execution_pool: str | None = None
    harness: str | None = None
    pm_harness: str | None = None
    director_harness: str | None = None
    coder_harness: str | None = None
    qa_harness: str | None = None
    model: str | None = None
    pm_model: str | None = None
    director_model: str | None = None
    coder_model: str | None = None
    qa_model: str | None = None
    max_concurrency: int | None = None
    process_template: str | None = None
    review_policy: str | None = None
    doc_profile: str | None = None
    ci_provider: str | None = None


class RunApproveRequest(BaseModel):
    comment: str = ""
    node_id: str = ""


class RunRejectRequest(BaseModel):
    reason: str = ""
    target_node: str = ""
    node_id: str = ""


class WishRequest(BaseModel):
    title: str = "wish"
    content: str = ""


class ProjectSelectRequest(BaseModel):
    project_id: str
    project_name: str = ""


class ProjectCreateRequest(BaseModel):
    mode: str = "scratch"  # scratch | local_folder | git_repo
    name: str = ""
    project_id: str = ""
    bind_existing: bool = False
    wish: str = ""
    docs: list[dict[str, str]] = []
    local_path: str = ""
    git_url: str = ""
    git_username: str = ""
    git_token: str = ""
    branch: str = ""
    collaboration_mode: str = ""
    process_template: str = ""
    execution_pool: str = ""
    harness: str = ""
    start_council: bool = False
    actor: str = "owner"


class ImportFolderRequest(BaseModel):
    name: str
    project_id: str = ""
    bind_existing: bool = False
    wish: str = ""
    files: list[dict[str, str]] = []
    execution_pool: str = ""
    harness: str = ""
    start_council: bool = False
    actor: str = "owner"


class PublicPoolSmokeRequest(BaseModel):
    harness: str = "dsh"


class SteeringStartRequest(BaseModel):
    topic: str
    human_note: str = ""
    kind: str = "REVIEW"
    actor: str = "owner"


class SteeringApproveRequest(BaseModel):
    expected_revision: int | None = None
    actor: str = "owner"
    comment: str = ""
    override_plan: dict[str, Any] | None = None


class SteeringRejectRequest(BaseModel):
    actor: str = "owner"
    reason: str = ""


class CodeBuddyKeyRequest(BaseModel):
    api_key: str
    internet_environment: str = ""


class PersonalCliKeyRequest(BaseModel):
    provider: str = "dsh"
    api_key: str = ""
    base_url: str = ""
    models: list[str] = []
    use_pool_key: bool = False


class ActorRequest(BaseModel):
    actor: str = "owner"


class SteerRequest(BaseModel):
    actor: str = "owner"
    note: str = ""
    verify_only: bool = False


class EditPlanTaskRequest(BaseModel):
    actor: str = "owner"
    priority: int | None = None
    depends_on: list[str] | None = None
