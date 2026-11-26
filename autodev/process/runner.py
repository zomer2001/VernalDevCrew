"""Process-as-Code 阶段推导与门禁状态机（WP-0）。

七阶段状态推导规则表（`stage_status`）：
+-----------------------+--------------------------------------------------------------------------------+
| 阶段 (stage_id)       | 状态推导规则                                                                   |
+-----------------------+--------------------------------------------------------------------------------+
| Any (enabled=False)   | SKIPPED                                                                        |
| requirements          | 有阻塞门 FAILED -> FAILED；有门 PENDING -> WAITING_GATE；                      |
|                       | ProjectStatus >= SPECIFIED（或已有切片且无 SRS 门且已进入 PLANNED+）-> PASSED；|
|                       | 已有切片 -> IN_PROGRESS；否则 NOT_STARTED                                      |
| architecture          | 有阻塞门 FAILED -> FAILED；status==PLANNED 或架构门 PENDING -> WAITING_GATE；  |
|                       | status in (APPROVED, RUNNING, STALLED, COMPLETED) 且架构门已过 -> PASSED；     |
|                       | council_messages 非空 -> IN_PROGRESS；否则 NOT_STARTED                         |
| detailed_design       | 有阻塞门 FAILED -> FAILED；当前里程碑详设门 PENDING -> WAITING_GATE；          |
|                       | 所有已规划里程碑详设门 PASSED/WAIVED -> PASSED；                               |
|                       | 存在 docs/design/*.md -> IN_PROGRESS；否则 NOT_STARTED                         |
| implementation        | 存在 FUSED 任务 -> FAILED；所有非取消任务 >= APPROVED -> PASSED；              |
|                       | 存在 IN_PROGRESS/READY/APPROVED 任务 -> IN_PROGRESS；否则 NOT_STARTED          |
| code_review           | 存在 CHANGES_REQUESTED 且未重提 -> FAILED；                                    |
|                       | 存在 AWAITING_HUMAN_REVIEW 或 OPEN code_review -> WAITING_GATE；               |
|                       | 所有非取消任务 >= APPROVED 且无 OPEN 审查 -> PASSED；                          |
|                       | 存在 REVIEWING 任务 -> IN_PROGRESS；否则 NOT_STARTED                           |
| testing               | 存在质量门 FAILED -> FAILED；                                                  |
|                       | 所有非取消任务 >= APPROVED 且质量门无 FAILED -> PASSED；                       |
|                       | 存在 VERIFYING 任务或已有测试运行记录 -> IN_PROGRESS；否则 NOT_STARTED         |
| integration_delivery  | 存在里程碑 FAILED 或最新 CI FAILURE -> FAILED；                                |
|                       | 存在里程碑 AWAITING_MERGE 或门禁 PENDING -> WAITING_GATE；                     |
|                       | 全部里程碑 MERGED (ProjectStatus==COMPLETED) -> PASSED；                       |
|                       | 存在 INTEGRATING / CI_RUNNING / 部分 MERGED -> IN_PROGRESS；否则 NOT_STARTED   |
+-----------------------+--------------------------------------------------------------------------------+
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from autodev.db import Database, now_iso
from autodev.models import MilestoneStatus, ProjectStatus, TaskStatus
from autodev.process.model import GateDef, ProcessTemplate, StageDef, StageState, StageView
from autodev.process.targets import DOCUMENT_GATES, document_target, target_is_current

if TYPE_CHECKING:
    from autodev.config import Settings, Workspace

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
BUILTIN_TEMPLATES = ("legacy", "standard", "feature", "bugfix", "doc_reverse")

ARTIFACT_PATHS: dict[str, str] = {
    "SRS": "docs/SRS.md",
    "ADD": "docs/ARCHITECTURE.md",
    "DETAILED_DESIGN": "docs/design",
    "ICD": "docs/INTERFACES.md",
    "TEST_REPORT": "docs/test",
}


class GateConflictError(RuntimeError):
    """门禁轮次或状态冲突（映射为 HTTP 409）。"""


def _parse_minimal_yaml(text: str) -> dict[str, Any]:
    """解析 JSON 超集 YAML 或常见缩进键值 YAML（离线零第三方依赖兜底）。"""
    stripped = text.strip()
    if not stripped:
        raise ValueError("流程模板内容不能为空")
    try:
        import yaml  # type: ignore[import-untyped]

        loaded = yaml.safe_load(stripped)
        if isinstance(loaded, dict):
            return loaded
    except ImportError:
        pass
    if stripped[0] in ("{", "["):
        loaded_json = json.loads(stripped)
        if not isinstance(loaded_json, dict):
            raise ValueError("流程模板顶层必须是对象")
        return loaded_json

    # 轻量缩进键值回退解析（支持 id/name/description/review_policy 顶层覆盖）
    out: dict[str, Any] = {}
    for raw_line in stripped.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        k, v = line.split(":", 1)
        key = k.strip()
        val = v.strip().strip('"').strip("'")
        if val.lower() in ("true", "false"):
            out[key] = val.lower() == "true"
        else:
            out[key] = val
    if "id" in out and "stages" not in out and out["id"] in BUILTIN_TEMPLATES:
        base = json.loads((TEMPLATES_DIR / f"{out['id']}.yaml").read_text("utf-8"))
        base.update(out)
        return base
    raise ValueError("无法解析自定义 YAML，请使用标准 YAML 1.2（JSON 兼容块）格式")


def parse_process_yaml(raw: str) -> ProcessTemplate:
    data = _parse_minimal_yaml(raw)
    return ProcessTemplate.model_validate(data)


def load_builtin_template(template_id: str) -> ProcessTemplate:
    tid = template_id if template_id in BUILTIN_TEMPLATES else "legacy"
    path = TEMPLATES_DIR / f"{tid}.yaml"
    return parse_process_yaml(path.read_text("utf-8"))


def load_template(ws: "Workspace", settings: "Settings | None" = None) -> ProcessTemplate:
    custom = ws.state_dir / "process.yaml"
    if custom.is_file():
        return parse_process_yaml(custom.read_text("utf-8"))
    cfg = settings or ws.load_settings()
    tid = (getattr(cfg, "process_template", "") or "legacy").strip()
    return load_builtin_template(tid)


def save_custom_template(ws: "Workspace", raw_yaml: str) -> ProcessTemplate:
    tpl = parse_process_yaml(raw_yaml)
    ws.ensure_dirs()
    out_path = ws.state_dir / "process.yaml"
    out_path.write_text(json.dumps(tpl.model_dump(), ensure_ascii=False, indent=2) + "\n", "utf-8")
    return tpl


def _hydrate_gate_row(row: Any, db: Database | None = None) -> dict[str, Any]:
    d = dict(row)
    d["evidence"] = json.loads(d.pop("evidence_json", None) or "{}")
    if db is not None and d["status"] in ("PENDING", "PASSED", "WAIVED") and not target_is_current(db, d):
        d["decision_status"] = d.get("decision_status") or d["status"]
        d["status"] = "STALE"
        d["stale_reason"] = d.get("stale_reason") or "审批目标已变更、缺失或尚未归档"
    return d


def list_gate_records(
    db: Database,
    *,
    status: str | None = None,
    stage_id: str | None = None,
    gate_id: str | None = None,
    scope_type: str | None = None,
    scope_id: str | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status=?")
        params.append(status)
    if stage_id:
        clauses.append("stage_id=?")
        params.append(stage_id)
    if gate_id:
        clauses.append("gate_id=?")
        params.append(gate_id)
    if scope_type:
        clauses.append("scope_type=?")
        params.append(scope_type)
    if scope_id:
        clauses.append("scope_id=?")
        params.append(scope_id)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = db._query(f"SELECT * FROM gate_records{where} ORDER BY record_id ASC", params)
    return [_hydrate_gate_row(r, db) for r in rows]


def latest_gate_record(db: Database, gate_id: str, scope_type: str, scope_id: str) -> dict[str, Any] | None:
    rows = db._query(
        "SELECT * FROM gate_records WHERE gate_id=? AND scope_type=? AND scope_id=? ORDER BY round DESC LIMIT 1",
        (gate_id, scope_type, scope_id),
    )
    return _hydrate_gate_row(rows[0], db) if rows else None


def ensure_gate_record(
    db: Database,
    *,
    stage_id: str,
    gate_id: str,
    kind: str,
    scope_type: str,
    scope_id: str,
    summary: str = "",
    new_round_if_failed: bool = False,
    force_new_round: bool = False,
) -> dict[str, Any]:
    """幂等创建或返回门禁记录。"""
    ts = now_iso()
    with db.transaction():
        target = document_target(db, gate_id, scope_id)
        rows = db._query("SELECT * FROM gate_records WHERE gate_id=? AND scope_type=? AND scope_id=? ORDER BY round DESC LIMIT 1",
                         (gate_id, scope_type, scope_id))
        latest = _hydrate_gate_row(rows[0]) if rows else None
        if latest:
            same_target = latest.get("target_sha", "") == target["target_sha"]
            if same_target and not force_new_round:
                if latest["status"] in ("PENDING", "PASSED", "WAIVED"):
                    return _hydrate_gate_row(rows[0], db)
                if latest["status"] == "FAILED" and not new_round_if_failed:
                    return latest
            if latest["status"] in ("PENDING", "PASSED", "WAIVED"):
                db._exec("UPDATE gate_records SET decision_status=status, status='STALE', stale_reason=? WHERE record_id=?",
                         ("审批目标已变更" if not same_target else "已发起新一轮评审", latest["record_id"]))
        next_round = int(latest["round"]) + 1 if latest else 1
        cur = db._exec(
            "INSERT INTO gate_records(stage_id, gate_id, kind, scope_type, scope_id, status, round, summary, evidence_json, created_at,"
            " target_path, target_sha, target_version, target_snapshot_json) VALUES(?, ?, ?, ?, ?, 'PENDING', ?, ?, '{}', ?, ?, ?, ?, ?)",
            (stage_id, gate_id, kind, scope_type, scope_id, next_round, summary, ts,
             target["target_path"], target["target_sha"], target["target_version"], target["target_snapshot_json"]),
        )
        rec_id = int(cur.lastrowid)
    db.add_event(
        "gate.requested",
        summary or f"等待流程门禁决策：{gate_id} ({scope_type}:{scope_id} · 第 {next_round} 轮)",
        actor="ProcessEngine",
        payload={"record_id": rec_id, "stage_id": stage_id, "gate_id": gate_id, "scope_id": scope_id, "round": next_round},
    )
    rows = db._query("SELECT * FROM gate_records WHERE record_id=?", (rec_id,))
    return _hydrate_gate_row(rows[0])


def refresh_document_gates(db: Database) -> None:
    """Invalidate old targets and open a new round within the caller's snapshot transaction."""
    rows = db._query("SELECT g.* FROM gate_records g JOIN (SELECT gate_id,scope_type,scope_id,MAX(round) AS r "
                     "FROM gate_records GROUP BY gate_id,scope_type,scope_id) latest ON g.gate_id=latest.gate_id "
                     "AND g.scope_type=latest.scope_type AND g.scope_id=latest.scope_id AND g.round=latest.r")
    for row in rows:
        if row["gate_id"] not in DOCUMENT_GATES:
            continue
        current = document_target(db, row["gate_id"], row["scope_id"])
        if row["target_sha"] != current["target_sha"]:
            ensure_gate_record(db, stage_id=row["stage_id"], gate_id=row["gate_id"], kind=row["kind"],
                               scope_type=row["scope_type"], scope_id=row["scope_id"], summary="文档已有新修订，请重新审阅")


