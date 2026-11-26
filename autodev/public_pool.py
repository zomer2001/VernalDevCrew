"""Deployment-owned configuration for the shared LoopForge execution identity."""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import quote, urlsplit

import httpx

from autodev.loopforge.client import LoopForgeClient, LoopForgeError, create_async_http_client

FIELDS = {
    "deepseek_api_key": "DEEPSEEK_API_KEY", "deepseek_base_url": "DEEPSEEK_BASE_URL",
    "openai_api_key": "OPENAI_API_KEY", "openai_base_url": "OPENAI_BASE_URL",
    "default_model": "VERNAL_PUBLIC_DEFAULT_MODEL", "reasoning_model": "VERNAL_PUBLIC_REASONING_MODEL",
}
POOLS = ("auto", "local_runner", "public_docker")


def directory() -> Path:
    return Path(os.getenv("VERNAL_POOL_DIR") or Path(__file__).resolve().parents[1] / "deploy/loopforge/.runner/pool")


def values() -> dict[str, str]:
    config = {k: os.getenv(v, "") for k, v in FIELDS.items()}
    path = directory() / "settings.json"
    if path.exists():
        config.update(json.loads(path.read_text()))
    return config


def save(updates: dict[str, str]) -> None:
    root = directory()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    for key, val in updates.items():
        if key not in FIELDS or not isinstance(val, str) or len(val) > 8192:
            raise ValueError("无效的公用池配置字段")
        if val and key.endswith("base_url"):
            url = urlsplit(val)
            if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError("模型端点需要 HTTP(S) 地址且不能包含凭据或查询参数")
    with (root / "settings.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        file = root / "settings.json"
        config = json.loads(file.read_text()) if file.exists() else {}
        config.update({k: v.strip() for k, v in updates.items()})
        with NamedTemporaryFile("w", dir=root, delete=False) as tmp:
            json.dump(config, tmp)
        Path(tmp.name).replace(file)


def server_url() -> str:
    return os.getenv("VERNAL_LOOPFORGE_URL", "http://127.0.0.1:8090").rstrip("/")


def identity() -> dict:
    path = directory() / "identity.json"
    return json.loads(path.read_text()) if path.is_file() else {}


def service_token() -> str:
    return os.getenv("VERNAL_POOL_TOKEN", "").strip() or identity().get("token", "")


def is_admin_user(user: dict | None) -> bool:
    if not isinstance(user, dict):
        return False
    return bool(
        user.get("isPlatformAdmin") is True
        or user.get("isAdmin") is True
        or str(user.get("role") or "").lower() == "admin"
        or str(user.get("platformRole") or "").lower() == "admin"
    )


def _token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _persist_identity(data: dict) -> None:
    root = directory()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = root / "identity.json"
    with NamedTemporaryFile("w", dir=root, delete=False) as tmp:
        json.dump(data, tmp)
    os.chmod(tmp.name, 0o600)
    Path(tmp.name).replace(path)


async def _cookie_verify_pool_identity(login: str, password: str) -> dict:
    """Use the pool account's own cookie session to resolve its authoritative LoopForge user record."""
    async with httpx.AsyncClient(
        base_url=server_url(),
        headers={"Origin": server_url()},
        timeout=10,
        trust_env=False,
    ) as http:
        resp = await http.post("/api/auth/password", json={"login": login, "password": password})
        if resp.status_code >= 400:
            raise LoopForgeError("无法通过公用账号凭据核验其原生用户身份", resp.status_code)
        me_resp = await http.get("/api/auth/current")
        if me_resp.status_code >= 400:
            raise LoopForgeError("无法读取公用账号会话身份", me_resp.status_code)
        me = me_resp.json()
        user = me.get("user") if isinstance(me.get("user"), dict) else me
        if not isinstance(user, dict) or user.get("isGuest") or not int(user.get("id") or 0):
            raise LoopForgeError("公用账号登录后未返回有效用户 ID", 409)
        return user


async def resolve_pool_user(
    pool_client: LoopForgeClient,
    *,
    users: list[dict] | None = None,
) -> dict:
    """Resolve and verify the non-admin LoopForge user bound to the public pool token.

    LoopForge 0.4.0's GET /api/auth/current only inspects browser session cookies and returns
    id=0/isGuest=true for Bearer API tokens. We therefore bind user_id at token issuance
    (or self-heal once via the stored pool credentials) and verify it against /api/users.
    """
    tok = str(pool_client.token or "").strip()
    if not tok:
        raise LoopForgeError("公用账号令牌未配置", 401)

    directory_users = users
    if directory_users is None:
        try:
            directory_users = await pool_client.list_users()
        except LoopForgeError as exc:
            if exc.status_code in (401, 403):
                raise LoopForgeError("公用账号令牌无效或已失效", exc.status_code) from exc
            directory_users = []

    ident = identity()
    tok_hash = _token_sha256(tok)
    token_matches_ident = bool(
        ident
        and (
            ident.get("token") == tok
            or (ident.get("token_sha256") and hmac.compare_digest(str(ident["token_sha256"]), tok_hash))
        )
    )

    bound_uid = 0
    bound_login = ""
    fallback_user: dict | None = None

    if token_matches_ident:
        bound_uid = int(ident.get("user_id") or 0)
        bound_login = str(ident.get("login") or "")
        if not bound_uid and ident.get("login") and ident.get("password"):
            verified = await _cookie_verify_pool_identity(str(ident["login"]), str(ident["password"]))
            bound_uid = int(verified["id"])
            bound_login = str(verified.get("loginName") or verified.get("login") or ident["login"])
            updated = {**ident, "user_id": bound_uid, "token_sha256": tok_hash}
            try:
                _persist_identity(updated)
            except OSError:
                pass
            fallback_user = verified

    if not bound_uid:
        env_uid = os.getenv("VERNAL_POOL_USER_ID", "").strip()
        if env_uid.isascii() and env_uid.isdigit() and int(env_uid) > 0:
            bound_uid = int(env_uid)

    if not bound_uid:
        # Fallback for cookie sessions or test doubles where /api/auth/current resolves Bearer tokens.
        try:
            me = await pool_client.auth_current()
            cand = me.get("user") if isinstance(me.get("user"), dict) else me
            if isinstance(cand, dict) and not cand.get("isGuest") and int(cand.get("id") or 0) > 0:
                bound_uid = int(cand["id"])
                bound_login = str(cand.get("loginName") or cand.get("login") or "")
                fallback_user = cand
        except LoopForgeError:
            pass

    if not bound_uid and bound_login and directory_users:
        matched = next(
            (
                u for u in directory_users
                if isinstance(u, dict) and str(u.get("loginName") or u.get("login") or "") == bound_login
            ),
            None,
        )
        if matched and int(matched.get("id") or 0) > 0:
            bound_uid = int(matched["id"])

    if not bound_uid:
        raise LoopForgeError("请为公用额度配置独立的普通执行账号（未能确认公用令牌对应的原生用户 ID）", 409)

    live_user = next(
        (u for u in (directory_users or []) if isinstance(u, dict) and int(u.get("id") or 0) == bound_uid),
        None,
    )
    user = dict(live_user) if live_user else (dict(fallback_user) if fallback_user else {"id": bound_uid, "loginName": bound_login})
    if bound_login and user.get("loginName") and str(user["loginName"]) != bound_login:
        raise LoopForgeError("公用账号记录与原生用户目录不一致，已拒绝派单", 409)
    if is_admin_user(user):
        raise LoopForgeError("请为公用额度配置独立的普通执行账号", 409)
    return user


async def provision(owner: LoopForgeClient) -> None:
    if service_token():
        return
    if owner.base_url.rstrip("/") != server_url():
        raise LoopForgeError("公用池账号仅可在当前部署签发")
    from autodev.loopforge.client import SessionCookie

    if isinstance(owner.token, SessionCookie):
        current = await owner.auth_current()
        u = current.get("user") or current
        if not u.get("id") or u.get("isGuest"):
            raise LoopForgeError("请先登录平台，再启用公用池")
    else:
        current = await owner.auth_current()
        u = current.get("user") or current
        if not u.get("id") or u.get("isGuest"):
            # LoopForge 0.4.0 Bearer tokens do not populate /api/auth/current; verify liveness via /api/projects.
            await owner.list_projects()
    root = directory()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (root / "identity.lock").open("a") as lock:
        # Never block the event loop waiting for another provisioning coroutine.
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.1)
        if service_token():
            return
        data = identity() or {"login": "vernalpool" + secrets.token_hex(5), "password": secrets.token_urlsafe(32)}
        _persist_identity(data)
        auth = {k: data[k] for k in ("login", "password")}
        async with httpx.AsyncClient(base_url=server_url(), headers={"Origin": server_url()},
                                     timeout=10, trust_env=False) as client:
            login = await client.post("/api/auth/password", json=auth)
            if login.status_code in (400, 401, 403, 404):
                await client.post("/api/auth/register", json=auth)
                login = await client.post("/api/auth/password", json=auth)
            if login.status_code >= 400:
                raise LoopForgeError("未能创建公用执行账号。已关闭注册的部署可配置 VERNAL_POOL_TOKEN")
            token = await client.post("/api/auth/tokens", json={"name": "Vernal public Docker", "expiresInDays": 365})
            if token.status_code >= 400:
                raise LoopForgeError("无法为公用账号签发令牌")
            reply = token.json()
            issued = reply.get("token") or reply.get("plainToken") or reply.get("value")
            if not issued:
                raise LoopForgeError("公用账号令牌响应不完整")
            meta = reply.get("metadata") if isinstance(reply.get("metadata"), dict) else {}
            uid = int(meta.get("userId") or reply.get("userId") or 0)
            if not uid:
                me_resp = await client.get("/api/auth/current")
                if me_resp.status_code == 200:
                    me_json = me_resp.json()
                    me_user = me_json.get("user") if isinstance(me_json.get("user"), dict) else me_json
                    if isinstance(me_user, dict) and not me_user.get("isGuest"):
                        uid = int(me_user.get("id") or 0)
            data["token"] = issued
            data["token_sha256"] = _token_sha256(issued)
            if uid > 0:
                data["user_id"] = uid
            _persist_identity(data)


