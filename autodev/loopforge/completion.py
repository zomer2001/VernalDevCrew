"""Unified LoopForge node completion policy shared by bridge.py and steering.py.

Separates normal completion, controlled callback-missing recovery, explicit
permission denial, invalid/missing artifacts, review rejection, and human
approval gates. Explicit permission denials must never be bypassed by calling
another approval API.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from autodev.loopforge.client import LoopForgeClient, LoopForgeError


class CompletionCategory(StrEnum):
    COMPLETED = "COMPLETED"
    CALLBACK_MISSING = "CALLBACK_MISSING"
    CALLBACK_PERMISSION_DENIED = "CALLBACK_PERMISSION_DENIED"
    ARTIFACT_INVALID = "ARTIFACT_INVALID"
    REVIEW_REJECTED = "REVIEW_REJECTED"
    HUMAN_APPROVAL_REQUIRED = "HUMAN_APPROVAL_REQUIRED"


CompletionAction = Literal["continue", "approve_recovery", "reject_rework", "block", "wait"]

_PERMISSION_DENIED_PATTERNS = (
    "requires approval, but approval policy is never",
    "mcp tool call requires approval",
    "approval policy is never",
    "审批策略拒绝",
    "当前环境禁止审批",
    "要求审批",
    "permission denied",
    "tool call denied",
    "not permitted by policy",
)

_CALLBACK_MISSING_PATTERNS = (
    "submit_gate_result",
    "未收到终态",
    "未调用终态",
    "gate callback missing",
    "callback_missing",
)


@dataclass(frozen=True)
class CompletionDecision:
    category: CompletionCategory
    action: CompletionAction
    reason: str
    run_id: str
    node_id: str
    attempt: int
    rework_count: int
    artifact_digest: str
    idempotency_key: str
    reject_target: str = ""


def _int_or_zero(value: Any) -> int:
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def _exit_code(node_record: dict[str, Any]) -> int | None:
    for key in ("exitCode", "exit_code"):
        if key in node_record and node_record[key] is not None:
            try:
                return int(node_record[key])
            except (TypeError, ValueError):
                return -1
    return None


def _matches_any(text: str, patterns: tuple[str, ...]) -> bool:
    low = (text or "").lower()
    return any(p in low for p in patterns)


def _node_diagnostic_text(node_record: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("error", "failureReason", "reason", "output", "summary", "stderr", "lastMessage"):
        val = node_record.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(val)
    return "\n".join(parts)


def _is_current_turn_artifact(item: dict[str, Any], node_id: str, node_record: dict[str, Any]) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get("nodeId") and item.get("nodeId") != node_id:
        return False
    if node_record.get("attempt") is not None and item.get("attempt") is not None:
        if _int_or_zero(item.get("attempt")) != _int_or_zero(node_record.get("attempt")):
            return False
    if node_record.get("reworkCount") is not None and item.get("reworkCount") is not None:
        if _int_or_zero(item.get("reworkCount")) != _int_or_zero(node_record.get("reworkCount")):
            return False
    started = node_record.get("startedAt")
    created = item.get("createdAt")
    if started and created:
        try:
            if datetime.fromisoformat(str(created)) < datetime.fromisoformat(str(started)):
                return False
        except ValueError:
            return False
    elif node_record.get("reworkCount") and started and not created:
        return False
    return True


def current_turn_artifacts(run: dict[str, Any], node_id: str, node_record: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for a in run.get("artifacts") or []:
        if isinstance(a, dict) and (a.get("nodeId") == node_id or (not a.get("nodeId") and node_id == "")):
            if _is_current_turn_artifact(a, node_id, node_record):
                records.append(a)
    for a in node_record.get("artifacts") or []:
        if isinstance(a, dict) and _is_current_turn_artifact(a, node_id, node_record):
            records.append(a)
    return records


def _artifact_name_matches(item: dict[str, Any], filename: str) -> bool:
    return any(str(item.get(k, "")).split("/")[-1] == filename for k in ("name", "path", "filename", "id"))


def compute_artifact_digest(artifacts: list[dict[str, Any]], extra_text: str = "") -> str:
    summary = [
        {
            "id": str(a.get("id") or a.get("artifactId") or ""),
            "name": str(a.get("name") or a.get("path") or a.get("filename") or ""),
            "sha256": str(a.get("sha256") or a.get("digest") or ""),
            "createdAt": str(a.get("createdAt") or ""),
            "content_sha": hashlib.sha256(str(a.get("content") or "").encode("utf-8")).hexdigest()
            if isinstance(a.get("content"), str) and a.get("content")
            else "",
        }
        for a in artifacts
    ]
    payload = {"artifacts": summary, "extra": hashlib.sha256(extra_text.encode("utf-8")).hexdigest() if extra_text else ""}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:24]


def build_idempotency_key(
    run_id: str, node_id: str, attempt: int, rework_count: int, artifact_digest: str
) -> str:
    raw = f"{run_id}:{node_id}:{attempt}:{rework_count}:{artifact_digest}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def is_review_node(wf_node: dict[str, Any]) -> bool:
    return any(
        isinstance(out, dict) and out.get("name") == "review-report.md" and out.get("required", True)
        for out in (wf_node.get("artifactOutputs") or [])
    )


def required_artifact_names(wf_node: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for out in wf_node.get("artifactOutputs") or []:
        if isinstance(out, dict) and out.get("required", True):
            name = str(out.get("name") or out.get("path") or "").split("/")[-1]
            if name:
                names.append(name)
    return names


async def read_review_verdict(client: LoopForgeClient, run: dict[str, Any], node_id: str) -> tuple[str, str, str]:
    """Read current-turn review-report.md and return (verdict, detail, raw_content)."""
    try:
        report = await client.named_artifact(run, "review-report.md", node_id=node_id)
    except LoopForgeError as exc:
        return "", str(exc), ""
    heading, _, details = report.strip().partition("\n")
    verdict = heading.strip()
    if verdict not in {"# PASS", "# REJECT"}:
        return "", "审查报告首行缺少 # PASS 或 # REJECT", report
    return verdict, details.strip()[:1200] or verdict, report


class CompletionLedger:
    """Idempotency and monotonic-turn guard, optionally backed by SQLite meta across restarts."""

    def __init__(self, db: Any = None, run_id: str = ""):
        self.db = db
        self.run_id = run_id
        self._keys: set[str] = set()
        self._high_water: dict[str, tuple[int, int]] = {}
        if db is not None and run_id:
            self._load()

    def _meta_key(self) -> str:
        return f"lf.completion_ledger:{self.run_id}"

    def _load(self) -> None:
        raw = self.db.meta_get(self._meta_key())
        if not raw:
            return
        try:
            data = json.loads(raw)
        except ValueError:
            return
        self._keys = set(data.get("keys") or [])
        for nid, pair in (data.get("high_water") or {}).items():
            if isinstance(pair, list) and len(pair) == 2:
                self._high_water[str(nid)] = (_int_or_zero(pair[0]), _int_or_zero(pair[1]))

    def _save(self) -> None:
        if self.db is None or not self.run_id:
            return
        payload = {
            "keys": sorted(self._keys)[-200:],
            "high_water": {k: [v[0], v[1]] for k, v in self._high_water.items()},
        }
        self.db.meta_set(self._meta_key(), json.dumps(payload, ensure_ascii=False))

    def is_duplicate_or_stale(self, decision: CompletionDecision) -> tuple[bool, str]:
        if decision.idempotency_key in self._keys:
            return True, "duplicate"
        prev = self._high_water.get(decision.node_id)
        cur = (decision.attempt, decision.rework_count)
        if prev is not None and cur < prev:
            return True, "stale"
        return False, ""

    def record(self, decision: CompletionDecision) -> None:
        self._keys.add(decision.idempotency_key)
        cur = (decision.attempt, decision.rework_count)
        prev = self._high_water.get(decision.node_id)
        if prev is None or cur >= prev:
            self._high_water[decision.node_id] = cur
        self._save()


async def evaluate_waiting_node(
    client: LoopForgeClient,
    run: dict[str, Any],
    node_id: str,
    node_record: dict[str, Any],
    wf_node: dict[str, Any] | None,
) -> CompletionDecision:
    """Classify a waiting/waiting_approval node under one strict protocol."""
    run_id = str(run.get("id") or "")
    attempt = _int_or_zero(node_record.get("attempt"))
    rework_count = _int_or_zero(node_record.get("reworkCount"))
    spec = wf_node if isinstance(wf_node, dict) else {}
    ntype = str(spec.get("type") or "")

    def make(
        category: CompletionCategory,
        action: CompletionAction,
        reason: str,
        *,
        digest: str = "",
        reject_target: str = "",
    ) -> CompletionDecision:
        key = build_idempotency_key(run_id, node_id, attempt, rework_count, digest)
        return CompletionDecision(
            category=category,
            action=action,
            reason=reason,
            run_id=run_id,
            node_id=node_id,
            attempt=attempt,
            rework_count=rework_count,
            artifact_digest=digest,
            idempotency_key=key,
            reject_target=reject_target,
        )

    # 1. Human approval nodes always stay human-gated.
    if ntype == "approval" or node_id in ("human_gate", "signoff") or node_record.get("requiresHumanApproval"):
        return make(
            CompletionCategory.HUMAN_APPROVAL_REQUIRED,
            "wait",
            f"节点 {node_id} 为人工审批门禁，保持等待人工决策",
        )

    # 2. Only explicitly typed agent nodes are eligible for callback inspection.
    if ntype != "agent":
        return make(
            CompletionCategory.ARTIFACT_INVALID,
            "wait",
            f"节点 {node_id} 类型为 {ntype or 'unknown'}，非 agent 节点不自动放行",
        )

    # 3. Explicit permission denial (structured field or runtime error/output text) is a hard env block.
    structured_cat = str(node_record.get("completionCategory") or node_record.get("gateFailureCode") or "").upper()
    diag_text = _node_diagnostic_text(node_record)
    if structured_cat == CompletionCategory.CALLBACK_PERMISSION_DENIED or _matches_any(
        diag_text, _PERMISSION_DENIED_PATTERNS
    ):
        return make(
            CompletionCategory.CALLBACK_PERMISSION_DENIED,
            "block",
            f"节点 {node_id} 完成回调被执行器审批策略明确拒绝（submit_gate_result 要求审批但策略为 never）；"
            "已停止无效重试且禁止通过其他审批 API 强制放行，请检查 Codex/Runner 工具授权配置",
        )

    # 4. Non-zero exit code must never be treated as completed.
    code = _exit_code(node_record)
    if code is not None and code != 0:
        return make(
            CompletionCategory.ARTIFACT_INVALID,
            "wait",
            f"节点 {node_id} 执行器退出码为 {code}，不能放行",
        )

    # 5. Must actually be a callback-missing situation, not an unknown pause reason.
    is_callback_missing = (
        structured_cat == CompletionCategory.CALLBACK_MISSING
        or node_record.get("gateCallbackMissing") is True
        or _matches_any(str(node_record.get("error") or ""), _CALLBACK_MISSING_PATTERNS)
    )
    if not is_callback_missing:
        return make(
            CompletionCategory.HUMAN_APPROVAL_REQUIRED,
            "wait",
            f"节点 {node_id} 等待原因未声明为回调缺失，保持等待人工核查",
        )

    # 6. Check protocol capability declaration.
    wf_snapshot = run.get("workflowSnapshot") or {}
    protocol = str(spec.get("completionProtocol") or wf_snapshot.get("completionProtocol") or "").strip().lower()
    if protocol in ("strict_mcp", "none", "disabled"):
        return make(
            CompletionCategory.ARTIFACT_INVALID,
            "wait",
            f"节点 {node_id} 声明了严格终态工具协议（{protocol}），不启用回调缺失恢复",
        )

    # 7. Verify current-turn artifacts & review verdict.
    turn_artifacts = current_turn_artifacts(run, node_id, node_record)
    if is_review_node(spec):
        verdict, detail, raw_report = await read_review_verdict(client, run, node_id)
        if not verdict:
            return make(
                CompletionCategory.ARTIFACT_INVALID,
                "wait",
                f"等待本轮独立审查报告：{detail}",
            )
        digest = compute_artifact_digest(turn_artifacts, raw_report)
        if verdict == "# REJECT":
            return make(
                CompletionCategory.REVIEW_REJECTED,
                "reject_rework",
                detail,
                digest=digest,
                reject_target=str(spec.get("rejectTo") or ""),
            )
        return make(
            CompletionCategory.CALLBACK_MISSING,
            "approve_recovery",
            f"节点 {node_id} 独立审查报告为 # PASS，按受控恢复协议继续",
            digest=digest,
        )

    req_names = required_artifact_names(spec)
    if req_names:
        missing = [name for name in req_names if not any(_artifact_name_matches(a, name) for a in turn_artifacts)]
        if missing:
            return make(
                CompletionCategory.ARTIFACT_INVALID,
                "wait",
                f"节点 {node_id} 当前轮次缺少声明产物 {', '.join(missing)}，拒绝自动放行",
            )
        digest = compute_artifact_digest(turn_artifacts)
        return make(
            CompletionCategory.CALLBACK_MISSING,
            "approve_recovery",
            f"节点 {node_id} 当前轮次声明产物齐全，按受控恢复协议交由下游确定性校验",
            digest=digest,
        )

    # 8. Nodes without declared required artifacts still require positive current-turn completion evidence:
    # exitCode == 0, current-turn artifact, or explicit controlled_recovery protocol with clean output.
    has_clean_exit = code == 0
    has_turn_output = bool(turn_artifacts) or bool(str(node_record.get("output") or "").strip())
    has_declared_recovery = protocol == "controlled_recovery"
    if not (has_clean_exit or has_turn_output or has_declared_recovery):
        return make(
            CompletionCategory.ARTIFACT_INVALID,
            "wait",
            f"节点 {node_id} 缺少当前轮次完成证据（无退出码 0、无当轮产物且未声明受控恢复协议），不自动放行",
        )

    digest = compute_artifact_digest(turn_artifacts, str(node_record.get("output") or ""))
    return make(
        CompletionCategory.CALLBACK_MISSING,
        "approve_recovery",
        f"节点 {node_id} 已核对当前轮次完成证据，按受控恢复协议交由下游校验",
        digest=digest,
    )
