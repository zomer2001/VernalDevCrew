"""质量门禁结果查询路由（WP-4：/vernal-api/tasks/{id}/quality 与 /vernal-api/milestones/{id}/quality）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from autodev.quality.runner import list_quality_results
from autodev.server.deps import get_ctx

router = APIRouter()


@router.get("/vernal-api/tasks/{task_id}/quality")
async def api_task_quality(task_id: str, request: Request, latest_only: bool = False) -> dict[str, Any]:
    eng = get_ctx(request).active_slot(request).engine
    rows = list_quality_results(eng.db, task_id=task_id, latest_only=latest_only)
    return {
        "task_id": task_id,
        "passed": not any(r["status"] == "FAILED" for r in rows),
        "results": rows,
    }


@router.get("/vernal-api/milestones/{milestone_id}/quality")
async def api_milestone_quality(milestone_id: str, request: Request) -> dict[str, Any]:
    eng = get_ctx(request).active_slot(request).engine
    ms_rows = list_quality_results(eng.db, milestone_id=milestone_id)
    task_ids = [t.task_id for t in eng.db.list_tasks(milestone_id=milestone_id)]
    task_rows: dict[str, list[dict[str, Any]]] = {
        tid: list_quality_results(eng.db, task_id=tid, latest_only=True) for tid in task_ids
    }
    failed = any(r["status"] == "FAILED" for r in ms_rows) or any(
        r["status"] == "FAILED" for rs in task_rows.values() for r in rs
    )
    return {
        "milestone_id": milestone_id,
        "passed": not failed,
        "milestone_results": ms_rows,
        "task_results": task_rows,
    }