def is_gate_passed(db: Database, gate_id: str, scope_type: str, scope_id: str) -> bool:
    rec = latest_gate_record(db, gate_id, scope_type, scope_id)
    return bool(rec and rec["status"] in ("PASSED", "WAIVED"))


def blocking_document_gates(db: Database, template: ProcessTemplate, milestone_id: str) -> list[str]:
    blocked = []
    for stage in template.stages:
        if not stage.enabled:
            continue
        for gate in stage.gates:
            if gate.blocking and gate.id in DOCUMENT_GATES:
                scope_id = milestone_id if gate.scope == "milestone" else "project"
                # Legacy blueprints did not require an ADD document. Preserve that contract
                # only when no document approval has ever been opened or materialized.
                if (gate.id == "architecture_approval" and not stage.required_artifacts
                        and latest_gate_record(db, gate.id, gate.scope, scope_id) is None
                        and not document_target(db, gate.id, scope_id)["target_sha"]):
                    continue
                if not is_gate_passed(db, gate.id, gate.scope, scope_id):
                    blocked.append(gate.id)
    return blocked


def is_stage_passed(
    db: Database,
    ws: "Workspace",
    template: ProcessTemplate,
    stage_id: str,
    *,
    scope_id: str = "project",
) -> bool:
    stage = next((s for s in template.stages if s.id == stage_id), None)
    if stage is None or not stage.enabled:
        return True
    for g in stage.gates:
        if not g.blocking:
            continue
        target_scope = scope_id if g.scope != "project" else "project"
        if not is_gate_passed(db, g.id, g.scope, target_scope):
            return False
    return True


