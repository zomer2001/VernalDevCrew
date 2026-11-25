"""详细设计与规范文档中心包（WP-1）。"""

from autodev.docs_center.exporter import inspect_docx_structure, markdown_to_docx_bytes
from autodev.docs_center.service import (
    add_doc_comment,
    diff_doc_versions,
    generate_detailed_design,
    generate_milestone_test_report,
    get_doc,
    list_doc_versions,
    list_docs,
    resolve_design_refs_snippet,
    save_doc_version,
    sync_standard_docs_from_artifacts,
)
from autodev.docs_center.templates import (
    render_adr_madr,
    render_architecture_markdown,
    render_detailed_design_markdown,
    render_srs_markdown,
)
from autodev.docs_center.validator import validate_detailed_design

__all__ = [
    "add_doc_comment",
    "diff_doc_versions",
    "generate_detailed_design",
    "generate_milestone_test_report",
    "get_doc",
    "inspect_docx_structure",
    "list_doc_versions",
    "list_docs",
    "markdown_to_docx_bytes",
    "render_adr_madr",
    "render_architecture_markdown",
    "render_detailed_design_markdown",
    "render_srs_markdown",
    "resolve_design_refs_snippet",
    "save_doc_version",
    "sync_standard_docs_from_artifacts",
    "validate_detailed_design",
]
