"""人工代码审查 HTTP 路由（WP-2：/vernal-api/reviews*）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from autodev.review.service import (
    ReviewConflictError,
    add_comment,
    decide_review,
    get_review,
    list_reviews,
    update_comment,
)
from autodev.server.deps import effective_actor, get_ctx

router = APIRouter()


class ReviewCommentCreateRequest(BaseModel):
    file: str
    line: int = 0
    side: str = "new"
    severity: str = "major"
    body: str
    actor: str = "owner"


class ReviewCommentPatchRequest(BaseModel):
    resolved: bool | None = None
    body: str | None = None
    severity: str | None = None


class ReviewDecisionRequest(BaseModel):
    decision: str
    note: str = ""
    expected_head: str = ""
    override_reason: str = ""
    checklist: dict[str, bool] = Field(default_factory=dict)
    target_task_ids: list[str] = Field(default_factory=list)
    actor: str = "owner"


@router.get("/vernal-api/reviews")
async def api_list_reviews(
    request: Request,
    status: str = "",
    scope_type: str = "",
    milestone_id: str = "",
) -> list[dict[str, Any]]:
    eng = get_ctx(request).active_slot(request).engine
    return list_reviews(
        eng.db,
        status=status.strip() or None,
        scope_type=scope_type.strip() or None,
        milestone_id=milestone_id.strip() or None,
    )


@router.get("/vernal-api/reviews/{review_id}")
async def api_get_review(review_id: int, request: Request) -> dict[str, Any]:
    eng = get_ctx(request).active_slot(request).engine
    try:
        return get_review(eng, review_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/vernal-api/reviews/{review_id}/patch")
async def api_download_review_patch(review_id: int, request: Request) -> PlainTextResponse:
    eng = get_ctx(request).active_slot(request).engine
    try:
        detail = get_review(eng, review_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    patch_txt = detail["diff"].get("raw_patch") or ""
    filename = f"review-{review_id}-r{detail['review']['round']}.patch"
    return PlainTextResponse(
        content=patch_txt,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/vernal-api/reviews/{review_id}/comments")
async def api_add_review_comment(review_id: int, request: Request, req: ReviewCommentCreateRequest) -> dict[str, Any]:
    eng = get_ctx(request).active_slot(request).engine
    actor = effective_actor(req.actor)
    try:
        return add_comment(
            eng.db,
            review_id,
            file=req.file,
            line=req.line,
            side=req.side,
            severity=req.severity,
            body=req.body,
            author=actor,
            source="human",
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.patch("/vernal-api/reviews/comments/{comment_id}")
async def api_patch_review_comment(comment_id: int, request: Request, req: ReviewCommentPatchRequest) -> dict[str, Any]:
    eng = get_ctx(request).active_slot(request).engine
    try:
        return update_comment(eng.db, comment_id, resolved=req.resolved, body=req.body, severity=req.severity)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/vernal-api/reviews/{review_id}/decision")
async def api_decide_review(review_id: int, request: Request, req: ReviewDecisionRequest) -> dict[str, Any]:
    from autodev.governance import append_audit_log, check_role_permission, platform_db_path_for

    ctx = get_ctx(request)
    eng = ctx.active_slot(request).engine
    actor = effective_actor(req.actor)
    pid = eng.settings.loopforge_project_id or eng.ws.root.name
    try:
        check_role_permission(eng.db, actor, ("admin", "architect", "reviewer"), project=pid)
        out = decide_review(
            eng,
            review_id,
            req.decision,
            audit_project=pid, actor_ip=request.client.host if request.client else "127.0.0.1",
            actor=actor,
            note=req.note,
            expected_head=req.expected_head,
            override_reason=req.override_reason,
            checklist=req.checklist,
            target_task_ids=req.target_task_ids,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ReviewConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    from autodev.governance.audit import flush_decisions
    flush_decisions(eng.db, platform_db_path_for(ctx.workspaces_parent()))
    return out
