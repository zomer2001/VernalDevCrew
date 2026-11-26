"""人工代码审查核心服务：AI 预审摘要合成、行内评论线程与审批/打回返工闭环（WP-2）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from autodev.db import Database, now_iso
from autodev.models import TaskStatus
from autodev.process.runner import decide_gate, ensure_gate_record, load_template
from autodev.quality.runner import list_quality_results
from autodev.review.diffs import milestone_diff, task_diff

if TYPE_CHECKING:
    from autodev.engine import Engine


class ReviewConflictError(RuntimeError):
    """审查基线过时或状态冲突（映射为 HTTP 409）。"""


def current_milestone_candidate_ref(engine: "Engine", milestone_id: str) -> str:
    """读取里程碑当前最新集成候选标识（files 模式为产物 digest，git 模式为集成分支 HEAD SHA）。"""
    cand = (engine.db.meta_get(f"candidate_ref:{milestone_id}") or "").strip()
    if cand:
        return cand
    if engine.settings.collaboration_mode == "files":
        return (engine.db.meta_get(f"delivery:{milestone_id}") or "").strip()
    ms = engine.db.get_milestone(milestone_id)
    if ms and ms.branch_name and (engine.ws.root / ".git").exists():
        from autodev import gitops

        return gitops.git(["rev-parse", ms.branch_name], engine.ws.root, check=False).stdout.strip()
    return ""


def _hydrate_review(row: Any) -> dict[str, Any]:
    d = dict(row)
    d["risk_reasons"] = json.loads(d.pop("risk_reasons_json", None) or "[]")
    d["checklist"] = json.loads(d.pop("checklist_json", None) or "{}")
    return d


def list_reviews(
    db: Database,
    *,
    status: str | None = None,
    scope_type: str | None = None,
    scope_id: str | None = None,
    milestone_id: str | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status=?")
        params.append(status.upper())
    if scope_type:
        clauses.append("scope_type=?")
        params.append(scope_type)
    if scope_id:
        clauses.append("scope_id=?")
        params.append(scope_id)
    if milestone_id:
        clauses.append("milestone_id=?")
        params.append(milestone_id)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = db._query(f"SELECT * FROM code_reviews{where} ORDER BY review_id DESC", params)
    return [_hydrate_review(r) for r in rows]


def list_review_comments(db: Database, review_id: int) -> list[dict[str, Any]]:
    rows = db._query("SELECT * FROM review_comments WHERE review_id=? ORDER BY comment_id ASC", (review_id,))
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        d["resolved"] = bool(d.get("resolved"))
        out.append(d)
    return out


def _read_latest_ai_verdict(engine: "Engine", task_id: str) -> dict[str, Any] | None:
    runs = [r for r in engine.db.list_runs(task_id) if r.get("stage") == "REVIEW"]
    if not runs:
        return None
    lp = runs[-1].get("log_path")
    if lp and Path(lp).is_dir():
        vf = Path(lp) / "verdict.json"
        if vf.is_file():
            try:
                return json.loads(vf.read_text("utf-8"))
            except json.JSONDecodeError:
                return None
    return None


def _build_ai_prereview_summary(
    engine: "Engine",
    *,
    scope_type: str,
    scope_id: str,
    diff_view: dict[str, Any],
    risk_reasons: list[str],
    quality_rows: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """零额外模型调用合成 AI 预审摘要与预置行内评论。"""
    stat = diff_view.get("stat") or {}
    lines = [
        f"### AI 预审摘要（{scope_type}:{scope_id}）",
        f"- **变更统计**：共修改 `{stat.get('files_changed', 0)}` 个文件，`+{stat.get('additions', 0)} / -{stat.get('deletions', 0)}` 行",
    ]
    if risk_reasons:
        lines.append("- **触发人工复核原因**：")
        for r in risk_reasons:
            lines.append(f"  - {r}")

    pre_comments: list[dict[str, Any]] = []
    if scope_type == "task":
        verdict = _read_latest_ai_verdict(engine, scope_id)
        if verdict:
            if verdict.get("summary"):
                lines.append(f"- **独立审查官结论**：{verdict['summary']}")
            checks = verdict.get("criteria_check") or []
            if checks:
                lines.append("- **验收标准核对**：")
                for c in checks:
                    mark = "通过" if c.get("met") else "待核查"
                    lines.append(f"  - [{mark}] {c.get('criterion', '')}（依据：{c.get('evidence', '-')}）")
            for issue in verdict.get("blocking_issues") or []:
                pre_comments.append(
                    {
                        "file": str(issue.get("file") or ""),
                        "line": int(issue.get("line") or 1),
                        "severity": "major",
                        "body": f"{issue.get('problem', '')}" + (f"（建议：{issue['fix']}）" if issue.get("fix") else ""),
                        "author": "AI-Reviewer",
                        "source": "ai",
                    }
                )

    for qr in quality_rows:
        for f in qr.get("findings") or []:
            sev = str(f.get("severity") or "minor")
            mapped_sev = "blocker" if sev == "blocker" else ("major" if sev == "high" else "minor")
            pre_comments.append(
                {
                    "file": str(f.get("file") or ""),
                    "line": int(f.get("line") or 1),
                    "severity": mapped_sev,
                    "body": f"[{qr.get('gate')}:{f.get('rule')}] {f.get('message', '')}",
                    "author": str(f.get("tool") or qr.get("gate") or "QualityGate"),
                    "source": "quality",
                }
            )

    files = list(diff_view.get("files") or [])
    files.sort(
        key=lambda f: (
            0 if str(f.get("path", "")).startswith("contracts/") else 1,
            -(int(f.get("additions", 0)) + int(f.get("deletions", 0))),
            str(f.get("path", "")),
        )
    )
    if files:
        lines.append("- **建议优先审阅文件**：")
        for f in files[:5]:
            lines.append(f"  - `{f['path']}` (`+{f.get('additions', 0)} / -{f.get('deletions', 0)}`)")

    return "\n".join(lines), pre_comments


def open_review(
    engine: "Engine",
    *,
    scope_type: str,
    scope_id: str,
    milestone_id: str,
    head_ref: str,
    base_ref: str = "",
    risk_reasons: list[str] | None = None,
) -> dict[str, Any]:
    db = engine.db
    existing_all = db._query(
        "SELECT * FROM code_reviews WHERE scope_type=? AND scope_id=? ORDER BY round DESC",
        (scope_type, scope_id),
    )
    if existing_all:
        latest = _hydrate_review(existing_all[0])
        if latest["status"] == "OPEN" and latest["head_ref"] == head_ref:
            return get_review(engine, int(latest["review_id"]))
        if latest["status"] == "OPEN":
            db._exec("UPDATE code_reviews SET status='SUPERSEDED', decided_at=? WHERE review_id=? AND status='OPEN'", (now_iso(), latest["review_id"]))
        next_round = int(latest["round"]) + 1
    else:
        next_round = 1

    if scope_type == "task":
        task = db.get_task(scope_id)
        diff_view = task_diff(engine, task) if task else {"files": [], "stat": {}, "raw_patch": ""}
        q_rows = list_quality_results(db, task_id=scope_id, latest_only=True)
    else:
        ms = db.get_milestone(scope_id)
        diff_view = milestone_diff(engine, ms) if ms else {"files": [], "stat": {}, "raw_patch": ""}
        q_rows = list_quality_results(db, milestone_id=scope_id)

    ai_md, pre_cmts = _build_ai_prereview_summary(
        engine,
        scope_type=scope_type,
        scope_id=scope_id,
        diff_view=diff_view,
        risk_reasons=risk_reasons or [],
        quality_rows=q_rows,
    )
    tpl = load_template(engine.ws, engine.settings)
    checklist_init = {item: False for item in (tpl.review_checklist or [])}

    ts = now_iso()
    with db.transaction():
        cur = db._exec(
            "INSERT INTO code_reviews(scope_type, scope_id, milestone_id, round, status, head_ref, base_ref,"
            " risk_reasons_json, ai_summary_md, checklist_json, created_at)"
            " VALUES(?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?)",
            (
                scope_type,
                scope_id,
                milestone_id,
                next_round,
                head_ref,
                base_ref,
                json.dumps(risk_reasons or [], ensure_ascii=False),
                ai_md,
                json.dumps(checklist_init, ensure_ascii=False),
                ts,
            ),
        )
        rev_id = int(cur.lastrowid)
        for pc in pre_cmts:
            db._exec(
                "INSERT INTO review_comments(review_id, file, line, side, severity, body, author, source, resolved, created_at)"
                " VALUES(?, ?, ?, 'new', ?, ?, ?, ?, 0, ?)",
                (rev_id, pc["file"], pc["line"], pc["severity"], pc["body"], pc["author"], pc["source"], ts),
            )

    ensure_gate_record(
        db,
        stage_id="code_review" if scope_type == "task" else "integration_delivery",
        gate_id="human_code_review" if scope_type == "task" else "milestone_merge_approval",
        kind="human_approval",
        scope_type=scope_type,
        scope_id=scope_id,
        summary=f"等待人工代码审查：{scope_id}（第 {next_round} 轮）",
        new_round_if_failed=True,
        force_new_round=True,
    )
    db.add_event(
        "review.requested",
        f"已创建第 {next_round} 轮人工代码审查：{scope_id}" + (f"（{'；'.join(risk_reasons or [])}）" if risk_reasons else ""),
        task_id=scope_id if scope_type == "task" else None,
        actor="ReviewGate",
        payload={"review_id": rev_id, "scope_type": scope_type, "scope_id": scope_id, "round": next_round},
    )
    return get_review(engine, rev_id)


def get_review(engine: "Engine", review_id: int) -> dict[str, Any]:
    rows = engine.db._query("SELECT * FROM code_reviews WHERE review_id=?", (review_id,))
    if not rows:
        raise KeyError(f"代码审查单不存在：{review_id}")
    rev = _hydrate_review(rows[0])
    scope_type = rev["scope_type"]
    scope_id = rev["scope_id"]
    if scope_type == "task":
        t = engine.db.get_task(scope_id)
        diff_view = task_diff(engine, t) if t else {"files": [], "stat": {}, "raw_patch": ""}
        q_rows = list_quality_results(engine.db, task_id=scope_id, latest_only=True)
    else:
        ms = engine.db.get_milestone(scope_id)
        diff_view = milestone_diff(engine, ms) if ms else {"files": [], "stat": {}, "raw_patch": ""}
        q_rows = list_quality_results(engine.db, milestone_id=scope_id)

    comments = list_review_comments(engine.db, review_id)
    prev_comments: list[dict[str, Any]] = []
    if int(rev["round"]) > 1:
        prev_rows = engine.db._query(
            "SELECT review_id FROM code_reviews WHERE scope_type=? AND scope_id=? AND round=?",
            (scope_type, scope_id, int(rev["round"]) - 1),
        )
        if prev_rows:
            prev_comments = list_review_comments(engine.db, int(prev_rows[0]["review_id"]))

    return {
        "review": rev,
        "diff": diff_view,
        "comments": comments,
        "previous_round_comments": prev_comments,
        "quality": q_rows,
    }


def add_comment(
    db: Database,
    review_id: int,
    *,
    file: str,
    line: int = 0,
    side: str = "new",
    severity: str = "major",
    body: str,
    author: str = "owner",
    source: str = "human",
) -> dict[str, Any]:
    rows = db._query("SELECT review_id FROM code_reviews WHERE review_id=?", (review_id,))
    if not rows:
        raise KeyError(f"代码审查单不存在：{review_id}")
    if severity not in ("blocker", "major", "minor", "nit"):
        raise ValueError(f"无效的评论严重级别：{severity}")
    if not body.strip():
        raise ValueError("评论内容不能为空")
    ts = now_iso()
    cur = db._exec(
        "INSERT INTO review_comments(review_id, file, line, side, severity, body, author, source, resolved, created_at)"
        " VALUES(?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
        (review_id, file.strip(), int(line), side, severity, body.strip(), author, source, ts),
    )
    cid = int(cur.lastrowid)
    out = dict(db._query("SELECT * FROM review_comments WHERE comment_id=?", (cid,))[0])
    out["resolved"] = bool(out.get("resolved"))
    return out


def update_comment(
    db: Database,
    comment_id: int,
    *,
    resolved: bool | None = None,
    body: str | None = None,
    severity: str | None = None,
) -> dict[str, Any]:
    rows = db._query("SELECT * FROM review_comments WHERE comment_id=?", (comment_id,))
    if not rows:
        raise KeyError(f"评论不存在：{comment_id}")
    updates: list[str] = []
    vals: list[Any] = []
    if resolved is not None:
        updates.append("resolved=?")
        vals.append(1 if resolved else 0)
    if body is not None:
        if not body.strip():
            raise ValueError("评论内容不能为空")
        updates.append("body=?")
        vals.append(body.strip())
    if severity is not None:
        if severity not in ("blocker", "major", "minor", "nit"):
            raise ValueError(f"无效的评论严重级别：{severity}")
        updates.append("severity=?")
        vals.append(severity)
    if updates:
        vals.append(comment_id)
        db._exec(f"UPDATE review_comments SET {', '.join(updates)} WHERE comment_id=?", vals)
    out = dict(db._query("SELECT * FROM review_comments WHERE comment_id=?", (comment_id,))[0])
    out["resolved"] = bool(out.get("resolved"))
    return out


def _unlock_ready_after_review(db: Database, milestone_id: str) -> None:
    all_tasks = {t.task_id: t for t in db.list_tasks()}
    for t in all_tasks.values():
        if t.milestone_id != milestone_id or t.status != TaskStatus.BLOCKED:
            continue
        if all(
            d in all_tasks and all_tasks[d].status in (TaskStatus.APPROVED, TaskStatus.MERGED)
            for d in t.depends_on
        ):
            db.update_task(t.task_id, status=TaskStatus.READY)
            db.add_event("task.ready", f"前置人工审查已通过，解锁 {t.task_id} 进入派单队列", task_id=t.task_id, actor="ReviewGate")


def decide_review(
    engine: "Engine",
    review_id: int,
    decision: str,
    *,
    actor: str = "owner",
    note: str = "",
    expected_head: str = "",
    override_reason: str = "",
    checklist: dict[str, bool] | None = None,
    target_task_ids: list[str] | None = None,
    audit_project: str = "",
    actor_ip: str = "127.0.0.1",
) -> dict[str, Any]:
    from autodev.governance.audit import enqueue_decision
    with engine.db.transaction():
        out = _decide_review(engine, review_id, decision, actor=actor, note=note, expected_head=expected_head, override_reason=override_reason, checklist=checklist, target_task_ids=target_task_ids)
        enqueue_decision(engine.db, f"review:{review_id}", actor=actor, actor_ip=actor_ip,
                         project=audit_project or engine.db.path.parent.parent.name, action="review.decided",
                         target=f"review#{review_id}", detail={"decision": out["review"]["status"], "override_reason": override_reason})
        return out


def _decide_review(
    engine: "Engine",
    review_id: int,
    decision: str,
    *,
    actor: str = "owner",
    note: str = "",
    expected_head: str = "",
    override_reason: str = "",
    checklist: dict[str, bool] | None = None,
    target_task_ids: list[str] | None = None,
) -> dict[str, Any]:
    db = engine.db
    rows = db._query("SELECT * FROM code_reviews WHERE review_id=?", (review_id,))
    if not rows:
        raise KeyError(f"代码审查单不存在：{review_id}")
    rev = _hydrate_review(rows[0])

    if expected_head.strip() and expected_head.strip() != rev["head_ref"]:
        raise ReviewConflictError(f"待审代码基线已更新（当前 {rev['head_ref'][:12]}，提交 {expected_head[:12]}），请刷新最新 Diff 后重新评审")

    if rev["scope_type"] == "milestone":
        current_cand = current_milestone_candidate_ref(engine, str(rev["scope_id"]))
        if current_cand and rev["head_ref"] != current_cand:
            raise ReviewConflictError(
                f"里程碑 {rev['scope_id']} 候选产物已更新（审查单针对 {rev['head_ref'][:12]}，当前候选 {current_cand[:12]}），请重新发起或基于最新候选评审"
            )

    norm = decision.strip().lower()
    if norm not in ("approve", "approved", "request_changes", "changes_requested"):
        raise ValueError(f"不支持的审查结论：{decision}")
    is_approve = norm in ("approve", "approved")

    # WP-7 职责分离（SoD）：禁止人工修改/转向责任人自审批准同一任务的审查单
    if is_approve and rev["scope_type"] == "task":
        reviewed_task = db.get_task(str(rev["scope_id"]))
        if reviewed_task and reviewed_task.owner_member and actor.strip() and reviewed_task.owner_member.strip() == actor.strip():
            if actor.strip() != "owner" or db._query("SELECT 1 FROM members LIMIT 1"):
                raise PermissionError(
                    f"职责分离策略拦截：任务 {rev['scope_id']} 由 {reviewed_task.owner_member} 主导修改，须由其他审查人批准"
                )

    if rev["status"] != "OPEN":
        target_st = "APPROVED" if is_approve else "CHANGES_REQUESTED"
        if rev["status"] == target_st:
            return get_review(engine, review_id)
        raise ReviewConflictError(f"审查单 #{review_id} 已处于 {rev['status']} 状态")

    comments = list_review_comments(db, review_id)
    unresolved = [c for c in comments if not c["resolved"]]
    unresolved_blockers = [c for c in unresolved if c["severity"] == "blocker"]

    if is_approve and unresolved_blockers and not override_reason.strip():
        raise ValueError(f"存在 {len(unresolved_blockers)} 条未解决的 Blocker 级评论，请先修复解决或填写强制放行理由 (override_reason)")

    ts = now_iso()
    merged_checklist = {**(rev.get("checklist") or {}), **(checklist or {})}
    final_status = "APPROVED" if is_approve else "CHANGES_REQUESTED"

    db._exec(
        "UPDATE code_reviews SET status=?, reviewer=?, decision_note=?, override_reason=?, checklist_json=?, decided_at=? WHERE review_id=? AND status='OPEN'",
        (
            final_status,
            actor,
            note.strip(),
            override_reason.strip() or None,
            json.dumps(merged_checklist, ensure_ascii=False),
            ts,
            review_id,
        ),
    )

    scope_type = rev["scope_type"]
    scope_id = rev["scope_id"]
    gate_id = "human_code_review" if scope_type == "task" else "milestone_merge_approval"
    gate_rec = ensure_gate_record(
        db,
        stage_id="code_review" if scope_type == "task" else "integration_delivery",
        gate_id=gate_id,
        kind="human_approval",
        scope_type=scope_type,
        scope_id=scope_id,
    )
    if gate_rec["status"] == "PENDING":
        decide_gate(
            db,
            int(gate_rec["record_id"]),
            "pass" if is_approve else "fail",
            actor=actor,
            comment=override_reason.strip() or note.strip() or ("人工代码审查通过" if is_approve else "人工代码审查要求返工"),
        )

    if is_approve:
        if scope_type == "task":
            task = db.get_task(scope_id)
            if task is not None:
                db.update_task(scope_id, status=TaskStatus.APPROVED, failure_reason=None, last_feedback=None)
                _unlock_ready_after_review(db, task.milestone_id)
            db.add_event(
                "review.approved",
                f"人工代码审查通过：{scope_id}" + (f"（附强制放行理由：{override_reason.strip()}）" if override_reason.strip() else ""),
                task_id=scope_id,
                actor=actor,
            )
        else:
            db.add_event("review.approved", f"里程碑 {scope_id} 人工代码审查通过", actor=actor)
    else:
        # 编译结构化返工单并驱动编码智能体返工
        rework_lines = [f"【人工审查意见 · 第 {rev['round']} 轮 · 审查人 {actor}】"]
        for c in unresolved:
            loc = f"{c['file']}:{c['line']}" if c.get("line") else c["file"]
            rework_lines.append(f"- [{c['severity']}] {loc} {c['body']}")
        if note.strip():
            rework_lines.append(f"总体意见：{note.strip()}")
        compiled_note = "\n".join(rework_lines)

        if scope_type == "task":
            engine.steer(scope_id, compiled_note, actor=actor)
            db.add_event("review.changes_requested", f"人工审查打回 {scope_id} 返工", task_id=scope_id, actor=actor)
        else:
            ms_tasks = [t for t in db.list_tasks(milestone_id=scope_id) if t.status != TaskStatus.CANCELLED]
            reopen_ids = list(target_task_ids or [])
            if not reopen_ids:
                commented_files = {c["file"] for c in unresolved if c.get("file")}
                for t in ms_tasks:
                    if any(cf == ap.rstrip("/") or cf.startswith(ap.rstrip("/") + "/") for cf in commented_files for ap in t.allowed_paths):
                        reopen_ids.append(t.task_id)
            if not reopen_ids and ms_tasks:
                reopen_ids = [ms_tasks[-1].task_id]
            for tid in reopen_ids:
                engine.steer(tid, compiled_note, actor=actor)
            db.add_event("review.changes_requested", f"里程碑 {scope_id} 审查打回任务：{', '.join(reopen_ids)}", actor=actor)

    return get_review(engine, review_id)