def decide_gate(
    db: Database,
    record_id: int,
    decision: str,
    *,
    actor: str = "owner",
    comment: str = "",
    expected_round: int | None = None,
    expected_sha: str | None = None,
    evidence: dict[str, Any] | None = None,
    audit_project: str = "",
    actor_ip: str = "127.0.0.1",
) -> dict[str, Any]:
    from autodev.governance.audit import enqueue_decision
    with db.transaction():
        out = _decide_gate(db, record_id, decision, actor=actor, comment=comment, expected_round=expected_round,
                           expected_sha=expected_sha, evidence=evidence)
        enqueue_decision(db, f"gate:{record_id}", actor=actor, actor_ip=actor_ip,
                         project=audit_project or db.path.parent.parent.name, action="gate.decided",
                         target=f"{out['gate_id']}#{record_id}", detail={"status": out["status"], "round": out["round"], "comment": comment})
        return out


def _decide_gate(
    db: Database,
    record_id: int,
    decision: str,
    *,
    actor: str = "owner",
    comment: str = "",
    expected_round: int | None = None,
    expected_sha: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    norm = decision.strip().lower()
    status_map = {"pass": "PASSED", "passed": "PASSED", "fail": "FAILED", "failed": "FAILED", "waive": "WAIVED", "waived": "WAIVED"}
    if norm not in status_map:
        raise ValueError(f"不支持的门禁决策：{decision}（可选 pass / fail / waive）")
    target_status = status_map[norm]
    if target_status == "WAIVED" and not comment.strip():
        raise ValueError("豁免门禁（waive）必须填写豁免原因")

    rows = db._query("SELECT * FROM gate_records WHERE record_id=?", (record_id,))
    if not rows:
        raise KeyError(f"门禁记录不存在：{record_id}")
    cur = _hydrate_gate_row(rows[0])

    if expected_sha is not None and expected_sha != cur.get("target_sha", ""):
        raise GateConflictError("审批内容版本已变更，请重新查看目标文档")
    latest = latest_gate_record(db, cur["gate_id"], cur["scope_type"], cur["scope_id"])
    if latest is None or latest["record_id"] != record_id or not target_is_current(db, cur):
        raise GateConflictError("审批目标已变更或尚未归档，请重新查看当前版本")

    if expected_round is not None and int(cur["round"]) != int(expected_round):
        raise GateConflictError(f"门禁轮次已变更（当前第 {cur['round']} 轮，请求第 {expected_round} 轮）")

    if cur["status"] != "PENDING":
        if cur["status"] == target_status:
            return cur
        raise GateConflictError(f"门禁记录 #{record_id} 已处于 {cur['status']} 状态，无法重复变更为 {target_status}")

    ts = now_iso()
    merged_ev = {**(cur.get("evidence") or {}), **(evidence or {})}
    db._exec(
        "UPDATE gate_records SET status=?, actor=?, summary=?, evidence_json=?, decided_at=? WHERE record_id=? AND status='PENDING'",
        (target_status, actor, comment.strip() or cur.get("summary") or "", json.dumps(merged_ev, ensure_ascii=False), ts, record_id),
    )
    if db.meta_get("speckit.enabled") == "1" and target_status in ("PASSED","WAIVED"):
        snapshot_ids = [x["id"] for x in json.loads(cur["target_snapshot_json"])]
        if snapshot_ids:
            marks = ",".join("?" for _ in snapshot_ids)
            db._exec(f"UPDATE spec_revisions SET status='APPROVED',approved_gate_id=? WHERE content_version_id IN ({marks}) "
                     "AND rev_id IN (SELECT MAX(rev_id) FROM spec_revisions GROUP BY feature_id,source_kind)",
                     (record_id,*snapshot_ids))
    ev_type = {"PASSED": "gate.passed", "FAILED": "gate.failed", "WAIVED": "gate.waived"}[target_status]
    db.add_event(
        ev_type,
        f"门禁 {cur['gate_id']} ({cur['scope_type']}:{cur['scope_id']}) 已决策为 {target_status}"
        + (f"：{comment.strip()}" if comment.strip() else ""),
        actor=actor,
        payload={"record_id": record_id, "gate_id": cur["gate_id"], "status": target_status, "round": cur["round"]},
    )
    updated = db._query("SELECT * FROM gate_records WHERE record_id=?", (record_id,))
    return _hydrate_gate_row(updated[0])


def _artifact_exists(ws: "Workspace", kind: str) -> tuple[bool, str]:
    rel = ARTIFACT_PATHS.get(kind, f"docs/{kind}.md")
    full = ws.root / rel
    if kind == "SRS":
        if full.is_file():
            return True, rel
        if (ws.root / "docs" / "PRD_SPEC.md").is_file():
            return True, "docs/PRD_SPEC.md"
        return False, rel
    if full.is_dir():
        has_md = any(p.suffix == ".md" for p in full.glob("*.md"))
        return has_md, rel
    return full.is_file(), rel


def _derive_single_stage(
    stage: StageDef,
    *,
    proj_status: ProjectStatus,
    slices_cnt: int,
    council_cnt: int,
    milestones: list[Any],
    tasks: list[Any],
    stage_records: list[dict[str, Any]],
    open_reviews_cnt: int,
    changes_req_cnt: int,
    failed_quality_cnt: int,
    failed_ci_cnt: int,
    ws: "Workspace",
) -> tuple[StageState, int]:
    if not stage.enabled:
        return "SKIPPED", 0

    latest_by_scope: dict[tuple[str, str, str], dict[str, Any]] = {}
    for rec in stage_records:
        latest_by_scope[(rec["gate_id"], rec["scope_type"], rec["scope_id"])] = rec

    blocking_ids = {g.id for g in stage.gates if g.blocking}
    pending_recs = [r for r in latest_by_scope.values() if r["status"] in ("PENDING", "STALE") and r["gate_id"] in blocking_ids]
    failed_recs = [r for r in latest_by_scope.values() if r["status"] == "FAILED" and r["gate_id"] in blocking_ids]

    sid = stage.id
    non_cancelled = [t for t in tasks if t.status != TaskStatus.CANCELLED]
    done_tasks = [t for t in non_cancelled if t.status in (TaskStatus.APPROVED, TaskStatus.MERGED)]

    if sid == "requirements":
        if failed_recs:
            return "FAILED", 0
        if pending_recs:
            return "WAITING_GATE", len(pending_recs)
        passed_statuses = {
            ProjectStatus.SPECIFIED,
            ProjectStatus.PLANNED,
            ProjectStatus.APPROVED,
            ProjectStatus.RUNNING,
            ProjectStatus.STALLED,
            ProjectStatus.COMPLETED,
        }
        has_srs_gate = any(g.id == "srs_approval" and g.blocking for g in stage.gates)
        srs_ok = (not has_srs_gate) or is_gate_passed_in_map(latest_by_scope, "srs_approval", "project", "project")
        if proj_status in passed_statuses and srs_ok:
            return "PASSED", 0
        if slices_cnt > 0:
            return "IN_PROGRESS", 0
        return "NOT_STARTED", 0

    if sid == "architecture":
        if failed_recs:
            return "FAILED", 0
        if proj_status == ProjectStatus.PLANNED or pending_recs:
            return "WAITING_GATE", max(1, len(pending_recs)) if proj_status == ProjectStatus.PLANNED else len(pending_recs)
        if proj_status in {ProjectStatus.APPROVED, ProjectStatus.RUNNING, ProjectStatus.STALLED, ProjectStatus.COMPLETED} and milestones:
            return "PASSED", 0
        if council_cnt > 0:
            return "IN_PROGRESS", 0
        return "NOT_STARTED", 0

    if sid == "detailed_design":
        if failed_recs:
            return "FAILED", 0
        if pending_recs:
            return "WAITING_GATE", len(pending_recs)
        planned_ms = [m for m in milestones if m.planned]
        has_dd_gate = any(g.id == "detailed_design_approval" and g.blocking for g in stage.gates)
        if planned_ms and has_dd_gate:
            if all(is_gate_passed_in_map(latest_by_scope, "detailed_design_approval", "milestone", m.milestone_id) for m in planned_ms):
                return "PASSED", 0
        exists, _ = _artifact_exists(ws, "DETAILED_DESIGN")
        if exists:
            return "PASSED" if not has_dd_gate else "IN_PROGRESS", 0
        return "NOT_STARTED", 0

    if sid == "implementation":
        if any(t.status == TaskStatus.FUSED for t in non_cancelled):
            return "FAILED", sum(1 for t in non_cancelled if t.status == TaskStatus.FUSED)
        if non_cancelled and len(done_tasks) == len(non_cancelled):
            return "PASSED", 0
        if any(t.status in (TaskStatus.READY, TaskStatus.IN_PROGRESS, TaskStatus.VERIFYING, TaskStatus.REVIEWING, TaskStatus.AWAITING_HUMAN_REVIEW, TaskStatus.APPROVED) for t in non_cancelled):
            return "IN_PROGRESS", 0
        return "NOT_STARTED", 0

    if sid == "code_review":
        awaiting_hr = sum(1 for t in non_cancelled if t.status == TaskStatus.AWAITING_HUMAN_REVIEW)
        wait_total = max(awaiting_hr, open_reviews_cnt) + len(pending_recs)
        if wait_total > 0:
            return "WAITING_GATE", wait_total
        if changes_req_cnt > 0 and not done_tasks:
            return "FAILED", changes_req_cnt
        if non_cancelled and len(done_tasks) == len(non_cancelled):
            return "PASSED", 0
        if any(t.status == TaskStatus.REVIEWING for t in non_cancelled) or done_tasks:
            return "IN_PROGRESS", 0
        return "NOT_STARTED", 0

    if sid == "testing":
        if failed_quality_cnt > 0 or failed_recs:
            return "FAILED", failed_quality_cnt + len(failed_recs)
        if non_cancelled and len(done_tasks) == len(non_cancelled):
            return "PASSED", 0
        if any(t.status == TaskStatus.VERIFYING for t in non_cancelled) or done_tasks:
            return "IN_PROGRESS", 0
        return "NOT_STARTED", 0

    # integration_delivery
    if any(m.status == MilestoneStatus.FAILED for m in milestones) or failed_ci_cnt > 0 or failed_recs:
        return "FAILED", failed_ci_cnt + len(failed_recs)
    aw_merge = sum(1 for m in milestones if m.status == MilestoneStatus.AWAITING_MERGE)
    if aw_merge > 0 or pending_recs:
        return "WAITING_GATE", aw_merge + len(pending_recs)
    if milestones and all(m.status == MilestoneStatus.MERGED for m in milestones):
        return "PASSED", 0
    if any(m.status in (MilestoneStatus.INTEGRATING, MilestoneStatus.CI_RUNNING, MilestoneStatus.MERGED) for m in milestones):
        return "IN_PROGRESS", 0
    return "NOT_STARTED", 0


def is_gate_passed_in_map(latest_map: dict[tuple[str, str, str], dict[str, Any]], gate_id: str, scope_type: str, scope_id: str) -> bool:
    rec = latest_map.get((gate_id, scope_type, scope_id))
    return bool(rec and rec["status"] in ("PASSED", "WAIVED"))


def stage_status(db: Database, ws: "Workspace", template: ProcessTemplate) -> list[StageView]:
    raw_status = db.meta_get("status", ProjectStatus.CREATED.value) or ProjectStatus.CREATED.value
    proj_status = ProjectStatus(raw_status)
    slices_cnt = len(db.list_slices())
    council_cnt = len(db.list_council_messages())
    milestones = db.list_milestones()
    tasks = db.list_tasks()
    all_records = list_gate_records(db)

    open_rev_rows = db._query("SELECT COUNT(*) AS c FROM code_reviews WHERE status='OPEN'")
    open_reviews_cnt = int(open_rev_rows[0]["c"] or 0) if open_rev_rows else 0
    cr_rows = db._query("SELECT COUNT(*) AS c FROM code_reviews WHERE status='CHANGES_REQUESTED'")
    changes_req_cnt = int(cr_rows[0]["c"] or 0) if cr_rows else 0

    # 最新一次尝试的质量门禁是否有失败
    fq_rows = db._query(
        "SELECT COUNT(*) AS c FROM quality_results q"
        " JOIN (SELECT COALESCE(task_id,'') AS tid, COALESCE(milestone_id,'') AS mid, gate, MAX(id) AS max_id"
        "       FROM quality_results GROUP BY COALESCE(task_id,''), COALESCE(milestone_id,''), gate) latest"
        " ON q.id = latest.max_id WHERE q.status='FAILED'"
    )
    failed_quality_cnt = int(fq_rows[0]["c"] or 0) if fq_rows else 0

    fc_rows = db._query(
        "SELECT COUNT(*) AS c FROM ci_runs c"
        " JOIN (SELECT milestone_id, MAX(ci_run_id) AS max_id FROM ci_runs GROUP BY milestone_id) latest"
        " ON c.ci_run_id = latest.max_id WHERE c.status IN ('FAILURE', 'TIMEOUT')"
    )
    failed_ci_cnt = int(fc_rows[0]["c"] or 0) if fc_rows else 0

    views: list[StageView] = []
    for s in template.stages:
        s_records = [r for r in all_records if r["stage_id"] == s.id]
        st, pending_cnt = _derive_single_stage(
            s,
            proj_status=proj_status,
            slices_cnt=slices_cnt,
            council_cnt=council_cnt,
            milestones=milestones,
            tasks=tasks,
            stage_records=s_records,
            open_reviews_cnt=open_reviews_cnt,
            changes_req_cnt=changes_req_cnt,
            failed_quality_cnt=failed_quality_cnt,
            failed_ci_cnt=failed_ci_cnt,
            ws=ws,
        )
        artifacts_info = []
        for kind in s.required_artifacts:
            ok, path_rel = _artifact_exists(ws, kind)
            artifacts_info.append({"kind": kind, "path": path_rel, "exists": ok})

        gate_views = []
        for g in s.gates:
            matching = [r for r in s_records if r["gate_id"] == g.id]
            gate_views.append(
                {
                    "id": g.id,
                    "name": g.name,
                    "kind": g.kind,
                    "scope": g.scope,
                    "blocking": g.blocking,
                    "records": matching,
                }
            )
        views.append(
            StageView(
                id=s.id,
                name=s.name,
                enabled=s.enabled,
                status=st,
                pending_count=pending_cnt,
                required_artifacts=artifacts_info,
                gates=gate_views,
            )
        )
    return views
