"""流程阶段门禁路由（WP-0：/vernal-api/process 与 /vernal-api/gates）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from autodev.process.runner import (
    BUILTIN_TEMPLATES,
    GateConflictError,
    decide_gate,
    list_gate_records,
    load_builtin_template,
    load_template,
    save_custom_template,
    stage_status,
)
from autodev.server.deps import effective_actor, get_ctx

router = APIRouter()


class GateDecisionRequest(BaseModel):
    decision: str
    comment: str = ""
    expected_round: int | None = None
    actor: str = "owner"
    evidence: dict[str, Any] = Field(default_factory=dict)


class ProcessTemplateUpdateRequest(BaseModel):
    template_id: str = ""
    yaml_content: str = ""
    actor: str = "owner"


@router.get("/vernal-api/process")
async def api_get_process(request: Request) -> dict[str, Any]:
    slot = get_ctx(request).active_slot(request)
    eng = slot.engine
    tpl = load_template(eng.ws, eng.settings)
    stages = [s.model_dump() for s in stage_status(eng.db, eng.ws, tpl)]
    needs_you = sum(int(s.get("pending_count") or 0) for s in stages if s.get("status") == "WAITING_GATE")
    return {
        "template": tpl.model_dump(),
        "available_templates": [load_builtin_template(tid).model_dump(include={"id", "name", "description"}) for tid in BUILTIN_TEMPLATES],
        "stages": stages,
        "needs_you": needs_you,
    }


@router.get("/vernal-api/gates")
async def api_list_gates(request: Request, status: str = "") -> list[dict[str, Any]]:
    eng = get_ctx(request).active_slot(request).engine
    return list_gate_records(eng.db, status=status.strip().upper() or None)


@router.post("/vernal-api/gates/{record_id}/decide")
async def api_decide_gate(record_id: int, request: Request, req: GateDecisionRequest) -> dict[str, Any]:
    from autodev.governance import append_audit_log, check_role_permission, platform_db_path_for

    ctx = get_ctx(request)
    eng = ctx.active_slot(request).engine
    actor = effective_actor(req.actor)
    pid = eng.settings.loopforge_project_id or eng.ws.root.name

    rec_rows = eng.db._query("SELECT * FROM gate_records WHERE record_id=?", (record_id,))
    if not rec_rows:
        raise HTTPException(status_code=404, detail=f"门禁记录不存在：{record_id}")
    rec_row = dict(rec_rows[0])

    # 校验门禁 RBAC 权限：豁免要求 admin；正常审批遵循模板 approver_roles（默认 admin / architect / reviewer）
    tpl = load_template(eng.ws, eng.settings)
    gate_def = next(
        (g for s in tpl.stages for g in s.gates if g.id == rec_row["gate_id"]),
        None,
    )
    is_waive = req.decision.strip().lower() in ("waive", "waived")
    allowed_roles = ("admin",) if is_waive else tuple(gate_def.approver_roles if (gate_def and gate_def.approver_roles) else ("admin", "architect", "reviewer"))
    try:
        check_role_permission(eng.db, actor, allowed_roles, project=pid)
        decided = decide_gate(
            eng.db,
            record_id,
            req.decision,
            audit_project=pid, actor_ip=request.client.host if request.client else "127.0.0.1",
            actor=actor,
            comment=req.comment,
            expected_round=req.expected_round,
            evidence=req.evidence,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except GateConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    from autodev.governance.audit import flush_decisions
    flush_decisions(eng.db, platform_db_path_for(ctx.workspaces_parent()))
    return decided


@router.put("/vernal-api/process/template")
async def api_put_process_template(request: Request, req: ProcessTemplateUpdateRequest) -> dict[str, Any]:
    from autodev.governance import append_audit_log, check_role_permission, platform_db_path_for

    ctx = get_ctx(request)
    slot = ctx.active_slot(request)
    eng = slot.engine
    actor = effective_actor(req.actor)
    pid = eng.settings.loopforge_project_id or eng.ws.root.name
    try:
        check_role_permission(eng.db, actor, ("admin", "architect"), project=pid)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    custom_file = eng.ws.state_dir / "process.yaml"

    if req.yaml_content.strip():
        try:
            tpl = save_custom_template(eng.ws, req.yaml_content)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"无效的流程模板 YAML：{exc}") from exc
        if tpl.id in BUILTIN_TEMPLATES:
            eng.settings.process_template = tpl.id
        if tpl.review_policy:
            eng.settings.review_policy = tpl.review_policy
        ctx.persist_config(eng)
        eng.db.add_event("process.template_updated", f"已应用自定义流程模板：{tpl.name}", actor=actor)
        return {"ok": True, "source": "custom_yaml", "template": tpl.model_dump()}

    tid = req.template_id.strip()
    if tid not in BUILTIN_TEMPLATES:
        raise HTTPException(status_code=422, detail=f"未知的内置流程模板：{tid}")
    if custom_file.exists():
        custom_file.unlink()
    tpl = load_builtin_template(tid)
    eng.settings.process_template = tid
    eng.settings.review_policy = tpl.review_policy
    ctx.persist_config(eng)
    eng.db.add_event("process.template_updated", f"已切换工程流程模板为 {tpl.name} ({tid})", actor=actor)
    return {"ok": True, "source": "builtin", "template": tpl.model_dump()}
