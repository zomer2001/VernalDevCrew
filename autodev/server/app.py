"""FastAPI REST API + SSE 实时事件推送看板。"""

from __future__ import annotations

import asyncio
import contextvars
import difflib
import hashlib
import io
import json
import os
import re
import time
import zipfile
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from autodev import gitops
from autodev.artifacts import MAX_TOTAL as FILE_EXCHANGE_MAX_TOTAL, FileExchange, checked_name
from autodev.config import Settings, Workspace
from autodev.council import interject_council, run_council
from autodev.engine import Engine, OpsError, PlanningError, init_workspace
from autodev.harnesses.base import extract_json
from autodev.harnesses.codex_cli import parse_events_jsonl
from autodev.loopforge.bootstrap import ensure_vernal_loopforge_assets
from autodev.loopforge.bridge import LoopForgeBridge, verify_context_sig
from autodev.loopforge.client import LoopForgeClient, LoopForgeError
from autodev.scheduler import Scheduler
from autodev.server.history import event_page, history_sources
from autodev.server.project_launcher import (
    choose_preferred_harness_from_runtimes,
    host_projects_root,
    list_host_directories,
    load_project_registry,
    merge_runner_runtimes,
    register_project_mapping,
    resolve_safe_host_dir,
    scan_workspace_source_files_detailed,
    slugify_project_id,
    write_codebase_overview_doc,
)
from autodev.server.proxy import LoopForgeProxy

STATIC_DIR = Path(__file__).parent / "static"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})
_PROJECT_ID_RE = re.compile(r"^[0-9A-Za-z._\-]{1,64}$")


@dataclass
class _ProjectSlot:
    project_key: str
    engine: Engine
    subscribers: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = field(default_factory=set)
    scheduler: Scheduler | None = None
    scheduler_task: asyncio.Task[Any] | None = None
    plan_task: asyncio.Task[Any] | None = None
    on_event: Any = None


class _EngineProxy:
    """按请求上下文自动路由到当前项目槽位的 Engine 代理，避免多人切项目串台。"""

    def __init__(self, getter: Any):
        self._getter = getter

    def __getattr__(self, name: str) -> Any:
        return getattr(self._getter().engine, name)


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
    pm_harness: str | None = None
    director_harness: str | None = None
    coder_harness: str | None = None
    qa_harness: str | None = None
    model: str | None = None
    pm_model: str | None = None
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


class ProjectCreateRequest(BaseModel):
    mode: str = "scratch"  # scratch | local_folder | git_repo
    name: str = ""
    project_id: str = ""
    wish: str = ""
    docs: list[dict[str, str]] = []
    local_path: str = ""
    git_url: str = ""
    branch: str = ""
    collaboration_mode: str = ""
    start_council: bool = False
    actor: str = "owner"


class ImportFolderRequest(BaseModel):
    name: str
    project_id: str = ""
    wish: str = ""
    files: list[dict[str, str]] = []
    start_council: bool = False
    actor: str = "owner"


class CodeBuddyKeyRequest(BaseModel):
    api_key: str
    internet_environment: str = ""


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


