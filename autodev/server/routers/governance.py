"""效能度量、防篡改审计日志与 RBAC 成员管理 HTTP 路由（WP-7）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from autodev.governance import (
    append_audit_log,
    check_role_permission,
    compute_portfolio_metrics,
    compute_project_metrics,
    export_audit_csv,
    list_members,
    platform_db_path_for,
    query_audit_logs,
    remove_member,
    upsert_member,
    verify_audit_chain,
)
from autodev.server.deps import effective_actor, get_ctx

router = APIRouter()


class MemberUpsertRequest(BaseModel):
    user_id: str
    role: str
    project: str = "*"
    actor: str = "owner"


class GovernanceSettingsRequest(BaseModel):
    data_classification: str | None = None
    token_budget_limit: int | None = None
    actor: str = "owner"


@router.get("/vernal-api/metrics")
async def api_get_metrics(request: Request, date_from: str = "", date_to: str = "") -> dict[str, Any]:
    slot = get_ctx(request).active_slot(request)
    eng = slot.engine
    pid = eng.settings.loopforge_project_id or eng.ws.root.name
    return compute_project_metrics(
        eng.db,
        project_id=pid,
        token_budget_limit=eng.settings.token_budget_limit,
        date_from=date_from.strip(),
        date_to=date_to.strip(),
    )


@router.get("/vernal-api/metrics/portfolio")
async def api_get_portfolio_metrics(request: Request) -> dict[str, Any]:
    ctx = get_ctx(request)
    eng = ctx.active_slot(request).engine
    return compute_portfolio_metrics(ctx.workspaces_parent(), active_engine=eng, allowed_projects=getattr(request.state, "allowed_projects", None))


@router.get("/vernal-api/audit")
async def api_list_audit_logs(
    request: Request,
    project: str = "",
    action: str = "",
    actor: str = "",
    limit: int = 200,
    format: str = "json",
) -> Any:
    ctx = get_ctx(request)
    pdb = platform_db_path_for(ctx.workspaces_parent())
    rows = query_audit_logs(pdb, project=project, action=action, actor=actor, limit=limit,
                            allowed_projects=None if getattr(request.state, "platform_admin", False)
                            else getattr(request.state, "allowed_projects", None))
    if format.strip().lower() == "csv":
        csv_str = export_audit_csv(rows)
        return Response(
            content=csv_str.encode("utf-8-sig"),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="vernal-audit.csv"'},
        )
    return rows


@router.get("/vernal-api/audit/verify")
async def api_verify_audit_logs(request: Request) -> dict[str, Any]:
    ctx = get_ctx(request)
    pdb = platform_db_path_for(ctx.workspaces_parent())
    return verify_audit_chain(pdb)


@router.get("/vernal-api/rbac/members")
async def api_list_rbac_members(request: Request, project: str = "") -> list[dict[str, Any]]:
    eng = get_ctx(request).active_slot(request).engine
    return list_members(eng.db, project=project)


@router.post("/vernal-api/rbac/members")
async def api_upsert_rbac_member(request: Request, req: MemberUpsertRequest) -> dict[str, Any]:
    ctx = get_ctx(request)
    eng = ctx.active_slot(request).engine
    actor = effective_actor(req.actor)
    if req.project not in ("*", eng.settings.loopforge_project_id or eng.ws.root.name):
        raise HTTPException(403, "研发角色只能在当前项目内收紧")
    try:
        # 原生管理资格由中间件校验，已配置的研发角色进一步收紧。
        check_role_permission(eng.db, actor, ("admin",), project=req.project)
        saved = upsert_member(eng.db, req.user_id, req.role, project=req.project)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    append_audit_log(
        platform_db_path_for(ctx.workspaces_parent()),
        actor=actor,
        actor_ip=request.client.host if request.client else "127.0.0.1",
        project=eng.settings.loopforge_project_id or eng.ws.root.name,
        action="rbac.member_upserted",
        target=f"{req.user_id}@{req.project}",
        detail={"role": saved["role"]},
    )
    return saved


@router.delete("/vernal-api/rbac/members/{user_id}")
async def api_delete_rbac_member(user_id: str, request: Request, project: str = "*", actor: str = "owner") -> dict[str, Any]:
    ctx = get_ctx(request)
    eng = ctx.active_slot(request).engine
    eff_actor = effective_actor(actor)
    try:
        check_role_permission(eng.db, eff_actor, ("admin",), project=project)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    removed = remove_member(eng.db, user_id, project=project)
    if not removed:
        raise HTTPException(status_code=404, detail=f"成员不存在：{user_id}")
    append_audit_log(
        platform_db_path_for(ctx.workspaces_parent()),
        actor=eff_actor,
        actor_ip=request.client.host if request.client else "127.0.0.1",
        project=eng.settings.loopforge_project_id or eng.ws.root.name,
        action="rbac.member_deleted",
        target=f"{user_id}@{project}",
    )
    return {"ok": True}


@router.post("/vernal-api/governance/settings")
async def api_update_governance_settings(request: Request, req: GovernanceSettingsRequest) -> dict[str, Any]:
    ctx = get_ctx(request)
    eng = ctx.active_slot(request).engine
    actor = effective_actor(req.actor)
    try:
        check_role_permission(eng.db, actor, ("admin",), project=eng.settings.loopforge_project_id or "*")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    if req.data_classification is not None:
        dc = req.data_classification.strip().lower()
        if dc not in ("public", "internal"):
            raise HTTPException(status_code=422, detail="data_classification 仅支持 public 或 internal")
        eng.settings.data_classification = dc
    if req.token_budget_limit is not None:
        if req.token_budget_limit < 0:
            raise HTTPException(status_code=422, detail="token_budget_limit 不能为负数")
        eng.settings.token_budget_limit = int(req.token_budget_limit)
    ctx.persist_config(eng)
    append_audit_log(
        platform_db_path_for(ctx.workspaces_parent()),
        actor=actor,
        actor_ip=request.client.host if request.client else "127.0.0.1",
        project=eng.settings.loopforge_project_id or eng.ws.root.name,
        action="governance.settings_updated",
        target="workspace_settings",
        detail={
            "data_classification": eng.settings.data_classification,
            "token_budget_limit": eng.settings.token_budget_limit,
        },
    )
    return {
        "ok": True,
        "data_classification": eng.settings.data_classification,
        "token_budget_limit": eng.settings.token_budget_limit,
    }