def status() -> dict:
    c = values()
    stamp = directory() / "image-ready"
    image_ready = stamp.is_file() and bool(stamp.read_text().strip())
    dk, ok = c["deepseek_api_key"], c["openai_api_key"]
    token = bool(service_token())
    configured = bool(dk or ok)
    ready = image_ready and token and configured
    return {
        "sandbox_image": os.getenv("VERNAL_SANDBOX_IMAGE", "autodev-runtime-sandbox:latest"),
        "image_ready": image_ready, "key_configured": configured, "identity_configured": token,
        "service_login": identity().get("login", ""), "available": ready, "engines": ["dsh", "opencode"],
        "deepseek_key_configured": bool(dk), "openai_key_configured": bool(ok),
        "deepseek_key_masked": ("****" + dk[-4:] if len(dk) >= 12 else "****") if dk else "",
        "openai_key_masked": ("****" + ok[-4:] if len(ok) >= 12 else "****") if ok else "",
        "deepseek_base_url": c["deepseek_base_url"], "openai_base_url": c["openai_base_url"],
        "default_model": c["default_model"] or ("deepseek-chat" if dk else ""),
        "reasoning_model": c["reasoning_model"] or ("deepseek-reasoner" if dk else ""),
        "runtimes": {name: {"label": name, "installed": image_ready, "ready": ready and bool(key),
                             "source": "public_docker", "needs_api_key": True, "api_key_configured": bool(key),
                             "note": "派单时校验公用账号的项目权限与凭据"}
                     for name, key in (("dsh", dk), ("opencode", dk or (ok and c["default_model"])))},
    }


