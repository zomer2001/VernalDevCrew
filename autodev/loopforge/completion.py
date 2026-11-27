"""Unified LoopForge node completion policy shared by bridge.py and steering.py.

Separates normal completion, controlled callback-missing recovery, explicit
permission denial, invalid/missing artifacts, review rejection, and human
approval gates. Explicit permission denials must never be bypassed by calling
another approval API.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from autodev.loopforge.client import LoopForgeClient, LoopForgeError


class CompletionCategory(StrEnum):
    CALLBACK_MISSING = "CALLBACK_MISSING"
    CALLBACK_PERMISSION_DENIED = "CALLBACK_PERMISSION_DENIED"
    ARTIFACT_INVALID = "ARTIFACT_INVALID"
    REVIEW_REJECTED = "REVIEW_REJECTED"
    HUMAN_APPROVAL_REQUIRED = "HUMAN_APPROVAL_REQUIRED"


CompletionAction = Literal["approve_recovery", "reject_rework", "block", "wait"]

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
                if isinstance(node_record[key], (bool, float)):
                    return -1
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
    # Missing turn identifiers are not evidence of freshness. Rework may reset
    # attempt, so both coordinates must match (or be proven by timestamps).
    if type(item.get("attempt")) is not int or item["attempt"] != node_record.get("attempt"):
        return False
    rework = node_record.get("reworkCount")
    if rework is None:
        return False
    if item.get("reworkCount") is not None and (type(item["reworkCount"]) is not int or item["reworkCount"] != rework):
        return False
    started, created = node_record.get("startedAt"), item.get("createdAt")
    if started and created:
        try:
            if datetime.fromisoformat(str(created)) < datetime.fromisoformat(str(started)):
                return False
        except (TypeError, ValueError):
            return False
    elif rework and item.get("reworkCount") is None:
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
    """Durable claim before network I/O; uncertain requests are never replayed.

    The upstream approval API has no idempotency/expected-turn contract. A local
    claim therefore provides at-most-once delivery, not remote exactly-once.
    A timeout needs reconciliation or an explicit human decision, not a retry.
    """

    def __init__(self, db: Any = None, run_id: str = ""):
        self.db = db
        self.run_id = run_id
        self._data: dict[str, Any] = {"version": 2, "turns": {}, "high_water": {}}

    def _meta_key(self) -> str:
        return f"lf.completion_ledger:{self.run_id}"

    def _load(self) -> None:
        if self.db is None:
            return
        raw = self.db.meta_get(self._meta_key())
        if not raw:
            return
        data = json.loads(raw)  # Corrupt state must fail closed.
        if data.get("version") not in (None, 2):
            raise ValueError("不支持的完成恢复记录版本，禁止重复提交")
        if data.get("version") is None:
            data = {"version": 2, "turns": {}, "high_water": {
                nid: [pair[1], pair[0]] for nid, pair in data.get("high_water", {}).items()
            }}
        self._data = data

    def _save(self) -> None:
        if self.db is not None:
            self.db.meta_set(self._meta_key(), json.dumps(self._data, ensure_ascii=False))

    @staticmethod
    def _turn(decision: CompletionDecision) -> str:
        return json.dumps([decision.node_id, decision.rework_count, decision.attempt])

    def is_duplicate_or_stale(self, decision: CompletionDecision) -> tuple[bool, str]:
        self._load()
        entry = self._data["turns"].get(self._turn(decision))
        if entry:
            return True, entry["state"]
        prev = self._data["high_water"].get(decision.node_id)
        cur = [decision.rework_count, decision.attempt]
        if prev is not None and cur <= prev:
            return True, "stale"
        return False, ""

    def claim(self, decision: CompletionDecision) -> tuple[bool, str]:
        """Serialize pollers and restarts, including changed evidence in one turn."""
        with self.db.transaction() if self.db is not None else nullcontext():
            blocked, reason = self.is_duplicate_or_stale(decision)
            if blocked:
                return False, reason
            self._data["turns"][self._turn(decision)] = {
                "state": "pending", "key": decision.idempotency_key, "action": decision.action,
            }
            self._data["high_water"][decision.node_id] = [decision.rework_count, decision.attempt]
            self._save()
            return True, ""

    def record(self, decision: CompletionDecision) -> None:
        with self.db.transaction() if self.db is not None else nullcontext():
            self._load()
            entry = self._data["turns"].get(self._turn(decision))
            if entry and entry["key"] == decision.idempotency_key:
                entry["state"] = "sent"
                self._save()


def callback_permission_denied(node):
    code = str(node.get("completionCategory") or node.get("gateFailureCode") or "").upper()
    return code == CompletionCategory.CALLBACK_PERMISSION_DENIED or _matches_any(
        _node_diagnostic_text(node), _PERMISSION_DENIED_PATTERNS)


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
    if callback_permission_denied(node_record):
        return make(
            CompletionCategory.CALLBACK_PERMISSION_DENIED,
            "block",
            f"节点 {node_id} 完成回调被执行器审批策略明确拒绝（submit_gate_result 要求审批但策略为 never）；"
            "已停止无效重试且禁止通过其他审批 API 强制放行，请检查 Codex/Runner 工具授权配置",
        )

    # 4. Only an explicitly successful process exit permits recovery.
    code = _exit_code(node_record)
    if code != 0:
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
    if protocol != "controlled_recovery":
        return make(
            CompletionCategory.ARTIFACT_INVALID,
            "wait",
            f"节点 {node_id} 未明确声明受控恢复能力（{protocol or 'unknown'}），不启用回调缺失恢复",
        )

    if not run_id or any(type(node_record.get(k)) is not int or node_record[k] < 0
                         for k in ("attempt", "reworkCount")):
        return make(CompletionCategory.ARTIFACT_INVALID, "wait", "缺少有效运行或轮次标识，不能恢复")

    # 7. Verify current-turn artifacts & review verdict.
    turn_artifacts = current_turn_artifacts(run, node_id, node_record)
    # Read only the artifacts whose provenance has already been checked.
    verified_run = {**run, "artifacts": [{**a, "nodeId": node_id} for a in turn_artifacts],
                    "nodes": {node_id: {**node_record, "artifacts": []}}}
    req_names = required_artifact_names(spec)
    contents: dict[str, str] = {}
    for name in req_names:
        if not any(_artifact_name_matches(a, name) for a in turn_artifacts):
            return make(CompletionCategory.ARTIFACT_INVALID, "wait", f"当前轮次缺少声明产物 {name}")
        try:
            contents[name] = await client.named_artifact(verified_run, name, node_id=node_id)
        except LoopForgeError as exc:
            return make(CompletionCategory.ARTIFACT_INVALID, "wait", f"声明产物不可读取：{exc}")
        if not contents[name].strip():
            return make(CompletionCategory.ARTIFACT_INVALID, "wait", f"声明产物 {name} 为空")
    if is_review_node(spec):
        report = contents["review-report.md"]
        heading, _, details = report.strip().partition("\n")
        verdict = heading.strip() if heading.strip() in {"# PASS", "# REJECT"} else ""
        detail = details.strip()[:1200] or heading
        if not verdict:
            return make(
                CompletionCategory.ARTIFACT_INVALID,
                "wait",
                f"等待本轮独立审查报告：{detail}",
            )
        digest = compute_artifact_digest(turn_artifacts, report)
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

    if req_names:
        digest = compute_artifact_digest(turn_artifacts, json.dumps(contents, sort_keys=True))
        return make(CompletionCategory.CALLBACK_MISSING, "approve_recovery",
                    f"节点 {node_id} 当前轮次声明产物可读取，交由下游确定性校验", digest=digest)

    # A protocol declaration and a successful exit are necessary, not sufficient.
    output = str(node_record.get("output") or "").strip()
    if not output:
        return make(CompletionCategory.ARTIFACT_INVALID, "wait", "缺少当前轮次完成输出，不自动放行")
    return make(CompletionCategory.CALLBACK_MISSING, "approve_recovery",
                f"节点 {node_id} 已核对当前轮次完成证据，按受控恢复协议交由下游校验",
                digest=compute_artifact_digest(turn_artifacts, output))
