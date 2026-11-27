"""Feature specifications inside the existing project, identity and planning lifecycle."""
from __future__ import annotations
import json
import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Query
from pydantic import BaseModel, Field

from autodev.server.deps import effective_actor, get_ctx
from autodev.loopforge.client import LoopForgeError
from autodev.loopforge import reconciler
from autodev.speckit import adapter, assets, compiler, service, missions, preflight
from autodev.speckit.assets import load_assets

router=APIRouter()


class EnableRequest(BaseModel):
    preset_id: Literal["vernal-core","vernal-lite"] = "vernal-core"
    migrate: bool = False


class UpgradeAssetsRequest(BaseModel):
    preset_id: Literal["vernal-core","vernal-lite"] | None = None
    reason: str = Field(default="", max_length=500)


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


def _engine(request, *, write=False, continuing=False, roles=("admin", "architect", "developer")):
    eng=get_ctx(request).active_slot(request).engine
    if write:
        from autodev.governance import check_role_permission
        try:
            check_role_permission(eng.db,effective_actor(),roles,
                                  project=eng.settings.loopforge_project_id or eng.ws.root.name)
        except PermissionError as exc:
            raise HTTPException(403,detail=str(exc)) from exc
        if not continuing and eng.db._query(
            "SELECT 1 FROM dispatch_records WHERE stage IN ('lite_bundle','std_spec','std_plan') "
            "AND state IN ('DISPATCHING','QUEUED','RUNNING','NEEDS_CONFIRMATION') LIMIT 1"
        ):
            raise HTTPException(409,detail="需求流程正在执行或待恢复，请先核对当前执行记录")
    return eng