def choose(settings, local: dict, public: dict) -> str:
    if settings.execution_pool != "auto":
        return settings.execution_pool
    roles = {getattr(settings, f"{role}_harness") for role in ("pm", "director", "coder", "qa")}
    explicit = {x for x in roles | {settings.harness} if x and x != "auto"}
    if explicit:
        candidates = [set(local.get(x, {}).get("runner_names", [])) if local.get(x, {}).get("ready") else set() for x in explicit]
        usable = bool(set.intersection(*candidates))
    else:
        usable = any(x.get("ready") for x in local.values())
    if not usable and public["available"] and all(public["runtimes"].get(x, {}).get("ready") for x in explicit):
        return "public_docker"
    return "local_runner"


async def client_for_project(settings, owner) -> LoopForgeClient:
    from autodev.loopforge.client import SessionCookie

    # The service identity and its keys may only be sent to the deployment's control plane.
    if settings.loopforge_url.rstrip("/") != server_url() or not settings.loopforge_project_id:
        raise LoopForgeError("公用池需要绑定当前部署内的项目")
    await provision(owner)
    state = status()
    if not state["available"]:
        raise LoopForgeError("公用池未就绪，请检查沙箱镜像、公用账号和模型 Key")
    pool_tok = service_token()
    if not isinstance(owner.token, SessionCookie) and str(owner.token or "").strip() == pool_tok:
        raise LoopForgeError("请为公用额度配置独立的普通执行账号")
    client = LoopForgeClient(server_url(), pool_tok)
    runners, projects = await asyncio.gather(client.list_runners(), client.list_projects())
    try:
        users = await client.list_users()
    except LoopForgeError as exc:
        if exc.status_code in (401, 403):
            raise LoopForgeError("公用账号令牌无效或已失效", exc.status_code) from exc
        users = []
    if not any(p.get("id") == settings.loopforge_project_id for p in projects):
        raise LoopForgeError("公用账号未获此项目权限，请在系统设置点击「一键授权当前项目」或在项目成员中添加公用账号")
    user = await resolve_pool_user(client, users=users)

    owner_info: dict = {}
    owner_id = 0
    if isinstance(owner.token, SessionCookie):
        owner_info = await owner.auth_current()
        ou = owner_info.get("user") or owner_info
        owner_id = int(ou.get("id") or 0) if not ou.get("isGuest") else 0
        if not owner_id:
            raise LoopForgeError("请先登录平台，再使用公用池派单", 401)
    else:
        try:
            owner_info = await owner.auth_current()
            ou = owner_info.get("user") or owner_info
            if isinstance(ou, dict) and not ou.get("isGuest"):
                owner_id = int(ou.get("id") or 0)
        except LoopForgeError:
            owner_info = {}
    if owner_id and str(user.get("id")) == str(owner_id):
        raise LoopForgeError("请为公用额度配置独立的普通执行账号")
    if is_admin_user(user) or not int(user.get("id") or 0):
        raise LoopForgeError("请为公用额度配置独立的普通执行账号")

    executor = int(settings.native_executor_user_id or user["id"])
    if settings.data_classification == "internal" or settings.native_executor_user_id:
        defaults = await client._req("GET", "/api/runners/default")
        if not isinstance(defaults, dict) or (defaults.get("cloudPool") or {}).get("configured") is not False:
            raise LoopForgeError("原生云执行池可能覆盖项目沙箱，无法证明出口受控", 409)
    if any(not r.get("ownerUserId") or int(r["ownerUserId"]) in {executor, int(user["id"])} for r in runners):
        raise LoopForgeError("公用池执行账号已关联个人执行机，无法保证本次原生派单进入公司沙箱", 409)
    project = next(p for p in projects if p.get("id") == settings.loopforge_project_id)
    if settings.native_executor_user_id or settings.native_approver_user_id or project.get("ownerId") or project.get("members"):
        writers = {str(project.get("ownerId"))} | {str(m.get("userId")) for m in (project.get("members") or []) if m.get("role") != "viewer"}
        if str(user["id"]) not in writers or str(executor) not in writers:
            raise LoopForgeError(f"请在原生项目成员页为执行人和公用账号 {identity().get('login', user['id'])} 授予可写成员身份", 403)
    if executor == int(user["id"]):
        await enroll_model_identity(client, user, state)
    elif owner_id and executor == int(owner_id):
        await enroll_model_identity(owner, owner_info.get("user") or owner_info, state)
    elif not (directory() / f"enrolled-{executor}").is_file():
        raise LoopForgeError("请由指定执行人登录并在公用池登记公司模型凭据", 409)
    client.issue_user_id = executor
    return client


