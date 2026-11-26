"""持续集成与交付包（WP-3）。"""

from autodev.ci.detect import detect_ci_template_kind, render_vernal_ci_workflow, synthesize_test_cmds, write_ci_workflow_non_destructive
from autodev.ci.service import (
    CISignatureError,
    ci_gate_enabled,
    handle_ci_callback,
    list_ci_runs,
    list_deployments,
    make_callback_url,
    poll_ci_run,
    trigger_deployment,
    trigger_milestone_ci,
    write_release_notes_and_tag,
)

__all__ = [
    "CISignatureError",
    "ci_gate_enabled",
    "detect_ci_template_kind",
    "handle_ci_callback",
    "list_ci_runs",
    "list_deployments",
    "make_callback_url",
    "poll_ci_run",
    "render_vernal_ci_workflow",
    "synthesize_test_cmds",
    "trigger_deployment",
    "trigger_milestone_ci",
    "write_ci_workflow_non_destructive",
    "write_release_notes_and_tag",
]
