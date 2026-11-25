"""Process-as-Code 流程阶段门禁引擎（WP-0）。"""

from autodev.process.model import GateDef, GateKind, ProcessTemplate, StageDef, StageId, StageView
from autodev.process.runner import (
    BUILTIN_TEMPLATES,
    decide_gate,
    ensure_gate_record,
    is_gate_passed,
    is_stage_passed,
    list_gate_records,
    load_template,
    parse_process_yaml,
    save_custom_template,
    stage_status,
)

__all__ = [
    "BUILTIN_TEMPLATES",
    "GateDef",
    "GateKind",
    "ProcessTemplate",
    "StageDef",
    "StageId",
    "StageView",
    "decide_gate",
    "ensure_gate_record",
    "is_gate_passed",
    "is_stage_passed",
    "list_gate_records",
    "load_template",
    "parse_process_yaml",
    "save_custom_template",
    "stage_status",
]