def broker_key(uid, provider):
    return "vb1." + hmac.new(service_token().encode(), f"native-model-broker:{uid}:{provider}".encode(), hashlib.sha256).hexdigest()


async def enroll_model_identity(client, user, state):
    uid = int(user["id"])
    provider = "deepseek" if values()["deepseek_api_key"] else "openai"
    origin = os.getenv("VERNAL_SANDBOX_URL", "http://vernal:8787").rstrip("/")
    base = f"{origin}/vernal-api/pool-broker/{uid}/{provider}"
    desired_models = [{"id": x} for x in dict.fromkeys((state["default_model"], state["reasoning_model"])) if x]
    credentials = {}
    existing = user.get("agentCliCredentials") or {}
    for name in state["engines"]:
        if not state["runtimes"][name]["ready"]:
            continue
        previous = existing.get(name) or {}
        if previous.get("apiKeyConfigured"):
            if previous.get("baseUrl") == base and previous.get("protocol", "chat") == "chat":
                prev_ids = [m.get("id") if isinstance(m, dict) else m for m in (previous.get("models") or [])]
                want_ids = [m["id"] for m in desired_models]
                if prev_ids == want_ids:
                    continue
            else:
                raise LoopForgeError("执行账号已配置个人模型端点，请使用专用公司执行身份或在个人设置移除旧绑定", 409)
        credentials[name] = {
            "apiKey": broker_key(uid, provider),
            "baseUrl": base,
            "protocol": "chat",
            "models": desired_models,
        }
    if credentials:
        put_resp = await client._req("PUT", f"/api/users/{uid}/credentials", json_body={"agentCliCredentials": credentials})
        saved: dict = {}
        if isinstance(put_resp, dict):
            u_obj = put_resp.get("user") if isinstance(put_resp.get("user"), dict) else put_resp
            if isinstance(u_obj, dict) and isinstance(u_obj.get("agentCliCredentials"), dict):
                saved = u_obj["agentCliCredentials"]
        if not saved:
            try:
                all_users = await client.list_users()
                matched = next((u for u in all_users if isinstance(u, dict) and int(u.get("id") or 0) == uid), None)
                if matched and isinstance(matched.get("agentCliCredentials"), dict):
                    saved = matched["agentCliCredentials"]
            except LoopForgeError:
                pass
        if not saved:
            try:
                checked = await client.auth_current()
                cu = checked.get("user") if isinstance(checked.get("user"), dict) else checked
                if isinstance(cu, dict) and not cu.get("isGuest") and int(cu.get("id") or 0) == uid:
                    saved = cu.get("agentCliCredentials") or {}
            except LoopForgeError:
                pass
        if any(not saved.get(k, {}).get("apiKeyConfigured") or saved[k].get("baseUrl") != base for k in credentials):
            raise LoopForgeError("原生凭据登记未确认，已停止派单", 409)
    directory().mkdir(parents=True, exist_ok=True)
    (directory() / f"enrolled-{uid}").touch(mode=0o600)


