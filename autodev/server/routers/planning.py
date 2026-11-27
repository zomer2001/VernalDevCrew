"""项目全局状态、资料摄入、立项规划、架构圆桌与调度控制路由。"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from autodev import gitops
from autodev.council import interject_council, run_council
from autodev.engine import OpsError, PlanningError
from autodev.scheduler import Scheduler
from autodev.server.deps import effective_actor, get_ctx
from autodev.server.project_launcher import host_projects_root
from autodev.server.schemas import (
    ActorRequest,
    CouncilInterjectRequest,
    EditPlanTaskRequest,
    PlanRequest,
    WishRequest,
)
from autodev.steering import current_rule_version, reconcile_steering_checkpoint

router = APIRouter()


def _read_git_meta_sync(ws_root) -> tuple[str, str]:
    if not (ws_root / ".git").exists():
        return "", ""
    branch = ""
    remote = ""
    try:
        branch = gitops.current_branch(ws_root)
        proc_rem = gitops.git(["remote", "get-url", "origin"], ws_root, check=False)
        if proc_rem.returncode == 0:
            remote = proc_rem.stdout.strip()
    except Exception:
        pass
    return branch, remote


@router.get("/vernal-api/state")
async def get_state(request: Request) -> dict[str, Any]:
    ctx = get_ctx(request)
    slot = ctx.active_slot(request)
    eng = slot.engine
    active_cps = [
        cp for cp in eng.db.list_checkpoints() if cp.get("status") in ("RUNNING", "AWAITING_APPROVAL", "SYNC_PENDING")
    ]
    now_m = time.monotonic()
    if active_cps and (now_m - slot.steering_last_sync >= 2.5):
        slot.steering_last_sync = now_m
        for cp in active_cps:
            cid = int(cp["checkpoint_id"])
            updated = await reconcile_steering_checkpoint(eng, cid)
            if updated and updated.get("status") in ("RUNNING", "AWAITING_APPROVAL", "SYNC_PENDING"):
                ctx.ensure_steering_poller(slot, cid)
    snap = eng.snapshot()
    snap["scheduler_running"] = ctx.is_scheduler_running(slot)
    snap["planning"] = ctx.is_planning(slot)
    from autodev.speckit.missions import all_missions
    snap["mission_count"] = len(all_missions(eng))
    snap["recent_events"] = eng.db.events_after(max(0, eng.db.last_event_id() - 120))
    snap["decisions"] = eng.db.list_decisions()
    snap["checkpoints"] = eng.db.list_checkpoints()
    snap["decisions_rule_version"] = current_rule_version(eng)
    cookie = request.headers.get("cookie", "")
    runtimes, runner_summary = await ctx.fetch_unified_runtimes(eng, cookie=cookie)
    current_user = await ctx.try_resolve_user(cookie, eng.settings.loopforge_url) if cookie else ""
    snap["current_user"] = current_user
    snap["project_key"] = slot.project_key
    snap["runtimes"] = runtimes
    snap["runner_summary"] = runner_summary
    snap["workspace_root"] = str(eng.ws.root)
    snap["host_dirs_available"] = host_projects_root().is_dir()
    snap["source_mode"] = eng.db.meta_get("source_mode") or (
        "git" if (eng.ws.root / ".git").exists() else "scratch"
    )
    branch, remote = await asyncio.to_thread(_read_git_meta_sync, eng.ws.root)
    snap["git_branch"] = branch
    snap["git_remote"] = remote
    return snap


@router.get("/vernal-api/slices")
def get_slices(request: Request) -> list[dict[str, Any]]:
    eng = get_ctx(request).active_slot(request).engine
    return [
        {
            "slice_id": s.slice_id,
            "source_file": s.source_file,
            "heading_path": s.heading_path,
            "summary": s.summary,
            "char_count": s.char_count,
            "content": s.content,
        }
        for s in eng.db.list_slices()
    ]


@router.get("/vernal-api/plan")
def get_plan(request: Request) -> dict[str, Any]:
    eng = get_ctx(request).active_slot(request).engine
    if eng.db.meta_get("speckit.enabled")=="1":
        from autodev.speckit.service import current_specs,latest,product_spec
        plans=[latest(eng,r["feature_id"],"plan") for r in current_specs(eng)]
        return {"plan":{"summary":eng.db.meta_get("plan_summary", ""),"tasks":[t.to_dict() for t in eng.db.list_tasks() if t.feature_id],
                        "milestones":[m.to_dict() for m in eng.db.list_milestones()],"modules":[]},
                "blueprint":None,"prd":product_spec(eng).model_dump(),"squads":eng.db.list_squads(),
                "markdown":"\n\n".join(r["content_text"] for r in plans if r)}

    def _load_json(p):
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text("utf-8"))
        except json.JSONDecodeError:
            return None

    return {
        "plan": _load_json(eng.ws.plan_json_path),
        "blueprint": _load_json(eng.ws.blueprint_path),
        "prd": _load_json(eng.ws.prd_json_path),
        "squads": eng.db.list_squads(),
        "markdown": eng.ws.plan_md_path.read_text("utf-8") if eng.ws.plan_md_path.exists() else "",
    }


@router.post("/vernal-api/ingest")
def api_ingest(request: Request) -> dict[str, Any]:
    eng = get_ctx(request).active_slot(request).engine
    try:
        count = eng.ingest()
    except OpsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"slices": count}


@router.post("/vernal-api/plan")
async def api_plan(request: Request, req: PlanRequest) -> dict[str, Any]:
    ctx = get_ctx(request)
    slot = ctx.active_slot(request)
    eng = slot.engine
    if ctx.is_planning(slot):
        raise HTTPException(status_code=409, detail="规划任务已在后台运行")
    if ctx.is_scheduler_running(slot):
        raise HTTPException(status_code=409, detail="调度器正在运行，请先停止")

    async def _run_plan() -> None:
        try:
            if not eng.db.list_slices():
                try:
                    eng.ingest()
                except OpsError:
                    pass
            await eng.plan(extra_instructions=req.extra_instructions, mode=req.mode)
        except (OpsError, PlanningError, Exception) as exc:
            eng.db.add_event("plan.failed", f"规划失败：{exc}")

    slot.plan_task = ctx.start_background(slot, "_run_plan", _run_plan())
    return {"started": True}


@router.post("/vernal-api/plan/approve")
def api_approve_plan(request: Request, req: ActorRequest) -> dict[str, Any]:
    eng = get_ctx(request).active_slot(request).engine
    try:
        eng.approve_plan(actor=effective_actor(req.actor))
    except OpsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": eng.status.value}


@router.patch("/vernal-api/tasks/{task_id}/plan")
def api_edit_task_plan(task_id: str, request: Request, req: EditPlanTaskRequest) -> dict[str, Any]:
    eng = get_ctx(request).active_slot(request).engine
    try:
        eng.update_task_plan(task_id, actor=effective_actor(req.actor), priority=req.priority, depends_on=req.depends_on)
    except OpsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


def start_slot_scheduler(ctx, slot, *, watch: bool = True) -> bool:
    if ctx.is_scheduler_running(slot):
        return False
    eng = slot.engine
    from .speckit import start_lite_delivery
    active = [t.task_id for t in eng.db.list_tasks() if t.status not in ("MERGED", "CANCELLED")]
    if start_lite_delivery(ctx, slot, active):
        return True
    sched = Scheduler(eng)
    sched.check_runtime()
    slot.scheduler = sched

    async def _run_sched() -> None:
        try:
            await sched.run(watch=watch)
        except Exception as exc:
            eng.db.add_event("scheduler.error", f"调度器异常：{exc}")

    slot.scheduler_task = ctx.start_background(slot, "_run_sched", _run_sched())
    return True


@router.post("/vernal-api/scheduler/start")
async def api_scheduler_start(request: Request, watch: bool = True) -> dict[str, Any]:
    ctx = get_ctx(request)
    slot = ctx.active_slot(request)
    try:
        start_slot_scheduler(ctx, slot, watch=watch)
    except OpsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"running": True}


@router.post("/vernal-api/scheduler/stop")
async def api_scheduler_stop(request: Request, pause_remote: bool = False) -> dict[str, Any]:
    slot = get_ctx(request).active_slot(request)
    slot.engine.planning_stop.set()
    if slot.scheduler is not None:
        slot.scheduler.request_stop()
    paused = []
    if pause_remote:
        from autodev.server.routers.runtime import project_run
        from autodev.loopforge.client import LoopForgeError
        ctx = get_ctx(request)
        for row in slot.engine.db._query("SELECT DISTINCT lf_run_id FROM dispatch_records WHERE state='RUNNING'"):
            run_id = row["lf_run_id"]
            await project_run(ctx, slot, run_id)
            try:
                await ctx.lf_client(slot).pause_run(run_id)
                confirmed = await project_run(ctx, slot, run_id)
                if confirmed.get("status") != "paused":
                    raise HTTPException(409, "远端仍在处理暂停请求，保留原派单记录")
            except LoopForgeError as exc:
                raise HTTPException(exc.status_code or 503, str(exc)) from exc
            slot.engine.db._exec("UPDATE dispatch_records SET state='PAUSED' WHERE lf_run_id=? AND state='RUNNING'", (run_id,))
            paused.append(run_id)
    return {"stopping": True, "paused_runs": paused, "observation_only": not pause_remote}


@router.get("/vernal-api/council")
def api_council_get(request: Request) -> dict[str, Any]:
    ctx = get_ctx(request)
    slot = ctx.active_slot(request)
    msgs = slot.engine.db.list_council_messages()
    consensus = next((m for m in reversed(msgs) if m.get("kind") == "consensus"), None)
    return {
        "messages": msgs,
        "consensus": consensus["body"] if consensus else None,
        "running": ctx.is_planning(slot),
    }


@router.post("/vernal-api/council/run")
async def api_council_run(request: Request, req: PlanRequest) -> dict[str, Any]:
    ctx = get_ctx(request)
    slot = ctx.active_slot(request)
    eng = slot.engine
    if ctx.is_planning(slot):
        raise HTTPException(status_code=409, detail="规划/圆桌会议已在运行中")
    if ctx.is_scheduler_running(slot):
        raise HTTPException(status_code=409, detail="调度器正在运行，请先停止")

    async def _run() -> None:
        try:
            await run_council(eng, extra_instructions=req.extra_instructions)
            if eng.db.list_milestones() and eng.db.meta_get("planning_mode") == "goal":
                progressed = [
                    t for t in eng.db.list_tasks() if t.status.value not in ("BLOCKED", "READY", "CANCELLED")
                ]
                if not progressed:
                    await eng.plan_blueprint(extra_instructions=req.extra_instructions)
        except Exception as exc:
            eng.db.add_event("council.failed", f"架构评审圆桌失败：{exc}", task_id="_council")

    slot.plan_task = ctx.start_background(slot, "_run", _run())
    return {"started": True}


@router.post("/vernal-api/council/interject")
async def api_council_interject(request: Request, req: CouncilInterjectRequest) -> dict[str, Any]:
    ctx = get_ctx(request)
    slot = ctx.active_slot(request)
    eng = slot.engine
    if ctx.is_planning(slot):
        raise HTTPException(status_code=409, detail="圆桌会议正在收敛，请稍候")
    if not req.note.strip():
        raise HTTPException(status_code=400, detail="插话内容不能为空")
    actor = effective_actor(req.actor)

    async def _run() -> None:
        try:
            await interject_council(eng, actor=actor, note=req.note, refresh_blueprint=req.refresh_blueprint)
        except Exception as exc:
            eng.db.add_event("council.failed", f"圆桌补充裁决失败：{exc}", task_id="_council")

    slot.plan_task = ctx.start_background(slot, "_run", _run())
    return {"started": True}


@router.post("/vernal-api/wish")
def api_wish(request: Request, req: WishRequest) -> dict[str, Any]:
    eng = get_ctx(request).active_slot(request).engine
    text = req.content.strip()
    if not text:
        raise HTTPException(status_code=400, detail="需求内容不能为空")
    slug = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "-", req.title.strip() or "requirement").strip("-")[:48] or "requirement"
    eng.ws.inputs_dir.mkdir(parents=True, exist_ok=True)
    path = eng.ws.inputs_dir / f"{slug}.md"
    path.write_text(text + "\n", "utf-8")
    try:
        count = eng.ingest()
    except OpsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    eng.db.add_event("wish.created", f"收到需求文档《{req.title}》，已切分 {count} 个资料切片", actor=effective_actor())
    return {"path": str(path), "slices": count}


@router.get("/vernal-api/wish")
def api_wish_list(request: Request) -> dict[str, Any]:
    eng = get_ctx(request).active_slot(request).engine
    items = []
    if eng.ws.inputs_dir.exists():
        for p in sorted(eng.ws.inputs_dir.glob("*.md")):
            items.append({"name": p.stem, "content": p.read_text("utf-8", errors="replace")[:20000]})
    return {"items": items}
