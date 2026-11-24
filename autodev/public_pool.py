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

from autodev.loopforge.client import LoopForgeClient, LoopForgeError

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


async def provision(owner: LoopForgeClient) -> None:
    if service_token():
        return
    if owner.base_url.rstrip("/") != server_url():
        raise LoopForgeError("公用池账号仅可在当前部署签发")
    current = await owner.auth_current()
    if not (current.get("user") or current).get("id"):
        raise LoopForgeError("请先登录平台，再启用公用池")
    root = directory()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = root / "identity.json"
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

        def persist() -> None:
            with NamedTemporaryFile("w", dir=root, delete=False) as tmp:
                json.dump(data, tmp)
            Path(tmp.name).replace(path)
        persist()
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
            data["token"] = issued
            persist()


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
    # The service identity and its keys may only be sent to the deployment's control plane.
    if settings.loopforge_url.rstrip("/") != server_url() or not settings.loopforge_project_id:
        raise LoopForgeError("公用池需要绑定当前部署内的项目")
    await provision(owner)
    state = status()
    if not state["available"]:
        raise LoopForgeError("公用池未就绪，请检查沙箱镜像、公用账号和模型 Key")
    client = LoopForgeClient(server_url(), service_token())
    me, runners, projects = await asyncio.gather(client.auth_current(), client.list_runners(), client.list_projects())
    if runners:
        raise LoopForgeError("公用账号关联了个人执行机，请使用专用的无 Runner 账号")
    if not any(p.get("id") == settings.loopforge_project_id for p in projects):
        raise LoopForgeError("公用账号未获此项目权限，请在 LoopForge 项目成员中添加公用账号")
    user = me.get("user") or me
    owner_info = await owner.auth_current()
    owner_id = (owner_info.get("user") or owner_info).get("id")
    if not owner_id or str(user.get("id")) == str(owner_id) or user.get("isAdmin") or user.get("role") == "admin":
        raise LoopForgeError("请为公用额度配置独立的普通执行账号")
    if not user.get("id"):
        raise LoopForgeError("公用账号令牌无效")
    c = values()
    credentials = {}
    for name in state["engines"]:
        if not state["runtimes"][name]["ready"]:
            continue
        prefix = "deepseek" if c["deepseek_api_key"] else "openai"
        models = list(dict.fromkeys(x for x in (state["default_model"], state["reasoning_model"]) if x))
        expiry = (int(time.time()) // 3600 + 3) * 3600
        endpoint = os.getenv("VERNAL_SANDBOX_URL", "http://vernal:8787").rstrip("/")
        credentials[name] = {"apiKey": model_ticket(prefix, expiry),
                             "baseUrl": f"{endpoint}/vernal-api/pool-model/{prefix}/v1", "models": models}
    try:
        await client._req("PUT", f"/api/users/{quote(str(user['id']), safe='')}/credentials",
                          json_body={"agentCliCredentials": credentials})
        checked = await client.auth_current()
    except LoopForgeError as exc:
        raise LoopForgeError("LoopForge 无法保存或读取公用账号的模型凭据", exc.status_code) from None
    saved = (checked.get("user") or checked).get("agentCliCredentials") or {}
    if any(not isinstance(saved.get(k), dict) or not saved[k].get("apiKeyConfigured")
           or saved[k].get("baseUrl") != credentials[k]["baseUrl"] for k in credentials):
        raise LoopForgeError("LoopForge 未确认公用账号的模型凭据，已停止派单")
    client.issue_user_id = user["id"]
    return client


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
    return "public_docker", await client_for_project(settings, owner)


def context_url(settings) -> str:
    if getattr(settings, "selected_pool", "") == "public_docker":
        return os.getenv("VERNAL_SANDBOX_URL", "http://vernal:8787")
    return settings.vernal_public_url


def model_ticket(provider: str, expiry: int) -> str:
    # A ticket can only be redeemed for this provider and the configured pool models.
    signature = hmac.new(service_token().encode(), f"pool-model:{provider}:{expiry}".encode(), hashlib.sha256)
    return f"{expiry}.{signature.hexdigest()}"


def valid_ticket(provider: str, raw: str) -> bool:
    if provider not in ("deepseek", "openai") or not service_token():
        return False
    expiry = raw.partition(".")[0]
    if not expiry.isascii() or not expiry.isdecimal() or len(expiry) > 11:
        return False
    return time.time() < int(expiry) and hmac.compare_digest(raw.encode(), model_ticket(provider, int(expiry)).encode())


async def model_request(provider: str, operation: str, request):
    from fastapi import HTTPException
    from fastapi.responses import StreamingResponse
    from starlette.background import BackgroundTask

    auth = request.headers.get("authorization", "").split()
    if len(auth) != 2 or auth[0].lower() != "bearer" or not valid_ticket(provider, auth[1]):
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
    payload = await request.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("model"), str) or payload["model"] not in allowed:
        raise HTTPException(422, "所选模型不在管理员配置的公用模型列表中")
    base = config[provider + "_base_url"] or ("https://api.deepseek.com" if provider == "deepseek" else "https://api.openai.com/v1")
    client = httpx.AsyncClient(timeout=httpx.Timeout(300, connect=10), follow_redirects=False)
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