def check_egress(settings, selected):
    if settings.data_classification != "internal":
        return
    trusted = set(os.environ.get("VERNAL_TRUSTED_RUNNERS", "").split(","))
    if not selected or selected not in trusted:
        raise LoopForgeError("internal 项目需要管理员确认出口受控的执行节点", 403)


async def check_workflow_sandbox(settings, client, workflow_id, pool):
    if pool != "public_docker" or settings.data_classification != "internal":
        return
    from urllib.parse import quote
    from autodev.loopforge.bridge import verify_context_sig
    import re

    project = settings.loopforge_project_id
    workflow = await client._req("GET", "/api/workflows/" + quote(workflow_id, safe=""))
    profiles = await client.list_sandboxes()
    sandbox = next((p for p in profiles if p["id"] == workflow.get("sandboxProfileId")), {})
    if (workflow.get("projectId") != project or sandbox.get("projectId") != project or
            sandbox.get("driver") != "docker" or sandbox.get("environmentId") or sandbox.get("environment")):
        raise LoopForgeError("原生 SOP 未使用该项目受控的公司沙箱", 403)
    sources = await client._req("GET", "/api/dockerfiles", params={"projectId":project})
    image = next((d for d in sources if d["id"] == sandbox.get("dockerfileId")), {})
    match = re.fullmatch(r"FROM ([\w./:@-]+)\n(?:ENTRYPOINT \[\]\n)?ENV VERNAL_NATIVE_PROJECT=([^\s]+) VERNAL_NATIVE_EXPIRY=(\d+) "
                         r"VERNAL_NATIVE_PROOF=([0-9a-f]+)\s*", image.get("content", ""))
    if (not match or image.get("projectId") != project or image.get("files") or match[2] != project
            or int(match[3]) <= time.time() or not verify_context_sig(
                settings.project_signing_key(),project,"model-sandbox:" + match[3],match[4])):
        raise LoopForgeError("原生 SOP 的沙箱配方或项目签名未通过校验", 403)
    check_egress(settings, "docker:" + match[1])


