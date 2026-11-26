"""CI/CD 持续集成与部署路由（WP-3：/vernal-api/ci/* 与 /vernal-api/deployments）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from autodev.ci.service import (
    CISignatureError,
    get_ci_run,
    handle_ci_callback,
    list_ci_runs,
    list_deployments,
    poll_ci_run,
    trigger_deployment,
    trigger_milestone_ci,
)
from autodev.review.service import current_milestone_candidate_ref
from autodev.server.deps import PROJECT_ID_RE, current_user_var, get_ctx

router = APIRouter()


class DeploymentTriggerRequest(BaseModel):
    """受控部署请求体：禁止客户端直接传入任何待执行 shell 命令。"""

    model_config = ConfigDict(extra="forbid")

    milestone_id: str
    target: str = "staging"
    version_no: int | None = None
    rollback_to_previous: bool = False
    actor: str = ""


@router.get("/vernal-api/ci/runs")
async def api_list_ci_runs(request: Request, milestone: str = "") -> list[dict[str, Any]]:
    eng = get_ctx(request).active_slot(request).engine
    return list_ci_runs(eng.db, milestone_id=milestone.strip() or None)


@router.post("/vernal-api/ci/runs/{ci_run_id}/poll")
async def api_poll_ci_run(ci_run_id: int, request: Request) -> dict[str, Any]:
    eng = get_ctx(request).active_slot(request).engine
    try:
        return await poll_ci_run(eng, ci_run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/vernal-api/ci/runs/{ci_run_id}/retry")
async def api_retry_ci_run(ci_run_id: int, request: Request) -> dict[str, Any]:
    eng = get_ctx(request).active_slot(request).engine
    cur = get_ci_run(eng.db, ci_run_id)
    if cur is None:
        raise HTTPException(status_code=404, detail=f"CI 记录不存在：{ci_run_id}")
    ms = eng.db.get_milestone(str(cur["milestone_id"]))
    if ms is None:
        raise HTTPException(status_code=404, detail="关联里程碑不存在")
    cand = str(cur.get("candidate_ref") or current_milestone_candidate_ref(eng, ms.milestone_id) or cur["commit_sha"])
    return await trigger_milestone_ci(eng, ms, cand)


@router.post("/vernal-api/ci/callback/{project}/{milestone}/{round_no}/{sig}")
async def api_ci_callback(
    project: str,
    milestone: str,
    round_no: int,
    sig: str,
    request: Request,
) -> dict[str, Any]:
    if not PROJECT_ID_RE.fullmatch(project) or project in (".", ".."):
        raise HTTPException(status_code=400, detail=f"非法的项目标识：{project}")
    ctx = get_ctx(request)
    slot = ctx.find_or_open_slot(project, create=False)
    if slot is None:
        raise HTTPException(status_code=404, detail=f"未知项目：{project}")
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            payload = {"raw": payload}
    except Exception:
        payload = {}
    try:
        return handle_ci_callback(slot.engine, project, milestone, round_no, sig, payload)
    except CISignatureError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/vernal-api/deployments")
async def api_list_deployments(request: Request, milestone: str = "") -> list[dict[str, Any]]:
    eng = get_ctx(request).active_slot(request).engine
    return list_deployments(eng.db, milestone_id=milestone.strip() or None)


@router.post("/vernal-api/deployments")
async def api_create_deployment(request: Request, req: DeploymentTriggerRequest) -> dict[str, Any]:
    eng = get_ctx(request).active_slot(request).engine
    session_login = current_user_var.get()
    supplied_actor = req.actor.strip() or (request.headers.get("x-vernal-actor") or "").strip()
    if session_login:
        if supplied_actor and supplied_actor != session_login:
            raise HTTPException(status_code=403, detail="部署操作者身份与当前已认证账号不一致")
        actor = session_login
    else:
        if not supplied_actor:
            raise HTTPException(status_code=422, detail="必须提供有效的部署操作者 (actor)")
        actor = supplied_actor

    ver = req.version_no
    if req.rollback_to_previous and ver is None:
        deliveries = eng.db.list_milestone_deliveries(req.milestone_id)
        if len(deliveries) < 2:
            raise HTTPException(status_code=422, detail="没有可回滚的历史交付版本")
        ver = int(deliveries[-2]["version_no"])
    try:
        dep = await trigger_deployment(
            eng,
            req.milestone_id,
            target=req.target,
            version_no=ver,
            actor=actor,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    from autodev.governance import append_audit_log, platform_db_path_for

    ctx = get_ctx(request)
    append_audit_log(
        platform_db_path_for(ctx.workspaces_parent()),
        actor=actor,
        actor_ip=request.client.host if request.client else "127.0.0.1",
        project=eng.settings.loopforge_project_id or eng.ws.root.name,
        action="cd.deployed",
        target=f"{req.milestone_id}@v{dep['version_no']}",
        detail={"target": dep["target"], "status": dep["status"]},
    )
    return dep
