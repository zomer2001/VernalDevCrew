"""Vernal 统一网关入口：生命周期管理、鉴权与同源 CSRF 中间件、模块化路由装配及底层引擎代理。"""

from __future__ import annotations

import asyncio
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from autodev.config import EXECUTION_IDENTITY
from autodev.engine import Engine
from autodev.harnesses.link_worker import LINK_USER
from autodev.loopforge.bootstrap import ensure_vernal_loopforge_assets  # noqa: F401
from autodev.loopforge.bridge import verify_context_sig
from autodev.loopforge.client import LoopForgeError
from autodev.security import verify_same_origin
from autodev.server.deps import (
    PROJECT_ID_RE,
    ServerContext, require_engineering_role,
    current_slot_var,
    current_user_var,
    is_loopback, has_launcher_secret,
    is_runner_launcher,
)
from autodev.public_pool import server_url
from autodev.loopforge.client import SessionCookie
from autodev.server.link import register_link
from autodev.server.proxy import LoopForgeProxy
from autodev.server.routers import (
    ci,
    codeintel,
    docs,
    governance,
    planning,
    process,
    projects,
    quality,
    reviews,
    runtime,
    steering,
    tasks,
    trace,
    speckit,
)

STATIC_DIR = Path(__file__).parent / "static"

_BOOTSTRAP_ALLOWED_PATHS = frozenset(
    {
        "/vernal-api/link/create-code",
        "/vernal-api/project/resume",
        "/vernal-api/project/select",
        "/vernal-api/project/create",
        "/vernal-api/project/import-folder",
        "/vernal-api/host/dirs",
        "/vernal-api/credentials/codebuddy",
        "/vernal-api/runtime/status",
        "/vernal-api/runtime/bind",
        "/vernal-api/runner/health",
        "/vernal-api/runner/connect",
        "/vernal-api/runtime/public-pool",
        "/vernal-api/runtime/gitea/credentials",
    }
)


def _auto_resume_active_schedulers(ctx: ServerContext) -> list[str]:
    """服务重启后自动恢复处于 RUNNING 且存在在途/待执行任务的工作区调度器。"""
    if os.environ.get("VERNAL_DISABLE_AUTO_RESUME", "").strip() == "1":
        return []
    from autodev.config import Settings
    from autodev.engine import OpsError
    from autodev.models import ACTIVE_TASK_STATUSES, ProjectStatus, TaskStatus
    from autodev.server.project_launcher import load_project_registry
    from autodev.server.routers.planning import start_slot_scheduler

    candidate_pids: list[str] = []
    parent = ctx.workspaces_parent()
    reg = load_project_registry(parent)
    for pid, meta in reg.items():
        if isinstance(meta, dict) and meta.get("bind_state") != "PENDING_UPSTREAM":
            candidate_pids.append(str(pid))
    if parent.is_dir():
        for entry in sorted(parent.iterdir()):
            if not entry.is_dir() or entry.is_symlink():
                continue
            cfg_path = entry / ".autodev" / "config.json"
            db_path = entry / ".autodev" / "state.db"
            if cfg_path.is_file() and db_path.is_file():
                try:
                    pid = Settings.load(cfg_path).loopforge_project_id
                    if pid and pid not in candidate_pids:
                        candidate_pids.append(pid)
                except Exception:
                    continue
    for pid in candidate_pids:
        try:
            ctx.find_or_open_slot(pid, create=False)
        except Exception:
            continue

    resumed: list[str] = []
    for slot in list(ctx.all_slots):
        eng = slot.engine
        if ctx.is_scheduler_running(slot) or eng.status != ProjectStatus.RUNNING:
            continue
        if eng.db._query("SELECT 1 FROM dispatch_records WHERE stage='lite_delivery' "
                         "AND state IN ('DISPATCHING','QUEUED','RUNNING','NEEDS_CONFIRMATION') LIMIT 1"):
            continue  # The receipt reconciler owns restart recovery for tagged native deliveries.
        tasks = eng.db.list_tasks()
        has_active = any(t.status in ACTIVE_TASK_STATUSES for t in tasks)
        has_dispatch = bool(
            eng.db._query("SELECT 1 FROM dispatch_records WHERE state IN ('RUNNING', 'QUEUED') AND stage NOT IN ('std_plan','std_spec','lite_bundle') LIMIT 1")
        )
        has_ready = any(t.status == TaskStatus.READY for t in tasks)
        if eng.settings.runtime_backend == "loopforge":
            if not (eng.settings.service_token() and eng.settings.loopforge_project_id):
                continue
            should_resume = has_active or has_dispatch or has_ready
        else:
            should_resume = has_active or has_dispatch
        if not should_resume:
            continue
        try:
            if start_slot_scheduler(ctx, slot, watch=True):
                eng.db.add_event(
                    "scheduler.auto_resumed",
                    "服务重启后已自动恢复项目调度与实时进度追踪",
                    actor="system",
                )
                resumed.append(slot.project_key)
        except (OpsError, Exception):
            continue
    return resumed


