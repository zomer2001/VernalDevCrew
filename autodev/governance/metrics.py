"""研发效能度量（WP-7：基于现有台账表零额外埋点计算 DORA 与 AI 工程质量指标）。"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from autodev.config import Workspace
from autodev.db import Database
from autodev.models import MilestoneStatus, TaskStatus
from autodev.server.project_launcher import load_project_registry

if TYPE_CHECKING:
    from autodev.engine import Engine

# 默认每百万 Token 折算单价（CNY，支持按模型前缀匹配）
DEFAULT_MODEL_PRICES_PER_1M_CNY: dict[str, tuple[float, float]] = {
    "deepseek-reasoner": (4.0, 16.0),
    "deepseek": (2.0, 8.0),
    "gpt-5": (15.0, 60.0),
    "claude": (22.0, 110.0),
    "default": (4.0, 12.0),
}


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def compute_project_metrics(
    db: Database,
    *,
    project_id: str = "",
    token_budget_limit: int = 0,
    date_from: str = "",
    date_to: str = "",
) -> dict[str, Any]:
    tasks = [t for t in db.list_tasks() if t.status != TaskStatus.CANCELLED]
    milestones = db.list_milestones()

    total_tasks = len(tasks)
    completed_tasks = [t for t in tasks if t.status in (TaskStatus.APPROVED, TaskStatus.MERGED)]
    fused_tasks = [t for t in tasks if t.status == TaskStatus.FUSED]
    first_pass_tasks = [t for t in completed_tasks if int(t.retry_count or 0) == 0]

    first_pass_rate = round(len(first_pass_tasks) / len(completed_tasks), 4) if completed_tasks else 0.0
    avg_rework = round(sum(int(t.retry_count or 0) for t in tasks) / total_tasks, 3) if total_tasks else 0.0
    fuse_rate = round(len(fused_tasks) / total_tasks, 4) if total_tasks else 0.0

    # 任务交付周期（从首次 CODE 运行 started_at 到最后一次运行 finished_at）
    lead_times: list[float] = []
    run_rows = db._query("SELECT task_id, started_at, finished_at, input_tokens, cached_tokens, output_tokens, reasoning_tokens FROM task_runs")
    by_task_times: dict[str, list[tuple[datetime | None, datetime | None]]] = {}
    in_tok = cached_tok = out_tok = reas_tok = 0

    dt_from = _parse_iso(date_from) if date_from else None
    dt_to = _parse_iso(date_to) if date_to else None

    for r in run_rows:
        st = _parse_iso(r["started_at"])
        fn = _parse_iso(r["finished_at"])
        if dt_from and st and st < dt_from:
            continue
        if dt_to and st and st > dt_to:
            continue
        in_tok += int(r["input_tokens"] or 0)
        cached_tok += int(r["cached_tokens"] or 0)
        out_tok += int(r["output_tokens"] or 0)
        reas_tok += int(r["reasoning_tokens"] or 0)
        by_task_times.setdefault(str(r["task_id"]), []).append((st, fn))

    completed_ids = {t.task_id for t in completed_tasks}
    for tid, spans in by_task_times.items():
        if tid not in completed_ids:
            continue
        starts = [s for s, _ in spans if s is not None]
        ends = [e for _, e in spans if e is not None]
        if starts and ends:
            dur = (max(ends) - min(starts)).total_seconds()
            if dur >= 0:
                lead_times.append(dur)
    avg_lead_sec = round(sum(lead_times) / len(lead_times), 2) if lead_times else 0.0

    # 人工介入统计（steer、人工审查打回、门禁豁免）
    steer_cnt = int(db._query("SELECT COUNT(*) AS c FROM events WHERE event_type LIKE '%steer%'")[0]["c"] or 0)
    review_reject_cnt = int(db._query("SELECT COUNT(*) AS c FROM code_reviews WHERE status='CHANGES_REQUESTED'")[0]["c"] or 0)
    gate_waive_cnt = int(db._query("SELECT COUNT(*) AS c FROM gate_records WHERE status='WAIVED'")[0]["c"] or 0)

    # 审查问题来源分布
    cmt_rows = db._query("SELECT source, COUNT(*) AS c FROM review_comments GROUP BY source")
    findings_by_source = {str(r["source"]): int(r["c"] or 0) for r in cmt_rows}

    # CI 通过率
    ci_rows = db._query("SELECT status FROM ci_runs WHERE status IN ('SUCCESS', 'FAILURE', 'TIMEOUT')")
    ci_total = len(ci_rows)
    ci_ok = sum(1 for r in ci_rows if r["status"] == "SUCCESS")
    ci_pass_rate = round(ci_ok / ci_total, 4) if ci_total else 1.0

    # 覆盖率趋势
    cov_rows = db._query("SELECT id, task_id, milestone_id, metrics_json, created_at FROM quality_results WHERE gate='diff_coverage' ORDER BY id ASC")
    coverage_trend: list[dict[str, Any]] = []
    for cr in cov_rows[-20:]:
        try:
            m = json.loads(cr["metrics_json"] or "{}")
        except json.JSONDecodeError:
            m = {}
        if "changed_line_coverage" in m:
            coverage_trend.append(
                {
                    "id": int(cr["id"]),
                    "scope": cr["task_id"] or cr["milestone_id"] or "",
                    "coverage": float(m["changed_line_coverage"]),
                    "created_at": cr["created_at"],
                }
            )

    in_price, out_price = DEFAULT_MODEL_PRICES_PER_1M_CNY["default"]
    est_cost = round(((in_tok / 1_000_000.0) * in_price) + (((out_tok + reas_tok) / 1_000_000.0) * out_price), 4)
    total_tok = in_tok + out_tok + reas_tok

    return {
        "project": project_id,
        "milestones": {
            "total": len(milestones),
            "merged": sum(1 for m in milestones if m.status == MilestoneStatus.MERGED),
        },
        "tasks": {
            "total": total_tasks,
            "completed": len(completed_tasks),
            "fused": len(fused_tasks),
            "first_pass_ai_review_rate": first_pass_rate,
            "avg_rework_rounds": avg_rework,
            "fuse_rate": fuse_rate,
            "avg_lead_time_sec": avg_lead_sec,
        },
        "human_interventions": {
            "total": steer_cnt + review_reject_cnt + gate_waive_cnt,
            "steers": steer_cnt,
            "review_changes_requested": review_reject_cnt,
            "gate_waivers": gate_waive_cnt,
        },
        "review_findings": {
            "ai": findings_by_source.get("ai", 0),
            "quality": findings_by_source.get("quality", 0),
            "human": findings_by_source.get("human", 0),
        },
        "ci": {
            "finished_runs": ci_total,
            "passed_runs": ci_ok,
            "pass_rate": ci_pass_rate,
        },
        "coverage_trend": coverage_trend,
        "tokens": {
            "input": in_tok,
            "cached": cached_tok,
            "output": out_tok,
            "reasoning": reas_tok,
            "total": total_tok,
            "budget_limit": int(token_budget_limit or 0),
            "estimated_cost_cny": est_cost,
        },
    }


def compute_portfolio_metrics(workspaces_parent: Path, *, active_engine: "Engine | None" = None, allowed_projects: set[str] | None = None) -> dict[str, Any]:
    """跨工作区汇总领导驾驶舱指标。"""
    seen_roots: set[Path] = set()
    projects_out: list[dict[str, Any]] = []

    def _collect_one(pid: str, ws_root: Path, existing_eng: "Engine | None" = None) -> None:
        resolved = ws_root.resolve()
        if resolved in seen_roots or not (resolved / ".autodev" / "state.db").is_file():
            return
        if allowed_projects is not None:
            actual = existing_eng.settings if existing_eng else Workspace(resolved).load_settings()
            if actual.loopforge_project_id not in allowed_projects:
                return
            pid = actual.loopforge_project_id
        seen_roots.add(resolved)
        if existing_eng is not None and existing_eng.ws.root.resolve() == resolved:
            projects_out.append(
                compute_project_metrics(
                    existing_eng.db,
                    project_id=pid,
                    token_budget_limit=existing_eng.settings.token_budget_limit,
                )
            )
            return
        ws = Workspace(resolved)
        db = Database(ws.db_path)
        try:
            cfg = ws.load_settings()
            projects_out.append(
                compute_project_metrics(
                    db,
                    project_id=pid or cfg.loopforge_project_id or resolved.name,
                    token_budget_limit=cfg.token_budget_limit,
                )
            )
        finally:
            db.close()

    if active_engine is not None:
        _collect_one(
            active_engine.settings.loopforge_project_id or active_engine.ws.root.name,
            active_engine.ws.root,
            existing_eng=active_engine,
        )

    if workspaces_parent.is_dir():
        reg = load_project_registry(workspaces_parent)
        for pid, meta in sorted(reg.items()):
            if isinstance(meta, dict) and meta.get("root"):
                _collect_one(str(pid), Path(str(meta["root"])))
        for entry in sorted(workspaces_parent.iterdir()):
            if entry.is_dir() and not entry.is_symlink():
                _collect_one(entry.name, entry)

    totals = {
        "project_count": len(projects_out),
        "tasks_total": sum(p["tasks"]["total"] for p in projects_out),
        "tasks_completed": sum(p["tasks"]["completed"] for p in projects_out),
        "tokens_total": sum(p["tokens"]["total"] for p in projects_out),
        "estimated_cost_cny": round(sum(p["tokens"]["estimated_cost_cny"] for p in projects_out), 4),
    }
    return {"totals": totals, "projects": projects_out}
