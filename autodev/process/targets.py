"""Content targets for document approvals. No execution or model calls."""

import hashlib
import json


DOCUMENT_GATES = {"srs_approval": "docs/SRS.md", "architecture_approval": "docs/ARCHITECTURE.md",
                  "detailed_design_approval": "docs/design/"}


def normalize_document(content: str) -> str:
    return content.replace("\r\n", "\n").replace("\r", "\n").rstrip() + "\n"


def document_sha(content: str) -> str:
    return hashlib.sha256(normalize_document(content).encode("utf-8")).hexdigest()


def document_target(db, gate_id: str, scope_id: str) -> dict:
    prefix = DOCUMENT_GATES.get(gate_id, "")
    if not prefix:
        return {"target_path": "", "target_sha": "", "target_version": 0, "target_snapshot_json": "[]"}
    rows = db._query("SELECT d.* FROM doc_versions d JOIN (SELECT path, MAX(version) AS v FROM doc_versions GROUP BY path) latest "
                     "ON d.path=latest.path AND d.version=latest.v ORDER BY d.path")
    if gate_id == "detailed_design_approval":
        scoped = f"docs/design/{scope_id}/"
        selected = [r for r in rows if r["path"].startswith((scoped, f".autodev/design/{scope_id}/"))]
        if not selected:
            selected = [r for r in rows if r["path"].startswith((prefix, ".autodev/design/")) and len(r["path"].split("/")) == 3]
        prefix = scoped
    else:
        selected = [r for r in rows if r["path"] == prefix]
    if db.meta_get("speckit.enabled") == "1" and gate_id in ("srs_approval", "architecture_approval"):
        kinds = ("spec", "constitution") if gate_id == "srs_approval" else ("plan", "spec", "constitution")
        sources = db._query(
            "SELECT r.content_version_id FROM spec_revisions r JOIN "
            "(SELECT feature_id,source_kind,MAX(rev_no) n FROM spec_revisions GROUP BY feature_id,source_kind) l "
            "ON r.feature_id=l.feature_id AND r.source_kind=l.source_kind AND r.rev_no=l.n "
            f"WHERE r.source_kind IN ({','.join('?' for _ in kinds)})", kinds)
        ids = {r["content_version_id"] for r in sources}
        selected += [r for r in rows if r["id"] in ids]
    snapshots = [{"id": r["id"], "path": r["path"], "version": r["version"], "sha": r["content_sha"]} for r in selected]
    raw = json.dumps(snapshots, ensure_ascii=False, sort_keys=True)
    sha = (snapshots[0]["sha"] if len(snapshots) == 1 else hashlib.sha256(raw.encode()).hexdigest()) if snapshots else ""
    return {"target_path": prefix, "target_sha": sha, "target_version": max((r["version"] for r in snapshots), default=0),
            "target_snapshot_json": raw}