def create_app(initial_engine: Engine, proxy_transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    slots: dict[str, _ProjectSlot] = {}
    all_slots: list[_ProjectSlot] = []
    auth_cache: dict[str, tuple[float, str]] = {}
    proj_perm_cache: dict[str, tuple[float, set[str]]] = {}
    runner_rt_cache: dict[str, tuple[float, dict[str, dict[str, Any]], dict[str, Any]]] = {}

    def _register_slot(eng: Engine) -> _ProjectSlot:
        slot = _ProjectSlot(project_key=eng.settings.loopforge_project_id or eng.ws.root.name, engine=eng)

        def _on_ev(_ev: dict[str, Any]) -> None:
            for loop, wake in list(slot.subscribers):
                if not loop.is_closed():
                    loop.call_soon_threadsafe(wake.set)

        slot.on_event = _on_ev
        eng.db.listeners.append(_on_ev)
        all_slots.append(slot)
        slots[eng.ws.root.name] = slot
        if eng.settings.loopforge_project_id:
            slots[eng.settings.loopforge_project_id] = slot
        return slot

    default_slot_ref: list[_ProjectSlot] = [_register_slot(initial_engine)]
    current_slot_var: contextvars.ContextVar[_ProjectSlot | None] = contextvars.ContextVar("vernal_slot", default=None)

    def _slot() -> _ProjectSlot:
        return current_slot_var.get() or default_slot_ref[0]

    engine: Any = _EngineProxy(_slot)

    def _workspaces_parent() -> Path:
        base_root = initial_engine.ws.root.resolve()
        parent = base_root.parent
        if parent.name != "workspaces":
            parent = base_root.parent / "workspaces"
            parent.mkdir(parents=True, exist_ok=True)
        return parent

    def _find_or_open_slot(
        pid: str,
        *,
        create: bool = False,
        project_name: str = "",
        seed_engine: Engine | None = None,
    ) -> _ProjectSlot | None:
        existing = slots.get(pid)
        if existing is not None:
            return existing

        parent = _workspaces_parent()
        target_ws: Workspace | None = None
        reg = load_project_registry(parent)
        mapped = reg.get(pid)
        if isinstance(mapped, dict) and mapped.get("root"):
            mp = Path(str(mapped["root"]))
            if mp.is_dir() and (mp / ".autodev" / "state.db").is_file():
                target_ws = Workspace(mp)
        if target_ws is None and parent.is_dir():
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
                direct = parent / pid
                if (direct / ".autodev" / "state.db").is_file():
                    target_ws = Workspace(direct)

        if target_ws is None:
            if not create:
                return None
            target_ws = init_workspace(parent / pid, name=project_name.strip() or pid, collaboration_mode="files")

        new_settings = target_ws.load_settings()
        base_eng = seed_engine or default_slot_ref[0].engine
        new_settings.runtime_backend = "loopforge"
        new_settings.loopforge_url = base_eng.settings.loopforge_url
        new_settings.gitea_url = base_eng.settings.gitea_url
        new_settings.vernal_public_url = base_eng.settings.vernal_public_url
        if not new_settings.loopforge_token and base_eng.settings.loopforge_token:
            new_settings.loopforge_token = base_eng.settings.loopforge_token
        new_settings.loopforge_project_id = pid
        if not (target_ws.root / ".git").exists():
            new_settings.collaboration_mode = "files"

        new_eng = Engine(target_ws, settings=new_settings)
        slot = _register_slot(new_eng)
        slots[pid] = slot
        return slot

    def _is_loopback(request: Request) -> bool:
        if request.headers.get("x-forwarded-for"):
            return False
        host = request.client.host if request.client else ""
        return host in _LOOPBACK_HOSTS

    async def _verify_cookie_user(cookie: str, lf_url: str) -> str:
        if not cookie.strip():
            raise HTTPException(status_code=401, detail="请先登录 LoopForge 后再执行该操作")
        ck_key = f"{lf_url}|{hashlib.sha256(cookie.encode('utf-8')).hexdigest()}"
        cached = auth_cache.get(ck_key)
        now = time.monotonic()
        if cached and now - cached[0] < 10.0:
            return cached[1]
        try:
            me = await LoopForgeClient(lf_url).auth_current_with_cookie(cookie)
        except LoopForgeError as exc:
            raise HTTPException(status_code=401, detail=f"身份验证失败：{exc}") from exc
        user = me.get("user") if isinstance(me.get("user"), dict) else me
        login = str((user or {}).get("loginName") or (user or {}).get("login") or "")
        if not login or (user or {}).get("isGuest"):
            raise HTTPException(status_code=401, detail="当前为未登录或访客会话，无权执行该操作")
        auth_cache[ck_key] = (now, login)
        return login

    async def _verify_project_access(cookie: str, lf_url: str, pid: str) -> None:
        if not pid:
            return
        ck_key = f"{lf_url}|{hashlib.sha256(cookie.encode('utf-8')).hexdigest()}"
        cached = proj_perm_cache.get(ck_key)
        now = time.monotonic()
        if cached and now - cached[0] < 10.0:
            allowed_ids = cached[1]
        else:
            try:
                projects = await LoopForgeClient(lf_url).list_projects_with_cookie(cookie)
            except LoopForgeError:
                return
            allowed_ids = {str(p.get("id") or "") for p in projects if isinstance(p, dict) and p.get("id")}
            proj_perm_cache[ck_key] = (now, allowed_ids)
        if allowed_ids and pid not in allowed_ids:
            raise HTTPException(status_code=403, detail=f"当前登录用户无权访问项目 {pid}")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            for s in list(all_slots):
                if s.scheduler is not None:
                    s.scheduler.request_stop()
                for t in (s.scheduler_task, s.plan_task):
                    if t is not None and not t.done():
                        t.cancel()
                        try:
                            await t
                        except (asyncio.CancelledError, Exception):
                            pass
                if s.on_event in s.engine.db.listeners:
                    s.engine.db.listeners.remove(s.on_event)
                if s.engine is not initial_engine:
                    try:
                        s.engine.db.close()
                    except Exception:
                        pass
            if proxy is not None:
                await proxy.close()

    app = FastAPI(title="Vernal", version="0.2.0", lifespan=lifespan, docs_url="/vernal-api/docs", openapi_url="/vernal-api/openapi.json", redoc_url=None)
    # Vernal 自有静态资源，与 LoopForge 路径零冲突
    app.mount("/vernal-static", StaticFiles(directory=str(STATIC_DIR)), name="vernal-static")
    proxy = LoopForgeProxy(initial_engine.settings.loopforge_url, transport=proxy_transport)

    @app.middleware("http")
    async def vernal_project_and_auth_middleware(request: Request, call_next: Any):
        path = request.url.path
        if path.startswith("/vernal-static/"):
            resp = await call_next(request)
            resp.headers["Cache-Control"] = "no-cache"
            return resp

        if not path.startswith("/vernal-api/"):
            return await call_next(request)

        # 1. 执行机自动绑定码：严格限定仅本机回环地址可调用
        if path == "/vernal-api/runner/bind-code":
            if not _is_loopback(request):
                return JSONResponse(
                    {"detail": "执行机自动绑定码仅限宿主机本机脚本调用，局域网成员请通过网页「设置 → 执行机器」绑定"},
                    status_code=403,
                )

        # 2. 按请求头/查询参数定位项目槽位（多人同时打开不同项目互不串台）
        req_pid = (request.headers.get("x-vernal-project") or request.query_params.get("project") or "").strip()
        target_slot = default_slot_ref[0]
        uninitialized_bootstrap = False
        if req_pid:
            if not _PROJECT_ID_RE.fullmatch(req_pid):
                return JSONResponse({"detail": "非法的项目标识"}, status_code=400)
            resolved = _find_or_open_slot(req_pid, create=False)
            if resolved is not None:
                target_slot = resolved
            elif path in (
                "/vernal-api/project/select",
                "/vernal-api/project/create",
                "/vernal-api/project/import-folder",
                "/vernal-api/host/dirs",
                "/vernal-api/credentials/codebuddy",
                "/vernal-api/runtime/status",
                "/vernal-api/runtime/bind",
                "/vernal-api/runner/health",
            ):
                # 新项目首次打开或创建/接管尚未建本地工作区：允许引导与建项接口先执行
                uninitialized_bootstrap = True
            else:
                return JSONResponse({"detail": f"项目工作区尚未初始化：{req_pid}"}, status_code=404)
        request.state.requested_project_id = req_pid
        request.state.workspace_initialized = not uninitialized_bootstrap

        tok = current_slot_var.set(target_slot)
        try:
            # 3. 敏感操作与跨机访问鉴权（验证 LoopForge 登录态与项目权限）
            is_local = _is_loopback(request)
            cookie = request.headers.get("cookie", "")
            require_auth_env = os.environ.get("VERNAL_REQUIRE_AUTH", "").strip() == "1"
            lf_url = target_slot.engine.settings.loopforge_url

            # 3a. Runner 上下文/基线拉取端点：允许有效 HMAC 签名或本机调用，拦截未授权局域网读取
            is_baseline_dl = bool(
                re.fullmatch(r"/vernal-api/tasks/[^/]+/baseline\.json", path)
                or re.fullmatch(r"/vernal-api/plan-context/[^/]+\.json", path)
            )
            if is_baseline_dl:
                sig = (request.query_params.get("sig") or "").strip()
                res_key = (
                    f"baseline:{path.split('/')[-2]}"
                    if path.endswith("/baseline.json")
                    else f"plan:{path.rsplit('/', 1)[-1][:-5]}"
                )
                sig_ok = verify_context_sig(
                    target_slot.engine.settings.effective_loopforge_token(),
                    target_slot.engine.settings.loopforge_project_id,
                    res_key,
                    sig,
                )
                if not sig_ok and (not is_local or require_auth_env):
                    try:
                        await _verify_cookie_user(cookie, lf_url)
                        await _verify_project_access(cookie, lf_url, req_pid or target_slot.engine.settings.loopforge_project_id)
                    except HTTPException as exc:
                        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

            # 3b. 写操作、敏感控制及远程下载：局域网访问 / 启用强制鉴权 / LoopForge 模式下携带 Cookie 时强制校验登录与项目权限
            is_sensitive_op = (
                request.method in ("POST", "PUT", "PATCH", "DELETE")
                and path not in ("/vernal-api/runtime/bind", "/vernal-api/runner/bind-code")
            ) or (not is_local and (path.endswith("/files.zip") or path == "/vernal-api/host/dirs"))
            use_lf_cookie_auth = bool(cookie) and target_slot.engine.settings.runtime_backend == "loopforge"

            if is_sensitive_op and (not is_local or require_auth_env or use_lf_cookie_auth):
                try:
                    await _verify_cookie_user(cookie, lf_url)
                    check_pid = req_pid or target_slot.engine.settings.loopforge_project_id
                    skip_perm_paths = (
                        "/vernal-api/project/select",
                        "/vernal-api/project/create",
                        "/vernal-api/project/import-folder",
                        "/vernal-api/host/dirs",
                        "/vernal-api/credentials/codebuddy",
                    )
                    if check_pid and path not in skip_perm_paths:
                        await _verify_project_access(cookie, lf_url, check_pid)
                except HTTPException as exc:
                    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

            return await call_next(request)
        finally:
            current_slot_var.reset(tok)

    def _is_scheduler_running(slot: _ProjectSlot | None = None) -> bool:
        s = slot or _slot()
        return s.scheduler_task is not None and not s.scheduler_task.done()

    def _is_planning(slot: _ProjectSlot | None = None) -> bool:
        s = slot or _slot()
        return s.plan_task is not None and not s.plan_task.done()

    async def _fetch_unified_runtimes(
        eng: Engine,
        *,
        cookie: str = "",
        refresh: bool = False,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
        local_rt = eng.runtimes_status(refresh=refresh)
        if eng.settings.runtime_backend != "loopforge":
            return merge_runner_runtimes(local_rt, eng.settings)
        tok_val = eng.settings.effective_loopforge_token()
        cache_key = f"{eng.settings.loopforge_url}|{hashlib.sha256((tok_val or cookie).encode('utf-8')).hexdigest()[:16]}"
        now = time.monotonic()
        if not refresh:
            cached = runner_rt_cache.get(cache_key)
            if cached and now - cached[0] < 3.0:
                return cached[1], cached[2]
        runners: list[dict[str, Any]] = []
        user_info: dict[str, Any] = {}
        client = LoopForgeClient(eng.settings.loopforge_url, tok_val)
        if tok_val:
            try:
                runners = await client.list_runners()
            except LoopForgeError:
                runners = []
        try:
            if cookie:
                user_info = await client.auth_current_with_cookie(cookie)
            elif tok_val and any(isinstance(r, dict) and r.get("status") == "online" for r in runners):
                user_info = await client.auth_current()
        except LoopForgeError:
            user_info = {}
        merged, summary = merge_runner_runtimes(local_rt, eng.settings, runners=runners, user_info=user_info)
        runner_rt_cache[cache_key] = (now, merged, summary)
        return merged, summary

    @app.get("/vernal-api/state")
    async def get_state(request: Request) -> dict[str, Any]:
        eng = _slot().engine
        snap = eng.snapshot()
        snap["scheduler_running"] = _is_scheduler_running()
        snap["planning"] = _is_planning()
        snap["recent_events"] = eng.db.events_after(max(0, eng.db.last_event_id() - 120))
        runtimes, runner_summary = await _fetch_unified_runtimes(eng, cookie=request.headers.get("cookie", ""))
        snap["runtimes"] = runtimes
        snap["runner_summary"] = runner_summary
        snap["workspace_root"] = str(eng.ws.root)
        snap["host_dirs_available"] = host_projects_root().is_dir()
        snap["source_mode"] = eng.db.meta_get("source_mode") or (
            "git" if (eng.ws.root / ".git").exists() else "scratch"
        )
        snap["git_branch"] = ""
        snap["git_remote"] = ""
        if (eng.ws.root / ".git").exists():
            try:
                snap["git_branch"] = gitops.current_branch(eng.ws.root)
                proc_rem = gitops.git(["remote", "get-url", "origin"], eng.ws.root, check=False)
                if proc_rem.returncode == 0:
                    snap["git_remote"] = proc_rem.stdout.strip()
            except Exception:
                pass
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
        slot = _slot()
        eng = slot.engine
        if _is_planning(slot):
            raise HTTPException(status_code=409, detail="规划任务已在后台运行")
        if _is_scheduler_running(slot):
            raise HTTPException(status_code=409, detail="调度器正在运行，请先停止")

        async def _run_plan() -> None:
            try:
                # 新人一键点「开始圆桌与立项」时，若尚未摄入资料则自动先摄入，省去多余手动步骤
                if not eng.db.list_slices():
                    try:
                        eng.ingest()
                    except OpsError:
                        pass
                await eng.plan(extra_instructions=req.extra_instructions, mode=req.mode)
            except (OpsError, PlanningError, Exception) as exc:
                eng.db.add_event("plan.failed", f"规划失败：{exc}")

        slot.plan_task = asyncio.create_task(_run_plan())
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
        slot = _slot()
        eng = slot.engine
        if _is_scheduler_running(slot):
            return {"running": True}
        sched = Scheduler(eng)
        try:
            sched.check_runtime()
        except OpsError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        slot.scheduler = sched

        async def _run_sched() -> None:
            try:
                await sched.run(watch=watch)
            except Exception as exc:
                eng.db.add_event("scheduler.error", f"调度器异常：{exc}")

        slot.scheduler_task = asyncio.create_task(_run_sched())
        return {"running": True}

    @app.post("/vernal-api/scheduler/stop")
    async def api_scheduler_stop() -> dict[str, Any]:
        slot = _slot()
        if slot.scheduler is not None:
            slot.scheduler.request_stop()
        return {"stopping": True}

    def _lf_client() -> LoopForgeClient:
        eng = _slot().engine
        return LoopForgeClient(eng.settings.loopforge_url, eng.settings.effective_loopforge_token())

    # ---------- 多智能体架构评审圆桌（Council） ----------
    @app.get("/vernal-api/council")
    def api_council_get() -> dict[str, Any]:
        slot = _slot()
        msgs = slot.engine.db.list_council_messages()
        consensus = next((m for m in reversed(msgs) if m.get("kind") == "consensus"), None)
        return {
            "messages": msgs,
            "consensus": consensus["body"] if consensus else None,
            "running": _is_planning(slot),
        }

    @app.post("/vernal-api/council/run")
    async def api_council_run(req: PlanRequest) -> dict[str, Any]:
        slot = _slot()
        eng = slot.engine
        if _is_planning(slot):
            raise HTTPException(status_code=409, detail="规划/圆桌会议已在运行中")
        if _is_scheduler_running(slot):
            raise HTTPException(status_code=409, detail="调度器正在运行，请先停止")

        async def _run() -> None:
            try:
                await run_council(eng, extra_instructions=req.extra_instructions)
            except Exception as exc:
                eng.db.add_event("council.failed", f"架构评审圆桌失败：{exc}", task_id="_council")

        slot.plan_task = asyncio.create_task(_run())
        return {"started": True}

    @app.post("/vernal-api/council/interject")
    async def api_council_interject(req: CouncilInterjectRequest) -> dict[str, Any]:
        slot = _slot()
        eng = slot.engine
        if _is_planning(slot):
            raise HTTPException(status_code=409, detail="圆桌会议正在收敛，请稍候")
        if not req.note.strip():
            raise HTTPException(status_code=400, detail="插话内容不能为空")

        async def _run() -> None:
            try:
                await interject_council(eng, actor=req.actor, note=req.note)
            except Exception as exc:
                eng.db.add_event("council.failed", f"圆桌补充裁决失败：{exc}", task_id="_council")

        slot.plan_task = asyncio.create_task(_run())
        return {"started": True}

    # ---------- LoopForge 底座连接、BFF 代理与统一审批收件箱 ----------
    @app.get("/vernal-api/runtime/status")
    async def api_runtime_status(request: Request) -> dict[str, Any]:
        client = _lf_client()
        lf_ok, lf_health = await client.health()
        gt_ok, gt_health = await client.gitea_health(engine.settings.gitea_url)
        has_token = bool(engine.settings.effective_loopforge_token())
        payload: dict[str, Any] = {
            "runtime_backend": engine.settings.runtime_backend,
            "collaboration_mode": engine.settings.collaboration_mode,
            "workspace_initialized": bool(getattr(request.state, "workspace_initialized", True)),
            "requested_project_id": str(getattr(request.state, "requested_project_id", "")),
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
            "runners": [],
            "online_runners": [],
            "runners_online_count": 0,
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
                runners: list[dict[str, Any]] = []
                try:
                    runners = await client.list_runners()
                except LoopForgeError:
                    runners = []
                online_runners = [r for r in runners if isinstance(r, dict) and r.get("status") == "online"]
                payload["loopforge"]["authenticated"] = True
                payload["projects"] = projects
                payload["workflows"] = workflows
                payload["agents"] = agents
                payload["sandboxes"] = sandboxes
                payload["executors"] = executors
                payload["runners"] = runners
                payload["online_runners"] = online_runners
                payload["runners_online_count"] = len(online_runners)
                payload["recent_runs"] = runs[:25]
            except LoopForgeError as exc:
                payload["auth_error"] = str(exc)
        return payload

    @app.get("/vernal-api/runner/health")
    async def api_runner_health(request: Request, format: str = "json") -> Response:
        """核对 LoopForge 控制面实际收到的在线 Runner 心跳（供启动脚本与监控面板核验）。"""
        if not _is_loopback(request):
            raise HTTPException(status_code=403, detail="执行机心跳诊断接口仅限本机调用")
        eng = _slot().engine
        token = eng.settings.effective_loopforge_token()
        if not token:
            if format == "raw":
                return Response(content="unbound:0:", media_type="text/plain")
            return JSONResponse({"status": "unbound", "online_count": 0, "runners": []})
        client = LoopForgeClient(eng.settings.loopforge_url, token)
        try:
            runners = await client.list_runners()
        except LoopForgeError as exc:
            if format == "raw":
                return Response(content=f"error:0:{exc}", media_type="text/plain")
            return JSONResponse({"status": "error", "online_count": 0, "error": str(exc), "runners": []})
        online = [r for r in runners if isinstance(r, dict) and r.get("status") == "online"]
        names = [str(r.get("name") or r.get("id") or "") for r in online]
        if format == "raw":
            state_txt = "online" if online else "offline"
            return Response(content=f"{state_txt}:{len(online)}:{','.join(names)}", media_type="text/plain")
        return JSONResponse({"status": "online" if online else "offline", "online_count": len(online), "names": names, "runners": runners})

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
        if engine.settings.loopforge_project_id:
            slots[engine.settings.loopforge_project_id] = _slot()
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
                if assets.get("project_id"):
                    slots[str(assets["project_id"])] = _slot()
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
        if assets.get("project_id"):
            slots[str(assets["project_id"])] = _slot()
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
    async def api_runner_bind_code(request: Request, format: str = "json") -> Response:
        """为本机零 Python 启动脚本签发一次性 LoopForge Runner 绑定码（仅限宿主机回环调用，禁止跨工作区借令牌）。"""
        if not _is_loopback(request):
            raise HTTPException(status_code=403, detail="执行机自动绑定码仅限宿主机本机调用")
        eng = _slot().engine
        token = eng.settings.effective_loopforge_token()
        if not token:
            raise HTTPException(status_code=401, detail="当前项目尚未绑定 LoopForge 登录态，请先在浏览器登录 http://127.0.0.1:8787")
        client = LoopForgeClient(eng.settings.loopforge_url, token)
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
    async def api_select_project(request: Request, req: ProjectSelectRequest) -> dict[str, Any]:
        """初始化或切换到目标项目槽位（按项目独立隔离，不关闭也不干扰其他成员正在操作的项目）。"""
        pid = req.project_id.strip()
        if not pid or not _PROJECT_ID_RE.fullmatch(pid):
            raise HTTPException(status_code=400, detail="非法的项目标识")

        cookie = request.headers.get("cookie", "")
        if cookie or not _is_loopback(request) or os.environ.get("VERNAL_REQUIRE_AUTH", "").strip() == "1":
            await _verify_project_access(cookie, _slot().engine.settings.loopforge_url, pid)

        cur_slot = _slot()
        if cur_slot.engine.settings.loopforge_project_id == pid:
            slots[pid] = cur_slot
            return {"switched": False, "project_id": pid, "workspace": cur_slot.engine.ws.root.name}

        target_slot = _find_or_open_slot(
            pid,
            create=True,
            project_name=req.project_name,
            seed_engine=cur_slot.engine,
        )
        assert target_slot is not None
        target_eng = target_slot.engine
        tok = current_slot_var.set(target_slot)
        try:
            if target_eng.settings.effective_loopforge_token():
                try:
                    await ensure_vernal_loopforge_assets(_lf_client(), pid, target_eng.settings)
                except LoopForgeError:
                    pass
            _persist_runtime_config()
        finally:
            current_slot_var.reset(tok)

        # 仅当调用方未携带 X-Vernal-Project（单用户/旧调用兼容）时更新默认槽位；携带项目头时完全隔离
        if not request.headers.get("x-vernal-project"):
            default_slot_ref[0] = target_slot
        return {
            "switched": True,
            "project_id": pid,
            "workspace": target_eng.ws.root.name,
            "workflow_id": target_eng.settings.loopforge_workflow_id,
        }


    @app.get("/vernal-api/host/dirs")
    def api_host_dirs(path: str = "") -> dict[str, Any]:
        """浏览宿主机挂载目录，供前端点选本地文件夹原地接管。"""
        try:
            return list_host_directories(path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    async def _finalize_launched_project(
        *,
        ws: Workspace,
        pid: str,
        display_name: str,
        collab_mode: str,
        source_mode: str,
        seed_eng: Engine,
        git_url: str = "",
        branch: str = "",
        wish: str = "",
        docs: list[dict[str, str]] | None = None,
        start_council: bool = False,
        actor: str = "owner",
        update_default_slot: bool = True,
    ) -> dict[str, Any]:
        new_settings = ws.load_settings()
        new_settings.runtime_backend = seed_eng.settings.runtime_backend
        new_settings.loopforge_url = seed_eng.settings.loopforge_url
        new_settings.gitea_url = seed_eng.settings.gitea_url
        new_settings.vernal_public_url = seed_eng.settings.vernal_public_url
        if not new_settings.loopforge_token and seed_eng.settings.loopforge_token:
            new_settings.loopforge_token = seed_eng.settings.loopforge_token
        new_settings.loopforge_project_id = pid
        new_settings.collaboration_mode = collab_mode
        if branch:
            new_settings.base_branch = branch

        new_eng = Engine(ws, settings=new_settings)
        new_eng.db.meta_set("source_mode", source_mode)
        if git_url:
            new_eng.db.meta_set("git_url", git_url)
        if branch:
            new_eng.db.meta_set("git_branch", branch)

        # 写入附带需求文档或初始许愿
        if docs:
            ws.inputs_dir.mkdir(parents=True, exist_ok=True)
            for idx, doc in enumerate(docs, 1):
                raw_fn = Path(str(doc.get("filename") or f"doc-{idx}.md")).name
                safe_fn = re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+", "_", raw_fn).strip("._") or f"doc-{idx}.md"
                if "." not in safe_fn:
                    safe_fn += ".md"
                content = str(doc.get("content") or "")
                if content.strip():
                    (ws.inputs_dir / safe_fn).write_text(content, "utf-8")
        if wish.strip():
            ws.inputs_dir.mkdir(parents=True, exist_ok=True)
            (ws.inputs_dir / "01-wish.md").write_text(f"# 项目需求\n\n{wish.strip()}\n", "utf-8")

        if source_mode in ("local_folder", "git_repo", "import_folder"):
            write_codebase_overview_doc(ws.root, source_mode=source_mode, git_url=git_url, branch=branch)
            if collab_mode == "files":
                seed_files, overflow, total_bytes = scan_workspace_source_files_detailed(
                    ws.root, max_total_bytes=FILE_EXCHANGE_MAX_TOTAL
                )
                if overflow:
                    new_eng.db.close()
                    sample = "、".join(overflow[:5]) + (f" 等 {len(overflow)} 个文件" if len(overflow) > 5 else "")
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"项目源码总大小 {total_bytes / (1024 * 1024):.2f} MiB 超过文件交接模式上限 "
                            f"（{FILE_EXCHANGE_MAX_TOTAL // (1024 * 1024)} MiB），以下文件无法完整交接给开发任务：{sample}。"
                            "请精简大文件，或为该目录初始化 Git 后使用 Git 分支模式。"
                        ),
                    )
                if seed_files:
                    FileExchange(ws, new_eng.db).seed_baseline(seed_files)

        slices = 0
        try:
            slices = new_eng.ingest()
        except OpsError:
            slices = 0

        register_project_mapping(
            _workspaces_parent(),
            pid,
            ws.root,
            source_mode=source_mode,
            git_url=git_url,
            branch=branch,
        )

        slot = _register_slot(new_eng)
        slots[pid] = slot
        if update_default_slot:
            default_slot_ref[0] = slot

        lf_synced = False
        lf_repo_id = ""
        tok_ctx = current_slot_var.set(slot)
        try:
            tok_val = new_eng.settings.effective_loopforge_token()
            if new_eng.settings.runtime_backend == "loopforge" and tok_val:
                client = LoopForgeClient(new_eng.settings.loopforge_url, tok_val)
                try:
                    existing_projs = await client.list_projects()
                    proj_obj = next((p for p in existing_projs if str(p.get("id")) == pid), None)
                    if proj_obj is None:
                        proj_obj = await client.create_project(
                            project_id=pid,
                            name=display_name,
                            description=f"Vernal 启动台创建（{source_mode}）",
                        )
                    if git_url:
                        repo_id = f"{pid}-repo"
                        try:
                            await client.put_repository(
                                {
                                    "id": repo_id,
                                    "projectId": pid,
                                    "name": display_name,
                                    "cloneUrl": git_url,
                                    "repoUrl": git_url,
                                    "defaultBranch": branch or new_eng.settings.base_branch,
                                }
                            )
                            lf_repo_id = repo_id
                            new_eng.settings.loopforge_repository_id = repo_id
                            if isinstance(proj_obj, dict):
                                await client.put_project({**proj_obj, "id": pid, "name": display_name, "defaultRepositoryId": repo_id})
                        except LoopForgeError:
                            pass
                    await ensure_vernal_loopforge_assets(client, pid, new_eng.settings)
                    lf_synced = True
                    proj_perm_cache.clear()
                except LoopForgeError:
                    pass
            _persist_runtime_config()
            if start_council and slices > 0:
                async def _auto_plan() -> None:
                    try:
                        await new_eng.plan(extra_instructions="", mode=new_eng.settings.planning_mode)
                    except Exception as exc:
                        new_eng.db.add_event("plan.failed", f"自动启动圆桌失败：{exc}")

                slot.plan_task = asyncio.create_task(_auto_plan())
        finally:
            current_slot_var.reset(tok_ctx)

        new_eng.db.add_event(
            "project.launched",
            f"已通过启动台初始化项目「{display_name}」（方式={source_mode}，协作={collab_mode}，切片={slices}）",
            actor=actor,
        )
        return {
            "ok": True,
            "project_id": pid,
            "name": display_name,
            "workspace_root": str(ws.root),
            "source_mode": source_mode,
            "collaboration_mode": collab_mode,
            "branch": branch or new_eng.settings.base_branch,
            "slices": slices,
            "loopforge_synced": lf_synced,
            "repository_id": lf_repo_id,
            "council_started": bool(start_council and slices > 0),
        }

    async def _guard_project_target(
        request: Request,
        pid: str,
        target_dir: Path,
        *,
        allow_same_dir: bool = False,
    ) -> None:
        """创建或导入前校验同名冲突与多用户项目权限，避免误覆盖已有项目目录。"""
        parent = _workspaces_parent()
        reg = load_project_registry(parent)
        resolved_target = target_dir.resolve()
        mapped = reg.get(pid)
        if isinstance(mapped, dict) and mapped.get("root"):
            mapped_path = Path(str(mapped["root"])).resolve()
            if not allow_same_dir or mapped_path != resolved_target:
                raise HTTPException(
                    status_code=409,
                    detail=f"项目「{pid}」已存在，请更换项目名称，避免覆盖已有工作区",
                )
        if pid in slots:
            existing_root = slots[pid].engine.ws.root.resolve()
            if not allow_same_dir or existing_root != resolved_target:
                raise HTTPException(
                    status_code=409,
                    detail=f"项目「{pid}」已存在，请更换项目名称，避免覆盖已有工作区",
                )
        if not allow_same_dir and target_dir.exists() and any(target_dir.iterdir()):
            raise HTTPException(
                status_code=409,
                detail=f"目标工作区目录「{pid}」已存在且非空，请更换项目名称避免覆盖已有文件",
            )
        cookie = request.headers.get("cookie", "")
        eng = _slot().engine
        if cookie and eng.settings.runtime_backend == "loopforge":
            try:
                existing_projs = await LoopForgeClient(eng.settings.loopforge_url).list_projects_with_cookie(cookie)
                if any(str(p.get("id")) == pid for p in existing_projs):
                    if not allow_same_dir:
                        raise HTTPException(
                            status_code=409,
                            detail=f"LoopForge 中已存在同名项目「{pid}」，请更换项目名称",
                        )
                    await _verify_project_access(cookie, eng.settings.loopforge_url, pid)
            except LoopForgeError:
                pass

    @app.post("/vernal-api/project/create")
    async def api_create_project(request: Request, req: ProjectCreateRequest) -> dict[str, Any]:
        """统一项目启动台：支持从零新建、原地接管本地文件夹、克隆已有 Git 仓库与分支。"""
        mode = (req.mode or "scratch").strip().lower()
        if mode not in ("scratch", "local_folder", "git_repo"):
            raise HTTPException(status_code=422, detail=f"不支持的创建方式：{mode}")

        seed_eng = _slot().engine
        parent = _workspaces_parent()
        parent.mkdir(parents=True, exist_ok=True)
        update_default = not bool(request.headers.get("x-vernal-project"))

        if mode == "scratch":
            display_name = req.name.strip() or "新项目"
            pid = req.project_id.strip() or slugify_project_id(display_name, fallback="proj")
            if not _PROJECT_ID_RE.fullmatch(pid):
                raise HTTPException(status_code=400, detail="项目标识仅支持字母、数字、点、下划线和短横线")
            collab = req.collaboration_mode.strip().lower() or "files"
            if collab not in ("files", "git"):
                raise HTTPException(status_code=422, detail="协作方式仅支持 files 或 git")
            ws_dir = parent / pid
            await _guard_project_target(request, pid, ws_dir, allow_same_dir=False)
            ws = init_workspace(ws_dir, name=display_name, collaboration_mode=collab)
            return await _finalize_launched_project(
                ws=ws,
                pid=pid,
                display_name=display_name,
                collab_mode=collab,
                source_mode="scratch",
                seed_eng=seed_eng,
                wish=req.wish,
                docs=req.docs,
                start_council=req.start_council,
                actor=req.actor,
                update_default_slot=update_default,
            )

        if mode == "local_folder":
            if not req.local_path.strip():
                raise HTTPException(status_code=400, detail="请选择或填写本机项目文件夹路径")
            try:
                folder = resolve_safe_host_dir(req.local_path)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            display_name = req.name.strip() or folder.name
            pid = req.project_id.strip() or slugify_project_id(display_name, fallback="local")
            if not _PROJECT_ID_RE.fullmatch(pid):
                raise HTTPException(status_code=400, detail="非法的项目标识")
            await _guard_project_target(request, pid, folder, allow_same_dir=True)
            is_git = (folder / ".git").exists()
            detected_branch = ""
            detected_remote = ""
            if is_git:
                try:
                    detected_branch = gitops.current_branch(folder)
                    pr = gitops.git(["remote", "get-url", "origin"], folder, check=False)
                    if pr.returncode == 0:
                        detected_remote = pr.stdout.strip()
                except Exception:
                    pass
            collab = req.collaboration_mode.strip().lower() or ("git" if is_git else "files")
            if collab not in ("files", "git"):
                collab = "git" if is_git else "files"
            if collab == "files":
                _, overflow, total_bytes = scan_workspace_source_files_detailed(
                    folder, max_total_bytes=FILE_EXCHANGE_MAX_TOTAL
                )
                if overflow:
                    sample = "、".join(overflow[:5]) + (f" 等 {len(overflow)} 个文件" if len(overflow) > 5 else "")
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"目录文本源码总大小 {total_bytes / (1024 * 1024):.2f} MiB 超过文件交接上限 "
                            f"（{FILE_EXCHANGE_MAX_TOTAL // (1024 * 1024)} MiB），受影响文件：{sample}。"
                            "请精简无关大文件，或使用 Git 模式接管。"
                        ),
                    )
            target_branch = req.branch.strip() or detected_branch
            ws = init_workspace(folder, name=display_name, collaboration_mode=collab, base_branch=target_branch or None)
            return await _finalize_launched_project(
                ws=ws,
                pid=pid,
                display_name=display_name,
                collab_mode=collab,
                source_mode="local_folder",
                seed_eng=seed_eng,
                git_url=detected_remote,
                branch=target_branch,
                wish=req.wish,
                docs=req.docs,
                start_council=req.start_council,
                actor=req.actor,
                update_default_slot=update_default,
            )

        # mode == "git_repo"
        git_url = req.git_url.strip()
        if not git_url:
            raise HTTPException(status_code=400, detail="请填写 Git 仓库地址")
        repo_basename = git_url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git") or "git-project"
        display_name = req.name.strip() or repo_basename
        pid = req.project_id.strip() or slugify_project_id(display_name, fallback="repo")
        if not _PROJECT_ID_RE.fullmatch(pid):
            raise HTTPException(status_code=400, detail="非法的项目标识")
        target_dir = parent / pid
        await _guard_project_target(request, pid, target_dir, allow_same_dir=False)
        branch = req.branch.strip()
        clone_args = ["clone"]
        if branch:
            clone_args.extend(["--branch", branch])
        clone_args.extend([git_url, str(target_dir)])
        proc = gitops.git(clone_args, parent, check=False)
        if proc.returncode != 0:
            err_msg = (proc.stderr or proc.stdout).strip() or "git clone 失败"
            raise HTTPException(status_code=400, detail=f"克隆仓库失败：{err_msg}")

        actual_branch = branch
        try:
            actual_branch = gitops.current_branch(target_dir)
        except Exception:
            pass
        ws = init_workspace(target_dir, name=display_name, collaboration_mode="git", base_branch=actual_branch or None)
        return await _finalize_launched_project(
            ws=ws,
            pid=pid,
            display_name=display_name,
            collab_mode="git",
            source_mode="git_repo",
            seed_eng=seed_eng,
            git_url=git_url,
            branch=actual_branch,
            wish=req.wish,
            docs=req.docs,
            start_council=req.start_council,
            actor=req.actor,
            update_default_slot=update_default,
        )

    @app.post("/vernal-api/project/import-folder")
    async def api_import_folder(request: Request, req: ImportFolderRequest) -> dict[str, Any]:
        """浏览器原生文件夹选择（webkitdirectory）兜底导入：与文件交接共用 4 MiB 上限，写盘前完整校验冲突与体积。"""
        display_name = req.name.strip() or "导入项目"
        pid = req.project_id.strip() or slugify_project_id(display_name, fallback="import")
        if not _PROJECT_ID_RE.fullmatch(pid):
            raise HTTPException(status_code=400, detail="非法的项目标识")
        if not req.files:
            raise HTTPException(status_code=400, detail="未收到任何文件")
        if len(req.files) > 500:
            raise HTTPException(status_code=400, detail="单次浏览器导入最多支持 500 个文本文件，大型仓库请使用「原地接管本地文件夹」或「接入 Git 仓库」")

        parent = _workspaces_parent()
        target_dir = parent / pid
        await _guard_project_target(request, pid, target_dir, allow_same_dir=False)

        # 写盘前先校验全部有效文件的总大小，与 FileExchange.MAX_TOTAL（4 MiB）严格一致，避免“导入成功但开发任务丢文件”
        staged: list[tuple[str, bytes]] = []
        overflow: list[str] = []
        total_bytes = 0
        for item in req.files:
            rel = str(item.get("path") or "").replace("\\", "/").lstrip("/")
            content = str(item.get("content") or "")
            if not rel or ".." in Path(rel).parts:
                continue
            parts = Path(rel).parts
            if any(p in (".git", "node_modules", "__pycache__", ".venv", ".autodev", "inputs") for p in parts):
                continue
            try:
                safe_rel = checked_name(rel)
            except ValueError:
                continue
            encoded = content.encode("utf-8", errors="ignore")
            if total_bytes + len(encoded) > FILE_EXCHANGE_MAX_TOTAL:
                overflow.append(safe_rel)
                total_bytes += len(encoded)
                continue
            total_bytes += len(encoded)
            staged.append((safe_rel, encoded))

        if overflow:
            sample = "、".join(overflow[:5]) + (f" 等 {len(overflow)} 个文件" if len(overflow) > 5 else "")
            raise HTTPException(
                status_code=400,
                detail=(
                    f"导入文本总大小 {total_bytes / (1024 * 1024):.2f} MiB 超过文件交接上限 "
                    f"（{FILE_EXCHANGE_MAX_TOTAL // (1024 * 1024)} MiB），无法完整交给开发任务；"
                    f"超限受影响文件：{sample}。请精简大文件或改用「接入 Git 仓库」/「原地接管 Git 目录」。"
                ),
            )
        if not staged:
            raise HTTPException(status_code=400, detail="没有可导入的有效文本源码文件")

        target_dir.mkdir(parents=True, exist_ok=True)
        root_resolved = target_dir.resolve()
        for safe_rel, encoded in staged:
            dest = (root_resolved / safe_rel).resolve()
            if not dest.is_relative_to(root_resolved):
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(encoded)
        imported = len(staged)

        ws = init_workspace(target_dir, name=display_name, collaboration_mode="files")
        result = await _finalize_launched_project(
            ws=ws,
            pid=pid,
            display_name=display_name,
            collab_mode="files",
            source_mode="import_folder",
            seed_eng=_slot().engine,
            wish=req.wish,
            start_council=req.start_council,
            actor=req.actor,
            update_default_slot=not bool(request.headers.get("x-vernal-project")),
        )
        result["imported_files"] = imported
        return result

    @app.post("/vernal-api/credentials/codebuddy")
    async def api_save_codebuddy_key(request: Request, req: CodeBuddyKeyRequest) -> dict[str, Any]:
        """直接在 Vernal 编队面板保存当前登录用户的 CodeBuddy API Key 到底层 LoopForge 个人凭据。"""
        api_key = req.api_key.strip()
        if not api_key:
            raise HTTPException(status_code=400, detail="请输入有效的 CodeBuddy API Key")
        env_val = req.internet_environment.strip()
        if env_val not in ("", "internal", "ioa"):
            raise HTTPException(status_code=422, detail="网络环境仅支持国际站（留空）、国内站（internal）或 iOA（ioa）")

        eng = _slot().engine
        cookie = request.headers.get("cookie", "")
        tok_val = eng.settings.effective_loopforge_token()
        client = LoopForgeClient(eng.settings.loopforge_url, tok_val)
        try:
            me = await (client.auth_current_with_cookie(cookie) if cookie else client.auth_current())
        except LoopForgeError as exc:
            raise HTTPException(status_code=401, detail=f"获取当前登录用户失败：{exc}") from exc
        user_obj = me.get("user") if isinstance(me.get("user"), dict) else me
        uid = (user_obj or {}).get("id")
        if not uid:
            raise HTTPException(status_code=401, detail="未能识别当前 LoopForge 用户 ID，请先在网页完成登录")
        try:
            await client.save_user_codebuddy_key(uid, api_key, env_val, cookie_header=cookie)
        except LoopForgeError as exc:
            raise HTTPException(status_code=exc.status_code or 502, detail=f"保存 CodeBuddy API Key 失败：{exc}") from exc
        runner_rt_cache.clear()
        return {"ok": True, "codebuddy_api_key_configured": True, "internet_environment": env_val}

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
        git_block = (
            "[git]\n"
            f"base_branch = {json.dumps(engine.settings.base_branch, ensure_ascii=False)}\n"
            f"protected_paths = {json.dumps(engine.settings.protected_paths, ensure_ascii=False)}\n\n"
        )
        import re

        if re.search(r"(?m)^\[runtime\]\s*$", text):
            text = re.sub(r"(?ms)^\[runtime\]\s*.*?(?=^\[|\Z)", lambda _m: rt_block, text)
        else:
            text = text.rstrip("\n") + "\n\n" + rt_block
        if re.search(r"(?m)^\[git\]\s*$", text):
            text = re.sub(r"(?ms)^\[git\]\s*.*?(?=^\[|\Z)", lambda _m: git_block, text)
        else:
            text = text.rstrip("\n") + "\n\n" + git_block
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
    async def api_agent_rescan(request: Request) -> dict[str, Any]:
        eng = _slot().engine
        runtimes, runner_summary = await _fetch_unified_runtimes(
            eng,
            cookie=request.headers.get("cookie", ""),
            refresh=True,
        )
        return {
            "ok": True,
            "runtimes": runtimes,
            "runner_summary": runner_summary,
            "preferred_harness": choose_preferred_harness_from_runtimes(runtimes, eng.settings),
        }

    @app.post("/vernal-api/agent/config")
    async def api_agent_config(req: AgentConfigRequest) -> dict[str, Any]:
        """网页端可视化修改多角色 Harness、模型与并发数，自动落盘 config.toml 并同步到底层 LoopForge 角色。"""
        from autodev.config import SUPPORTED_HARNESSES, normalize_harness_name

        valid_h = {"", "auto", *SUPPORTED_HARNESSES}
        for hfield in ("harness", "pm_harness", "director_harness", "coder_harness", "qa_harness"):
            raw_val = getattr(req, hfield)
            if raw_val is not None:
                norm_val = normalize_harness_name(raw_val)
                if norm_val not in valid_h:
                    raise HTTPException(422, f"不支持的执行器: {raw_val}")
                if hfield == "harness" and not norm_val:
                    norm_val = "auto"
                setattr(engine.settings, hfield, norm_val)
                if hfield == "director_harness" and req.pm_harness is None:
                    engine.settings.pm_harness = norm_val

        for mfield in ("model", "pm_model", "director_model", "coder_model", "qa_model"):
            mval = getattr(req, mfield)
            if mval is not None:
                cleaned = mval.strip()
                setattr(engine.settings, mfield, cleaned)
                if mfield == "director_model":
                    if req.pm_model is None:
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
        eng = _slot().engine
        snap = eng.snapshot()
        lf_analytics: dict[str, Any] = {}
        lf_ok = False
        lf_error = ""
        if eng.settings.effective_loopforge_token():
            try:
                lf_analytics = await _lf_client().get_analytics()
                lf_ok = True
            except LoopForgeError as exc:
                lf_analytics = {}
                lf_error = str(exc)
        else:
            lf_error = "当前项目尚未绑定 LoopForge Token，无法拉取底座运行统计"
        linked_runs = [
            {"task_id": t["task_id"], "title": t["title"], "status": t["status"], "run_id": t["loopforge_run_id"]}
            for t in snap["tasks"]
            if t.get("loopforge_run_id")
        ]
        return {
            "project_id": eng.settings.loopforge_project_id,
            "workspace": eng.ws.root.name,
            "local_usage": snap["usage_total"],
            "local_tasks_count": len(snap["tasks"]),
            "linked_loopforge_runs": linked_runs,
            "loopforge": lf_analytics,
            "loopforge_ok": lf_ok,
            "loopforge_error": lf_error,
            "sources": {
                "local": f"Vernal 工作区数据库 ({eng.ws.root.name}/.autodev/state.db)",
                "loopforge": f"LoopForge 控制面统计 ({eng.settings.loopforge_url}/api/analytics)",
            },
        }

    def _sources(request: Request) -> dict[str, Path]:
        local = _is_loopback(request)
        return history_sources(_slot().engine.ws, include_neighbors=local)

    @app.get("/vernal-api/history/workspaces")
    def api_history_sources(request: Request) -> dict[str, Any]:
        return {"current": _slot().engine.ws.root.resolve().name, "workspaces": list(_sources(request))}

    @app.get("/vernal-api/history/events")
    def api_history_page(request: Request, workspace: str, before: int | None = Query(None, ge=1),
                         limit: int = Query(60, ge=1, le=200)) -> dict[str, Any]:
        path = _sources(request).get(workspace)
        if path is None:
            raise HTTPException(status_code=404, detail="找不到这个历史工作区")
        return event_page(path, before, limit)

    @app.get("/vernal-api/events")
    async def api_events(request: Request, after: int | None = Query(None, ge=0)) -> StreamingResponse:
        slot = _slot()
        eng = slot.engine
        cursor = eng.db.last_event_id() if after is None else after
        resumed = request.headers.get("last-event-id", "")
        if resumed.isascii() and resumed.isdigit() and len(resumed) <= 18:
            cursor = max(after or 0, int(resumed))

        async def gen():
            wake = asyncio.Event()
            pair = (asyncio.get_running_loop(), wake)
            slot.subscribers.add(pair)
            last_id = cursor
            try:
                while True:
                    wake.clear()
                    batch = eng.db.events_after(last_id)
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
                slot.subscribers.discard(pair)

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