@router.get("/vernal-api/specifications")
def state(request: Request):
    eng=_engine(request)
    intake = [missions.view(eng,m) for m in missions.all_missions(eng)]
    if not service.enabled(eng):
        return {"enabled":False,"legacy":eng.ws.prd_json_path.is_file(),"features":[],"missions":intake,
                "running":get_ctx(request).is_planning(get_ctx(request).active_slot(request))}
    try:
        lock,_=load_assets(eng)
        specs=service.current_specs(eng)
        features=[]
        for spec in specs:
            feature=spec["feature_id"]
            features.append({"feature_id":feature,"sources":{k:service.latest(eng,feature,k) for k in ("spec","plan","tasks","checklist","analyze")},
                             "clarifications":service.list_clarifications(eng,feature),
                             "quality_review":service.quality_review(eng,feature),
                             "last_step":json.loads(eng.db.meta_get(f"speckit.last_step:{feature}") or "null")})
        return {"enabled":True,"assets":lock,"features":features,"missions":intake,
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


@router.post("/vernal-api/specifications/upgrade-assets")
def upgrade_assets_route(req: UpgradeAssetsRequest, request: Request):
    try:
        _idle(request)
        return assets.upgrade_assets(_engine(request, write=True, roles=("admin", "architect")), req.preset_id, actor=effective_actor(), reason=req.reason)
    except (ValueError, OSError) as exc:
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
    mission = missions.get(eng, req.feature_id)
    if mission and req.step in ("checklist", "plan", "tasks", "analyze"):
        if req.instructions.strip():
            raise HTTPException(409, detail="请先将补充要求保存为目标修订，再继续组合规划")
        try:
            return await _continue(request, req.feature_id, mission["revision"])
        except (ValueError, OSError) as exc:
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


class MissionRequest(BaseModel):
    feature_id: str
    brief: str = Field(min_length=1,max_length=50000)
    intent: Literal["new_system","feature","bugfix","refactor","documentation"] = "feature"
    expected_revision: int = Field(default=0,ge=0)


class RevisionGuard(BaseModel):
    expected_revision: int = Field(ge=1)


class ContinueRequest(RevisionGuard):
    retry_failed: bool = False


class MissionAnswer(BaseModel):
    clarify_id: int
    answer: str = Field(min_length=1,max_length=10000)
    supplement: str = Field(default="",max_length=10000)


class MissionAnswers(BaseModel):
    expected_rev_id: int = Field(ge=1)
    answers: list[MissionAnswer] = Field(min_length=1,max_length=3)


class PrerequisiteEvidence(RevisionGuard):
    key: str
    evidence: str = Field(min_length=1,max_length=10000)


class ConvertProfileRequest(RevisionGuard):
    target_preset: Literal["vernal-core", "vernal-lite"]
    target_process: Literal["standard", "quick-prototype", "feature", "bugfix"]
    reason: str = Field(default="", max_length=500)


def _idle(request):
    ctx=get_ctx(request);slot=ctx.active_slot(request)
    if ctx.is_planning(slot) or ctx.is_scheduler_running(slot):
        raise service.SpecConflictError("项目已有规划或执行任务，请结束后继续")
    return ctx,slot


def start_lite_delivery(ctx, slot, task_ids, *, bound=None):
    from autodev.scheduler import Scheduler
    from autodev.speckit.adapter import deliver_lite_package
    if len(task_ids) != 1 or ctx.is_scheduler_running(slot):
        return False
    task = slot.engine.db.get_task(task_ids[0])
    if not task or not task.feature_id:
        return False
    mission = missions.get(slot.engine, task.feature_id)
    if not mission or not mission.get("lite_revisions") or sum(
            t.feature_id == task.feature_id for t in slot.engine.db.list_tasks()) != 1:
        return False
    slot.scheduler = Scheduler(slot.engine)
    slot.scheduler.stop_event = slot.engine.planning_stop
    slot.scheduler.stop_event.clear()
    slot.scheduler_task = ctx.start_background(slot, "_lite_delivery", deliver_lite_package(slot.scheduler, task.task_id, bound=bound))
    return True


async def _continue(request,feature,revision, *, retry_failed=False):
    eng=_engine(request,write=True,continuing=True)
    ctx = get_ctx(request)
    slot = ctx.active_slot(request)
    if (slot.plan_task and not slot.plan_task.done()) or ctx.is_scheduler_running(slot):
        raise service.SpecConflictError("项目已有规划或执行任务")
    mission = missions.get(eng, feature)
    if not retry_failed and mission and mission["revision"] == revision and mission.get("lite_revisions"):
        frozen = [t.task_id for t in eng.db.list_tasks() if t.feature_id == feature]
        if len(frozen) == 1:
            bound = asyncio.get_running_loop().create_future()
            if start_lite_delivery(ctx, slot, frozen, bound=bound):
                await asyncio.wait((bound, slot.scheduler_task), return_when=asyncio.FIRST_COMPLETED)
                if bound.done():
                    return {"started": True, "run_id": bound.result()}
                await slot.scheduler_task
                task = eng.db.get_task(frozen[0])
                return {"started": False, "run_id": task.loopforge_run_id, "status": task.status}
    check = await preflight.inspect(eng)
    if not check["ready"]:
        return {"started": False, "preflight": check}
    result = await reconciler.start(eng, feature, revision, actor=effective_actor(), retry_failed=retry_failed)
    return {**result, "preflight": check}


@router.get("/vernal-api/missions/preflight")
async def mission_preflight(request: Request):
    return await preflight.inspect(_engine(request))


@router.post("/vernal-api/missions")
def mission_save(req: MissionRequest,request: Request):
    try:
        _idle(request)
        return missions.save(_engine(request,write=True),req.feature_id,req.brief,req.intent,req.expected_revision,actor=effective_actor())
    except (ValueError,OSError) as exc:
        raise _error(exc) from exc


@router.post("/vernal-api/missions/{feature}/continue")
async def mission_continue(feature: str,req: ContinueRequest,request: Request):
    try:
        return await _continue(request, feature, req.expected_revision, retry_failed=req.retry_failed)
    except (ValueError,OSError) as exc:
        raise _error(exc) from exc


@router.get("/vernal-api/missions/{feature}/stream")
async def mission_stream(feature: str, request: Request, run_id: str = "", cursor_run_id: str = "", after: int = Query(default=0, ge=0)):
    from autodev.speckit import execution
    from autodev.server.routers.tasks import _parse_loopforge_node_streams
    eng = _engine(request)
    if not missions.get(eng, feature):
        raise HTTPException(status_code=404, detail="研发目标不存在")
    runs = execution.history(eng, feature)
    selected = next((r for r in runs if r["run_id"] == run_id), None) if run_id else next(iter(runs), None)
    if run_id and not selected:
        raise HTTPException(status_code=404, detail="执行记录不属于当前需求")
    result = {"runs": runs, "selected": selected, "nodes": [], "after": after, "has_more": False}
    if not selected:
        return result
    ctx = get_ctx(request)
    client = ctx.lf_client(ctx.active_slot(request))
    rid = selected["run_id"]
    if cursor_run_id != rid:
        after = 0
    result["after"] = after
    try:
        run = await asyncio.wait_for(client.project_run(rid, eng.settings.loopforge_project_id), timeout=4)
        logs = await asyncio.wait_for(client.get_run_logs(rid, after=after, limit=200), timeout=4)
        nodes = _parse_loopforge_node_streams(run, logs)
        for node in nodes:
            native = (run.get("nodes") or {}).get(node["id"], {})
            node["attempt"] = native.get("attempt")
            node["rework_count"] = native.get("reworkCount")
        items = [item for node in nodes for item in node["items"]]
        result.update(nodes=nodes, run_status=run.get("status", ""), timing=run.get("timing") or {},
                      after=max([after] + [item["sequence"] for item in items] +
                                [int(item.get("sequence") or 0) for item in logs.get("logs", [])]) if isinstance(logs, dict) else after,
                      has_more=bool(logs.get("hasMore")) if isinstance(logs, dict) else False)
        return result
    except (LoopForgeError, TimeoutError) as exc:
        return {**result, "error": str(exc), "connection": "unavailable"}


@router.post("/vernal-api/missions/{feature}/answers")
async def mission_answers(feature: str,req: MissionAnswers,request: Request):
    eng=_engine(request,write=True)
    try:
        _idle(request); missions.assert_idle(eng)
        mission=missions.get(eng,feature)
        if not mission:
            raise ValueError("研发目标不存在")
        ids={q["clarify_id"] for q in service.list_clarifications(eng,feature)}
        if len({a.clarify_id for a in req.answers})!=len(req.answers) or any(a.clarify_id not in ids for a in req.answers):
            raise ValueError("回答必须属于当前功能，且不能重复提交同一问题")
        with eng.db.transaction():
            from difflib import unified_diff
            before=(service.clarification_source(eng,feature) or {}).get("content_text", "")
            rev=service.answer_batch(eng,feature,[a.model_dump() for a in req.answers],expected_rev_id=req.expected_rev_id,actor=effective_actor())
            revision=rev["rev_id"]
        remaining=any(q["status"]=="OPEN" and q["severity"]=="BLOCKING" for q in service.list_clarifications(eng,feature))
        try:
            result={"started":False} if remaining else await _continue(request,feature,mission["revision"])
        except Exception as exc:
            result={"started":False,"continuation_error":str(exc)}
        diff="".join(unified_diff(before.splitlines(keepends=True),rev["content_text"].splitlines(keepends=True),fromfile="回答前",tofile="回答后"))
        return {**result,"saved":True,"spec_rev_id":revision,"content":rev["content_text"],"diff":diff}
    except (ValueError,OSError) as exc:
        raise _error(exc) from exc


@router.post("/vernal-api/missions/{feature}/prerequisites")
def mission_prerequisite(feature: str,req: PrerequisiteEvidence,request: Request):
    try:
        _idle(request)
        return missions.prerequisite_evidence(_engine(request,write=True),feature,req.key,req.evidence,req.expected_revision,actor=effective_actor())
    except (ValueError,OSError) as exc:
        raise _error(exc) from exc


@router.post("/vernal-api/missions/{feature}/convert-profile")
def mission_convert_profile(feature: str, req: ConvertProfileRequest, request: Request):
    try:
        _idle(request)
        return missions.convert_profile(
            _engine(request, write=True, roles=("admin", "architect")),
            feature,
            target_preset=req.target_preset,
            target_process=req.target_process,
            expected_revision=req.expected_revision,
            actor=effective_actor(),
            reason=req.reason,
        )
    except (ValueError, OSError) as exc:
        raise _error(exc) from exc


@router.post("/vernal-api/missions/recover")
async def mission_recover(request: Request):
    try:
        ctx = get_ctx(request)
        slot = ctx.active_slot(request)
        eng=_engine(request)
        from autodev.governance import check_role_permission
        check_role_permission(eng.db,effective_actor(),("admin", "architect", "developer"),
                              project=eng.settings.loopforge_project_id or eng.ws.root.name)
        await reconciler.reconcile_once(ctx, slot)
        return {"recovered":True}
    except PermissionError as exc:
        raise HTTPException(403,detail=str(exc)) from exc
    except (ValueError,OSError) as exc:
        raise _error(exc) from exc