def evaluate_document_content(db, record: dict) -> dict:
    """Pure content validity check: compares snapshot target SHA, paths, and on-disk contents."""
    if record["gate_id"] not in DOCUMENT_GATES:
        return {"content_current": True, "stale_reason": "", "affected_paths": [], "blockers": []}
    current = document_target(db, record["gate_id"], record["scope_id"])
    if not record.get("target_sha"):
        msg = "目标审批文档尚未归档或不存在，请先在文档中心归档或生成"
        return {
            "content_current": False,
            "stale_reason": msg,
            "affected_paths": [record.get("target_path") or DOCUMENT_GATES[record["gate_id"]]],
            "blockers": [{"code": "TARGET_MISSING", "message": msg, "feature_id": ""}],
        }
    if record["target_sha"] != current["target_sha"]:
        msg = "文档已有新版本，请审阅当前版本"
        return {
            "content_current": False,
            "stale_reason": msg,
            "affected_paths": [record.get("target_path") or DOCUMENT_GATES[record["gate_id"]]],
            "blockers": [{"code": "TARGET_CHANGED", "message": msg, "feature_id": ""}],
        }
    root = db.path.parent.parent.resolve()
    snapshots = json.loads(current["target_snapshot_json"])
    if record["gate_id"] == "detailed_design_approval":
        # docs_center exports its service at package import time; defer until runner is loaded.
        from autodev.docs_center.paths import detailed_design_approval_paths
        try:
            actual_paths = {p.relative_to(root).as_posix() for p in detailed_design_approval_paths(root, record["scope_id"])}
        except (OSError, ValueError):
            msg = "详细设计目录不可读取或已变更，请先在文档中心检查"
            return {
                "content_current": False,
                "stale_reason": msg,
                "affected_paths": [record.get("target_path") or "docs/design/"],
                "blockers": [{"code": "FILE_MISSING_OR_DRIFTED", "message": msg, "feature_id": ""}],
            }
        expected_paths = {item["path"] for item in snapshots}
        if actual_paths != expected_paths:
            diff = sorted(actual_paths ^ expected_paths)
            msg = f"详细设计文档集合已变化（{', '.join(diff[:3])}），请先归档并审阅当前版本"
            return {
                "content_current": False,
                "stale_reason": msg,
                "affected_paths": diff,
                "blockers": [{"code": "FILE_MISSING_OR_DRIFTED", "message": msg, "feature_id": ""}],
            }
    drifted: list[str] = []
    for item in snapshots:
        path = (root / item["path"]).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            drifted.append(item["path"])
            continue
        try:
            if document_sha(path.read_text("utf-8")) != item["sha"]:
                drifted.append(item["path"])
        except (OSError, UnicodeError):
            drifted.append(item["path"])
    if drifted:
        msg = f"文档文件缺失或存在未归档外部改动（{', '.join(drifted[:3])}），请先在文档修订或恢复入口处理"
        return {
            "content_current": False,
            "stale_reason": msg,
            "affected_paths": drifted,
            "blockers": [{"code": "FILE_MISSING_OR_DRIFTED", "message": msg, "feature_id": ""}],
        }
    return {"content_current": True, "stale_reason": "", "affected_paths": [], "blockers": []}


def content_is_current(db, record: dict) -> bool:
    return bool(evaluate_document_content(db, record)["content_current"])


def evaluate_gate_record(db, record: dict, *, is_latest: bool | None = None) -> dict:
    """Evaluate both document snapshot currency and approval preconditions cleanly."""
    content_eval = evaluate_document_content(db, record)
    content_current = bool(content_eval["content_current"])
    blockers: list[dict] = list(content_eval["blockers"])
    seen_keys = {(b["code"], b.get("feature_id", "")) for b in blockers}

    status = str(record.get("status") or "")
    in_flight_codes = {"CHECKLIST_RUNNING", "ASSESSMENT_RUNNING", "MISSION_RUNNING"}
    if (
        record["gate_id"] in ("srs_approval", "architecture_approval")
        and db.meta_get("speckit.enabled") == "1"
    ):
        from types import SimpleNamespace
        from autodev.speckit.missions import approval_readiness

        engine = SimpleNamespace(db=db, ws=SimpleNamespace(root=db.path.parent.parent, state_dir=db.path.parent))
        readiness = approval_readiness(engine)
        for b in readiness["blockers"]:
            # Downstream planning/execution after approval must not invalidate an already-passed gate.
            if status in ("PASSED", "WAIVED") and b["code"] in in_flight_codes:
                continue
            key = (b["code"], b.get("feature_id", ""))
            if key not in seen_keys:
                seen_keys.add(key)
                blockers.append(b)
            if b["code"] == "FILE_MISSING_OR_DRIFTED":
                content_current = False

    if is_latest is False and ("TARGET_CHANGED", "") not in seen_keys:
        content_current = False
        blockers.insert(0, {"code": "TARGET_CHANGED", "message": "审批轮次已过期，请查看最新轮次", "feature_id": ""})

    ready_for_decision = content_current and (is_latest is not False) and not blockers
    can_approve = status == "PENDING" and ready_for_decision
    approval_valid = status in ("PASSED", "WAIVED") and content_current and (is_latest is not False) and not blockers
    return {
        "content_current": content_current,
        "ready_for_decision": ready_for_decision,
        "can_approve": can_approve,
        "approval_valid": approval_valid,
        "stale_reason": content_eval["stale_reason"] if not content_current else "",
        "affected_paths": content_eval["affected_paths"],
        "blockers": blockers,
    }


def target_is_current(db, record: dict) -> bool:
    """Fail-closed compatibility check: requires BOTH content validity and approval preconditions."""
    ev = evaluate_gate_record(db, record)
    if str(record.get("status") or "") in ("PASSED", "WAIVED"):
        return bool(ev["content_current"] and ev["approval_valid"])
    return bool(ev["content_current"] and ev["ready_for_decision"])
