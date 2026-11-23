"""FastAPI REST API + SSE 实时事件推送看板。"""

from __future__ import annotations

import asyncio
import difflib
import io
import json
import re
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from autodev import gitops
from autodev.artifacts import FileExchange
from autodev.config import Settings, Workspace
from autodev.council import interject_council, run_council
from autodev.engine import Engine, OpsError, PlanningError, init_workspace
from autodev.harnesses.base import extract_json
from autodev.harnesses.codex_cli import parse_events_jsonl
from autodev.loopforge.bootstrap import ensure_vernal_loopforge_assets
from autodev.loopforge.bridge import LoopForgeBridge
from autodev.loopforge.client import LoopForgeClient, LoopForgeError
from autodev.scheduler import Scheduler
from autodev.server.history import event_page, history_sources
from autodev.server.proxy import LoopForgeProxy

STATIC_DIR = Path(__file__).parent / "static"


class PlanRequest(BaseModel):
    extra_instructions: str = ""
    mode: str | None = None


class CouncilInterjectRequest(BaseModel):
    actor: str = "owner"
    note: str = ""


class RuntimeConfigRequest(BaseModel):
    collaboration_mode: str | None = None
    runtime_backend: str | None = None
    loopforge_url: str | None = None
    loopforge_token: str | None = None
    loopforge_project_id: str | None = None
    loopforge_workflow_id: str | None = None
    loopforge_repository_id: str | None = None


class AgentConfigRequest(BaseModel):
    harness: str | None = None
    director_harness: str | None = None
    coder_harness: str | None = None
    qa_harness: str | None = None
    model: str | None = None
    director_model: str | None = None
    coder_model: str | None = None
    qa_model: str | None = None
    max_concurrency: int | None = None


class RunApproveRequest(BaseModel):
    comment: str = ""
    node_id: str = ""


class RunRejectRequest(BaseModel):
    reason: str = ""
    target_node: str = ""
    node_id: str = ""


class WishRequest(BaseModel):
    title: str = "wish"
    content: str = ""


class ProjectSelectRequest(BaseModel):
    project_id: str
    project_name: str = ""


class ActorRequest(BaseModel):
    actor: str = "owner"


class SteerRequest(BaseModel):
    actor: str = "owner"
    note: str = ""
    verify_only: bool = False


class EditPlanTaskRequest(BaseModel):
    actor: str = "owner"
    priority: int | None = None
    depends_on: list[str] | None = None


