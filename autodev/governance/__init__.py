"""平台治理模块（WP-7：研发效能度量、防篡改哈希链审计日志、RBAC 职责分离与模型安全网关）。"""

from autodev.governance.audit import append_audit_log, export_audit_csv, platform_db_path_for, query_audit_logs, verify_audit_chain
from autodev.governance.gateway import enforce_model_gateway_policy, record_model_gateway_call, scrub_sensitive_text
from autodev.governance.metrics import compute_portfolio_metrics, compute_project_metrics
from autodev.governance.rbac import VALID_ROLES, check_role_permission, list_members, remove_member, upsert_member

__all__ = [
    "VALID_ROLES",
    "append_audit_log",
    "check_role_permission",
    "compute_portfolio_metrics",
    "compute_project_metrics",
    "enforce_model_gateway_policy",
    "export_audit_csv",
    "list_members",
    "platform_db_path_for",
    "query_audit_logs",
    "record_model_gateway_call",
    "remove_member",
    "scrub_sensitive_text",
    "upsert_member",
    "verify_audit_chain",
]
