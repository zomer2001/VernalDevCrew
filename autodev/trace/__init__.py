"""需求双向追溯矩阵模块（WP-6：SRS -> 详细设计 -> 任务 -> 代码 -> 测试 -> 结果）。"""

from autodev.trace.service import (
    build_trace_matrix,
    check_p0_trace_gate,
    export_trace_matrix_csv,
    export_trace_matrix_docx,
    sync_requirements,
)

__all__ = [
    "build_trace_matrix",
    "check_p0_trace_gate",
    "export_trace_matrix_csv",
    "export_trace_matrix_docx",
    "sync_requirements",
]
