"""人工代码审查触发策略纯函数判定器（WP-2：off / milestone / risk / all）。"""

from __future__ import annotations

from fnmatch import fnmatch
import re
from typing import TYPE_CHECKING, Any

from autodev.db import Task
from autodev.process.runner import load_template

if TYPE_CHECKING:
    from autodev.config import Settings, Workspace

_SENSITIVE_CODE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("子进程与命令执行", re.compile(r"\b(?:subprocess\.|os\.system|child_process)\b")),
    ("动态代码执行", re.compile(r"\b(?:eval|exec)\s*\(")),
    ("SQL 动态拼接风险", re.compile(r"""(?i)\b(?:SELECT|UPDATE|DELETE|INSERT)\b.*(?:\+|f["']|\{)""")),
    ("密码学或鉴权敏感逻辑", re.compile(r"(?i)\b(?:crypto|hmac|jwt|password|auth_token|secret)\b")),
]


def effective_review_policy(ws: "Workspace", settings: "Settings") -> str:
    """配置优先级：settings.review_policy 非 off 显式设置 > 流程模板 review_policy > off。"""
    explicit = (getattr(settings, "review_policy", "") or "").strip().lower()
    tpl = load_template(ws, settings)
    if explicit and explicit != "off":
        return explicit
    return tpl.review_policy or "off"


def _matches_any_prefix_or_glob(path: str, patterns: list[str]) -> bool:
    norm = path.strip("/")
    for pat in patterns:
        p = pat.strip()
        if not p:
            continue
        p_clean = p.strip("/")
        if norm == p_clean or norm.startswith(p_clean + "/") or fnmatch(norm, p):
            return True
    return False


def evaluate_review_policy(
    policy: str,
    task: Task,
    diff_view: dict[str, Any],
    quality_results: list[dict[str, Any]] | None = None,
    *,
    protected_paths: list[str] | None = None,
    risk_lines: int = 400,
    required_paths: list[str] | None = None,
) -> tuple[bool, list[str]]:
    """返回 `(need_human_review, risk_reasons)`。"""
    norm = (policy or "off").strip().lower()
    if norm in ("off", "milestone"):
        return False, []
    if norm == "all":
        return True, ["当前策略要求全部任务执行人工 Code Review (policy=all)"]

    # risk 策略逐条核查
    reasons: list[str] = []
    files = [str(f.get("path") or "") for f in (diff_view.get("files") or [])]
    prot = [*(protected_paths or []), "contracts/"]
    req_globs = list(required_paths or [])

    if task.role in ("contract", "integrator"):
        reasons.append(f"关键架构角色任务 (role={task.role})")

    touched_prot = [f for f in files if _matches_any_prefix_or_glob(f, prot)]
    if touched_prot:
        reasons.append(f"触及受保护或契约目录：{', '.join(touched_prot[:3])}")

    if req_globs:
        touched_req = [f for f in files if _matches_any_prefix_or_glob(f, req_globs)]
        if touched_req:
            reasons.append(f"命中必审路径规则：{', '.join(touched_req[:3])}")

    total_lines = int((diff_view.get("stat") or {}).get("total_lines") or 0)
    if risk_lines > 0 and total_lines > risk_lines:
        reasons.append(f"单任务变更规模较大 ({total_lines} 行 > 阈值 {risk_lines} 行)")

    if task.retry_count >= 2:
        reasons.append(f"经历多轮返工后才通过验证 (retry_count={task.retry_count})")

    added_lines: list[str] = []
    for f in diff_view.get("files") or []:
        for h in f.get("hunks") or []:
            for ln in h.get("lines") or []:
                if ln.get("type") == "add":
                    added_lines.append(str(ln.get("text") or ""))
    joined_added = "\n".join(added_lines)
    for label, regex in _SENSITIVE_CODE_PATTERNS:
        if regex.search(joined_added):
            reasons.append(f"新增代码命中敏感模式：{label}")

    for qr in quality_results or []:
        for finding in qr.get("findings") or []:
            sev = str(finding.get("severity") or "").lower()
            if sev in ("blocker", "high", "medium") and qr.get("gate") in ("sast", "secrets"):
                reasons.append(f"安全门禁发现告警 ({qr.get('gate')}:{finding.get('rule')})")
                break

    return bool(reasons), reasons