def create_app(engine: Engine, proxy_transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    subscribers: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = set()
    state: dict[str, Any] = {
        "scheduler": None,
        "scheduler_task": None,
        "plan_task": None,
    }

    def on_event(ev: dict[str, Any]) -> None:
        for loop, wake in list(subscribers):
            if not loop.is_closed():
                loop.call_soon_threadsafe(wake.set)

    engine.db.listeners.append(on_event)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            sched: Scheduler | None = state["scheduler"]
            if sched is not None:
                sched.request_stop()
            for key in ("scheduler_task", "plan_task"):
                t = state[key]
                if t is not None and not t.done():
                    t.cancel()
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass
            if on_event in engine.db.listeners:
                engine.db.listeners.remove(on_event)
            px = state.get("proxy")
            if px is not None:
                await px.close()

    app = FastAPI(title="Vernal", version="0.2.0", lifespan=lifespan, docs_url="/vernal-api/docs", openapi_url="/vernal-api/openapi.json", redoc_url=None)
    # Vernal 自有静态资源与旧版独立看板（仅调试用），与 LoopForge 路径零冲突
    app.mount("/vernal-static", StaticFiles(directory=str(STATIC_DIR)), name="vernal-static")
    proxy = LoopForgeProxy(engine.settings.loopforge_url, transport=proxy_transport)
    state["proxy"] = proxy

    def _is_scheduler_running() -> bool:
        t = state["scheduler_task"]
        return t is not None and not t.done()

    def _is_planning() -> bool:
        t = state["plan_task"]
        return t is not None and not t.done()

    @app.get("/vernal-api/state")
    def get_state() -> dict[str, Any]:
        snap = engine.snapshot()
        snap["scheduler_running"] = _is_scheduler_running()
        snap["planning"] = _is_planning()
        snap["recent_events"] = engine.db.events_after(max(0, engine.db.last_event_id() - 120))
        return snap

    @app.get("/vernal-api/slices")
    def get_slices() -> list[dict[str, Any]]:
        return [
            {
                "slice_id": s.slice_id,
                "source_file": s.source_file,
                "heading_path": s.heading_path,
                "summary": s.summary,
                "char_count": s.char_count,
                "content": s.content,
            }
            for s in engine.db.list_slices()
        ]

    @app.get("/vernal-api/plan")
    def get_plan() -> dict[str, Any]:
        plan_data = None
        if engine.ws.plan_json_path.exists():
            try:
                plan_data = json.loads(engine.ws.plan_json_path.read_text("utf-8"))
            except json.JSONDecodeError:
                plan_data = None
        blueprint = None
        if engine.ws.blueprint_path.exists():
            try:
                blueprint = json.loads(engine.ws.blueprint_path.read_text("utf-8"))
            except json.JSONDecodeError:
                blueprint = None
        prd = None
        if engine.ws.prd_json_path.exists():
            try:
                prd = json.loads(engine.ws.prd_json_path.read_text("utf-8"))
            except json.JSONDecodeError:
                prd = None
        plan_md = engine.ws.plan_md_path.read_text("utf-8") if engine.ws.plan_md_path.exists() else ""
        return {
            "plan": plan_data,
            "blueprint": blueprint,
            "prd": prd,
            "squads": engine.db.list_squads(),
            "markdown": plan_md,
        }

    @app.post("/vernal-api/ingest")
    def api_ingest() -> dict[str, Any]:
        try:
            count = engine.ingest()
        except OpsError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"slices": count}

    @app.post("/vernal-api/plan")
    async def api_plan(req: PlanRequest) -> dict[str, Any]:
        if _is_planning():
            raise HTTPException(status_code=409, detail="规划任务已在后台运行")
        if _is_scheduler_running():
            raise HTTPException(status_code=409, detail="调度器正在运行，请先停止")

        async def _run_plan() -> None:
            try:
                await engine.plan(extra_instructions=req.extra_instructions, mode=req.mode)
            except (OpsError, PlanningError, Exception) as exc:
                engine.db.add_event("plan.failed", f"规划失败：{exc}")

        state["plan_task"] = asyncio.create_task(_run_plan())
        return {"started": True}

    @app.post("/vernal-api/plan/approve")
    def api_approve_plan(req: ActorRequest) -> dict[str, Any]:
        try:
            engine.approve_plan(actor=req.actor)
        except OpsError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"status": engine.status.value}

    @app.patch("/vernal-api/tasks/{task_id}/plan")
    def api_edit_task_plan(task_id: str, req: EditPlanTaskRequest) -> dict[str, Any]:
        try:
            engine.update_task_plan(task_id, actor=req.actor, priority=req.priority, depends_on=req.depends_on)
        except OpsError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True}

    def _planner_runs() -> list[dict[str, Any]]:
        runs = engine.db.list_runs("_planner")
        if runs:
            return runs
        # 兼容升级前已有 _planner 目录但未入 task_runs 的情况
        pdir = engine.ws.runs_dir / "_planner"
        if not pdir.exists():
            return []
        fallback = []
        for idx, sub in enumerate(sorted(pdir.glob("attempt-*")), 1):
            fallback.append(
                {
                    "run_id": -idx,
                    "task_id": "_planner",
                    "attempt_no": idx,
                    "stage": "PLAN",
                    "harness_name": engine.settings.harness,
                    "session_id": None,
                    "exit_code": 0 if (sub / "last_message.txt").exists() else None,
                    "input_tokens": 0,
                    "cached_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_tokens": 0,
                    "verdict": "DONE" if (sub / "last_message.txt").exists() else "RUNNING",
                    "summary_log": "",
                    "log_path": str(sub),
                    "started_at": "",
                    "finished_at": "",
                }
            )
        return fallback

    def _resolve_log_path(task_id: str, run: dict[str, Any]) -> Path | None:
        if run.get("log_path"):
            p = Path(run["log_path"])
            if p.exists():
                return p
        stage = (run.get("stage") or "").lower()
        attempt = run.get("attempt_no", 1)
        if task_id == "_planner":
            cand = engine.ws.runs_dir / "_planner" / f"attempt-{attempt}"
            return cand if cand.exists() else None
        if stage == "verify":
            cand = engine.ws.runs_dir / task_id / f"attempt-{attempt}-verify.log"
            return cand if cand.exists() else None
        cand = engine.ws.runs_dir / task_id / f"attempt-{attempt}-{stage}"
        return cand if cand.exists() else None

    def _read_review_verdict(log_path: Path | None) -> dict[str, Any] | None:
        if log_path is None or not log_path.is_dir():
            return None
        vf = log_path / "verdict.json"
        if vf.exists():
            try:
                return json.loads(vf.read_text("utf-8"))
            except json.JSONDecodeError:
                pass
        lm = log_path / "last_message.txt"
        if lm.exists():
            parsed = extract_json(lm.read_text("utf-8", errors="replace"))
            if isinstance(parsed, dict) and "verdict" in parsed:
                return parsed
        return None

    def _enrich_run(task_id: str, run: dict[str, Any]) -> dict[str, Any]:
        out = dict(run)
        lp = _resolve_log_path(task_id, run)
        out["resolved_log_path"] = str(lp) if lp else None
        out["has_prompt"] = bool(lp and lp.is_dir() and (lp / "prompt.md").exists())
        out["has_events"] = bool(lp and lp.is_dir() and (lp / "events.jsonl").exists())
        out["has_verify_log"] = bool(lp and lp.is_file())
        if run.get("stage") == "REVIEW":
            out["review_verdict"] = _read_review_verdict(lp)
        elif run.get("stage") == "VERIFY" and lp and lp.is_file():
            out["verify_log"] = lp.read_text("utf-8", errors="replace")[:30_000]
        return out

    _SPECIAL_ROLES: dict[str, tuple[str, str, str]] = {
        "_pm": ("产品经理 · PM", "pm", "补全产品业务流程、异常边界与验收红线（docs/PRD_SPEC.md）"),
        "_planner": ("研发总监 · 立项蓝图", "director", "划分研发组领地、制定里程碑路线图与首轮任务 DAG"),
        "_director": ("研发总监 · 滚动排期与仲裁", "director", "结合主干真实代码滚动拆解下一里程碑，并在阻塞时自动仲裁"),
        "_council_pm": ("圆桌 · 产品经理 (PM)", "pm", "架构评审圆桌第 1 轮：业务边界、异常流与验收红线提案"),
        "_council_architect": ("圆桌 · 契约与异构架构师", "architect", "架构评审圆桌第 1 轮：跨语言契约、模块领地与异构沙箱提案"),
        "_council_qa_lead": ("圆桌 · QA 质量总监", "qa_lead", "架构评审圆桌第 1 轮：可测性审查、集成风险与门禁提案"),
        "_council_director": ("圆桌 · 研发总监裁决 (ADR)", "director", "架构评审圆桌第 2 轮：回应分歧并拍板 ADR 决策纪要"),
    }

    def _special_runs(task_id: str) -> list[dict[str, Any]]:
        runs = engine.db.list_runs(task_id)
        if runs:
            return runs
        if task_id == "_planner":
            return _planner_runs()
        return []

    @app.get("/vernal-api/tasks/{task_id}")
    def api_task_detail(task_id: str) -> dict[str, Any]:
        if task_id in _SPECIAL_ROLES:
            title, role_key, desc = _SPECIAL_ROLES[task_id]
            runs = [_enrich_run(task_id, r) for r in _special_runs(task_id)]
            return {
                "task": {
                    "task_id": task_id,
                    "milestone_id": "-",
                    "squad": "顶层指挥组",
                    "module": role_key,
                    "role": role_key,
                    "title": title,
                    "description": desc,
                    "acceptance_criteria": [],
                    "allowed_paths": ["（只读沙箱，不修改业务代码）"],
                    "verify_cmds": [],
                    "status": "RUNNING" if (_is_planning() and task_id in ("_pm", "_planner")) else engine.status.value,
                    "branch_name": None,
                    "worktree_path": None,
                    "failure_reason": None,
                },
                "runs": runs,
                "events": engine.db.events_after(0, limit=400, task_id=task_id)[-150:],
                "diff_stat": "",
            }

        task = engine.db.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
        diffstat = ""
        if task.worktree_path and task.base_sha and Path(task.worktree_path).exists():
            try:
                diffstat = gitops.working_diff_stat(Path(task.worktree_path), task.base_sha)
            except Exception:
                diffstat = ""
        elif task.base_sha and task.candidate_commit_sha:
            try:
                diffstat = gitops.git(["diff", "--stat", f"{task.base_sha}..{task.candidate_commit_sha}"], engine.ws.root, check=False).stdout.strip()
            except Exception:
                diffstat = ""
        d = task.to_dict()
        d["status"] = task.status.value
        runs = [_enrich_run(task_id, r) for r in engine.db.list_runs(task_id)]
        return {
            "task": d,
            "runs": runs,
            "events": engine.db.events_after(0, limit=400, task_id=task_id)[-150:],
            "diff_stat": diffstat,
        }

    def _format_file_patch_diff(patch: dict[str, Any]) -> tuple[str, str]:
        stats: list[str] = []
        blocks: list[str] = []
        for path in sorted(patch):
            old_val, new_val = patch[path]
            old_lines = (old_val or "").splitlines(keepends=True)
            new_lines = (new_val or "").splitlines(keepends=True)
            tag = "A" if old_val is None else ("D" if new_val is None else "M")
            stats.append(f"{tag}\t{path}")
            blocks.append(f"diff --git a/{path} b/{path}")
            blocks.extend(
                ln.rstrip("\n")
                for ln in difflib.unified_diff(
                    old_lines,
                    new_lines,
                    fromfile=f"a/{path}" if old_val is not None else "/dev/null",
                    tofile=f"b/{path}" if new_val is not None else "/dev/null",
                )
            )
        return "\n".join(blocks), "\n".join(stats)

    @app.get("/vernal-api/tasks/{task_id}/trace")
    async def api_task_trace(task_id: str, run_id: int | None = Query(default=None)) -> dict[str, Any]:
        runs = _special_runs(task_id) if task_id in _SPECIAL_ROLES else engine.db.list_runs(task_id)
        task = None if task_id in _SPECIAL_ROLES else engine.db.get_task(task_id)
        if task_id not in _SPECIAL_ROLES and task is None:
            raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
        selected = None
        if run_id is not None:
            selected = next((r for r in runs if r["run_id"] == run_id), None)
        else:
            for r in reversed(runs):
                lp = _resolve_log_path(task_id, r)
                if lp and lp.is_dir() and (lp / "events.jsonl").exists():
                    selected = r
                    break
        if selected is not None:
            lp = _resolve_log_path(task_id, selected)
            steps = parse_events_jsonl(lp / "events.jsonl") if (lp and lp.is_dir()) else []
            return {"run": selected, "steps": steps}

        # 远端 LoopForge SOP 运行实况转换（同时支持编码任务与圆桌/规划任务）
        lf_run_id = task.loopforge_run_id if task is not None else ""
        if not lf_run_id and runs:
            latest_r = runs[-1]
            lp = _resolve_log_path(task_id, latest_r)
            if lp and lp.is_dir() and (lp / "loopforge_run.json").is_file():
                try:
                    lf_run_id = str(json.loads((lp / "loopforge_run.json").read_text("utf-8")).get("run_id") or "")
                except Exception:
                    lf_run_id = ""
            if not lf_run_id and latest_r.get("session_id"):
                lf_run_id = str(latest_r["session_id"])

        if lf_run_id and engine.settings.effective_loopforge_token():
            steps: list[dict[str, Any]] = []
            try:
                lf_run = await _lf_client().get_run(lf_run_id)
                steps.append(
                    {
                        "kind": "turn",
                        "summary": f"LoopForge SOP Run {lf_run_id} · 状态 {lf_run.get('status', '-')}",
                    }
                )
                for nid, nstate in (lf_run.get("nodes") or {}).items():
                    if not isinstance(nstate, dict):
                        continue
                    nst = nstate.get("status") or "pending"
                    steps.append(
                        {
                            "kind": "command",
                            "phase": "started" if nst in ("running", "waiting") else "done",
                            "command": f"[SOP 节点] {nid} ({nst})",
                            "exit_code": 0 if nst == "succeeded" else (1 if nst == "failed" else None),
                            "output": str(nstate.get("output") or nstate.get("error") or ""),
                        }
                    )
                logs = await _lf_client().get_run_logs(lf_run_id)
                log_items = logs.get("logs") if isinstance(logs, dict) and isinstance(logs.get("logs"), list) else logs
                if isinstance(log_items, list) and log_items:
                    tail = "\n".join(
                        f"[{(it.get('context') or {}).get('nodeId') or it.get('nodeId') or it.get('stream') or 'run'}] {it.get('body') or it.get('message') or it.get('line') or ''}"
                        if isinstance(it, dict)
                        else str(it)
                        for it in log_items[-80:]
                    )
                    steps.append({"kind": "message", "text": tail})
                elif isinstance(logs, dict) and logs.get("text"):
                    steps.append({"kind": "message", "text": str(logs["text"])[-12000:]})
            except LoopForgeError as exc:
                steps.append({"kind": "warning", "text": f"读取 LoopForge 实时日志失败：{exc}"})
            return {
                "run": {
                    "run_id": 0,
                    "attempt_no": max(1, (task.retry_count if task is not None else 0) + 1),
                    "stage": "LOOPFORGE_SOP",
                    "verdict": task.status.value if task is not None else "LOOPFORGE",
                    "loopforge_run_id": lf_run_id,
                },
                "steps": steps,
            }
        return {"run": None, "steps": []}

    @app.get("/vernal-api/tasks/{task_id}/baseline.json")
    def api_task_baseline(task_id: str) -> dict[str, str]:
        task = engine.db.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
        return FileExchange(engine.ws, engine.db).baseline(task.depends_on)

    @app.get("/vernal-api/plan-context/{stage_key}.json")
    def api_plan_context(stage_key: str) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9A-Za-z._\-]{1,64}", stage_key):
            raise HTTPException(status_code=400, detail="非法的规划上下文标识")
        p = engine.ws.state_dir / "baselines" / f"plan-{stage_key}.json"
        if not p.is_file():
            raise HTTPException(status_code=404, detail=f"规划上下文不存在: {stage_key}")
        try:
            return json.loads(p.read_text("utf-8"))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"读取规划上下文失败: {exc}") from exc

    @app.get("/vernal-api/tasks/{task_id}/diff")
    async def api_task_diff(task_id: str) -> dict[str, Any]:
        if task_id in _SPECIAL_ROLES:
            return {"diff": "", "diff_stat": "", "source": "readonly"}
        task = engine.db.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
        if task.artifact_digest:
            patch = FileExchange(engine.ws, engine.db).get(task.artifact_digest)
            diff_text, stat_text = _format_file_patch_diff(patch)
            return {"source": "files", "diff_stat": stat_text, "diff": diff_text}
        if task.loopforge_run_id and engine.settings.effective_loopforge_token():
            try:
                lf_diff = await _lf_client().get_run_diff(task.loopforge_run_id)
                if isinstance(lf_diff, dict):
                    raw_diff = str(lf_diff.get("diff") or lf_diff.get("patch") or lf_diff.get("text") or "")
                    stat = str(lf_diff.get("stat") or lf_diff.get("summary") or "")
                else:
                    raw_diff, stat = str(lf_diff or ""), ""
                if raw_diff:
                    return {"source": f"loopforge:{task.loopforge_run_id[:10]}", "diff_stat": stat, "diff": raw_diff}
            except LoopForgeError:
                pass
        if task.worktree_path and task.base_sha and Path(task.worktree_path).exists():
            wt = Path(task.worktree_path)
            return {
                "diff": gitops.working_diff(wt, task.base_sha),
                "diff_stat": gitops.working_diff_stat(wt, task.base_sha),
                "source": "worktree",
            }
        if task.base_sha and task.candidate_commit_sha:
            return {
                "diff": gitops.commit_diff(engine.ws.root, task.base_sha, task.candidate_commit_sha),
                "diff_stat": gitops.git(
                    ["diff", "--stat", f"{task.base_sha}..{task.candidate_commit_sha}"], engine.ws.root, check=False
                ).stdout.strip(),
                "source": "commit",
            }
        return {"diff": "", "diff_stat": "", "source": "none"}

    @app.get("/vernal-api/tasks/{task_id}/runs/{run_id}/artifacts")
    def api_run_artifacts(task_id: str, run_id: int) -> dict[str, Any]:
        runs = _special_runs(task_id) if task_id in _SPECIAL_ROLES else engine.db.list_runs(task_id)
        run = next((r for r in runs if r["run_id"] == run_id), None)
        if run is None:
            raise HTTPException(status_code=404, detail=f"运行记录不存在: {run_id}")
        lp = _resolve_log_path(task_id, run)
        if lp is None:
            return {"run": run, "prompt": "", "command": "", "stderr": "", "verify_log": "", "last_message": "", "review_verdict": None}

        def _read(path: Path, limit: int = 60_000) -> str:
            if not path.exists() or not path.is_file():
                return ""
            txt = path.read_text("utf-8", errors="replace")
            return txt[:limit] + ("\n…（内容过长已截断）" if len(txt) > limit else "")

        if lp.is_file():
            return {
                "run": run,
                "prompt": "",
                "command": "",
                "stderr": "",
                "verify_log": _read(lp),
                "last_message": "",
                "review_verdict": None,
            }
        return {
            "run": run,
            "prompt": _read(lp / "prompt.md"),
            "command": _read(lp / "command.json"),
            "stderr": _read(lp / "stderr.log"),
            "verify_log": "",
            "last_message": _read(lp / "last_message.txt"),
            "review_verdict": _read_review_verdict(lp),
        }

    @app.post("/vernal-api/tasks/{task_id}/steer")
    def api_steer(task_id: str, req: SteerRequest) -> dict[str, Any]:
        try:
            engine.steer(task_id, note=req.note, actor=req.actor, verify_only=req.verify_only)
        except OpsError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True}

    @app.get("/vernal-api/milestones/{milestone_id}/report")
    def api_milestone_report(milestone_id: str) -> dict[str, Any]:
        ms = engine.db.get_milestone(milestone_id)
        if ms is None:
            raise HTTPException(status_code=404, detail=f"里程碑不存在: {milestone_id}")
        content = ""
        if ms.report_path and Path(ms.report_path).exists():
            content = Path(ms.report_path).read_text("utf-8")
        return {
            "milestone_id": ms.milestone_id,
            "status": ms.status.value,
            "commit_matrix": json.loads(ms.commit_matrix_json) if ms.commit_matrix_json else {},
            "report": content,
        }

    @app.get("/vernal-api/milestones/{milestone_id}/files.zip")
    def download_files(milestone_id: str) -> Response:
        milestone = engine.db.get_milestone(milestone_id)
        key = engine.db.meta_get(f"delivery:{milestone_id}")
        if not milestone or milestone.status != "MERGED" or not key:
            raise HTTPException(404, "尚无已验收的文件交付")
        files = FileExchange(engine.ws, engine.db).get(key)
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, content in files.items():
                archive.writestr(name, content)
        return Response(stream.getvalue(), media_type="application/zip",
                        headers={"Content-Disposition": 'attachment; filename="vernal-delivery.zip"'})

    @app.post("/vernal-api/milestones/{milestone_id}/merge")
    def api_merge_milestone(milestone_id: str, req: ActorRequest) -> dict[str, Any]:
        try:
            engine.merge_milestone(milestone_id, actor=req.actor)
        except (OpsError, gitops.GitError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"status": engine.status.value}

    @app.post("/vernal-api/milestones/{milestone_id}/reintegrate")
    def api_reintegrate(milestone_id: str, req: ActorRequest) -> dict[str, Any]:
        try:
            engine.reintegrate(milestone_id, actor=req.actor)
        except OpsError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/vernal-api/scheduler/start")
    async def api_scheduler_start(watch: bool = True) -> dict[str, Any]:
        if _is_scheduler_running():
            return {"running": True}
        sched = Scheduler(engine)
        try:
            sched.check_runtime()
        except OpsError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        state["scheduler"] = sched

        async def _run_sched() -> None:
            try:
                await sched.run(watch=watch)
            except Exception as exc:
                engine.db.add_event("scheduler.error", f"调度器异常：{exc}")

        state["scheduler_task"] = asyncio.create_task(_run_sched())
        return {"running": True}

    @app.post("/vernal-api/scheduler/stop")
    async def api_scheduler_stop() -> dict[str, Any]:
        sched: Scheduler | None = state["scheduler"]
        if sched is not None:
            sched.request_stop()
        return {"stopping": True}

    def _lf_client() -> LoopForgeClient:
        return LoopForgeClient(engine.settings.loopforge_url, engine.settings.effective_loopforge_token())

    # ---------- 多智能体架构评审圆桌（Council） ----------
    @app.get("/vernal-api/council")
    def api_council_get() -> dict[str, Any]:
        msgs = engine.db.list_council_messages()
        consensus = next((m for m in reversed(msgs) if m.get("kind") == "consensus"), None)
        return {
            "messages": msgs,
            "consensus": consensus["body"] if consensus else None,
            "running": _is_planning(),
        }

    @app.post("/vernal-api/council/run")
    async def api_council_run(req: PlanRequest) -> dict[str, Any]:
        if _is_planning():
            raise HTTPException(status_code=409, detail="规划/圆桌会议已在运行中")
        if _is_scheduler_running():
            raise HTTPException(status_code=409, detail="调度器正在运行，请先停止")

        async def _run() -> None:
            try:
                await run_council(engine, extra_instructions=req.extra_instructions)
            except Exception as exc:
                engine.db.add_event("council.failed", f"架构评审圆桌失败：{exc}", task_id="_council")

        state["plan_task"] = asyncio.create_task(_run())
        return {"started": True}

    @app.post("/vernal-api/council/interject")
    async def api_council_interject(req: CouncilInterjectRequest) -> dict[str, Any]:
        if _is_planning():
            raise HTTPException(status_code=409, detail="圆桌会议正在收敛，请稍候")
        if not req.note.strip():
            raise HTTPException(status_code=400, detail="插话内容不能为空")

        async def _run() -> None:
            try:
                await interject_council(engine, actor=req.actor, note=req.note)
            except Exception as exc:
                engine.db.add_event("council.failed", f"圆桌补充裁决失败：{exc}", task_id="_council")

        state["plan_task"] = asyncio.create_task(_run())
        return {"started": True}

    # ---------- LoopForge 底座连接、BFF 代理与统一审批收件箱 ----------
    @app.get("/vernal-api/runtime/status")
    async def api_runtime_status() -> dict[str, Any]:
        client = _lf_client()
        lf_ok, lf_health = await client.health()
        gt_ok, gt_health = await client.gitea_health(engine.settings.gitea_url)
        has_token = bool(engine.settings.effective_loopforge_token())
        payload: dict[str, Any] = {
            "runtime_backend": engine.settings.runtime_backend,
            "collaboration_mode": engine.settings.collaboration_mode,
            "loopforge": {
                "url": engine.settings.loopforge_url,
                "reachable": lf_ok,
                "authenticated": False,
                "has_token": has_token,
                "project_id": engine.settings.loopforge_project_id,
                "workflow_id": engine.settings.loopforge_workflow_id,
                "repository_id": engine.settings.loopforge_repository_id,
                "health": lf_health,
            },
            "gitea": {"url": engine.settings.gitea_url, "reachable": gt_ok, "health": gt_health},
            "projects": [],
            "workflows": [],
            "agents": [],
            "sandboxes": [],
            "executors": [],
            "recent_runs": [],
            "auth_error": "",
        }
        if lf_ok and has_token:
            try:
                projects, workflows, agents, sandboxes, executors, runs = await asyncio.gather(
                    client.list_projects(),
                    client.list_workflows(),
                    client.list_agents(),
                    client.list_sandboxes(),
                    client.list_executors(),
                    client.list_runs(),
                )
                payload["loopforge"]["authenticated"] = True
                payload["projects"] = projects
                payload["workflows"] = workflows
                payload["agents"] = agents
                payload["sandboxes"] = sandboxes
                payload["executors"] = executors
                payload["recent_runs"] = runs[:25]
            except LoopForgeError as exc:
                payload["auth_error"] = str(exc)
        return payload

    @app.post("/vernal-api/runtime/config")
    async def api_runtime_config(req: RuntimeConfigRequest) -> dict[str, Any]:
        if req.runtime_backend is not None and req.runtime_backend not in ("local", "loopforge"):
            raise HTTPException(422, "未知的执行底座")
        if req.collaboration_mode is not None and req.collaboration_mode != engine.settings.collaboration_mode:
            if req.collaboration_mode not in ("git", "files"):
                raise HTTPException(422, "未知的协作方式")
            if _is_planning() or _is_scheduler_running() or engine.db.list_milestones():
                raise HTTPException(409, "请在立项前选择协作方式，已有任务需要沿用原有交付记录")
            if req.collaboration_mode == "git":
                gitops.ensure_repo(engine.ws.root, engine.settings.base_branch)
        for field in (
            "collaboration_mode",
            "runtime_backend",
            "loopforge_url",
            "loopforge_token",
            "loopforge_project_id",
            "loopforge_workflow_id",
            "loopforge_repository_id",
        ):
            val = getattr(req, field)
            if val is not None:
                setattr(engine.settings, field, val.strip())
        if (
            engine.settings.runtime_backend == "loopforge"
            and engine.settings.effective_loopforge_token()
            and engine.settings.loopforge_project_id
        ):
            try:
                await ensure_vernal_loopforge_assets(
                    _lf_client(),
                    engine.settings.loopforge_project_id,
                    engine.settings,
                )
            except LoopForgeError:
                pass
        _persist_runtime_config()
        engine.reset_harness()
        engine.db.add_event(
            "runtime.configured",
            f"运行底座配置已更新（backend={engine.settings.runtime_backend}, workflow={engine.settings.loopforge_workflow_id or '-'}）",
            actor="owner",
        )
        return {
            "ok": True,
            "runtime_backend": engine.settings.runtime_backend,
            "workflow_id": engine.settings.loopforge_workflow_id,
        }

    @app.post("/vernal-api/runtime/bind")
    async def api_runtime_bind(request: Request) -> dict[str, Any]:
        """复用浏览器当前的 LoopForge 登录态（同源 Cookie），自动签发 Vernal 后台专用 afp_ Token 并装配标准角色与 SOP。"""
        cookie = request.headers.get("cookie", "")
        client = _lf_client()
        try:
            me = await client.auth_current_with_cookie(cookie)
        except LoopForgeError as exc:
            raise HTTPException(status_code=exc.status_code or 502, detail=str(exc)) from exc
        user = me.get("user") if isinstance(me.get("user"), dict) else me
        login = str((user or {}).get("loginName") or (user or {}).get("login") or "")
        if not login or (user or {}).get("isGuest"):
            raise HTTPException(status_code=401, detail="尚未登录 LoopForge，请先登录后再绑定")
        # 已有可用 token 时直接复用并确保标准资产就绪
        if engine.settings.effective_loopforge_token():
            try:
                await client.list_projects()
                assets = await LoopForgeBridge(engine.settings, engine.db).ensure_project_and_assets()
                engine.settings.runtime_backend = "loopforge"
                engine.reset_harness()
                _persist_runtime_config()
                return {
                    "bound": True,
                    "reused": True,
                    "login": login,
                    "project_id": assets["project_id"],
                    "workflow_id": assets["selected_workflow_id"],
                }
            except LoopForgeError:
                pass
        try:
            created = await client.create_token_with_cookie(cookie, name=f"Vernal Runtime Bridge ({login})")
        except LoopForgeError as exc:
            raise HTTPException(status_code=exc.status_code or 502, detail=f"签发 Token 失败：{exc}") from exc
        token = str(created.get("token") or created.get("plainToken") or created.get("value") or "")
        if not token:
            raise HTTPException(status_code=502, detail=f"LoopForge 未返回 Token 明文：{list(created.keys())}")
        engine.settings.loopforge_token = token
        engine.settings.runtime_backend = "loopforge"
        try:
            assets = await LoopForgeBridge(engine.settings, engine.db).ensure_project_and_assets()
        except LoopForgeError as exc:
            _persist_runtime_config()
            raise HTTPException(502, f"登录态已保存，项目或标准 SOP 装配失败：{exc}") from exc
        _persist_runtime_config()
        engine.reset_harness()
        engine.db.add_event(
            "runtime.bound",
            f"已复用登录态（{login}）绑定项目 {assets['project_id']} 并自动装配 SOP {assets['selected_workflow_id']}",
            actor="Vernal",
        )
        return {
            "bound": True,
            "reused": False,
            "login": login,
            "project_id": assets["project_id"],
            "workflow_id": assets["selected_workflow_id"],
        }

    @app.post("/vernal-api/runner/bind-code")
    async def api_runner_bind_code(format: str = "json") -> Response:
        """为本机零 Python 启动脚本签发一次性 LoopForge Runner 绑定码。"""
        token = engine.settings.effective_loopforge_token()
        if not token:
            parent = engine.ws.root.resolve().parent
            if parent.is_dir():
                for cfg in sorted(parent.glob("*/.autodev/config.toml")):
                    try:
                        cand = Settings.load(cfg).effective_loopforge_token()
                        if cand:
                            token = cand
                            break
                    except Exception:
                        continue
        if not token:
            raise HTTPException(status_code=401, detail="尚未绑定 LoopForge 登录态，请先在浏览器登录 http://127.0.0.1:8787")
        client = LoopForgeClient(engine.settings.loopforge_url, token)
        try:
            res = await client.create_runner_bind_code()
        except LoopForgeError as exc:
            raise HTTPException(status_code=exc.status_code or 502, detail=str(exc)) from exc
        code = str(res.get("code") or "")
        if not code:
            raise HTTPException(status_code=502, detail="LoopForge 未返回绑定码")
        if format == "raw":
            return Response(content=code, media_type="text/plain")
        return JSONResponse({"code": code})

    @app.post("/vernal-api/project/select")
    async def api_select_project(req: ProjectSelectRequest) -> dict[str, Any]:
        """跟随原版顶栏 #projectSwitcher 切换当前挂载的 Vernal 架构工作区，保持单一项目上下文一致。"""
        nonlocal engine
        pid = req.project_id.strip()
        if not pid or not re.fullmatch(r"[0-9A-Za-z._\-]{1,64}", pid):
            raise HTTPException(status_code=400, detail="非法的项目标识")
        if engine.settings.loopforge_project_id == pid:
            return {"switched": False, "project_id": pid, "workspace": engine.ws.root.name}
        if _is_planning() or _is_scheduler_running():
            raise HTTPException(status_code=409, detail="当前项目正在运行规划或调度，请先等待完成或停止后再切换项目")

        parent = engine.ws.root.resolve().parent
        if parent.name != "workspaces":
            parent = engine.ws.root.resolve().parent / "workspaces"
            parent.mkdir(parents=True, exist_ok=True)

        # 优先查找已绑定到该 LoopForge project_id 的现有工作区（如 orbit-shooter -> vernal-default）
        target_ws: Workspace | None = None
        for entry in sorted(parent.iterdir()):
            if not entry.is_dir() or entry.is_symlink():
                continue
            cand = Workspace(entry)
            if cand.config_path.is_file() and cand.db_path.is_file():
                try:
                    cfg = Settings.load(cand.config_path)
                    if cfg.loopforge_project_id == pid:
                        target_ws = cand
                        break
                except Exception:
                    continue

        if target_ws is None:
            ws_dir = parent / pid
            if (ws_dir / ".autodev" / "state.db").is_file():
                target_ws = Workspace(ws_dir)
            else:
                target_ws = init_workspace(ws_dir, name=req.project_name.strip() or pid, collaboration_mode="files")

        prev_token = engine.settings.loopforge_token
        prev_url = engine.settings.loopforge_url
        prev_gitea = engine.settings.gitea_url
        if on_event in engine.db.listeners:
            engine.db.listeners.remove(on_event)
        engine.db.close()

        new_settings = target_ws.load_settings()
        new_settings.runtime_backend = "loopforge"
        new_settings.loopforge_url = prev_url
        new_settings.gitea_url = prev_gitea
        if prev_token and not new_settings.loopforge_token:
            new_settings.loopforge_token = prev_token
        new_settings.loopforge_project_id = pid
        if not (target_ws.root / ".git").exists():
            new_settings.collaboration_mode = "files"

        engine = Engine(target_ws, settings=new_settings)
        engine.db.listeners.append(on_event)
        if engine.settings.effective_loopforge_token():
            try:
                await ensure_vernal_loopforge_assets(_lf_client(), pid, engine.settings)
            except LoopForgeError:
                pass
        _persist_runtime_config()
        return {
            "switched": True,
            "project_id": pid,
            "workspace": engine.ws.root.name,
            "workflow_id": engine.settings.loopforge_workflow_id,
        }


    @app.post("/vernal-api/wish")
    def api_wish(req: WishRequest) -> dict[str, Any]:
        """许愿式需求录入：写入 inputs/ 并立即切片，供圆桌评审使用。"""
        text = req.content.strip()
        if not text:
            raise HTTPException(status_code=400, detail="需求内容不能为空")
        import re as _re

        slug = _re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "-", req.title.strip() or "wish").strip("-")[:48] or "wish"
        engine.ws.inputs_dir.mkdir(parents=True, exist_ok=True)
        path = engine.ws.inputs_dir / f"{slug}.md"
        path.write_text(text + "\n", "utf-8")
        try:
            count = engine.ingest()
        except OpsError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        engine.db.add_event("wish.created", f"收到许愿需求《{req.title}》，已切分 {count} 个资料切片", actor="owner")
        return {"path": str(path), "slices": count}

    @app.get("/vernal-api/wish")
    def api_wish_list() -> dict[str, Any]:
        items = []
        if engine.ws.inputs_dir.exists():
            for p in sorted(engine.ws.inputs_dir.glob("*.md")):
                items.append({"name": p.stem, "content": p.read_text("utf-8", errors="replace")[:20000]})
        return {"items": items}

    def _persist_runtime_config() -> None:
        cfg_path = engine.ws.config_path
        text = cfg_path.read_text("utf-8") if cfg_path.exists() else ""
        keys = ("collaboration_mode", "runtime_backend", "loopforge_url", "loopforge_token",
                "loopforge_project_id", "loopforge_workflow_id", "loopforge_repository_id", "gitea_url")
        rt_block = "[runtime]\n" + "".join(
            f"{key} = {json.dumps(getattr(engine.settings, key), ensure_ascii=False)}\n" for key in keys
        ) + "\n"
        import re

        if re.search(r"(?m)^\[runtime\]\s*$", text):
            text = re.sub(r"(?ms)^\[runtime\]\s*.*?(?=^\[|\Z)", lambda _m: rt_block, text)
        else:
            text = text.rstrip("\n") + "\n\n" + rt_block
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=cfg_path.parent, delete=False) as tmp:
            tmp.write(text)
        Path(tmp.name).replace(cfg_path)

    def _persist_agent_and_scheduler_config() -> None:
        cfg_path = engine.ws.config_path
        text = cfg_path.read_text("utf-8") if cfg_path.exists() else ""
        agent_keys = (
            "harness",
            "pm_harness",
            "director_harness",
            "coder_harness",
            "qa_harness",
            "codex_bin",
            "codex_profile",
            "opencode_bin",
            "opencode_server_url",
            "codebuddy_bin",
            "dsh_bin",
            "claude_bin",
            "model",
            "pm_model",
            "director_model",
            "planner_model",
            "coder_model",
            "qa_model",
            "reviewer_model",
            "codex_extra_args",
            "agent_network_access",
            "bootstrap_network_access",
            "agent_timeout_sec",
        )
        sched_keys = (
            "planning_mode",
            "enable_council",
            "enable_pm_spec",
            "director_max_arbitrations",
            "max_concurrency",
            "max_retries",
            "verify_timeout_sec",
            "require_plan_approval",
            "require_merge_approval",
        )
        ag_block = "[agent]\n" + "".join(
            f"{k} = {json.dumps(getattr(engine.settings, k), ensure_ascii=False)}\n" for k in agent_keys
        ) + "\n"
        sc_block = "[scheduler]\n" + "".join(
            f"{k} = {json.dumps(getattr(engine.settings, k), ensure_ascii=False)}\n" for k in sched_keys
        ) + "\n"
        if re.search(r"(?m)^\[agent\]\s*$", text):
            text = re.sub(r"(?ms)^\[agent\]\s*.*?(?=^\[|\Z)", lambda _m: ag_block, text)
        else:
            text = ag_block + text
        if re.search(r"(?m)^\[scheduler\]\s*$", text):
            text = re.sub(r"(?ms)^\[scheduler\]\s*.*?(?=^\[|\Z)", lambda _m: sc_block, text)
        else:
            text = text.rstrip("\n") + "\n\n" + sc_block
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=cfg_path.parent, delete=False) as tmp:
            tmp.write(text)
        Path(tmp.name).replace(cfg_path)

    @app.post("/vernal-api/agent/rescan")
    def api_agent_rescan() -> dict[str, Any]:
        from autodev.config import detect_preferred_harness

        runtimes = engine.runtimes_status(refresh=True)
        return {
            "ok": True,
            "runtimes": runtimes,
            "preferred_harness": detect_preferred_harness(engine.settings),
        }

    @app.post("/vernal-api/agent/config")
    async def api_agent_config(req: AgentConfigRequest) -> dict[str, Any]:
        """网页端可视化修改多角色 Harness、模型与并发数，自动落盘 config.toml 并同步到底层 LoopForge 角色。"""
        from autodev.config import SUPPORTED_HARNESSES, normalize_harness_name

        valid_h = {"", "auto", *SUPPORTED_HARNESSES}
        for hfield in ("harness", "director_harness", "coder_harness", "qa_harness"):
            raw_val = getattr(req, hfield)
            if raw_val is not None:
                norm_val = normalize_harness_name(raw_val)
                if norm_val not in valid_h:
                    raise HTTPException(422, f"不支持的执行器: {raw_val}")
                if hfield == "harness" and not norm_val:
                    norm_val = "auto"
                setattr(engine.settings, hfield, norm_val)
                if hfield == "director_harness":
                    engine.settings.pm_harness = norm_val

        for mfield in ("model", "director_model", "coder_model", "qa_model"):
            mval = getattr(req, mfield)
            if mval is not None:
                cleaned = mval.strip()
                setattr(engine.settings, mfield, cleaned)
                if mfield == "director_model":
                    engine.settings.pm_model = cleaned
                    engine.settings.planner_model = cleaned
                elif mfield == "qa_model":
                    engine.settings.reviewer_model = cleaned

        if req.max_concurrency is not None:
            if not (1 <= req.max_concurrency <= 16):
                raise HTTPException(422, "max_concurrency 需在 1 ~ 16 之间")
            engine.settings.max_concurrency = req.max_concurrency
            engine.db.ensure_slots(req.max_concurrency)

        engine.reset_harness()
        _persist_agent_and_scheduler_config()

        synced_loopforge = False
        if (
            engine.settings.runtime_backend == "loopforge"
            and engine.settings.effective_loopforge_token()
            and engine.settings.loopforge_project_id
        ):
            try:
                await ensure_vernal_loopforge_assets(
                    _lf_client(),
                    engine.settings.loopforge_project_id,
                    engine.settings,
                )
                synced_loopforge = True
            except LoopForgeError:
                pass

        eff_dir = engine.settings.harness_for("director")
        eff_cod = engine.settings.harness_for("coder")
        eff_qa = engine.settings.harness_for("reviewer")
        engine.db.add_event(
            "agent.configured",
            f"已更新智能体编队配置（总监={eff_dir}, 开发={eff_cod}, 审查={eff_qa}, 并发={engine.settings.max_concurrency}）",
            actor="owner",
        )
        return {
            "ok": True,
            "synced_loopforge": synced_loopforge,
            "effective": {
                "director_harness": eff_dir,
                "coder_harness": eff_cod,
                "qa_harness": eff_qa,
                "max_concurrency": engine.settings.max_concurrency,
            },
        }

    @app.get("/vernal-api/runtime/approvals")
    async def api_runtime_approvals() -> dict[str, Any]:
        snap = engine.snapshot()
        lf_items: list[dict[str, Any]] = []
        lf_err = ""
        if engine.settings.effective_loopforge_token():
            try:
                lf_items = await _lf_client().list_pending_approvals()
            except LoopForgeError as exc:
                lf_err = str(exc)
        return {
            "plan_pending": snap["status"] == "PLANNED",
            "fused_tasks": [t for t in snap["tasks"] if t["status"] == "FUSED"],
            "awaiting_milestones": [m for m in snap["milestones"] if m["status"] == "AWAITING_MERGE"],
            "loopforge_gates": lf_items,
            "loopforge_error": lf_err,
        }

    @app.post("/vernal-api/runtime/runs/{run_id}/approve")
    async def api_lf_approve(run_id: str, req: RunApproveRequest) -> Any:
        try:
            res = await _lf_client().approve_run(run_id, comment=req.comment, node_id=req.node_id)
            engine.db.add_event("loopforge.approved", f"已批准 LoopForge Run {run_id}", actor="owner")
            return res
        except LoopForgeError as exc:
            raise HTTPException(status_code=exc.status_code or 502, detail=str(exc)) from exc

    @app.post("/vernal-api/runtime/runs/{run_id}/reject")
    async def api_lf_reject(run_id: str, req: RunRejectRequest) -> Any:
        try:
            res = await _lf_client().reject_run(
                run_id, reason=req.reason, target_node=req.target_node, node_id=req.node_id
            )
            engine.db.add_event("loopforge.rejected", f"已打回 LoopForge Run {run_id}：{req.reason}", actor="owner")
            return res
        except LoopForgeError as exc:
            raise HTTPException(status_code=exc.status_code or 502, detail=str(exc)) from exc

    @app.post("/vernal-api/runtime/runs/{run_id}/retry")
    async def api_lf_retry(run_id: str) -> Any:
        try:
            return await _lf_client().retry_run(run_id)
        except LoopForgeError as exc:
            raise HTTPException(status_code=exc.status_code or 502, detail=str(exc)) from exc

    @app.get("/vernal-api/runtime/runs/{run_id}/diff")
    async def api_lf_diff(run_id: str) -> Any:
        try:
            return await _lf_client().get_run_diff(run_id)
        except LoopForgeError as exc:
            raise HTTPException(status_code=exc.status_code or 502, detail=str(exc)) from exc

    @app.get("/vernal-api/runtime/analytics")
    async def api_lf_analytics() -> dict[str, Any]:
        snap = engine.snapshot()
        lf_analytics: dict[str, Any] = {}
        if engine.settings.effective_loopforge_token():
            try:
                lf_analytics = await _lf_client().get_analytics()
            except LoopForgeError:
                lf_analytics = {}
        return {"local_usage": snap["usage_total"], "loopforge": lf_analytics}

    def _sources(request: Request) -> dict[str, Path]:
        local = bool(request.client and request.client.host in ("127.0.0.1", "::1"))
        return history_sources(engine.ws, include_neighbors=local)

    @app.get("/vernal-api/history/workspaces")
    def api_history_sources(request: Request) -> dict[str, Any]:
        return {"current": engine.ws.root.resolve().name, "workspaces": list(_sources(request))}

    @app.get("/vernal-api/history/events")
    def api_history_page(request: Request, workspace: str, before: int | None = Query(None, ge=1),
                         limit: int = Query(60, ge=1, le=200)) -> dict[str, Any]:
        path = _sources(request).get(workspace)
        if path is None:
            raise HTTPException(status_code=404, detail="找不到这个历史工作区")
        return event_page(path, before, limit)

    @app.get("/vernal-api/events")
    async def api_events(request: Request, after: int | None = Query(None, ge=0)) -> StreamingResponse:
        cursor = engine.db.last_event_id() if after is None else after
        resumed = request.headers.get("last-event-id", "")
        if resumed.isascii() and resumed.isdigit() and len(resumed) <= 18:
            cursor = max(after or 0, int(resumed))

        async def gen():
            wake = asyncio.Event()
            pair = (asyncio.get_running_loop(), wake)
            subscribers.add(pair)
            last_id = cursor
            try:
                while True:
                    wake.clear()
                    batch = engine.db.events_after(last_id)
                    for ev in batch:
                        last_id = ev["event_id"]
                        yield f"id: {last_id}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
                    if batch:
                        continue
                    try:
                        await asyncio.wait_for(wake.wait(), timeout=3)
                    except TimeoutError:
                        # CLI workers write to the same database from another process.
                        yield ": keepalive\n\n"
            finally:
                subscribers.discard(pair)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ---------- 同源反向代理：其余所有路径透明交给 LoopForge（含原生前端、/api/*、/runners/*、/mcp） ----------
    @app.api_route(
        "/{full_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
        include_in_schema=False,
    )
    async def loopforge_passthrough(full_path: str, request: Request):
        if full_path.startswith(("vernal-api/", "vernal-static/")):
            raise HTTPException(status_code=404, detail="Not Found")
        return await proxy.forward(request)

    return app