def select_runner(settings, runners, user):
    from autodev.server.project_launcher import merge_runner_runtimes
    roles = {settings.harness, settings.pm_harness, settings.director_harness,
             settings.coder_harness, settings.qa_harness} - {"", "auto"}
    for runner in sorted(runners, key=lambda r: not r.get("isDefault")):
        if runner.get("status") != "online" or not runner.get("id"):
            continue
        rows, _ = merge_runner_runtimes({}, settings, runners=[runner], user_info=user, include_pools=False)
        ready = {name for name, v in rows.items() if v.get("ready")}
        if ready and roles <= ready:
            return runner, rows
    return None, {}


async def resolve(settings, owner):
    """Choose one capable machine and the identity that owns this dispatch."""
    public = status()
    selected = settings.execution_pool
    if selected != "public_docker":
        runners = await owner.list_runners()
        online = [r for r in runners if isinstance(r, dict) and r.get("status") == "online"]
        user = {}
        if any(rt.get("provider") not in ("codex", "claude", "claude-code")
               for r in online for rt in r.get("runtimes", []) if isinstance(rt, dict)):
            try:
                user = await owner.auth_current()
            except LoopForgeError:
                pass
        runner, _ = select_runner(settings, online, user)
        if runner:
            if settings.data_classification == "internal":
                identity = await owner.auth_current()
                actor = (identity.get("user") or identity).get("id")
                if not actor or str(runner.get("ownerUserId")) != str(actor) or (
                        settings.native_executor_user_id and str(actor) != str(settings.native_executor_user_id)):
                    raise LoopForgeError("无法核验原生执行人与可信 Runner 的归属", 403)
                owner.issue_user_id = int(actor)
            check_egress(settings, str(runner["id"]))
            if not runner.get("isDefault"):
                await owner.set_default_runner(str(runner["id"]))
            return "local_runner", owner
        if selected == "local_runner" or not (public["key_configured"] and public["image_ready"]):
            if online:
                raise LoopForgeError("本机 Runner 在线，但没有一台机器具备本项目所需的 CLI 和凭据")
            names = "、".join(str(r.get("name") or r.get("id") or "") for r in runners)
            raise LoopForgeError(f"本机执行器当前离线（{names or '等待首次登录绑定'}），公用额度池未被选用")
    if public["image_ready"] and public["key_configured"] and not public["identity_configured"]:
        try:
            await provision(owner)
        except httpx.HTTPError:
            raise LoopForgeError("无法连接 LoopForge 以启用公用账号") from None
        public = status()
    if not public["available"]:
        raise LoopForgeError("公用池未就绪，请检查沙箱镜像、公用账号和模型 Key")
    roles = {settings.harness, settings.pm_harness, settings.director_harness,
             settings.coder_harness, settings.qa_harness} - {"", "auto"}
    if any(not public["runtimes"].get(h, {}).get("ready") for h in roles):
        raise LoopForgeError("公用池不支持当前角色执行器或对应 Key 未配置")
    check_egress(settings, "docker:" + public["sandbox_image"])
    return "public_docker", await client_for_project(settings, owner)


