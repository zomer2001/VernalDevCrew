"""Execution transport instructions, excluded from business prompts."""

from enum import StrEnum
from .completion import callback_permission_denied


FILE_DELIVERY_STAGES = (
    "文件交付模式下，编码和独立审查在本次任务工作区核对清单、代码及测试。"
    "任务审查通过后，平台集成并验证交付文件。对于原地接管的本机项目，平台在里程碑合并时检查原目录冲突并写回。"
    "将用户要求的原目录验收列在此次写回之后。"
    "任务卡的通过条件应能在当前工位独立完成。记录实际使用的执行器和测试命令，"
    "仅在用户明确指定时把某个命名沙箱列为必需环境。"
)


class FailureCategory(StrEnum):
    TRANSIENT_TECH_ERROR = "TRANSIENT_TECH_ERROR"
    ARTIFACT_DEFECT = "ARTIFACT_DEFECT"
    USER_DECISION_REQUIRED = "USER_DECISION_REQUIRED"
    ENV_OR_CONFLICT_BLOCKED = "ENV_OR_CONFLICT_BLOCKED"


def native_progress(run):
    """Map the native snapshot only; never count Vernal invocations as reworks."""
    specs = {n["id"]: n for n in (run.get("workflowSnapshot") or {}).get("nodes", [])}
    nodes = run.get("nodes") or {}
    status = run.get("status")
    active = [(key, n) for key, n in nodes.items() if n.get("status") in ("running", "waiting", "waiting_approval")]
    if status in ("waiting", "waiting_approval") and any(
        specs.get(key, {}).get("type") == "approval" or n.get("requiresHumanApproval") or key in ("human_gate", "signoff")
        for key, n in active
    ):
        return "WAITING_DECISION", FailureCategory.USER_DECISION_REQUIRED, ""
    if status not in ("failed", "cancelled"):
        return ("REWORKING" if any((n.get("reworkCount") or 0) > 0 for _, n in active) else "RUNNING"), None, ""
    for key, n in nodes.items():
        if n.get("status") != "failed":
            continue
        spec = specs.get(key, {})
        reason = n.get("error") or n.get("output") or run.get("error") or key
        if callback_permission_denied(n) or key == "prepare":
            return "EXECUTION_FAILED", FailureCategory.ENV_OR_CONFLICT_BLOCKED, reason
        limit = spec.get("maxReworks")
        if spec.get("rejectTo") and isinstance(limit, int) and (n.get("reworkCount") or 0) >= limit:
            return "REWORK_EXHAUSTED", FailureCategory.ARTIFACT_DEFECT, n.get("output") or reason
    return "EXECUTION_FAILED", FailureCategory.TRANSIENT_TECH_ERROR, run.get("error") or status
