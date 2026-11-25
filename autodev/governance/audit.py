"""平台级防篡改哈希链审计日志（WP-7：`workspaces/.vernal-platform.db`）。"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any

from autodev.db import now_iso

GENESIS_HASH = "0" * 64
_AUDIT_LOCK = threading.RLock()

PLATFORM_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    actor_ip TEXT NOT NULL,
    project TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_gateway_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    project TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL,
    duration_ms INTEGER NOT NULL DEFAULT 0
);
"""


def platform_db_path_for(workspaces_parent: Path) -> Path:
    workspaces_parent.mkdir(parents=True, exist_ok=True)
    return workspaces_parent / ".vernal-platform.db"


def _connect_platform_db(db_path: Path) -> sqlite3.Connection:
    from autodev.db import sqlite_journal_mode

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=20)
    conn.row_factory = sqlite3.Row
    mode = sqlite_journal_mode()
    conn.execute(f"PRAGMA journal_mode={mode}")
    if mode != "WAL":
        conn.execute("PRAGMA synchronous=FULL")
    conn.executescript(PLATFORM_SCHEMA)
    return conn


def _canonical_payload(
    *,
    ts: str,
    actor: str,
    actor_ip: str,
    project: str,
    action: str,
    target: str,
    detail_json: str,
) -> str:
    return json.dumps(
        {
            "action": action,
            "actor": actor,
            "actor_ip": actor_ip,
            "detail_json": detail_json,
            "project": project,
            "target": target,
            "ts": ts,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def compute_audit_hash(prev_hash: str, canonical_json: str) -> str:
    return hashlib.sha256((prev_hash + canonical_json).encode("utf-8")).hexdigest()


def append_audit_log(
    db_path: Path,
    *,
    actor: str,
    action: str,
    target: str = "",
    project: str = "",
    actor_ip: str = "127.0.0.1",
    detail: dict[str, Any] | None = None,
    event_key: str = "",
) -> dict[str, Any]:
    ts = now_iso()
    detail_str = json.dumps(detail or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    clean_actor = (actor or "anonymous").strip()
    clean_ip = (actor_ip or "127.0.0.1").strip()
    clean_proj = (project or "default").strip()
    clean_target = (target or "-").strip()

    with _AUDIT_LOCK:
        conn = _connect_platform_db(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            if event_key:
                conn.execute("CREATE TABLE IF NOT EXISTS audit_receipts(event_key TEXT PRIMARY KEY,row_id INTEGER NOT NULL)")
                existing = conn.execute("SELECT a.* FROM audit_receipts r JOIN audit_log a ON a.id=r.row_id WHERE event_key=?",
                                        (event_key,)).fetchone()
                if existing:
                    saved = dict(existing)
                    saved["detail"] = json.loads(saved.pop("detail_json"))
                    conn.commit()
                    return saved
            prev_row = conn.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
            prev_hash = str(prev_row["hash"]) if prev_row else GENESIS_HASH
            canonical = _canonical_payload(
                ts=ts,
                actor=clean_actor,
                actor_ip=clean_ip,
                project=clean_proj,
                action=action,
                target=clean_target,
                detail_json=detail_str,
            )
            row_hash = compute_audit_hash(prev_hash, canonical)
            cur = conn.execute(
                "INSERT INTO audit_log(ts, actor, actor_ip, project, action, target, detail_json, prev_hash, hash)"
                " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, clean_actor, clean_ip, clean_proj, action, clean_target, detail_str, prev_hash, row_hash),
            )
            row_id = int(cur.lastrowid)
            if event_key:
                conn.execute("INSERT INTO audit_receipts VALUES(?,?)",(event_key,row_id))
            conn.commit()
            saved = dict(conn.execute("SELECT * FROM audit_log WHERE id=?", (row_id,)).fetchone())
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    saved["detail"] = json.loads(saved.pop("detail_json") or "{}")
    return saved


def query_audit_logs(
    db_path: Path,
    *,
    project: str = "",
    action: str = "",
    actor: str = "",
    limit: int = 200,
    allowed_projects: set[str] | None = None,
) -> list[dict[str, Any]]:
    if not db_path.is_file():
        return []
    clauses: list[str] = []
    params: list[Any] = []
    if allowed_projects is not None:
        if not allowed_projects:
            return []
        clauses.append("project IN (" + ",".join("?" for _ in allowed_projects) + ")")
        params.extend(sorted(allowed_projects))
    if project.strip():
        clauses.append("project=?")
        params.append(project.strip())
    if action.strip():
        clauses.append("action=?")
        params.append(action.strip())
    if actor.strip():
        clauses.append("actor=?")
        params.append(actor.strip())
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(int(limit), 2000)))
    conn = _connect_platform_db(db_path)
    try:
        rows = conn.execute(f"SELECT * FROM audit_log{where} ORDER BY id DESC LIMIT ?", params).fetchall()
    finally:
        conn.close()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        try:
            d["detail"] = json.loads(d.pop("detail_json") or "{}")
        except json.JSONDecodeError:
            d["detail"] = {}
        out.append(d)
    return out


def verify_audit_chain(db_path: Path) -> dict[str, Any]:
    """逐行重算 SHA-256 哈希链，检测任意字段篡改或中间记录删除。"""
    if not db_path.is_file():
        return {"valid": True, "checked_count": 0, "tampered_id": None, "reason": ""}
    conn = _connect_platform_db(db_path)
    try:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY id ASC").fetchall()
    finally:
        conn.close()

    expected_prev = GENESIS_HASH
    expected_seq_id = 1
    for idx, r in enumerate(rows):
        row_id = int(r["id"])
        if idx > 0 and row_id != expected_seq_id:
            return {
                "valid": False,
                "checked_count": idx,
                "tampered_id": row_id,
                "reason": f"检测到审计序号跳变（期望 id={expected_seq_id}，实际 id={row_id}）",
            }
        expected_seq_id = row_id + 1
        if str(r["prev_hash"]) != expected_prev:
            return {
                "valid": False,
                "checked_count": idx,
                "tampered_id": row_id,
                "reason": f"记录 #{row_id} 的 prev_hash 与前序哈希不匹配",
            }
        canonical = _canonical_payload(
            ts=str(r["ts"]),
            actor=str(r["actor"]),
            actor_ip=str(r["actor_ip"]),
            project=str(r["project"]),
            action=str(r["action"]),
            target=str(r["target"]),
            detail_json=str(r["detail_json"]),
        )
        recomputed = compute_audit_hash(expected_prev, canonical)
        if recomputed != str(r["hash"]):
            return {
                "valid": False,
                "checked_count": idx,
                "tampered_id": row_id,
                "reason": f"记录 #{row_id} 内容已被篡改（计算哈希 {recomputed[:12]} != 存储哈希 {str(r['hash'])[:12]}）",
            }
        expected_prev = str(r["hash"])

    return {"valid": True, "checked_count": len(rows), "tampered_id": None, "head_hash": expected_prev, "reason": ""}


def export_audit_csv(rows: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["ID", "Timestamp", "Actor", "ActorIP", "Project", "Action", "Target", "Detail", "PrevHash", "Hash"])
    for r in rows:
        writer.writerow(
            [
                r["id"],
                r["ts"],
                r["actor"],
                r["actor_ip"],
                r["project"],
                r["action"],
                r["target"],
                json.dumps(r.get("detail") or {}, ensure_ascii=False),
                r["prev_hash"],
                r["hash"],
            ]
        )
    return buf.getvalue()


def enqueue_decision(db, key, **payload):
    import uuid
    # Stable workspace identity survives moves and distinguishes recycled project ids.
    origin = db.meta_get("audit_origin")
    if not origin:
        origin = uuid.uuid4().hex
        db.meta_set("audit_origin", origin)
    db._exec("INSERT OR IGNORE INTO audit_outbox(event_key,payload) VALUES(?,?)",
             (origin + ":" + key, json.dumps(payload, ensure_ascii=False)))


def flush_decisions(db, platform_path):
    for row in db._query("SELECT event_key,payload FROM audit_outbox WHERE delivered=0 ORDER BY rowid"):
        append_audit_log(platform_path, event_key=row["event_key"], **json.loads(row["payload"]))
        db._exec("UPDATE audit_outbox SET delivered=1 WHERE event_key=?", (row["event_key"],))