def context_url(settings) -> str:
    if getattr(settings, "selected_pool", "") == "public_docker":
        return os.getenv("VERNAL_SANDBOX_URL", "http://vernal:8787")
    return settings.vernal_public_url


def model_ticket(project_id, provider, expiry=None):
    if expiry is None:
        # Previous local single-workspace wire format.
        project_id, provider, expiry = "", project_id, provider
    message = f"pool-model:{provider}:{expiry}" if not project_id else json.dumps(
        ["vernal-pool-v2", project_id, provider, int(expiry)], separators=(",", ":"))
    signature = hmac.new(service_token().encode(), message.encode(), hashlib.sha256).hexdigest()
    return f"{expiry}.{signature}"


def valid_ticket(project_id, provider, raw=None):
    if raw is None:
        project_id, provider, raw = "", project_id, provider
    if provider not in ("deepseek", "openai") or not service_token():
        return False
    expiry = raw.partition(".")[0]
    if not expiry.isascii() or not expiry.isdecimal() or len(expiry) > 11:
        return False
    return time.time() < int(expiry) and hmac.compare_digest(raw.encode(), model_ticket(project_id, provider, int(expiry)).encode())


async def model_request(provider: str, operation: str, request, *, project_id="", payload=None):
    from fastapi import HTTPException
    from fastapi.responses import StreamingResponse
    from starlette.background import BackgroundTask

    auth = request.headers.get("authorization", "").split()
    if len(auth) != 2 or auth[0].lower() != "bearer" or not valid_ticket(project_id, provider, auth[1]):
        raise HTTPException(401, "公用池访问票据无效或已过期")
    config = values()
    key = config[provider + "_api_key"]
    if not key:
        raise HTTPException(503, "公用额度已停用")
    if (request.method, operation) not in (("POST", "chat/completions"), ("GET", "models")):
        raise HTTPException(404)
    models = [config["default_model"], config["reasoning_model"]]
    if provider == "deepseek":
        models = [models[0] or "deepseek-chat", models[1] or "deepseek-reasoner"]
    allowed = {name for name in models if name}
    if operation == "models":
        return {"object": "list", "data": [{"id": name, "object": "model"} for name in sorted(allowed)]}
    payload = await request.json() if payload is None else payload
    if not isinstance(payload, dict) or not isinstance(payload.get("model"), str) or payload["model"] not in allowed:
        raise HTTPException(422, "所选模型不在管理员配置的公用模型列表中")
    base = config[provider + "_base_url"] or ("https://api.deepseek.com" if provider == "deepseek" else "https://api.openai.com/v1")
    client = create_async_http_client(base, timeout=httpx.Timeout(300, connect=10), follow_redirects=False)
    try:
        outbound = client.build_request("POST", base.rstrip("/") + "/chat/completions",
                                        headers={"Authorization": f"Bearer {key}"}, json=payload)
        response = await client.send(outbound, stream=True)
    except httpx.HTTPError:
        await client.aclose()
        raise HTTPException(502, "公用模型端点无法连接") from None
    if response.status_code >= 300:
        await response.aclose()
        await client.aclose()
        raise HTTPException(response.status_code if response.status_code >= 400 else 502, "公用模型端点拒绝请求")
    async def close():
        await response.aclose()
        await client.aclose()
    return StreamingResponse(response.aiter_bytes(), media_type=response.headers.get("content-type", "application/json"),
                             background=BackgroundTask(close))
