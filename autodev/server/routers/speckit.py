"""Feature specifications inside the existing project, identity and planning lifecycle."""
from __future__ import annotations
import json
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from autodev.server.deps import effective_actor, get_ctx
from autodev.speckit import adapter, compiler, service
from autodev.speckit.assets import load_assets

router=APIRouter()


class EnableRequest(BaseModel):
    preset_id: Literal["vernal-core","vernal-lite"] = "vernal-core"
    migrate: bool = False


class StepRequest(BaseModel):
    feature_id: str
    step: Literal["constitution","specify","clarify","checklist","plan","tasks","analyze"]
    instructions: str = ""


class RevisionRequest(BaseModel):
    content: str
    expected_rev_id: int = Field(ge=0)
    summary: str = "人工修订"


class AnswerRequest(BaseModel):
    answer: str
    expected_rev_id: int = Field(ge=1)


class PublishRequest(BaseModel):
    expected_hashes: list[str]


class RestoreRequest(BaseModel):
    expected_rev_id: int = Field(ge=1)


def _error(exc):
    return HTTPException(409 if isinstance(exc,service.SpecConflictError) else 422,detail=str(exc))


def _engine(request, *, write=False):
    eng=get_ctx(request).active_slot(request).engine
    if write:
        from autodev.governance import check_role_permission
        try:
            check_role_permission(eng.db,effective_actor(),("admin","architect","developer"),
                                  project=eng.settings.loopforge_project_id or eng.ws.root.name)
        except PermissionError as exc:
            raise HTTPException(403,detail=str(exc)) from exc
    return eng


@router.get("/vernal-api/specifications")
def state(request: Request):
    eng=_engine(request)
    if not service.enabled(eng):
        return {"enabled":False,"legacy":eng.ws.prd_json_path.is_file(),"features":[]}
    try:
        lock,_=load_assets(eng)
        specs=service.current_specs(eng)
        features=[]
        for spec in specs:
            feature=spec["feature_id"]
            features.append({"feature_id":feature,"sources":{k:service.latest(eng,feature,k) for k in ("spec","plan","tasks","checklist","analyze")},
                             "clarifications":service.list_clarifications(eng,feature),
                             "last_step":json.loads(eng.db.meta_get(f"speckit.last_step:{feature}") or "null")})
        return {"enabled":True,"assets":lock,"features":features,
                "constitution":service.latest(eng,"project","constitution"),
                "running":get_ctx(request).is_planning(get_ctx(request).active_slot(request))}
    except (ValueError,OSError) as exc:
        raise _error(exc) from exc


@router.post("/vernal-api/specifications/enable")
def enable(req: EnableRequest,request: Request):
    try:
        return service.enable(_engine(request,write=True),req.preset_id,migrate=req.migrate,actor=effective_actor())
    except (ValueError,OSError) as exc:
        raise _error(exc) from exc


@router.post("/vernal-api/specifications/run")
async def run(req: StepRequest,request: Request):
    ctx=get_ctx(request); slot=ctx.active_slot(request); eng=_engine(request,write=True)
    if ctx.is_planning(slot) or ctx.is_scheduler_running(slot):
        raise HTTPException(409,detail="当前项目已有规划或执行任务，请完成后再运行规格步骤")
    try:
        from autodev.speckit.paths import feature_spec_dir
        feature_spec_dir(req.feature_id)
        lock,_=load_assets(eng)
    except (ValueError,OSError) as exc:
        raise _error(exc) from exc
    actor=effective_actor()
    async def work():
        try:
            await adapter.run_step(eng,req.feature_id,req.step,lock["preset_id"],instructions=req.instructions,actor=actor)
        except Exception as exc:
            eng.db.add_event("speckit.step_failed",str(exc),actor=actor)
    slot.plan_task=ctx.start_background(slot,"_speckit_step",work())
    return {"started":True}


@router.put("/vernal-api/specifications/{feature_id}/{kind}")
def save(feature_id: str,kind: str,req: RevisionRequest,request: Request):
    if kind not in ("constitution","spec","plan","tasks"):
        raise HTTPException(422,detail="检查结论只能由检查步骤生成")
    try:
        return service.publish_revision(_engine(request,write=True),feature_id,kind,req.content,
                                        expected_rev_id=req.expected_rev_id,author=effective_actor(),summary=req.summary)
    except (ValueError,OSError) as exc:
        raise _error(exc) from exc


@router.post("/vernal-api/specifications/clarifications/{clarify_id}/answer")
def answer(clarify_id: int,req: AnswerRequest,request: Request):
    try:
        return service.answer_clarification(_engine(request,write=True),clarify_id,req.answer,
                                            expected_rev_id=req.expected_rev_id,actor=effective_actor())
    except (ValueError,OSError) as exc:
        raise _error(exc) from exc


@router.post("/vernal-api/specifications/{feature_id}/{kind}/restore")
def restore(feature_id: str,kind: str,req: RestoreRequest,request: Request):
    try:
        return service.restore_source(_engine(request,write=True),feature_id,kind,expected_rev_id=req.expected_rev_id,actor=effective_actor())
    except (ValueError,OSError) as exc:
        raise _error(exc) from exc


@router.get("/vernal-api/specifications/{feature_id}/compile")
def compile(feature_id: str,request: Request):
    try:
        return {"packages":[p.model_dump() for p in compiler.compile_tasks(_engine(request),feature_id)]}
    except (ValueError,OSError) as exc:
        raise _error(exc) from exc


@router.post("/vernal-api/specifications/{feature_id}/publish")
def publish(feature_id: str,req: PublishRequest,request: Request):
    try:
        return {"task_ids":compiler.publish_tasks(_engine(request,write=True),feature_id,
                                                  expected_hashes=req.expected_hashes,actor=effective_actor())}
    except (ValueError,OSError) as exc:
        raise _error(exc) from exc