def create_app(initial_engine: Engine, proxy_transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    ctx = ServerContext(initial_engine)
    proxy = LoopForgeProxy(server_url(), transport=proxy_transport)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        from autodev.governance.audit import flush_decisions, platform_db_path_for
        async def drain():
            while True:
                for slot in list(ctx.all_slots):
                    try:
                        flush_decisions(slot.engine.db, platform_db_path_for(ctx.workspaces_parent()))
                        from autodev.local_git_bridge import collect_task_mirrors
                        await collect_task_mirrors(slot.engine)
                    except (OSError, httpx.HTTPError, __import__("sqlite3").Error):
                        continue
                await asyncio.sleep(10)
        from autodev.loopforge.reconciler import serve
        reconciliation = asyncio.create_task(serve(ctx))
        delivery = asyncio.create_task(drain())
        _auto_resume_active_schedulers(ctx)
        try:
            yield
        finally:
            reconciliation.cancel()
            delivery.cancel()
            await asyncio.gather(delivery, reconciliation, return_exceptions=True)
            for s in list(ctx.all_slots):
                if s.scheduler is not None:
                    s.scheduler.request_stop()
                for t in (s.scheduler_task, s.plan_task, *list(s.steering_tasks.values())):
                    if t is not None and not t.done():
                        t.cancel()
                        try:
                            await t
                        except (asyncio.CancelledError, Exception):
                            pass
                s.steering_tasks.clear()
                if s.on_event in s.engine.db.listeners:
                    s.engine.db.listeners.remove(s.on_event)
                if s.engine is not initial_engine:
                    try:
                        s.engine.db.close()
                    except Exception:
                        pass
            await proxy.close()

    app = FastAPI(
        title="Vernal",
        version="0.3.0",
        lifespan=lifespan,
        docs_url="/vernal-api/openapi-docs",
        openapi_url="/vernal-api/openapi.json",
        redoc_url=None,
    )
    app.state.ctx = ctx
    app.mount("/vernal-static", StaticFiles(directory=str(STATIC_DIR)), name="vernal-static")

    @app.get("/vernal-api/health")
    def health():
        return {"status": "ok"}

    @app.middleware("http")
    async def vernal_project_and_auth_middleware(request: Request, call_next: Any):
        path = request.url.path
        if path.startswith("/vernal-static/"):
            resp = await call_next(request)
            resp.headers["Cache-Control"] = "no-cache"
            return resp

        if re.fullmatch(r"/vernal-api/(?:pool-model/(?:[^/]+/)?(?:openai|deepseek)/v1/.+|pool-broker/[0-9]+/(?:openai|deepseek)/exchange)", path):
            return await call_next(request)

        if path == "/vernal-api/health" and request.method == "GET":
            return await call_next(request)

        if not path.startswith("/vernal-api/"):
            return await call_next(request)

        if path in ("/vernal-api/runner/bind-code", "/vernal-api/runner/health"):
            if not is_runner_launcher(request):
                return JSONResponse(
                    {"detail": "执行机自动绑定码仅限宿主机本机脚本调用，局域网成员请通过网页绑定"},
                    status_code=403,
                )

        req_pid = (request.headers.get("x-vernal-project") or request.query_params.get("project") or "").strip()
        git_transport = re.fullmatch(
            r"/vernal-api/git-bridge/([A-Za-z0-9._-]+)/vernal-task-[a-f0-9]{32}/[0-9]+/[a-f0-9]+/(info/refs|git-upload-pack|git-receive-pack)",
            path,
        )
        preview = re.fullmatch(r"/vernal-api/preview/([A-Za-z0-9._-]+)/[^/]+/.+", path)
        ci_cb = re.fullmatch(r"/vernal-api/ci/callback/([^/]+)/[^/]+/[0-9]+/[^/]+", path)
        cand_upload = re.fullmatch(r"/vernal-api/candidate-upload/([A-Za-z0-9._-]+)/([0-9]+)/([a-f0-9]+)", path)
        if git_transport or preview or ci_cb or cand_upload:
            req_pid = (git_transport or preview or ci_cb or cand_upload).group(1)

        if path in ("/vernal-api/audit", "/vernal-api/audit/verify", "/vernal-api/metrics/portfolio"):
            req_pid = ""
        target_slot = ctx.active_slot(request)
        uninitialized_bootstrap = False
        if req_pid:
            if not PROJECT_ID_RE.fullmatch(req_pid) or req_pid in (".", ".."):
                return JSONResponse({"detail": "非法的项目标识"}, status_code=400)
            resolved = ctx.find_or_open_slot(req_pid, create=False)
            if resolved is not None:
                if resolved.engine.settings.loopforge_project_id not in ("", req_pid):
                    return JSONResponse({"detail": "项目归属不匹配"}, status_code=403)
                target_slot = resolved
            elif path in _BOOTSTRAP_ALLOWED_PATHS:
                uninitialized_bootstrap = True
            else:
                if not is_loopback(request) or os.getenv("VERNAL_REQUIRE_AUTH") == "1":
                    try:
                        await ctx.verify_project_access(request.headers.get("cookie", ""), server_url(), req_pid)
                    except HTTPException as exc:
                        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
                return JSONResponse({"detail": f"项目工作区尚未初始化：{req_pid}"}, status_code=404)
        request.state.requested_project_id = req_pid
        request.state.workspace_initialized = not uninitialized_bootstrap

        tok = current_slot_var.set(target_slot)
        user_tok = None
        session_context = None
        link_user_context = None
        try:
            is_local = is_loopback(request)
            cookie = request.headers.get("cookie", "")
            require_auth_env = os.environ.get("VERNAL_REQUIRE_AUTH", "").strip() == "1"
            lf_url = server_url()
            device = re.fullmatch(r"/vernal-api/link/([a-f0-9]{32})/(?:baseline|capabilities|tasks/(?:claim|complete|[^/]+/input)|events|deliveries/[0-9]+|conflicts|runner-code)", path)
            delegate = re.fullmatch(r"/vernal-api/link-node/[a-f0-9]{32}", path)
            signed = bool(delegate or git_transport or ci_cb or cand_upload or device or path == "/vernal-api/link/exchange"
                          or re.fullmatch(r"/vernal-api/git-snapshots/[a-f0-9]+\.pack", path))
            if path in ("/vernal-api/runner/bind-code", "/vernal-api/runner/health"):
                signed = has_launcher_secret(request)
            sig = request.query_params.get("sig", "")
            if path == "/vernal-api/runner/installer" and sig:
                import time
                expiry = request.query_params.get("expires", "")
                component = request.query_params.get("component", "runner")
                cfg = target_slot.engine.settings
                signed = (expiry.isascii() and expiry.isdigit() and len(expiry) <= 12 and
                          int(time.time()) < int(expiry) and verify_context_sig(
                              cfg.project_signing_key(), cfg.loopforge_project_id, f"installer:{component}:{expiry}", sig))
            baseline = re.fullmatch(r"/vernal-api/tasks/([^/]+)/baseline\.json", path)
            plan = re.fullmatch(r"/vernal-api/plan-context/([^/]+)\.json", path)
            if (baseline or plan) and sig:
                resource = "baseline:" + baseline[1] if baseline else "plan:" + plan[1]
                settings = target_slot.engine.settings
                signed = any(verify_context_sig(key, settings.loopforge_project_id, resource, sig)
                             for key in settings.verification_keys())
            protected = not signed and (not is_local or require_auth_env or
                                        bool(cookie))
            try:
                if cookie and not signed:
                    verify_same_origin(request)
                if protected:
                    login = await ctx.verify_cookie_user(cookie, lf_url)
                    user_tok = current_user_var.set(login)
                    link_user_context = LINK_USER.set(login)
                    session_context = EXECUTION_IDENTITY.set((lf_url, SessionCookie(cookie)))
                    deployment_operation = (
                        path in ("/vernal-api/runtime/public-pool", "/vernal-api/runtime/gitea/credentials")
                        and request.method == "POST"
                    )
                    if deployment_operation:
                        return await call_next(request)
                    user, projects = await ctx.access_scope(cookie)
                    request.state.allowed_projects = set(projects)
                    request.state.platform_admin = user.get("isPlatformAdmin") is True
                    check_pid = req_pid or target_slot.engine.settings.loopforge_project_id
                    global_routes = {"/vernal-api/project/select", "/vernal-api/project/create",
                                     "/vernal-api/project/import-folder", "/vernal-api/project/resume", "/vernal-api/projects",
                                     "/vernal-api/metrics/portfolio", "/vernal-api/audit", "/vernal-api/audit/verify"}
                    if uninitialized_bootstrap and path not in global_routes | {
                        "/vernal-api/link/create-code", "/vernal-api/runtime/status", "/vernal-api/runner/connect",
                        "/vernal-api/credentials/codebuddy", "/vernal-api/host/dirs", "/vernal-api/runtime/public-pool"}:
                        raise HTTPException(409, "请先绑定当前项目工作区")
                    if path not in global_routes and (check_pid or path != "/vernal-api/runtime/bind"):
                        await ctx.verify_project_access(cookie, lf_url, check_pid)
                    if path == "/vernal-api/audit/verify" and not request.state.platform_admin:
                        raise HTTPException(403, "平台审计链校验需要平台管理员")
                    if request.method not in ("GET", "HEAD", "OPTIONS") and path not in global_routes:
                        native = projects.get(check_pid, {})
                        manage = (path.startswith("/vernal-api/rbac/members") or path in {
                            "/vernal-api/runtime/config", "/vernal-api/governance/settings"})
                        uid = str(user.get("id") or "")
                        role = next((m.get("role") for m in (native.get("members") or [])
                                     if uid and str(m.get("userId")) == uid), "")
                        if role == "viewer" and not (request.state.platform_admin or str(native.get("ownerId")) == uid):
                            raise HTTPException(403, "只读项目成员不能修改项目")
                        roles = None
                        if manage:
                            roles = ("admin",)
                        elif path in ("/vernal-api/agent/config", "/vernal-api/plan/approve", "/vernal-api/scheduler/start") or path.endswith(("/merge", "/restart")):
                            roles = ("admin", "architect", "developer") if path.endswith(("/start", "/restart")) else ("admin", "architect")
                        if path.startswith("/vernal-api/specifications"):
                            roles = ("admin", "architect", "developer")
                        if roles:
                            require_engineering_role(target_slot, user, native, roles, manage=manage)
                if device:
                    await app.state.verify_device(request, device[1])
            except HTTPException as exc:
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
            if path in ("/vernal-api/project/create", "/vernal-api/project/import-folder", "/vernal-api/project/resume"):
                async with ctx.binding_lock:
                    return await call_next(request)
            return await call_next(request)
        finally:
            if link_user_context is not None:
                LINK_USER.reset(link_user_context)
            if session_context is not None:
                EXECUTION_IDENTITY.reset(session_context)
            if user_tok is not None:
                current_user_var.reset(user_tok)
            current_slot_var.reset(tok)

    for mod in (
        planning,
        tasks,
        projects,
        runtime,
        steering,
        process,
        quality,
        reviews,
        ci,
        docs,
        codeintel,
        trace,
        governance,
        speckit,
    ):
        app.router.routes.extend(mod.router.routes)

    register_link(
        app,
        ctx.active_slot,
        ctx.find_or_open_slot,
        ctx.verify_cookie_user,
        ctx.verify_project_access,
        ctx.session_execution_token,
        ctx.persist_config,
    )

    @app.api_route(
        "/{full_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
        include_in_schema=False,
    )
    async def loopforge_passthrough(full_path: str, request: Request):
        if full_path.startswith(("vernal-api/", "vernal-static/")):
            raise HTTPException(status_code=404, detail="Not Found")
        decision = re.fullmatch(r"api/runs/([^/]+)/(?:approve|reject|pass)/?", full_path)
        if request.method == "POST" and decision:
            verify_same_origin(request)
            from autodev.server.routers.runtime import authorize_run_decision
            from autodev.loopforge.client import LoopForgeClient, LoopForgeError
            cookie = request.headers.get("cookie","")
            if not cookie:
                raise HTTPException(401, "原生节点签字需要个人登录会话")
            client = LoopForgeClient(server_url(), SessionCookie(cookie), transport=proxy_transport)
            try:
                run = await client.get_run(decision[1])
            except LoopForgeError as exc:
                raise HTTPException(exc.status_code or 503, "无法读取待签字的原生运行") from exc
            from autodev.loopforge.client import run_project_id
            user, projects = await ctx.access_scope(cookie)
            if not projects.get(run_project_id(run)):
                raise HTTPException(403, "无权访问此运行")
            body = await request.body()
            import json
            try:
                node = (json.loads(body) if body else {}).get("nodeId", "")
            except (ValueError, AttributeError):
                raise HTTPException(422, "无效的审批请求")
            await authorize_run_decision(ctx, request, run, node)
        # The native switcher consumes this directory directly. The 0.4.0
        # directory response itself is broader than the logged-in user's projects.
        if full_path.rstrip("/") == "api/projects" and request.method == "GET" and request.headers.get("cookie"):
            _, native = await ctx.access_scope(request.headers["cookie"])
            return JSONResponse(list(native.values()), headers={"Cache-Control": "private, no-store", "Vary": "Cookie"})
        return await proxy.forward(request)

    return app
