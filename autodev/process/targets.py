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
    snapshots = [{"id": r["id"], "path": r["path"], "version": r["version"], "sha": r["content_sha"]} for r in selected]
    raw = json.dumps(snapshots, ensure_ascii=False, sort_keys=True)
    sha = (snapshots[0]["sha"] if len(snapshots) == 1 else hashlib.sha256(raw.encode()).hexdigest()) if snapshots else ""
    return {"target_path": prefix, "target_sha": sha, "target_version": max((r["version"] for r in snapshots), default=0),
            "target_snapshot_json": raw}


def target_is_current(db, record: dict) -> bool:
    if record["gate_id"] not in DOCUMENT_GATES:
        return True
    current = document_target(db, record["gate_id"], record["scope_id"])
    if not record.get("target_sha") or record["target_sha"] != current["target_sha"]:
        return False
    root = db.path.parent.parent.resolve()
    snapshots = json.loads(current["target_snapshot_json"])
    if record["gate_id"] == "detailed_design_approval":
        # docs_center exports its service at package import time; defer until runner is loaded.
        from autodev.docs_center.paths import detailed_design_approval_paths
        try:
            actual_paths = {p.relative_to(root).as_posix() for p in detailed_design_approval_paths(root, record["scope_id"])}
        except (OSError, ValueError):
            return False
        if actual_paths != {item["path"] for item in snapshots}:
            return False
    for item in snapshots:
        path = (root / item["path"]).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            return False
        try:
            if document_sha(path.read_text("utf-8")) != item["sha"]:
                return False
        except (OSError, UnicodeError):
            return False
    return True
