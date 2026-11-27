"""SQLite（WAL）全局台账：切片、研发组、里程碑、任务、工位实况席、调用记录与事件流。"""

from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
import sqlite3
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any


def sqlite_journal_mode() -> str:
    """Use DELETE journal inside Docker host mounts where WAL -shm mmap is unsafe across VM boundaries."""
    override = os.environ.get("VERNAL_SQLITE_JOURNAL_MODE", "").strip().upper()
    if override in ("DELETE", "TRUNCATE", "PERSIST", "WAL"):
        return override
    if Path("/.dockerenv").exists():
        return "DELETE"
    return "WAL"

from autodev.models import (
    ACTIVE_TASK_STATUSES,
    MilestoneStatus,
    PlanTask,
    ProjectBlueprint,
    ProjectPlan,
    SquadSpec,
    TaskStatus,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS doc_slices (
    slice_id TEXT PRIMARY KEY,
    source_file TEXT NOT NULL,
    heading_path TEXT NOT NULL,
    summary TEXT NOT NULL,
    content TEXT NOT NULL,
    char_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS squads (
    squad_id TEXT PRIMARY KEY,
    seq_order INTEGER NOT NULL DEFAULT 0,
    name TEXT NOT NULL,
    mission TEXT NOT NULL,
    owned_paths_json TEXT NOT NULL,
    roles_json TEXT NOT NULL,
    sandbox_profile TEXT NOT NULL DEFAULT 'generic-docker'
);
CREATE TABLE IF NOT EXISTS council_messages (
    msg_id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_no INTEGER NOT NULL DEFAULT 1,
    speaker_role TEXT NOT NULL,
    speaker_name TEXT NOT NULL,
    harness TEXT,
    model TEXT,
    kind TEXT NOT NULL,
    headline TEXT NOT NULL,
    body_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS milestones (
    milestone_id TEXT PRIMARY KEY,
    seq_order INTEGER NOT NULL,
    title TEXT NOT NULL,
    goal TEXT NOT NULL,
    integration_cmd TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    planned INTEGER NOT NULL DEFAULT 1,
    target_squads_json TEXT NOT NULL DEFAULT '[]',
    key_deliverables_json TEXT NOT NULL DEFAULT '[]',
    wave_notes TEXT,
    branch_name TEXT,
    commit_matrix_json TEXT,
    report_path TEXT,
    failure_reason TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    milestone_id TEXT NOT NULL REFERENCES milestones(milestone_id) ON DELETE CASCADE,
    squad TEXT NOT NULL DEFAULT '',
    module TEXT NOT NULL,
    role TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    acceptance_criteria_json TEXT NOT NULL,
    depends_on_json TEXT NOT NULL,
    doc_slice_ids_json TEXT NOT NULL,
    allowed_paths_json TEXT NOT NULL,
    verify_cmds_json TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 100,
    status TEXT NOT NULL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    arbitration_count INTEGER NOT NULL DEFAULT 0,
    assigned_slot TEXT,
    active_agent_label TEXT,
    loopforge_issue_id TEXT,
    loopforge_run_id TEXT,
    worktree_path TEXT,
    branch_name TEXT,
    base_sha TEXT,
    last_session_id TEXT,
    candidate_commit_sha TEXT,
    artifact_digest TEXT,
    bound_contract_hash TEXT,
    last_feedback TEXT,
    failure_reason TEXT,
    steer_note TEXT,
    pending_action TEXT,
    owner_member TEXT,
    exec_seq INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS worker_slots (
    slot_id TEXT PRIMARY KEY,
    slot_kind TEXT NOT NULL,
    slot_label TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'IDLE',
    squad TEXT,
    role TEXT,
    harness TEXT,
    model TEXT,
    task_id TEXT,
    worktree_path TEXT,
    session_id TEXT,
    current_action TEXT,
    started_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS task_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    attempt_no INTEGER NOT NULL,
    stage TEXT NOT NULL,
    harness_name TEXT,
    agent_label TEXT,
    session_id TEXT,
    exit_code INTEGER,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    verdict TEXT,
    summary_log TEXT,
    log_path TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL,
    event_type TEXT NOT NULL,
    task_id TEXT,
    message TEXT NOT NULL,
    payload_json TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_decisions (
    decision_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    title TEXT NOT NULL,
    rule_text TEXT NOT NULL,
    rationale TEXT NOT NULL DEFAULT '',
    affected_paths_json TEXT NOT NULL DEFAULT '[]',
    checkpoint_id INTEGER,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evolution_checkpoints (
    checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_no INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'REVIEW',
    trigger_topic TEXT NOT NULL,
    human_note TEXT NOT NULL DEFAULT '',
    loopforge_issue_id TEXT,
    loopforge_run_id TEXT,
    status TEXT NOT NULL DEFAULT 'RUNNING',
    opinions_json TEXT NOT NULL DEFAULT '{}',
    draft_plan_json TEXT NOT NULL DEFAULT '{}',
    applied_summary_json TEXT NOT NULL DEFAULT '{}',
    rule_version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dispatch_records (
    dispatch_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL, exec_seq INTEGER NOT NULL, attempt_no INTEGER NOT NULL,
    pool TEXT NOT NULL, actor TEXT NOT NULL, issue_id TEXT NOT NULL DEFAULT '',
    lf_task_id TEXT NOT NULL DEFAULT '', lf_run_id TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    command_key TEXT UNIQUE, UNIQUE(task_id, exec_seq)
);
CREATE TABLE IF NOT EXISTS link_codes (
    code_hash TEXT PRIMARY KEY, owner TEXT NOT NULL, expires REAL NOT NULL, runner_code TEXT
);
CREATE TABLE IF NOT EXISTS host_links (
    id TEXT PRIMARY KEY, owner TEXT NOT NULL, token_hash TEXT NOT NULL, kind TEXT NOT NULL,
    platform TEXT NOT NULL, clis TEXT NOT NULL, seen REAL NOT NULL, baseline_hash TEXT, runner_code TEXT
);
CREATE TABLE IF NOT EXISTS link_jobs (
    id TEXT PRIMARY KEY, link_id TEXT NOT NULL REFERENCES host_links(id), state TEXT NOT NULL,
    payload TEXT NOT NULL, expires REAL NOT NULL, result TEXT, claim TEXT
);

CREATE TABLE IF NOT EXISTS milestone_deliveries (
    delivery_id INTEGER PRIMARY KEY AUTOINCREMENT,
    milestone_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    collab_mode TEXT NOT NULL,
    commit_sha TEXT,
    prev_commit_sha TEXT,
    artifact_digest TEXT,
    rolling_branch TEXT,
    archive_branch TEXT,
    compare_url TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(milestone_id, version_no)
);
CREATE INDEX IF NOT EXISTS idx_runs_task ON task_runs(task_id);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id);

CREATE TABLE IF NOT EXISTS gate_records (
    record_id INTEGER PRIMARY KEY AUTOINCREMENT,
    stage_id TEXT NOT NULL,
    gate_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    round INTEGER NOT NULL DEFAULT 1,
    actor TEXT,
    summary TEXT,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_gate_scope_round
    ON gate_records(gate_id, scope_type, scope_id, round);

CREATE TABLE IF NOT EXISTS quality_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    milestone_id TEXT,
    attempt INTEGER NOT NULL DEFAULT 1,
    gate TEXT NOT NULL,
    status TEXT NOT NULL,
    skipped_reason TEXT NOT NULL DEFAULT '',
    duration_sec REAL NOT NULL DEFAULT 0,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    findings_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS code_reviews (
    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    milestone_id TEXT,
    round INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'OPEN',
    head_ref TEXT NOT NULL,
    base_ref TEXT,
    risk_reasons_json TEXT NOT NULL DEFAULT '[]',
    ai_summary_md TEXT,
    checklist_json TEXT NOT NULL DEFAULT '{}',
    reviewer TEXT,
    decision_note TEXT,
    override_reason TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_review_scope_round
    ON code_reviews(scope_type, scope_id, round);

CREATE TABLE IF NOT EXISTS review_comments (
    comment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id INTEGER NOT NULL,
    file TEXT NOT NULL,
    line INTEGER NOT NULL DEFAULT 0,
    side TEXT NOT NULL DEFAULT 'new',
    severity TEXT NOT NULL DEFAULT 'major',
    body TEXT NOT NULL,
    author TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'human',
    resolved INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_outbox (
    event_key TEXT PRIMARY KEY, payload TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS model_budget_reservations(
    request_id TEXT PRIMARY KEY, tokens INTEGER NOT NULL, finalized INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS candidate_evidence (
    task_id TEXT NOT NULL, candidate TEXT NOT NULL, attempt INTEGER NOT NULL,
    issue_id TEXT, run_id TEXT, diff_digest TEXT NOT NULL, review_context TEXT NOT NULL,
    verified_requirements TEXT NOT NULL, verified_at TEXT NOT NULL,
    PRIMARY KEY(task_id,candidate,attempt)
);

CREATE TABLE IF NOT EXISTS ci_runs (
    ci_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    milestone_id TEXT NOT NULL,
    round INTEGER NOT NULL DEFAULT 1,
    provider TEXT NOT NULL,
    ref TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    candidate_ref TEXT NOT NULL DEFAULT '',
    external_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'QUEUED',
    url TEXT,
    summary_json TEXT NOT NULL DEFAULT '{}',
    log_tail TEXT,
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deployments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    milestone_id TEXT NOT NULL,
    version_no INTEGER NOT NULL DEFAULT 1,
    target TEXT NOT NULL DEFAULT 'staging',
    status TEXT NOT NULL DEFAULT 'PENDING',
    actor TEXT NOT NULL,
    log_tail TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS doc_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    version INTEGER NOT NULL,
    content_sha TEXT NOT NULL,
    content_text TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(path, version)
);

CREATE TABLE IF NOT EXISTS spec_revisions (
    rev_id INTEGER PRIMARY KEY AUTOINCREMENT,
    feature_id TEXT NOT NULL,
    rev_no INTEGER NOT NULL,
    source_kind TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    content_sha TEXT NOT NULL,
    content_version_id INTEGER NOT NULL REFERENCES doc_versions(id),
    parent_rev_id INTEGER REFERENCES spec_revisions(rev_id),
    preset_id TEXT NOT NULL,
    preset_version TEXT NOT NULL,
    speckit_version TEXT NOT NULL,
    input_sha TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'DRAFT',
    approved_gate_id INTEGER,
    author TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    handoff_brief_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(feature_id, source_kind, rev_no)
);
CREATE TABLE IF NOT EXISTS clarifications (
    clarify_id INTEGER PRIMARY KEY AUTOINCREMENT,
    feature_id TEXT NOT NULL,
    spec_rev_id INTEGER NOT NULL REFERENCES spec_revisions(rev_id),
    question_key TEXT NOT NULL,
    category TEXT NOT NULL,
    severity TEXT NOT NULL,
    question TEXT NOT NULL,
    options_json TEXT NOT NULL DEFAULT '[]',
    default_option TEXT NOT NULL DEFAULT '',
    answer TEXT NOT NULL DEFAULT '',
    rationale TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'OPEN',
    answered_by TEXT NOT NULL DEFAULT '',
    applied_rev_id INTEGER REFERENCES spec_revisions(rev_id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(feature_id, spec_rev_id, question_key)
);
CREATE TABLE IF NOT EXISTS doc_comments (
    comment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    section TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,
    author TEXT NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS requirements (
    req_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    priority TEXT NOT NULL DEFAULT 'P1',
    acceptance_json TEXT NOT NULL DEFAULT '[]',
    source_section TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS members (
    user_id TEXT NOT NULL,
    project TEXT NOT NULL DEFAULT '*',
    role TEXT NOT NULL DEFAULT 'developer',
    created_at TEXT NOT NULL,
    PRIMARY KEY(user_id, project)
);
"""

_TASK_JSON_FIELDS = (
    "acceptance_criteria",
    "depends_on",
    "doc_slice_ids",
    "allowed_paths",
    "verify_cmds",
    "requirement_ids",
    "design_refs",
)
_MS_JSON_FIELDS = ("target_squads", "key_deliverables")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class Task:
    task_id: str
    milestone_id: str
    squad: str
    module: str
    role: str
    title: str
    description: str
    acceptance_criteria: list[str]
    depends_on: list[str]
    doc_slice_ids: list[str]
    allowed_paths: list[str]
    verify_cmds: list[str]
    priority: int
    status: TaskStatus
    retry_count: int
    max_retries: int
    arbitration_count: int
    assigned_slot: str | None
    active_agent_label: str | None
    loopforge_issue_id: str | None
    loopforge_run_id: str | None
    worktree_path: str | None
    branch_name: str | None
    base_sha: str | None
    last_session_id: str | None
    artifact_digest: str | None
    candidate_commit_sha: str | None
    bound_contract_hash: str | None
    last_feedback: str | None
    failure_reason: str | None
    steer_note: str | None
    pending_action: str | None = None
    owner_member: str | None = None
    exec_seq: int = 0
    updated_at: str | None = None
    requirement_ids: list[str] = field(default_factory=list)
    design_refs: list[str] = field(default_factory=list)
    feature_id: str = ""
    spec_rev_id: int = 0
    spec_sha: str = ""
    plan_sha: str = ""
    compiled_task_hash: str = ""
    compiled_package_json: str = "{}"
    task_key: str = ""
    handoff_brief_json: str = "{}"

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Task":
        data = dict(row)
        for name in _TASK_JSON_FIELDS:
            data[name] = json.loads(data.pop(f"{name}_json", None) or "[]")
        data["status"] = TaskStatus(data["status"])
        data["squad"] = data.get("squad") or data.get("module") or ""
        data["arbitration_count"] = int(data.get("arbitration_count") or 0)
        data["exec_seq"] = int(data.get("exec_seq") or 0)
        return cls(**{f.name: data.get(f.name) for f in fields(cls)})

    def to_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass
class Milestone:
    milestone_id: str
    seq_order: int
    title: str
    goal: str
    integration_cmd: str
    status: MilestoneStatus
    planned: bool
    target_squads: list[str]
    key_deliverables: list[str]
    wave_notes: str | None
    branch_name: str | None
    commit_matrix_json: str | None
    report_path: str | None
    failure_reason: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Milestone":
        data = dict(row)
        for name in _MS_JSON_FIELDS:
            data[name] = json.loads(data.pop(f"{name}_json", None) or "[]")
        data["status"] = MilestoneStatus(data["status"])
        data["planned"] = bool(data.get("planned", 1))
        return cls(**{f.name: data.get(f.name) for f in fields(cls)})

    def to_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass
class DocSlice:
    slice_id: str
    source_file: str
    heading_path: str
    summary: str
    content: str
    char_count: int


EventListener = Callable[[dict[str, Any]], None]


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=30)
        self._conn.row_factory = sqlite3.Row
        mode = sqlite_journal_mode()
        self._conn.execute(f"PRAGMA journal_mode={mode}")
        if mode != "WAL":
            self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.executescript(SCHEMA)
        with self.transaction():
            self._init_schema()
        self.listeners: list[EventListener] = []

    def _init_schema(self) -> None:
        def _cols(table: str) -> set[str]:
            return {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}

        additions = {
            "gate_records": {
                "target_path": "TEXT NOT NULL DEFAULT ''",
                "target_sha": "TEXT NOT NULL DEFAULT ''",
                "target_version": "INTEGER NOT NULL DEFAULT 0",
                "target_snapshot_json": "TEXT NOT NULL DEFAULT '[]'",
                "decision_status": "TEXT NOT NULL DEFAULT ''",
                "stale_reason": "TEXT NOT NULL DEFAULT ''",
            },
            "link_codes": {"owner_id": "TEXT", "verified_at": "REAL NOT NULL DEFAULT 0"},
            "host_links": {"codex_login": "INTEGER NOT NULL DEFAULT 0", "owner_id": "TEXT",
                           "verified_at": "REAL NOT NULL DEFAULT 0", "revoked_at": "REAL", "expires_at": "REAL"},
            "evolution_checkpoints": {
                "plan_revision": "INTEGER NOT NULL DEFAULT 0",
                "min_director_rework": "INTEGER NOT NULL DEFAULT 0",
                "draft_artifact_id": "TEXT",
                "rejected_plans_json": "TEXT NOT NULL DEFAULT '[]'",
                "last_error": "TEXT",
            },
            "tasks": {
                "feature_id": "TEXT NOT NULL DEFAULT ''",
                "spec_rev_id": "INTEGER NOT NULL DEFAULT 0",
                "spec_sha": "TEXT NOT NULL DEFAULT ''",
                "plan_sha": "TEXT NOT NULL DEFAULT ''",
                "compiled_task_hash": "TEXT NOT NULL DEFAULT ''",
                "compiled_package_json": "TEXT NOT NULL DEFAULT '{}'",
                "task_key": "TEXT NOT NULL DEFAULT ''",
                "handoff_brief_json": "TEXT NOT NULL DEFAULT '{}'",
                "source_checkpoint_id": "INTEGER",
                "source_item_idx": "INTEGER",
                "requirement_ids_json": "TEXT NOT NULL DEFAULT '[]'",
                "design_refs_json": "TEXT NOT NULL DEFAULT '[]'",
                "exec_seq": "INTEGER NOT NULL DEFAULT 0",
            },
            "milestone_deliveries": {"evidence_json": "TEXT NOT NULL DEFAULT '{}'"},
            "dispatch_records": {
                "feature_id": "TEXT NOT NULL DEFAULT ''",
                "stage": "TEXT NOT NULL DEFAULT ''",
                "expected_baseline_sha": "TEXT NOT NULL DEFAULT ''",
                "spec_sha": "TEXT NOT NULL DEFAULT ''",
                "contract_sha": "TEXT NOT NULL DEFAULT ''",
                "compiled_task_hash": "TEXT NOT NULL DEFAULT ''",
                "asset_versions_json": "TEXT NOT NULL DEFAULT '{}'",
            },
            "ci_runs": {
                "candidate_ref": "TEXT NOT NULL DEFAULT ''",
                "external_id": "TEXT NOT NULL DEFAULT ''",
            },
        }
        for table, columns in additions.items():
            existing = _cols(table)
            for column, declaration in columns.items():
                if column not in existing:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        # Keep historical run ids. Signed callbacks continue to resolve to the newest original round.
        self._conn.execute("""
            WITH ranked AS (
                SELECT ci_run_id, milestone_id, ROW_NUMBER() OVER (
                    PARTITION BY milestone_id,round ORDER BY ci_run_id DESC) AS duplicate,
                    MAX(round) OVER (PARTITION BY milestone_id) AS last_round FROM ci_runs
            ), reassigned AS (
                SELECT ci_run_id,last_round+ROW_NUMBER() OVER (
                    PARTITION BY milestone_id ORDER BY ci_run_id) AS new_round FROM ranked WHERE duplicate>1
            ) UPDATE ci_runs SET round=(SELECT new_round FROM reassigned WHERE reassigned.ci_run_id=ci_runs.ci_run_id)
              WHERE ci_run_id IN (SELECT ci_run_id FROM reassigned)
        """)
        self._conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_ci_milestone_round ON ci_runs(milestone_id,round)")
        self._conn.execute("UPDATE host_links SET expires_at=unixepoch()+90*86400 WHERE expires_at IS NULL")
        self._conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_checkpoint_item "
                           "ON tasks(source_checkpoint_id, source_item_idx) WHERE source_checkpoint_id IS NOT NULL")

        r_cols = _cols("task_runs")
        if "reasoning_tokens" not in r_cols:
            self._conn.execute("ALTER TABLE task_runs ADD COLUMN reasoning_tokens INTEGER NOT NULL DEFAULT 0")
        if "agent_label" not in r_cols:
            self._conn.execute("ALTER TABLE task_runs ADD COLUMN agent_label TEXT")

        m_cols = _cols("milestones")
        if "planned" not in m_cols:
            self._conn.execute("ALTER TABLE milestones ADD COLUMN planned INTEGER NOT NULL DEFAULT 1")
        if "target_squads_json" not in m_cols:
            self._conn.execute("ALTER TABLE milestones ADD COLUMN target_squads_json TEXT NOT NULL DEFAULT '[]'")
        if "key_deliverables_json" not in m_cols:
            self._conn.execute("ALTER TABLE milestones ADD COLUMN key_deliverables_json TEXT NOT NULL DEFAULT '[]'")
        if "wave_notes" not in m_cols:
            self._conn.execute("ALTER TABLE milestones ADD COLUMN wave_notes TEXT")

        t_cols = _cols("tasks")
        if "squad" not in t_cols:
            self._conn.execute("ALTER TABLE tasks ADD COLUMN squad TEXT NOT NULL DEFAULT ''")
        if "arbitration_count" not in t_cols:
            self._conn.execute("ALTER TABLE tasks ADD COLUMN arbitration_count INTEGER NOT NULL DEFAULT 0")
        if "assigned_slot" not in t_cols:
            self._conn.execute("ALTER TABLE tasks ADD COLUMN assigned_slot TEXT")
        if "active_agent_label" not in t_cols:
            self._conn.execute("ALTER TABLE tasks ADD COLUMN active_agent_label TEXT")
        if "artifact_digest" not in t_cols:
            self._conn.execute("ALTER TABLE tasks ADD COLUMN artifact_digest TEXT")
        if "loopforge_issue_id" not in t_cols:
            self._conn.execute("ALTER TABLE tasks ADD COLUMN loopforge_issue_id TEXT")
        if "loopforge_run_id" not in t_cols:
            self._conn.execute("ALTER TABLE tasks ADD COLUMN loopforge_run_id TEXT")

        s_cols = _cols("squads")
        if "sandbox_profile" not in s_cols:
            self._conn.execute("ALTER TABLE squads ADD COLUMN sandbox_profile TEXT NOT NULL DEFAULT 'generic-docker'")

    @contextmanager
    def transaction(self):
        """Only synchronous work may run inside this write transaction."""
        with self._lock:
            if self._conn.in_transaction:
                yield
                return
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self._conn.close()

    # ---------- 基础 ----------
    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    # ---------- meta ----------
    def meta_get(self, key: str, default: str | None = None) -> str | None:
        rows = self._query("SELECT value FROM meta WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def meta_set(self, key: str, value: str | None) -> None:
        self._exec(
            "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def next_task_number(self) -> int:
        last = self.meta_get("task_seq")
        sources = "SELECT task_id FROM tasks"
        if last is None:
            sources += " UNION SELECT task_id FROM task_runs UNION SELECT task_id FROM events"
        maximum = int(last or 0)
        for row in self._query(sources):
            match = re.fullmatch(r"T-([0-9]+)", row[0] or "")
            if match:
                maximum = max(maximum, int(match[1]))
        return maximum + 1

    def next_task_id(self) -> str:
        with self.transaction():
            number = self.next_task_number()
            self.meta_set("task_seq", str(number))
            return f"T-{number:03d}"

    # ---------- 切片 ----------
    def replace_slices(self, slices: Iterable[DocSlice]) -> None:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.execute("DELETE FROM doc_slices")
                self._conn.executemany(
                    "INSERT INTO doc_slices VALUES(?, ?, ?, ?, ?, ?)",
                    [(s.slice_id, s.source_file, s.heading_path, s.summary, s.content, s.char_count) for s in slices],
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def upsert_slice(self, s: DocSlice) -> None:
        self._exec(
            "INSERT INTO doc_slices VALUES(?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(slice_id) DO UPDATE SET source_file=excluded.source_file,"
            " heading_path=excluded.heading_path, summary=excluded.summary,"
            " content=excluded.content, char_count=excluded.char_count",
            (s.slice_id, s.source_file, s.heading_path, s.summary, s.content, s.char_count),
        )

    def list_slices(self) -> list[DocSlice]:
        return [DocSlice(**dict(r)) for r in self._query("SELECT * FROM doc_slices ORDER BY slice_id")]

    def get_slices(self, ids: Iterable[str]) -> list[DocSlice]:
        ids = list(ids)
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        rows = self._query(f"SELECT * FROM doc_slices WHERE slice_id IN ({marks})", ids)
        by_id = {r["slice_id"]: DocSlice(**dict(r)) for r in rows}
        return [by_id[i] for i in ids if i in by_id]

    # ---------- 多智能体架构评审圆桌（Council） ----------
    def add_council_message(
        self,
        *,
        round_no: int,
        speaker_role: str,
        speaker_name: str,
        kind: str,
        headline: str,
        body: dict[str, Any],
        harness: str = "",
        model: str = "",
    ) -> dict[str, Any]:
        created = now_iso()
        cur = self._exec(
            "INSERT INTO council_messages(round_no, speaker_role, speaker_name, harness, model, kind, headline, body_json, created_at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (round_no, speaker_role, speaker_name, harness, model, kind, headline, json.dumps(body, ensure_ascii=False), created),
        )
        return {
            "msg_id": int(cur.lastrowid),
            "round_no": round_no,
            "speaker_role": speaker_role,
            "speaker_name": speaker_name,
            "harness": harness,
            "model": model,
            "kind": kind,
            "headline": headline,
            "body": body,
            "created_at": created,
        }

    def list_council_messages(self) -> list[dict[str, Any]]:
        out = []
        for r in self._query("SELECT * FROM council_messages ORDER BY msg_id"):
            d = dict(r)
            d["body"] = json.loads(d.pop("body_json") or "{}")
            out.append(d)
        return out

    def clear_council_messages(self) -> None:
        self._exec("DELETE FROM council_messages")

    # ---------- 研发组（Squads） ----------
    def list_squads(self) -> list[dict[str, Any]]:
        out = []
        for r in self._query("SELECT * FROM squads ORDER BY seq_order, squad_id"):
            d = dict(r)
            d["owned_paths"] = json.loads(d.pop("owned_paths_json") or "[]")
            d["roles"] = json.loads(d.pop("roles_json") or "[]")
            out.append(d)
        return out

    # ---------- 规划入库（单次模式 & 立项蓝图滚动模式） ----------
    def _insert_plan_task(self, t: PlanTask, max_retries: int, done_ids: set[str] | None = None) -> None:
        done_ids = done_ids or set()
        unmet = [d for d in t.depends_on if d not in done_ids]
        status = TaskStatus.BLOCKED if unmet else TaskStatus.READY
        self._conn.execute(
            "INSERT INTO tasks(task_id, milestone_id, squad, module, role, title, description,"
            " acceptance_criteria_json, depends_on_json, doc_slice_ids_json, allowed_paths_json,"
            " verify_cmds_json, requirement_ids_json, design_refs_json, priority, status, max_retries, updated_at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                t.task_id,
                t.milestone_id,
                t.squad or t.module,
                t.module,
                t.role,
                t.title,
                t.description,
                json.dumps(t.acceptance_criteria, ensure_ascii=False),
                json.dumps(t.depends_on),
                json.dumps(t.doc_slice_ids),
                json.dumps(t.allowed_paths, ensure_ascii=False),
                json.dumps(t.verify_cmds, ensure_ascii=False),
                json.dumps(t.requirement_ids, ensure_ascii=False),
                json.dumps(t.design_refs, ensure_ascii=False),
                t.priority,
                status.value,
                max_retries,
                now_iso(),
            ),
        )

    def save_plan(self, plan: ProjectPlan, max_retries: int) -> None:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.execute("DELETE FROM tasks")
                self._conn.execute("DELETE FROM milestones")
                self._conn.execute("DELETE FROM squads")
                for i, mod in enumerate(plan.modules):
                    self._conn.execute(
                        "INSERT INTO squads(squad_id, seq_order, name, mission, owned_paths_json, roles_json)"
                        " VALUES(?, ?, ?, ?, ?, ?)",
                        (
                            mod.name,
                            i,
                            mod.name,
                            mod.description,
                            json.dumps([mod.path], ensure_ascii=False),
                            json.dumps(["coder"], ensure_ascii=False),
                        ),
                    )
                for i, m in enumerate(plan.milestones):
                    self._conn.execute(
                        "INSERT INTO milestones(milestone_id, seq_order, title, goal, integration_cmd, status, planned)"
                        " VALUES(?, ?, ?, ?, ?, ?, 1)",
                        (m.milestone_id, i, m.title, m.goal, m.integration_cmd, MilestoneStatus.PENDING.value),
                    )
                for t in plan.tasks:
                    self._insert_plan_task(t, max_retries)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def save_blueprint(self, bp: ProjectBlueprint, max_retries: int) -> None:
        planned_ms = {t.milestone_id for t in bp.initial_tasks}
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.execute("DELETE FROM tasks")
                self._conn.execute("DELETE FROM milestones")
                self._conn.execute("DELETE FROM squads")
                for i, sq in enumerate(bp.squads):
                    self._conn.execute(
                        "INSERT INTO squads(squad_id, seq_order, name, mission, owned_paths_json, roles_json, sandbox_profile)"
                        " VALUES(?, ?, ?, ?, ?, ?, ?)",
                        (
                            sq.squad_id,
                            i,
                            sq.name,
                            sq.mission,
                            json.dumps(sq.owned_paths, ensure_ascii=False),
                            json.dumps(sq.roles, ensure_ascii=False),
                            sq.sandbox_profile or "generic-docker",
                        ),
                    )
                for i, m in enumerate(bp.milestones):
                    is_planned = 1 if m.milestone_id in planned_ms else 0
                    self._conn.execute(
                        "INSERT INTO milestones(milestone_id, seq_order, title, goal, integration_cmd,"
                        " status, planned, target_squads_json, key_deliverables_json)"
                        " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            m.milestone_id,
                            i,
                            m.title,
                            m.goal,
                            m.integration_cmd,
                            MilestoneStatus.PENDING.value,
                            is_planned,
                            json.dumps(m.target_squads, ensure_ascii=False),
                            json.dumps(m.key_deliverables, ensure_ascii=False),
                        ),
                    )
                for t in bp.initial_tasks:
                    self._insert_plan_task(t, max_retries)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def append_wave_tasks(self, milestone_id: str, tasks: list[PlanTask], wave_notes: str, max_retries: int) -> None:
        done_ids = {
            t.task_id
            for t in self.list_tasks()
            if t.status in (TaskStatus.APPROVED, TaskStatus.MERGED)
        }
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                for t in tasks:
                    self._insert_plan_task(t, max_retries, done_ids=done_ids)
                self._conn.execute(
                    "UPDATE milestones SET planned=1, wave_notes=? WHERE milestone_id=?",
                    (wave_notes, milestone_id),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def insert_single_task(self, t: PlanTask, max_retries: int) -> None:
        done_ids = {
            x.task_id
            for x in self.list_tasks()
            if x.status in (TaskStatus.APPROVED, TaskStatus.MERGED)
        }
        with self._lock:
            self._insert_plan_task(t, max_retries, done_ids=done_ids)

    # ---------- 工位实况席（Worker Slots） ----------
    def ensure_slots(self, max_concurrency: int) -> None:
        now = now_iso()
        with self._lock:
            self._conn.execute(
                "INSERT INTO worker_slots(slot_id, slot_kind, slot_label, status, updated_at)"
                " VALUES('command', 'command', '指挥席 · 顶层大脑', 'IDLE', ?)"
                " ON CONFLICT(slot_id) DO NOTHING",
                (now,),
            )
            for idx in range(1, max_concurrency + 1):
                sid = f"slot-{idx}"
                self._conn.execute(
                    "INSERT INTO worker_slots(slot_id, slot_kind, slot_label, status, updated_at)"
                    " VALUES(?, 'worker', ?, 'IDLE', ?)"
                    " ON CONFLICT(slot_id) DO NOTHING",
                    (sid, f"工位 #{idx}", now),
                )
            # 清理缩减并发后多余的空闲工位
            self._conn.execute(
                "DELETE FROM worker_slots WHERE slot_kind='worker' AND status='IDLE'"
                " AND CAST(SUBSTR(slot_id, 6) AS INTEGER) > ?",
                (max_concurrency,),
            )

    def update_slot(self, slot_id: str, **values: Any) -> None:
        values["updated_at"] = now_iso()
        cols = ", ".join(f"{k}=?" for k in values)
        self._exec(f"UPDATE worker_slots SET {cols} WHERE slot_id=?", [*values.values(), slot_id])

    def release_slot(self, slot_id: str) -> None:
        self.update_slot(
            slot_id,
            status="IDLE",
            squad=None,
            role=None,
            harness=None,
            model=None,
            task_id=None,
            worktree_path=None,
            session_id=None,
            current_action=None,
            started_at=None,
        )

    def reset_all_slots(self) -> None:
        self._exec(
            "UPDATE worker_slots SET status='IDLE', squad=NULL, role=NULL, harness=NULL, model=NULL,"
            " task_id=NULL, worktree_path=NULL, session_id=NULL, current_action=NULL, started_at=NULL, updated_at=?",
            (now_iso(),),
        )

    def list_slots(self) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT * FROM worker_slots ORDER BY CASE WHEN slot_kind='command' THEN 0 ELSE 1 END, slot_id"
        )
        return [dict(r) for r in rows]

    # ---------- 里程碑 ----------
    def list_milestone_deliveries(self, milestone_id=None):
        sql = "SELECT * FROM milestone_deliveries"
        rows = (self._query(sql + " WHERE milestone_id=? ORDER BY version_no", (milestone_id,))
                if milestone_id else self._query(sql + " ORDER BY delivery_id"))
        return [dict(row) for row in rows]

    def record_milestone_delivery(self, milestone_id, version_no, collab_mode, *, commit_sha=None,
                                  prev_commit_sha=None, artifact_digest=None, rolling_branch=None,
                                  archive_branch=None, compare_url=None):
        self._exec("INSERT INTO milestone_deliveries(milestone_id,version_no,collab_mode,commit_sha,prev_commit_sha,"
                   "artifact_digest,rolling_branch,archive_branch,compare_url,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (milestone_id, version_no, collab_mode, commit_sha, prev_commit_sha, artifact_digest,
                    rolling_branch, archive_branch, compare_url, now_iso()))

    def list_milestones(self) -> list[Milestone]:
        return [Milestone.from_row(r) for r in self._query("SELECT * FROM milestones ORDER BY seq_order")]

    def get_milestone(self, milestone_id: str) -> Milestone | None:
        rows = self._query("SELECT * FROM milestones WHERE milestone_id=?", (milestone_id,))
        return Milestone.from_row(rows[0]) if rows else None

    def update_milestone(self, milestone_id: str, **values: Any) -> None:
        if not values:
            return
        cols, params = [], []
        for k, v in values.items():
            if k in _MS_JSON_FIELDS:
                k, v = f"{k}_json", json.dumps(v, ensure_ascii=False)
            cols.append(f"{k}=?")
            params.append(v.value if hasattr(v, "value") else (int(v) if isinstance(v, bool) else v))
        self._exec(f"UPDATE milestones SET {', '.join(cols)} WHERE milestone_id=?", [*params, milestone_id])

    # ---------- 任务 ----------
    def list_tasks(self, milestone_id: str | None = None, status: TaskStatus | None = None) -> list[Task]:
        sql = "SELECT t.* FROM tasks t JOIN milestones m ON m.milestone_id=t.milestone_id WHERE 1=1"
        params: list[Any] = []
        if milestone_id:
            sql += " AND t.milestone_id=?"
            params.append(milestone_id)
        if status:
            sql += " AND t.status=?"
            params.append(status.value)
        sql += " ORDER BY m.seq_order, t.priority, t.task_id"
        return [Task.from_row(r) for r in self._query(sql, params)]

    def get_task(self, task_id: str) -> Task | None:
        rows = self._query("SELECT * FROM tasks WHERE task_id=?", (task_id,))
        return Task.from_row(rows[0]) if rows else None

    def claim_task_execution(self, task_id: str, *, allow_from_ready: bool = False) -> int | None:
        """原子声明并递增任务执行代次（exec_seq），用于隔离旧协程与替代执行。"""
        active_vals = [s.value for s in ACTIVE_TASK_STATUSES]
        if allow_from_ready:
            active_vals.append(TaskStatus.READY.value)
        marks = ",".join("?" * len(active_vals))
        ts = now_iso()
        with self.transaction():
            if allow_from_ready:
                cur = self._exec(
                    f"UPDATE tasks SET exec_seq = exec_seq + 1, status = CASE WHEN status='READY' THEN 'IN_PROGRESS' ELSE status END,"
                    f" updated_at = ? WHERE task_id = ? AND status IN ({marks})",
                    [ts, task_id, *active_vals],
                )
            else:
                cur = self._exec(
                    f"UPDATE tasks SET exec_seq = exec_seq + 1, updated_at = ? WHERE task_id = ? AND status IN ({marks})",
                    [ts, task_id, *active_vals],
                )
            if cur.rowcount != 1:
                return None
            row = self._query("SELECT exec_seq FROM tasks WHERE task_id=?", (task_id,))
            return int(row[0]["exec_seq"]) if row else None

    def _ownership_where(
        self,
        task_id: str,
        *,
        expected_exec_seq: int | None = None,
        expected_attempt: int | None = None,
        expected_run_id: str | None = None,
    ) -> tuple[str, list[Any]]:
        active_vals = [s.value for s in ACTIVE_TASK_STATUSES]
        marks = ",".join("?" * len(active_vals))
        clauses = ["task_id=?", f"status IN ({marks})"]
        params: list[Any] = [task_id, *active_vals]
        if expected_exec_seq is not None:
            clauses.append("exec_seq=?")
            params.append(int(expected_exec_seq))
        if expected_attempt is not None:
            clauses.append("retry_count=?")
            params.append(int(expected_attempt) - 1)
        if expected_run_id is not None:
            if expected_run_id == "":
                clauses.append("(loopforge_run_id IS NULL OR loopforge_run_id='')")
            else:
                clauses.append("loopforge_run_id=?")
                params.append(expected_run_id)
        return " AND ".join(clauses), params

    def get_owned_task(
        self,
        task_id: str,
        *,
        expected_exec_seq: int | None = None,
        expected_attempt: int | None = None,
        expected_run_id: str | None = None,
    ) -> Task | None:
        where_sql, params = self._ownership_where(
            task_id,
            expected_exec_seq=expected_exec_seq,
            expected_attempt=expected_attempt,
            expected_run_id=expected_run_id,
        )
        rows = self._query(f"SELECT * FROM tasks WHERE {where_sql}", params)
        return Task.from_row(rows[0]) if rows else None

    def is_task_owned(
        self,
        task_id: str,
        *,
        expected_exec_seq: int | None = None,
        expected_attempt: int | None = None,
        expected_run_id: str | None = None,
    ) -> bool:
        return (
            self.get_owned_task(
                task_id,
                expected_exec_seq=expected_exec_seq,
                expected_attempt=expected_attempt,
                expected_run_id=expected_run_id,
            )
            is not None
        )

    def update_task_if_owned(
        self,
        task_id: str,
        *,
        expected_exec_seq: int | None = None,
        expected_attempt: int | None = None,
        expected_run_id: str | None = None,
        **values: Any,
    ) -> bool:
        """CAS 原子更新：仅当任务仍处于活跃状态且代次/重试轮次/远端 Run ID 完全匹配时生效。"""
        values["updated_at"] = now_iso()
        cols, set_params = [], []
        for key, value in values.items():
            if key in _TASK_JSON_FIELDS:
                key, value = f"{key}_json", json.dumps(value, ensure_ascii=False)
            cols.append(f"{key}=?")
            set_params.append(value.value if hasattr(value, "value") else value)
        where_sql, where_params = self._ownership_where(
            task_id,
            expected_exec_seq=expected_exec_seq,
            expected_attempt=expected_attempt,
            expected_run_id=expected_run_id,
        )
        cur = self._exec(f"UPDATE tasks SET {', '.join(cols)} WHERE {where_sql}", [*set_params, *where_params])
        return cur.rowcount == 1

    def update_task(self, task_id: str, **values: Any) -> None:
        values["updated_at"] = now_iso()
        bump_for_status = False
        if "status" in values:
            raw_st = values["status"].value if hasattr(values["status"], "value") else str(values["status"])
            if raw_st not in {s.value for s in ACTIVE_TASK_STATUSES}:
                bump_for_status = True
        new_run_id = values.get("loopforge_run_id") if isinstance(values.get("loopforge_run_id"), str) else ""
        cols, params = [], []
        if "exec_seq" not in values:
            if bump_for_status:
                cols.append("exec_seq = exec_seq + 1")
            elif new_run_id.strip():
                cols.append(
                    "exec_seq = CASE WHEN loopforge_run_id IS NOT NULL AND loopforge_run_id != '' AND loopforge_run_id != ?"
                    " THEN exec_seq + 1 ELSE exec_seq END"
                )
                params.append(new_run_id.strip())
        for key, value in values.items():
            if key in _TASK_JSON_FIELDS:
                key, value = f"{key}_json", json.dumps(value, ensure_ascii=False)
            cols.append(f"{key}=?")
            params.append(value.value if hasattr(value, "value") else value)
        self._exec(f"UPDATE tasks SET {', '.join(cols)} WHERE task_id=? AND status != 'CANCELLED'", [*params, task_id])

    # ---------- 调用记录 ----------
    def start_run(
        self, task_id: str, attempt_no: int, stage: str, harness_name: str = "", agent_label: str = ""
    ) -> int:
        cur = self._exec(
            "INSERT INTO task_runs(task_id, attempt_no, stage, harness_name, agent_label, started_at)"
            " VALUES(?, ?, ?, ?, ?, ?)",
            (task_id, attempt_no, stage, harness_name, agent_label, now_iso()),
        )
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, **values: Any) -> None:
        values["finished_at"] = now_iso()
        cols = ", ".join(f"{k}=?" for k in values)
        self._exec(f"UPDATE task_runs SET {cols} WHERE run_id=?", [*values.values(), run_id])

    def list_runs(self, task_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._query("SELECT * FROM task_runs WHERE task_id=? ORDER BY run_id", (task_id,))]

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM task_runs WHERE run_id=?", (run_id,))
        return dict(rows[0]) if rows else None

    def usage_by_task(self) -> dict[str, dict[str, int]]:
        rows = self._query(
            "SELECT task_id, SUM(input_tokens) i, SUM(cached_tokens) c, SUM(output_tokens) o, SUM(reasoning_tokens) r"
            " FROM task_runs GROUP BY task_id"
        )
        return {
            r["task_id"]: {
                "input": r["i"] or 0,
                "cached": r["c"] or 0,
                "output": r["o"] or 0,
                "reasoning": r["r"] or 0,
            }
            for r in rows
        }

    def latest_run_by_task(self) -> dict[str, dict[str, Any]]:
        rows = self._query(
            "SELECT r.* FROM task_runs r"
            " JOIN (SELECT task_id, MAX(run_id) AS max_id FROM task_runs GROUP BY task_id) m"
            " ON r.run_id = m.max_id"
        )
        return {r["task_id"]: dict(r) for r in rows}

    def latest_event_by_task(self) -> dict[str, dict[str, Any]]:
        rows = self._query(
            "SELECT e.* FROM events e"
            " JOIN (SELECT task_id, MAX(event_id) AS max_id FROM events WHERE task_id IS NOT NULL GROUP BY task_id) m"
            " ON e.event_id = m.max_id"
        )
        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d.pop("payload_json")) if d.get("payload_json") else None
            out[d["task_id"]] = d
        return out

    # ---------- 事件 ----------
    def add_event(
        self,
        event_type: str,
        message: str,
        task_id: str | None = None,
        actor: str = "system",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        created = now_iso()
        payload_json = json.dumps(payload, ensure_ascii=False) if payload else None
        cur = self._exec(
            "INSERT INTO events(actor, event_type, task_id, message, payload_json, created_at)"
            " VALUES(?, ?, ?, ?, ?, ?)",
            (actor, event_type, task_id, message, payload_json, created),
        )
        event = {
            "event_id": int(cur.lastrowid),
            "actor": actor,
            "event_type": event_type,
            "task_id": task_id,
            "message": message,
            "payload": payload,
            "created_at": created,
        }
        for listener in list(self.listeners):
            try:
                listener(event)
            except Exception:
                pass
        return event

    def events_after(self, event_id: int, limit: int = 500, task_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM events WHERE event_id>?"
        params: list[Any] = [event_id]
        if task_id:
            sql += " AND task_id=?"
            params.append(task_id)
        sql += " ORDER BY event_id LIMIT ?"
        params.append(limit)
        out = []
        for r in self._query(sql, params):
            d = dict(r)
            d["payload"] = json.loads(d.pop("payload_json")) if d.get("payload_json") else None
            out.append(d)
        return out

    def last_event_id(self) -> int:
        rows = self._query("SELECT MAX(event_id) m FROM events")
        return int(rows[0]["m"] or 0)

    # ---------- 项目生效规矩（同步 LoopForge 全局 Rule）与演进节点（Checkpoints） ----------
    def list_decisions(self, *, active_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM project_decisions"
        if active_only:
            sql += " WHERE status='ACTIVE'"
        sql += " ORDER BY decision_id"
        out: list[dict[str, Any]] = []
        for r in self._query(sql):
            d = dict(r)
            d["affected_paths"] = json.loads(d.pop("affected_paths_json", None) or "[]")
            out.append(d)
        return out

    def upsert_decision(
        self,
        *,
        decision_id: str,
        title: str,
        rule_text: str,
        rationale: str = "",
        affected_paths: list[str] | None = None,
        status: str = "ACTIVE",
        checkpoint_id: int | None = None,
    ) -> dict[str, Any]:
        ts = now_iso()
        existing = self._query("SELECT version FROM project_decisions WHERE decision_id=?", (decision_id,))
        ver = (int(existing[0]["version"]) + 1) if existing else 1
        paths_json = json.dumps(affected_paths or [], ensure_ascii=False)
        self._exec(
            "INSERT INTO project_decisions(decision_id, version, status, title, rule_text, rationale, affected_paths_json, checkpoint_id, updated_at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(decision_id) DO UPDATE SET"
            " version=excluded.version, status=excluded.status, title=excluded.title,"
            " rule_text=excluded.rule_text, rationale=excluded.rationale,"
            " affected_paths_json=excluded.affected_paths_json, checkpoint_id=excluded.checkpoint_id, updated_at=excluded.updated_at",
            (decision_id, ver, status, title, rule_text, rationale, paths_json, checkpoint_id, ts),
        )
        return {
            "decision_id": decision_id,
            "version": ver,
            "status": status,
            "title": title,
            "rule_text": rule_text,
            "rationale": rationale,
            "affected_paths": affected_paths or [],
            "checkpoint_id": checkpoint_id,
            "updated_at": ts,
        }

    def revoke_decision(self, decision_id: str, *, reason: str = "", checkpoint_id: int | None = None) -> bool:
        rows = self._query("SELECT * FROM project_decisions WHERE decision_id=?", (decision_id,))
        if not rows:
            return False
        cur = dict(rows[0])
        rationale = f"{cur.get('rationale') or ''}\n[已废止] {reason}".strip() if reason else (cur.get("rationale") or "")
        self._exec(
            "UPDATE project_decisions SET status='REVOKED', version=?, rationale=?, checkpoint_id=?, updated_at=? WHERE decision_id=?",
            (int(cur["version"]) + 1, rationale, checkpoint_id, now_iso(), decision_id),
        )
        return True

    def create_checkpoint(
        self,
        *,
        kind: str,
        trigger_topic: str,
        human_note: str = "",
        loopforge_issue_id: str | None = None,
        loopforge_run_id: str | None = None,
        status: str = "RUNNING",
    ) -> dict[str, Any]:
        with self.transaction():
            rows = self._query("SELECT COALESCE(MAX(round_no), 0) AS m FROM evolution_checkpoints")
            round_no = int(rows[0]["m"] or 0) + 1
            ts = now_iso()
            cur = self._exec(
                "INSERT INTO evolution_checkpoints(round_no, kind, trigger_topic, human_note, loopforge_issue_id, loopforge_run_id, status, created_at, updated_at)"
                " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (round_no, kind, trigger_topic, human_note, loopforge_issue_id, loopforge_run_id, status, ts, ts),
            )
            return self.get_checkpoint(int(cur.lastrowid)) or {}

    def update_checkpoint(self, checkpoint_id: int, **fields_to_update: Any) -> None:
        if not fields_to_update:
            return
        cols: list[str] = []
        vals: list[Any] = []
        for k, v in fields_to_update.items():
            if k in ("opinions", "draft_plan", "applied_summary", "rejected_plans"):
                cols.append(f"{k}_json=?")
                vals.append(json.dumps(v, ensure_ascii=False))
            else:
                cols.append(f"{k}=?")
                vals.append(v)
        cols.append("updated_at=?")
        vals.append(now_iso())
        vals.append(checkpoint_id)
        self._exec(f"UPDATE evolution_checkpoints SET {', '.join(cols)} WHERE checkpoint_id=?", tuple(vals))

    def _hydrate_checkpoint(self, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        d = dict(row)
        for k in ("opinions", "draft_plan", "applied_summary", "rejected_plans"):
            d[k] = json.loads(d.pop(f"{k}_json", None) or "{}")
        return d

    def get_checkpoint(self, checkpoint_id: int) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM evolution_checkpoints WHERE checkpoint_id=?", (checkpoint_id,))
        return self._hydrate_checkpoint(rows[0]) if rows else None

    def list_checkpoints(self) -> list[dict[str, Any]]:
        rows = self._query("SELECT * FROM evolution_checkpoints ORDER BY checkpoint_id ASC")
        return [self._hydrate_checkpoint(r) for r in rows]
