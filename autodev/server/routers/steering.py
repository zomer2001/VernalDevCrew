"""进展与问题评审及阶段转向变更单路由。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from autodev.loopforge.client import LoopForgeError
from autodev.server.deps import effective_actor, get_ctx
from autodev.server.schemas import (
    ActorRequest,
    SteeringApproveRequest,
    SteeringRejectRequest,
    SteeringStartRequest,
)
from autodev.steering import (
    approve_steering_checkpoint,
    current_rule_version,
    reconcile_steering_checkpoint,
    reject_steering_checkpoint,
    retry_steering_checkpoint,
    start_steering_review,
)

router = APIRouter()


@router.get("/vernal-api/steering")
async def api_steering_list(request: Request) -> dict[str, Any]:
    ctx = get_ctx(request)
    slot = ctx.active_slot(request)
    eng = slot.engine
    for cp in eng.db.list_checkpoints():
        if cp.get("status") in ("RUNNING", "AWAITING_APPROVAL", "SYNC_PENDING"):
            cid = int(cp["checkpoint_id"])
            updated = await reconcile_steering_checkpoint(eng, cid)
            if updated and updated.get("status") in ("RUNNING", "AWAITING_APPROVAL", "SYNC_PENDING"):
                ctx.ensure_steering_poller(slot, cid)
    return {
        "decisions": eng.db.list_decisions(),
        "checkpoints": eng.db.list_checkpoints(),
        "rule_version": current_rule_version(eng),
    }


@router.post("/vernal-api/steering/start")
async def api_steering_start(request: Request, req: SteeringStartRequest) -> dict[str, Any]:
    ctx = get_ctx(request)
    slot = ctx.active_slot(request)
    eng = slot.engine
    if not req.topic.strip() and not req.human_note.strip():
        raise HTTPException(status_code=400, detail="请填写要讨论的议题或调整方向")
    try:
        cp = await start_steering_review(
            eng,
            topic=req.topic.strip() or req.human_note.strip()[:72],
            human_note=req.human_note,
            kind=req.kind,
            actor=effective_actor(req.actor),
        )
    except LoopForgeError as exc:
        raise HTTPException(status_code=exc.status_code or 502, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if cp.get("status") in ("RUNNING", "AWAITING_APPROVAL", "SYNC_PENDING"):
        ctx.ensure_steering_poller(slot, int(cp["checkpoint_id"]))
    return {"ok": True, "checkpoint": cp}


@router.post("/vernal-api/steering/{checkpoint_id}/retry")
async def api_steering_retry(checkpoint_id: int, request: Request, req: ActorRequest) -> dict[str, Any]:
    ctx = get_ctx(request)
    slot = ctx.active_slot(request)
    try:
        cp = await retry_steering_checkpoint(slot.engine, checkpoint_id, effective_actor(req.actor))
    except LoopForgeError as exc:
        raise HTTPException(exc.status_code or 502, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    ctx.ensure_steering_poller(slot, checkpoint_id)
    return {"ok": True, "checkpoint": cp}


async def _authorize_checkpoint(request, checkpoint_id):
    from autodev.server.routers.runtime import project_run, authorize_run_decision
    ctx = get_ctx(request)
    slot = ctx.active_slot(request)
    cp = slot.engine.db.get_checkpoint(checkpoint_id)
    if cp and cp.get("loopforge_run_id"):
        run = await project_run(ctx,slot,cp["loopforge_run_id"])
        await authorize_run_decision(ctx,request,run,"director_plan")


@router.post("/vernal-api/steering/{checkpoint_id}/approve")
async def api_steering_approve(checkpoint_id: int, request: Request, req: SteeringApproveRequest) -> dict[str, Any]:
    eng = get_ctx(request).active_slot(request).engine
    if request.headers.get("cookie") or eng.settings.native_approver_user_id:
        await _authorize_checkpoint(request,checkpoint_id)
    try:
        cp = await approve_steering_checkpoint(
            eng,
            checkpoint_id,
            actor=effective_actor(req.actor),
            comment=req.comment,
            override_plan=req.override_plan,
            expected_revision=req.expected_revision,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except LoopForgeError as exc:
        raise HTTPException(status_code=exc.status_code or 502, detail=str(exc)) from exc
    return {
        "ok": True,
        "checkpoint": cp,
        "decisions": eng.db.list_decisions(),
        "rule_version": current_rule_version(eng),
    }


@router.post("/vernal-api/steering/{checkpoint_id}/reject")
async def api_steering_reject(checkpoint_id: int, request: Request, req: SteeringRejectRequest) -> dict[str, Any]:
    ctx = get_ctx(request)
    slot = ctx.active_slot(request)
    eng = slot.engine
    if request.headers.get("cookie") or eng.settings.native_approver_user_id:
        await _authorize_checkpoint(request,checkpoint_id)
    try:
        cp = await reject_steering_checkpoint(
            eng,
            checkpoint_id,
            actor=effective_actor(req.actor),
            reason=req.reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except LoopForgeError as exc:
        raise HTTPException(status_code=exc.status_code or 502, detail=str(exc)) from exc
    if cp.get("status") in ("RUNNING", "AWAITING_APPROVAL", "SYNC_PENDING"):
        ctx.ensure_steering_poller(slot, int(cp["checkpoint_id"]))
    return {"ok": True, "checkpoint": cp}
