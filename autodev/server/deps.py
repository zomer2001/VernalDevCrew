"""服务端共享上下文、多项目槽位隔离、登录鉴权与配置持久化依赖。"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import hmac
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request

from autodev.public_pool import server_url
from autodev.config import Settings, Workspace
from autodev.engine import Engine, init_workspace
from autodev.loopforge.client import LoopForgeClient, LoopForgeError
from autodev.scheduler import Scheduler
from autodev.security import persist_workspace_settings
from autodev.server.project_launcher import load_project_registry, merge_runner_runtimes
from autodev.steering import reconcile_steering_checkpoint

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})
PROJECT_ID_RE = re.compile(r"^[0-9A-Za-z._\-]{1,64}$")

current_slot_var: contextvars.ContextVar["ProjectSlot | None"] = contextvars.ContextVar("vernal_slot", default=None)
current_user_var: contextvars.ContextVar[str] = contextvars.ContextVar("vernal_user", default="")


@dataclass
class ProjectSlot:
    project_key: str
    engine: Engine
    subscribers: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = field(default_factory=set)
    scheduler: Scheduler | None = None
    scheduler_task: asyncio.Task[Any] | None = None
    plan_task: asyncio.Task[Any] | None = None
    steering_tasks: dict[int, asyncio.Task[Any]] = field(default_factory=dict)
    steering_last_sync: float = 0.0
    on_event: Any = None


def is_loopback(request: Request) -> bool:
    if request.headers.get("x-forwarded-for"):
        return False
    host = request.client.host if request.client else ""
    return host in LOOPBACK_HOSTS


def has_launcher_secret(request: Request) -> bool:
    secret = os.environ.get("VERNAL_RUNNER_BOOTSTRAP_SECRET", "")
    supplied = request.headers.get("x-vernal-runner-secret", "")
    return bool(secret and hmac.compare_digest(secret.encode(), supplied.encode()))


def is_runner_launcher(request: Request) -> bool:
    return is_loopback(request) or has_launcher_secret(request)


_latest_ctx: "ServerContext | None" = None


def native_access(user: dict, project: dict) -> bool:
    # Mirrors 0.4.0 workbenchProjectAllowsUser, including historical shared projects.
    uid = str(user.get("id") or "")
    owner = str(project.get("ownerId") or "")
    members = project.get("members") or []
    if not project.get("id") or project.get("archivedAt"):
        return False
    return (user.get("isPlatformAdmin") is True or (not owner and not members)
            or bool(uid and (uid == owner or any(str(m.get("userId")) == uid for m in members))))


class ServerContext:
    """单个 FastAPI 实例持有的多项目槽位表与鉴权缓存。"""

    def __init__(self, initial_engine: Engine):
        global _latest_ctx
        _latest_ctx = self
        self.native_users = {}
        self.initial_engine = initial_engine
        self.slots: dict[str, ProjectSlot] = {}
        self.all_slots: list[ProjectSlot] = []
        self.auth_cache: dict[str, tuple[float, str]] = {}
        self.session_tokens: dict[tuple[str, str], tuple[float, str]] = {}
        self.session_lock = asyncio.Lock()
        self.binding_lock = asyncio.Lock()
        self.proj_perm_cache: dict[str, tuple[float, dict[str, dict]]] = {}
        self.runner_rt_cache: dict[tuple[Any, ...], tuple[float, dict[str, dict[str, Any]], dict[str, Any]]] = {}
        initial_slot = self.register_slot(initial_engine)
        self.initial_slot = initial_slot
        self.cli_default_slot = initial_slot

    def register_slot(self, eng: Engine) -> ProjectSlot:
        key = eng.settings.loopforge_project_id or eng.ws.root.name
        if key in self.slots:
            raise HTTPException(409, "原生项目已绑定到工作区")
        slot = ProjectSlot(project_key=key, engine=eng)

        def _on_ev(_ev: dict[str, Any]) -> None:
            for loop, wake in list(slot.subscribers):
                if not loop.is_closed():
                    loop.call_soon_threadsafe(wake.set)

        slot.on_event = _on_ev
        eng.db.listeners.append(_on_ev)
        self.all_slots.append(slot)
        eng.settings.loopforge_url = server_url()
        self.slots[slot.project_key] = slot
        return slot

    def active_slot(self, request: Request | None = None) -> ProjectSlot:
        bound = current_slot_var.get()
        if bound is not None:
            return bound
        if request is not None and request.headers.get("cookie", "").strip():
            return self.initial_slot
        return self.cli_default_slot

    def update_cli_default_if_unscoped(self, request: Request, slot: ProjectSlot) -> None:
        """仅单机无 Cookie 且未显式携带 X-Vernal-Project 头时更新 CLI 默认槽位，彻底杜绝多浏览器用户串台。"""
        if not request.headers.get("x-vernal-project") and not request.headers.get("cookie", "").strip():
            self.cli_default_slot = slot

    def workspaces_parent(self) -> Path:
        base_root = self.initial_engine.ws.root.resolve()
        parent = base_root.parent
        if parent.name != "workspaces":
            parent = base_root.parent / "workspaces"
            parent.mkdir(parents=True, exist_ok=True)
        return parent

    def find_or_open_slot(
        self,
        pid: str,
        *,
        create: bool = False,
        project_name: str = "",
        seed_engine: Engine | None = None,
    ) -> ProjectSlot | None:
        existing = self.slots.get(pid)
        if existing is not None:
            return existing

        parent = self.workspaces_parent()
        target_ws: Workspace | None = None
        reg = load_project_registry(parent)
        mapped = reg.get(pid)
        if isinstance(mapped, dict) and mapped.get("bind_state") == "PENDING_UPSTREAM":
            return None
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
        if new_settings.loopforge_project_id and new_settings.loopforge_project_id != pid:
            return None
        new_settings.loopforge_url = server_url()
        new_settings.loopforge_project_id = pid

        new_eng = Engine(target_ws, settings=new_settings)
        slot = self.register_slot(new_eng)
        self.slots[pid] = slot
        return slot

    async def verify_cookie_user(self, cookie: str, lf_url: str) -> str:
        lf_url = server_url()
        if not cookie.strip():
            raise HTTPException(status_code=401, detail="请先登录平台后再执行该操作")
        ck_key = f"{lf_url}|{hashlib.sha256(cookie.encode('utf-8')).hexdigest()}"
        cached = self.auth_cache.get(ck_key)
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
        self.native_users[ck_key] = user
        self.auth_cache[ck_key] = (now, login)
        return login

    async def try_resolve_user(self, cookie: str, lf_url: str) -> str:
        if not cookie.strip():
            return ""
        try:
            return await self.verify_cookie_user(cookie, lf_url)
        except HTTPException:
            return ""

    async def access_scope(self, cookie: str) -> tuple[dict, dict]:
        url = server_url()
        await self.verify_cookie_user(cookie, url)
        key = f"{url}|{hashlib.sha256(cookie.encode()).hexdigest()}"
        cached = self.proj_perm_cache.get(key)
        now = time.monotonic()
        if not cached or now - cached[0] >= 10:
            try:
                projects = await LoopForgeClient(url).list_projects_with_cookie(cookie)
            except LoopForgeError as exc:
                raise HTTPException(503, "暂时无法确认项目访问权限") from exc
            cached = now, {str(p["id"]): p for p in projects
                           if isinstance(p, dict) and native_access(self.native_users[key], p)}
            self.proj_perm_cache[key] = cached
        return self.native_users[key], cached[1]

    async def verify_project_access(self, cookie: str, lf_url: str, pid: str, *, strict: bool = True) -> None:
        _, projects = await self.access_scope(cookie)
        if not pid or pid not in projects:
            raise HTTPException(403, "当前用户无权访问此项目")

    async def session_execution_token(self, cookie: str, settings: Settings) -> str:
        url = server_url()
        login = await self.verify_cookie_user(cookie, url)
        key = (url, hashlib.sha256(cookie.encode()).hexdigest())
        async with self.session_lock:
            now = time.monotonic()
            hit = self.session_tokens.get(key)
            if hit and hit[0] > now:
                return hit[1]
            for stale in [k for k, value in self.session_tokens.items() if value[0] <= now]:
                del self.session_tokens[stale]
            client = LoopForgeClient(url)
            issued = await client.create_token_with_cookie(cookie, name=f"Vernal Runner ({login})", expires_in_days=1)
            personal = issued.get("token") or issued.get("plainToken") or issued.get("value")
            if not personal:
                raise HTTPException(401, "未取得当前登录账号的执行令牌")
            self.session_tokens[key] = (now + 12 * 3600, personal)
            return personal

    def start_background(self, slot: ProjectSlot, operation: str, coroutine):
        slot.engine.planning_stop.clear()
        actor = current_user_var.get() or "local"
        slot.engine.db.add_event("background.started", operation, actor=actor,
                                 payload={"credential_ref": "deployment-service", "project": slot.project_key})
        from autodev.harnesses.link_worker import WORKER_DELEGATE
        context = contextvars.Context()
        if slot.engine.db.meta_get("source_mode") == "host_link" and current_user_var.get():
            context.run(WORKER_DELEGATE.set, (slot.project_key, actor))
        return context.run(asyncio.create_task, coroutine)

    def bind_slot(self, slot: ProjectSlot, pid: str) -> None:
        if not pid or not PROJECT_ID_RE.fullmatch(pid) or pid in (".", ".."):
            raise HTTPException(422, "项目标识无效")
        other = self.find_or_open_slot(pid)
        if other and other is not slot:
            raise HTTPException(409, "该项目已绑定其他工作区")
        if slot.engine.settings.loopforge_project_id not in ("", pid):
            raise HTTPException(409, "已绑定的工作区不能切换归属")
        self.slots[pid] = slot
        slot.project_key = pid
        slot.engine.settings.loopforge_project_id = pid

    def is_scheduler_running(self, slot: ProjectSlot | None = None) -> bool:
        s = slot or self.active_slot()
        return s.scheduler_task is not None and not s.scheduler_task.done()

    def is_planning(self, slot: ProjectSlot | None = None) -> bool:
        s = slot or self.active_slot()
        return (s.plan_task is not None and not s.plan_task.done()) or bool(s.engine.db._query(
            "SELECT 1 FROM dispatch_records WHERE stage IN ('lite_bundle','std_plan','std_spec') "
            "AND state IN ('DISPATCHING','QUEUED','RUNNING') LIMIT 1"))

    def lf_client(self, slot: ProjectSlot | None = None) -> LoopForgeClient:
        eng = (slot or self.active_slot()).engine
        return LoopForgeClient(eng.settings.loopforge_url, eng.settings.effective_loopforge_token())

    def persist_config(self, eng: Engine | None = None) -> None:
        target = eng or self.active_slot().engine
        persist_workspace_settings(target.ws, target.settings)

    def ensure_steering_poller(self, slot: ProjectSlot, checkpoint_id: int) -> None:
        existing = slot.steering_tasks.get(checkpoint_id)
        if existing is not None and not existing.done():
            return

        async def _poll() -> None:
            try:
                for _ in range(900):
                    cp = await reconcile_steering_checkpoint(slot.engine, checkpoint_id)
                    if cp is None or cp.get("status") not in ("RUNNING", "AWAITING_APPROVAL", "SYNC_PENDING"):
                        break
                    await asyncio.sleep(2.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            finally:
                slot.steering_tasks.pop(checkpoint_id, None)

        slot.steering_tasks[checkpoint_id] = self.start_background(slot, "steering", _poll())

    async def fetch_unified_runtimes(
        self,
        eng: Engine,
        *,
        cookie: str = "",
        refresh: bool = False,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
        local_rt = eng.runtimes_status(refresh=refresh)
        if eng.settings.runtime_backend != "loopforge":
            return merge_runner_runtimes(local_rt, eng.settings)
        tok_val = eng.settings.effective_loopforge_token()
        cache_key = (
            eng.ws.root,
            eng.settings.execution_pool,
            eng.settings.harness,
            eng.settings.pm_harness,
            eng.settings.director_harness,
            eng.settings.coder_harness,
            eng.settings.qa_harness,
            eng.settings.loopforge_url,
            hashlib.sha256(tok_val.encode()).hexdigest(),
            hashlib.sha256(cookie.encode()).hexdigest(),
        )
        now = time.monotonic()
        if not refresh:
            cached = self.runner_rt_cache.get(cache_key)
            if cached and now - cached[0] < 3.0:
                return cached[1], cached[2]
        runners: list[dict[str, Any]] = []
        user_info: dict[str, Any] = {}
        client = LoopForgeClient(eng.settings.loopforge_url, tok_val)
        if tok_val or cookie:
            try:
                runners = await client.list_runners_with_cookie(cookie) if cookie else await client.list_runners()
            except LoopForgeError:
                runners = []
        try:
            if cookie:
                user_info = await client.auth_current_with_cookie(cookie)
            elif tok_val and any(isinstance(r, dict) and r.get("status") == "online" for r in runners):
                user_info = await client.auth_current()
        except LoopForgeError:
            user_info = {}
        owner = (user_info.get("user") or user_info) if user_info else {}
        login = owner.get("loginName") or owner.get("login")
        if cookie and login and eng.db.meta_get("source_mode") == "host_link":
            for device in eng.db._query(
                "SELECT clis,codex_login FROM host_links WHERE owner=? AND platform='win32' "
                "AND revoked_at IS NULL AND expires_at>unixepoch() AND baseline_hash IS NOT NULL AND seen>? AND clis!='[]'",
                (login, time.time() - 30),
            ):
                runners.append(
                    {
                        "name": f"{login} · Windows CLI",
                        "status": "online",
                        "runtimes": [
                            {
                                "provider": name,
                                "authMode": "local_cli",
                                "loginDetected": bool(device["codex_login"]) if name == "codex" else None,
                            }
                            for name in json.loads(device["clis"])
                        ],
                    }
                )
        merged, summary = merge_runner_runtimes(local_rt, eng.settings, runners=runners, user_info=user_info)
        creds = (owner.get("agentCliCredentials") or {}) if isinstance(owner, dict) else {}
        summary["personal_cli"] = {
            name: bool((creds.get(name) or {}).get("apiKeyConfigured"))
            for name in ("dsh", "opencode")
        }
        pool_info = (summary.get("pools") or {}).get("public_docker")
        if isinstance(pool_info, dict):
            from autodev import public_pool

            install_ready = bool(pool_info.get("image_ready") and pool_info.get("key_configured"))
            pool_ident = public_pool.identity()
            pool_uid = int(pool_ident.get("user_id") or 0)
            pool_login = str(pool_info.get("service_login") or "")
            identity_verified = False
            has_personal_runner = False
            pool_user_row: dict[str, Any] | None = None
            if pool_info.get("identity_configured") and (tok_val or cookie):
                try:
                    users = await client.list_users()
                    if pool_uid:
                        pool_user_row = next((u for u in users if isinstance(u, dict) and int(u.get("id") or 0) == pool_uid), None)
                    if pool_user_row is None and pool_login:
                        pool_user_row = next(
                            (u for u in users if isinstance(u, dict) and str(u.get("loginName") or u.get("login") or "") == pool_login),
                            None,
                        )
                    if pool_user_row and int(pool_user_row.get("id") or 0) > 0:
                        pool_uid = int(pool_user_row["id"])
                        has_personal_runner = any(
                            isinstance(r, dict) and int(r.get("ownerUserId") or 0) == pool_uid for r in runners
                        )
                        identity_verified = not public_pool.is_admin_user(pool_user_row) and not has_personal_runner
                except Exception:
                    pass
            authorized: bool | None = None
            can_authorize = False
            if eng.settings.loopforge_project_id and cookie and (pool_login or pool_uid):
                try:
                    u_obj, native_projects = await self.access_scope(cookie)
                    proj = native_projects.get(eng.settings.loopforge_project_id)
                    if isinstance(proj, dict):
                        uid = str((u_obj or {}).get("id") or "")
                        members = proj.get("members") or []
                        authorized = (
                            (pool_uid > 0 and str(proj.get("ownerId") or "") == str(pool_uid))
                            or (pool_login and str(proj.get("ownerName") or "") == pool_login)
                            or any(
                                m.get("role") != "viewer"
                                and (
                                    (pool_uid > 0 and str(m.get("userId") or "") == str(pool_uid))
                                    or (pool_login and str(m.get("loginName") or m.get("login") or "") == pool_login)
                                )
                                for m in members
                            )
                        )
                        can_authorize = bool(
                            (u_obj or {}).get("isPlatformAdmin") is True
                            or public_pool.is_admin_user(u_obj)
                            or (uid and uid == str(proj.get("ownerId")))
                            or (not proj.get("ownerId") and not members)
                            or any(uid and str(m.get("userId")) == uid and m.get("role") == "maintainer" for m in members)
                        )
                except Exception:
                    pass
            last_rows = eng.db._query(
                "SELECT task_id, state, lf_run_id, updated_at FROM dispatch_records "
                "WHERE pool='public_docker' ORDER BY dispatch_id DESC LIMIT 1"
            )
            last_run = dict(last_rows[0]) if last_rows else None
            last_smoke = None
            raw_smoke = eng.db.meta_get("public_pool.last_smoke")
            if raw_smoke:
                try:
                    last_smoke = json.loads(raw_smoke)
                except Exception:
                    last_smoke = None
            pool_info["install_ready"] = install_ready
            pool_info["service_user_id"] = pool_uid or None
            pool_info["identity_verified"] = identity_verified
            pool_info["has_personal_runner"] = has_personal_runner
            pool_info["project_authorized"] = authorized
            pool_info["can_authorize"] = can_authorize
            pool_info["project_ready"] = bool(
                install_ready and pool_info.get("identity_configured") and identity_verified and authorized is True
            )
            pool_info["last_sandbox_run"] = last_run
            pool_info["last_smoke"] = last_smoke
        self.runner_rt_cache[cache_key] = (now, merged, summary)
        return merged, summary


def get_ctx(request: Request) -> ServerContext:
    app = request.scope.get("app")
    if app is not None and hasattr(app, "state") and hasattr(app.state, "ctx"):
        return app.state.ctx
    assert _latest_ctx is not None
    return _latest_ctx


def require_engineering_role(slot, user, project, roles, *, manage=False):
    from autodev.governance.rbac import check_role_permission
    uid = str(user.get("id") or "")
    admin = user.get("isPlatformAdmin") is True
    owner = bool(uid and uid == str(project.get("ownerId")))
    member = next((m for m in (project.get("members") or []) if uid and str(m.get("userId")) == uid), {})
    native_manager = admin or owner or member.get("role") == "maintainer"
    configured = slot.engine.db._query("SELECT 1 FROM members WHERE project IN (?, '*') LIMIT 1", (slot.project_key,))
    if not native_manager and (member.get("role") == "viewer" or manage or not configured):
        raise HTTPException(403, "原生项目角色无权执行此操作")
    try:
        check_role_permission(slot.engine.db, current_user_var.get(), roles, project=slot.project_key)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc


def effective_actor(explicit_actor: str = "") -> str:
    login = current_user_var.get()
    if login:
        return login
    cleaned = (explicit_actor or "").strip()
    return cleaned if cleaned and cleaned != "owner" else (cleaned or "owner")
