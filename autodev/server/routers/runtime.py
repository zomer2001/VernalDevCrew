"""执行底座连接、执行池、智能体编队、Runner 接入与统一待办路由。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from autodev import gitops, local_git_bridge, public_pool
from autodev.config import SUPPORTED_HARNESSES, normalize_harness_name
from autodev.loopforge.bootstrap import ensure_vernal_loopforge_assets
from autodev.loopforge.bridge import LoopForgeBridge
from autodev.loopforge.client import run_project_id, LoopForgeClient, LoopForgeError
from autodev.server.deps import effective_actor, get_ctx, is_loopback, is_runner_launcher
from autodev.server.project_launcher import choose_preferred_harness_from_runtimes
from autodev.server.schemas import (
    AgentConfigRequest,
    CodeBuddyKeyRequest,
    PersonalCliKeyRequest,
    PublicPoolSmokeRequest,
    RunApproveRequest,
    RunRejectRequest,
    RuntimeConfigRequest,
)
from autodev.steering import approve_steering_checkpoint, reject_steering_checkpoint

router = APIRouter()


def _public_gitea_url(eng) -> str:
    host_url = os.getenv("VERNAL_GITEA_HOST_URL", "").strip()
    if host_url:
        return host_url.rstrip("/")
    raw = (eng.settings.gitea_url or "http://127.0.0.1:3000").rstrip("/")
    if urlsplit(raw).hostname == "gitea":
        return "http://127.0.0.1:3000"
    return raw


async def _require_pool_admin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and (urlsplit(origin).netloc != request.headers.get("host") or urlsplit(origin).scheme not in ("http", "https")):
        raise HTTPException(403, "请求来源不匹配")
    trusted_local = is_loopback(request) and request.url.hostname in ("localhost", "127.0.0.1", "::1", "testserver")
    if not trusted_local or os.environ.get("VERNAL_REQUIRE_AUTH") == "1":
        cookie = request.headers.get("cookie", "")
        if not cookie:
            raise HTTPException(401, "请先登录")
        try:
            me = await LoopForgeClient(public_pool.server_url()).auth_current_with_cookie(cookie)
        except LoopForgeError as exc:
            raise HTTPException(exc.status_code or 502, "无法核验平台管理员") from exc
        user = me.get("user") or me
        admins = {s.strip() for s in os.getenv("VERNAL_POOL_ADMINS", "").split(",") if s.strip()}
        if not (
            user.get("isPlatformAdmin") is True
            or user.get("platformRole") == "admin"
            or user.get("role") == "admin"
            or (user.get("loginName") or user.get("login")) in admins
        ):
            raise HTTPException(403, "只有部署管理员可以设置公用额度")


@router.get("/vernal-api/runtime/status")
async def api_runtime_status(request: Request) -> dict[str, Any]:
    ctx = get_ctx(request)
    slot = ctx.active_slot(request)
    eng = slot.engine
    client = ctx.lf_client(slot)
    if not getattr(request.state, "workspace_initialized", True):
        try:
            runners = await client.list_runners() if eng.settings.effective_loopforge_token() else []
        except LoopForgeError:
            runners = []
        online = [r for r in runners if r.get("status") == "online"]
        return {"workspace_initialized": False, "online_runners": online, "runners_online_count": len(online), "requested_project_id": request.state.requested_project_id,
                "loopforge": {"project_id": request.state.requested_project_id, "authenticated": True,
                              "reachable": True, "workflow_id": ""}, "needs_binding": True}
    lf_ok, lf_health = await client.health()
    gt_ok, gt_health = await client.gitea_health(eng.settings.gitea_url)
    has_token = bool(eng.settings.effective_loopforge_token())
    gitea_public = _public_gitea_url(eng)
    gitea_repo_url = ""
    if eng.db.meta_get("gitea_mirror") == "1" and eng.settings.loopforge_project_id:
        from urllib.parse import quote
        gitea_repo_url = f"{gitea_public}/vernal-local/{quote(eng.settings.loopforge_project_id, safe='')}"
    payload: dict[str, Any] = {
        "runtime_backend": eng.settings.runtime_backend,
        "collaboration_mode": eng.settings.collaboration_mode,
        "workspace_initialized": bool(getattr(request.state, "workspace_initialized", True)),
        "requested_project_id": str(getattr(request.state, "requested_project_id", "")),
        "loopforge": {
            "url": eng.settings.loopforge_url,
            "reachable": lf_ok,
            "authenticated": False,
            "has_token": has_token,
            "project_id": eng.settings.loopforge_project_id,
            "workflow_id": eng.settings.loopforge_workflow_id,
            "repository_id": eng.settings.loopforge_repository_id,
            "health": lf_health,
        },
        "gitea": {
            "url": gitea_public,
            "internal_url": eng.settings.gitea_url,
            "repo_url": gitea_repo_url,
            "admin_user": "vernal-local",
            "reachable": gt_ok,
            "health": gt_health,
        },
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
            allowed = getattr(request.state, "allowed_projects", None)
            payload["projects"] = [p for p in projects if allowed is None or p.get("id") in allowed]
            payload["workflows"] = [a for a in workflows if not a.get("projectId") or a["projectId"] == eng.settings.loopforge_project_id]
            payload["agents"] = [a for a in agents if not a.get("projectId") or a["projectId"] == eng.settings.loopforge_project_id]
            payload["sandboxes"] = [a for a in sandboxes if not a.get("projectId") or a["projectId"] == eng.settings.loopforge_project_id]
            payload["executors"] = [a for a in executors if not a.get("projectId") or a["projectId"] == eng.settings.loopforge_project_id]
            payload["runners"] = runners
            payload["online_runners"] = online_runners
            payload["runners_online_count"] = len(online_runners)
            payload["recent_runs"] = [r for r in runs if run_project_id(r) == eng.settings.loopforge_project_id][:25]
        except LoopForgeError as exc:
            payload["auth_error"] = str(exc)
    return payload


@router.get("/vernal-api/runner/health")
async def api_runner_health(request: Request, format: str = "json") -> Response:
    if not is_runner_launcher(request):
        raise HTTPException(status_code=403, detail="执行机心跳诊断接口仅限本机调用")
    eng = get_ctx(request).active_slot(request).engine
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


@router.api_route("/vernal-api/git-bridge/{project}/{repo}/{expires}/{sig}/{operation:path}", methods=["GET", "POST"], include_in_schema=False)
async def bridge_git_http(request: Request, project: str, repo: str, expires: int, sig: str, operation: str):
    ctx = get_ctx(request)
    return await local_git_bridge.relay(request, project, repo, expires, sig, operation, ctx.active_slot(request).engine)


@router.post("/vernal-api/candidate-upload/{project}/{expires}/{sig}", include_in_schema=False)
async def upload_large_candidate(request: Request, project: str, expires: int, sig: str) -> dict[str, Any]:
    import time
    from tempfile import NamedTemporaryFile
    from autodev.loopforge.bridge import verify_context_sig

    eng = get_ctx(request).active_slot(request).engine
    if eng.settings.loopforge_project_id not in ("", project):
        raise HTTPException(403, "候选回传项目归属不匹配")
    if expires <= int(time.time()) or not verify_context_sig(
        eng.settings.project_signing_key(), project, f"candidate-upload:{expires}", sig
    ):
        raise HTTPException(403, "候选回传签名无效或已过期")
    raw = await request.body()
    if not raw or len(raw) > 24 * 1024 * 1024:
        raise HTTPException(413, "候选包为空或超过 24 MiB 上限")
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(422, f"候选包不是合法 UTF-8 JSON：{exc}") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(422, "候选包顶层必须是 JSON 对象")
    digest = hashlib.sha256(raw).hexdigest()
    cand_dir = eng.ws.state_dir / "candidates"
    cand_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = cand_dir / f"{digest}.json"
    if not target.is_file():
        with NamedTemporaryFile("wb", dir=cand_dir, delete=False) as tmp:
            tmp.write(raw)
        os.chmod(tmp.name, 0o600)
        Path(tmp.name).replace(target)
    return {"vernal_ref": digest, "sha256": digest, "bytes": len(raw)}


def _smoke_test_active(eng) -> bool:
    import time
    raw = eng.db.meta_get("public_pool.smoke_active_until") or ""
    if not raw.isascii() or not raw.isdigit():
        return False
    return int(raw) > int(time.time())


@router.post("/vernal-api/pool-broker/{uid}/{provider}/exchange", include_in_schema=False)
async def exchange_run_ticket(uid: int, provider: str, request: Request):
    import hmac, time
    import re
    from autodev.loopforge.client import run_project_id
    token = request.headers.get("authorization", "").removeprefix("Bearer ")
    if provider not in ("deepseek","openai") or not public_pool.service_token() or not hmac.compare_digest(
            token.encode(), public_pool.broker_key(uid, provider).encode()):
        raise HTTPException(401, "无效的执行账号凭据")
    data = await request.json()
    run_id = data.get("run_id", "")
    if not run_id:
        from autodev.loopforge.bridge import verify_context_sig
        pid = data.get("project_id","")
        if not isinstance(pid,str) or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}",pid):
            raise HTTPException(403, "无效的原生沙箱项目")
        slot = get_ctx(request).find_or_open_slot(pid,create=False)
        expiry = str(data.get("expires",""))
        if not slot or slot.engine.settings.loopforge_project_id != pid or not expiry.isascii() or not expiry.isdigit() or len(expiry)>11:
            raise HTTPException(403, "无效的沙箱绑定")
        settings = slot.engine.settings
        if ((settings.execution_pool == "local_runner" and not _smoke_test_active(slot.engine))
                or int(expiry) <= time.time() or not verify_context_sig(
                settings.project_signing_key(),pid,"model-sandbox:"+expiry,str(data.get("proof","")))):
            raise HTTPException(403, "沙箱票据无效")
        native = LoopForgeClient(public_pool.server_url(), public_pool.service_token())
        projects = await native.list_projects()
        project = next((p for p in projects if p.get("id")==pid and not p.get("archivedAt")), {})
        executor = settings.native_executor_user_id
        if not executor:
            try:
                pool_user = await public_pool.resolve_pool_user(native)
                executor = int(pool_user["id"])
            except LoopForgeError as exc:
                raise HTTPException(exc.status_code or 403, str(exc)) from exc
        if uid != executor or (project.get("ownerId") != uid and not any(
                m.get("userId")==uid and m.get("role") != "viewer" for m in (project.get("members") or []))):
            raise HTTPException(403,"执行人没有项目公用池权限")
        return {"project_id":pid,"ticket":public_pool.model_ticket(pid,provider,min(int(expiry),int(time.time())+1800))}
    if not isinstance(run_id,str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}",run_id):
        raise HTTPException(422, "缺少原生 Run")
    try:
        run = await LoopForgeClient(public_pool.server_url(), public_pool.service_token()).get_run(run_id)
    except LoopForgeError as exc:
        raise HTTPException(exc.status_code or 503, "无法核验执行上下文") from exc
    pid = run_project_id(run)
    context = run.get("executionContext") or {}
    if not pid or run.get("id") != run_id or str(context.get("responsibleUserId")) != str(uid) or run.get("status") != "running":
        raise HTTPException(403, "Run 未绑定当前执行账号")
    slot = get_ctx(request).find_or_open_slot(pid, create=False)
    if (not slot or slot.engine.settings.loopforge_project_id != pid
            or (slot.engine.settings.execution_pool == "local_runner" and not _smoke_test_active(slot.engine))):
        raise HTTPException(403, "当前项目未启用公司模型")
    sandbox = (run.get("workflowSnapshot") or {}).get("sandboxProfileId") or ""
    if context.get("executionMode") != "cloud" or context.get("executionRunnerId") or not sandbox.startswith(pid + "-vernal-docker-"):
        raise HTTPException(403, "Run 未使用项目公司沙箱")
    return {"project_id": pid, "ticket": public_pool.model_ticket(pid, provider, int(time.time()) + 1800)}


@router.api_route("/vernal-api/pool-model/{provider}/v1/{operation:path}", methods=["GET", "POST"], include_in_schema=False)
@router.api_route("/vernal-api/pool-model/{project_id}/{provider}/v1/{operation:path}", methods=["GET", "POST"], include_in_schema=False)
async def pool_model(provider: str, operation: str, request: Request, project_id: str = ""):
    from autodev.governance import enforce_model_gateway_policy, platform_db_path_for, record_model_gateway_call, scrub_sensitive_text
    ctx = get_ctx(request)
    # FastAPI treats an unused path parameter as a query field on the legacy route.
    project_id = request.path_params.get("project_id", "")
    auth = request.headers.get("authorization","").split()
    if len(auth) != 2 or auth[0].lower() != "bearer" or not public_pool.valid_ticket(project_id, provider, auth[1]):
        raise HTTPException(401, "模型票据无效")
    if project_id:
        slot = ctx.find_or_open_slot(project_id, create=False)
        if not slot or slot.engine.settings.loopforge_project_id != project_id:
            raise HTTPException(404, "未知原生项目")
    else:
        if not is_loopback(request) or os.getenv("VERNAL_LEGACY_POOL_MODEL","1") != "1" or len(ctx.all_slots) != 1 or any(
            key in request.query_params for key in ("project","project_id")) or "x-vernal-project" in request.headers:
            raise HTTPException(403, "旧票据仅限本机单项目")
        slot = ctx.initial_slot
        if any(p.is_file() and p.resolve() != slot.engine.db.path.resolve()
               for p in ctx.workspaces_parent().glob("*/.autodev/state.db")):
            raise HTTPException(403, "多项目部署需要项目票据")
    eng = slot.engine
    pid = eng.settings.loopforge_project_id or eng.ws.root.name
    cfg = public_pool.values()
    endpoint = cfg.get(provider + "_base_url") or ("https://api.deepseek.com" if provider == "deepseek" else "https://api.openai.com/v1")
    payload = await request.json() if request.method == "POST" else {}
    if not isinstance(payload, dict):
        raise HTTPException(422, "需要 JSON 对象")
    model = payload.get("model") or ""
    pdb = platform_db_path_for(ctx.workspaces_parent())
    try:
        enforce_model_gateway_policy(eng, provider, endpoint, model_name=model, platform_db_path=pdb)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    def redact(obj):
        if isinstance(obj, str):
            return scrub_sensitive_text(obj)
        if isinstance(obj, list):
            return [redact(item) for item in obj]
        if isinstance(obj, dict):
            return {k:redact(v) for k,v in obj.items()}
        return obj
    payload = redact(payload)
    limit = eng.settings.token_budget_limit
    reservation = None
    if limit and operation == "chat/completions":
        import uuid
        cap = payload.get("max_completion_tokens", payload.get("max_tokens", 1024))
        if type(cap) is not int or cap <= 0:
            raise HTTPException(422, "输出预算无效")
        # UTF-8 bytes provide a conservative upper bound on ordinary text tokenization.
        count = payload.get("n", 1)
        if type(count) is not int or count < 1:
            raise HTTPException(422, "候选回答数量无效")
        if any(isinstance(m,dict) and isinstance(m.get("content"),list) and
               any(not isinstance(part,dict) or part.get("type") != "text" for part in m["content"])
               for m in payload.get("messages",[]) or []):
            raise HTTPException(422, "当前项目预算仅支持可计量的文本输入")
        cost = len(json.dumps(payload, ensure_ascii=False).encode()) + cap * count
        reservation = uuid.uuid4().hex
        with eng.db.transaction():
            held, settled = eng.db._query("SELECT COALESCE(SUM(CASE WHEN finalized=0 THEN tokens END),0),"
                " COALESCE(SUM(CASE WHEN finalized=1 THEN tokens END),0) FROM model_budget_reservations")[0]
            native = eng.db._query("SELECT COALESCE(SUM(input_tokens+output_tokens),0) n FROM task_runs")[0]["n"]
            baseline = eng.db.meta_get("model_budget_native_baseline")
            if baseline is None:
                baseline = str(native)
                eng.db.meta_set("model_budget_native_baseline", baseline)
            if max(int(baseline or 0) + settled, native) + held + cost > limit:
                raise HTTPException(429, "项目模型预算不足")
            eng.db._exec("INSERT INTO model_budget_reservations(request_id,tokens) VALUES(?,?)",(reservation,cost))
        if "max_completion_tokens" not in payload:
            payload["max_tokens"] = cap
    try:
        resp = await public_pool.model_request(provider, operation, request, project_id=project_id, payload=payload)
    except HTTPException as exc:
        if reservation:
            if exc.status_code in (400,401,403,404,422,429,503):
                eng.db._exec("DELETE FROM model_budget_reservations WHERE request_id=?", (reservation,))
            else:
                eng.db._exec("UPDATE model_budget_reservations SET finalized=1 WHERE request_id=?", (reservation,))
        raise
    if reservation:
        from autodev.governance.gateway import metered_response
        metered_response(resp, eng.db, reservation)
    record_model_gateway_call(pdb, project=pid, provider=provider, model=model or "-", status="ALLOWED")
    return resp


@router.post("/vernal-api/runtime/public-pool/enroll")
async def enroll_personal_company_provider(request: Request):
    from autodev.loopforge.client import SessionCookie
    cookie = request.headers.get("cookie","")
    ctx = get_ctx(request)
    user, projects = await ctx.access_scope(cookie)
    if ctx.active_slot(request).project_key not in projects:
        raise HTTPException(403, "需要本项目成员身份")
    client = LoopForgeClient(public_pool.server_url(), SessionCookie(cookie))
    me = await client.auth_current()
    try:
        await public_pool.enroll_model_identity(client, me.get("user") or me, public_pool.status())
    except LoopForgeError as exc:
        raise HTTPException(exc.status_code or 503, str(exc)) from exc
    return {"enrolled": True, "user_id": user["id"]}


@router.get("/vernal-api/runtime/public-pool")
async def public_pool_state(format: str = "json"):
    pool = public_pool.status()
    if format == "doctor":
        engines = "、".join(k for k, v in pool["runtimes"].items() if v["ready"])
        return Response(
            f"公用池 Key {'已配置' if pool['key_configured'] else '未配置'}，"
            f"专用账号 {'已配置' if pool['identity_configured'] else '待启用'}，"
            f"可选执行器 {engines or '暂无'}",
            media_type="text/plain",
        )
    return pool


@router.post("/vernal-api/runtime/public-pool")
async def update_public_pool(request: Request, updates: dict[str, str]) -> dict:
    ctx = get_ctx(request)
    slot = ctx.active_slot(request)
    eng = slot.engine
    await _require_pool_admin(request)
    try:
        public_pool.save(updates)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    ctx.runner_rt_cache.clear()
    result = public_pool.status()
    if result["key_configured"] and not result["identity_configured"] and eng.settings.effective_loopforge_token():
        try:
            await public_pool.provision(ctx.lf_client(slot))
            result = public_pool.status()
        except (LoopForgeError, httpx.HTTPError) as exc:
            result["setup_message"] = str(exc)
    return result


@router.post("/vernal-api/runtime/gitea/credentials")
async def api_gitea_credentials(request: Request) -> JSONResponse:
    """管理员受控读取本机内置 Gitea 管理员账号密码，省去终端查找密码文件。"""
    await _require_pool_admin(request)
    eng = get_ctx(request).active_slot(request).engine
    pw_file = local_git_bridge.password_path()
    if not pw_file.is_file():
        raise HTTPException(404, "内置 Gitea 密码尚未生成，请先运行 ./vernal.sh 启动内置 Gitea")
    password = pw_file.read_text("utf-8").strip()
    if not password:
        raise HTTPException(404, "内置 Gitea 密码文件为空")
    gitea_url = _public_gitea_url(eng)
    return JSONResponse(
        {
            "url": gitea_url,
            "login_url": f"{gitea_url}/user/login",
            "username": "vernal-local",
            "password": password,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/vernal-api/runtime/public-pool/smoke-test")
async def api_public_pool_smoke_test(request: Request, req: PublicPoolSmokeRequest) -> dict[str, Any]:
    """用当前项目走正式公用 Docker 沙箱派单链路做一次轻量真试跑，返回逐项可核验证据。"""
    import copy
    from autodev.db import now_iso
    from autodev.harnesses.loopforge_harness import LoopForgeHarness
    from autodev.loopforge.bootstrap import plan_workflow_id

    ctx = get_ctx(request)
    slot = ctx.active_slot(request)
    eng = slot.engine
    harness = normalize_harness_name(req.harness or "dsh")
    if harness not in ("dsh", "opencode"):
        raise HTTPException(422, "公用 Docker 沙箱仅支持试跑 dsh 或 opencode")
    pid = eng.settings.loopforge_project_id
    if eng.settings.runtime_backend != "loopforge" or not pid:
        raise HTTPException(400, "当前项目尚未绑定原生 LoopForge Project")

    probe_settings = copy.deepcopy(eng.settings)
    probe_settings.execution_pool = "public_docker"
    probe_settings.selected_pool = "public_docker"
    probe_settings.harness = harness
    probe_settings.agent_timeout_sec = min(int(probe_settings.agent_timeout_sec or 300), 240)
    for f in (
        "model", "pm_harness", "pm_model", "director_harness", "director_model",
        "planner_model", "coder_harness", "coder_model", "qa_harness", "qa_model", "reviewer_model",
    ):
        setattr(probe_settings, f, "")

    owner_client = ctx.lf_client(slot)
    model_name = probe_settings.model_for("director") or public_pool.status().get("default_model") or ""
    steps: list[dict[str, Any]] = []
    run_id = ""
    sandbox_id = ""

    def _record_result(ok: bool, err: str = "") -> dict[str, Any]:
        summary = {
            "ok": ok,
            "harness": harness,
            "model": model_name,
            "run_id": run_id,
            "sandbox_id": sandbox_id,
            "steps": steps,
            "error": err,
            "tested_at": now_iso(),
        }
        eng.db.meta_set("public_pool.last_smoke", json.dumps(summary, ensure_ascii=False))
        eng.db.add_event(
            "public_pool.smoke_tested",
            f"公用 Docker 沙箱试跑（{harness}/{model_name}）：{'通过' if ok else '失败 · ' + err}" + (f" · Run {run_id}" if run_id else ""),
            actor=effective_actor(),
        )
        ctx.runner_rt_cache.clear()
        return summary

    # Step 1: 公用账号身份 + 项目授权 + 经纪人凭据登记
    try:
        shared_client = await public_pool.client_for_project(probe_settings, owner_client)
        steps.append({"key": "authorize", "label": "项目授权与公用账号凭据", "ok": True, "detail": f"公用执行人 #{shared_client.issue_user_id} 已通过权限与凭据核验"})
    except (LoopForgeError, httpx.HTTPError) as exc:
        steps.append({"key": "authorize", "label": "项目授权与公用账号凭据", "ok": False, "detail": str(exc)})
        return _record_result(False, str(exc))

    # Step 2-4: 同步沙箱模板、发起轻量真实试跑、核验隔离；结束后清理临时试跑票据并恢复项目原工作流
    import time as _time
    from autodev.server import app as app_mod

    synced_probe_assets = False
    eng.db.meta_set("public_pool.smoke_active_until", str(int(_time.time()) + int(probe_settings.agent_timeout_sec) + 30))
    try:
        try:
            await app_mod.ensure_vernal_loopforge_assets(owner_client, pid, probe_settings)
            synced_probe_assets = True
            wf_id = plan_workflow_id(pid, "director")
            await public_pool.check_workflow_sandbox(probe_settings, owner_client, wf_id, "public_docker")
            steps.append({"key": "sandbox_sync", "label": "项目 Docker 沙箱与工作流同步", "ok": True, "detail": f"已同步 {wf_id}（{harness}/{model_name}）"})
        except (LoopForgeError, httpx.HTTPError) as exc:
            steps.append({"key": "sandbox_sync", "label": "项目 Docker 沙箱与工作流同步", "ok": False, "detail": str(exc)})
            return _record_result(False, str(exc))

        # Step 3: 派发轻量真实沙箱 Run 并等待模型响应与产物回传
        smoke_dir = eng.ws.runs_dir / f"_smoke_{harness}"
        smoke_dir.mkdir(parents=True, exist_ok=True)
        schema_path = smoke_dir / "smoke_schema.json"
        schema_path.write_text(
            json.dumps(
                {
                    "type": "object",
                    "properties": {
                        "ok": {"type": "boolean"},
                        "harness": {"type": "string"},
                        "marker": {"type": "string"},
                    },
                    "required": ["ok", "harness", "marker"],
                    "additionalProperties": False,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        prompt = (
            f"这是一次 Vernal 公用 Docker 沙箱连通性自检（时间戳 {now_iso()}）。"
            f"请不要扫描无关文件，直接输出合法 JSON 到 vernal-result.json："
            f'{{"ok": true, "harness": "{harness}", "marker": "vernal-smoke-ok"}}。'
        )
        lf_harness = LoopForgeHarness(probe_settings, eng.db, eng.ws, client=owner_client, poll_interval=1.0)
        res = await lf_harness.run_structured_readonly(
            prompt, schema_path=schema_path, cwd=eng.ws.root, log_dir=smoke_dir, role="director"
        )
        run_id = str(res.session_id or "")
        if not res.ok:
            steps.append({"key": "model_run", "label": "沙箱启动与模型响应", "ok": False, "detail": res.error or "试跑失败"})
            return _record_result(False, res.error or "沙箱试跑未通过")
        steps.append({"key": "model_run", "label": "沙箱启动与模型响应", "ok": True, "detail": f"已收到结构化产物回传（耗时 {res.duration_sec:.1f}s）"})

        # Step 4: 回查原生 Run，核验确实跑在项目 Docker 沙箱且未回落到本机 Runner
        try:
            run_obj = await shared_client.get_run(run_id) if run_id else {}
            ectx = run_obj.get("executionContext") or {}
            sandbox_id = str((run_obj.get("workflowSnapshot") or {}).get("sandboxProfileId") or "")
            in_cloud = ectx.get("executionMode") == "cloud" and not ectx.get("executionRunnerId")
            in_proj_sb = sandbox_id.startswith(pid + "-vernal-docker-")
            ok_artifact = isinstance(res.structured, dict) and res.structured.get("ok") is True
            if in_cloud and in_proj_sb and ok_artifact:
                steps.append({"key": "verify_isolation", "label": "隔离沙箱与交付核验", "ok": True, "detail": f"确认运行于 {sandbox_id}（未绑定本机 Runner）"})
                return _record_result(True)
            detail = f"隔离校验异常：mode={ectx.get('executionMode')}, runner={ectx.get('executionRunnerId') or '-'}, sandbox={sandbox_id or '-'}"
            steps.append({"key": "verify_isolation", "label": "隔离沙箱与交付核验", "ok": False, "detail": detail})
            return _record_result(False, detail)
        except (LoopForgeError, httpx.HTTPError) as exc:
            steps.append({"key": "verify_isolation", "label": "隔离沙箱与交付核验", "ok": False, "detail": str(exc)})
            return _record_result(False, str(exc))
    finally:
        eng.db.meta_set("public_pool.smoke_active_until", None)
        if synced_probe_assets and (
            eng.settings.execution_pool != probe_settings.execution_pool
            or eng.settings.harness != probe_settings.harness
        ):
            try:
                await app_mod.ensure_vernal_loopforge_assets(owner_client, pid, eng.settings)
            except (LoopForgeError, httpx.HTTPError):
                pass


@router.post("/vernal-api/runtime/public-pool/authorize-project")
async def authorize_public_pool_project(request: Request) -> dict[str, Any]:
    from autodev.loopforge.client import SessionCookie
    from autodev.server.deps import require_engineering_role

    ctx = get_ctx(request)
    slot = ctx.active_slot(request)
    eng = slot.engine
    pid = eng.settings.loopforge_project_id
    if not pid:
        raise HTTPException(400, "当前项目尚未绑定原生 Project")
    cookie = request.headers.get("cookie", "")
    if not cookie:
        raise HTTPException(401, "请先登录后再授权项目成员")
    user, projects = await ctx.access_scope(cookie)
    project = projects.get(pid)
    if not project:
        raise HTTPException(403, "无权访问当前原生项目")
    require_engineering_role(slot, user, project, ("admin",), manage=True)
    caller_client = LoopForgeClient(public_pool.server_url(), SessionCookie(cookie))
    try:
        await public_pool.provision(caller_client)
        pool_client = LoopForgeClient(public_pool.server_url(), public_pool.service_token())
        pool_user = await public_pool.resolve_pool_user(pool_client)
    except (LoopForgeError, httpx.HTTPError) as exc:
        raise HTTPException(getattr(exc, "status_code", None) or 502, f"无法准备公用执行账号：{exc}") from exc
    pool_uid = int(pool_user.get("id") or 0)
    pool_login = str(pool_user.get("loginName") or pool_user.get("login") or public_pool.identity().get("login") or "")
    if not pool_uid or public_pool.is_admin_user(pool_user):
        raise HTTPException(409, "公用执行身份必须是原生普通账号")
    members = list(project.get("members") or [])
    already = str(project.get("ownerId")) == str(pool_uid) or any(
        str(m.get("userId")) == str(pool_uid) and m.get("role") != "viewer" for m in members
    )
    if not already:
        updated_members = [m for m in members if str(m.get("userId")) != str(pool_uid)]
        updated_members.append({"userId": pool_uid, "role": "member"})
        # For historical ownerless projects, keep the authorizing user as maintainer so they never lock themselves out.
        caller_uid = int(user.get("id") or 0)
        if not project.get("ownerId") and caller_uid and caller_uid != pool_uid and not any(
            str(m.get("userId")) == str(caller_uid) for m in updated_members
        ):
            updated_members.append({"userId": caller_uid, "role": "maintainer"})
        try:
            await caller_client.put_project({**project, "members": updated_members})
            refreshed = next((p for p in await caller_client.list_projects() if p.get("id") == pid), None)
        except LoopForgeError as exc:
            raise HTTPException(exc.status_code or 502, f"授权公用账号加入项目失败：{exc}") from exc
        if refreshed is not None:
            confirmed = str(refreshed.get("ownerId")) == str(pool_uid) or any(
                str(m.get("userId")) == str(pool_uid) and m.get("role") != "viewer"
                for m in (refreshed.get("members") or [])
            )
            if not confirmed:
                raise HTTPException(502, "LoopForge 未确认公用账号成员写入，请刷新后重试")
    ctx.proj_perm_cache.clear()
    ctx.runner_rt_cache.clear()
    return {"ok": True, "project_id": pid, "service_login": pool_login, "service_user_id": pool_uid, "project_authorized": True, "already": already}


@router.post("/vernal-api/runtime/config")
async def api_runtime_config(request: Request, req: RuntimeConfigRequest) -> dict[str, Any]:
    ctx = get_ctx(request)
    slot = ctx.active_slot(request)
    eng = slot.engine
    if req.runtime_backend is not None and req.runtime_backend not in ("local", "loopforge"):
        raise HTTPException(422, "未知的执行底座")
    if req.runtime_backend == "local" and eng.settings.runtime_backend != "local" and (
            not is_loopback(request) or hasattr(request.state, "allowed_projects")):
        raise HTTPException(403, "本地调试执行仅限部署主机配置")
    if req.loopforge_url is not None and req.loopforge_url.strip().rstrip("/") != public_pool.server_url():
        raise HTTPException(403, "认证服务地址由部署配置固定")
    if req.loopforge_token is not None and (not is_loopback(request) or request.headers.get("cookie")):
        raise HTTPException(403, "服务凭据由部署管理")
    if req.loopforge_project_id is not None:
        pid = req.loopforge_project_id.strip()
        cookie = request.headers.get("cookie", "")
        if cookie or not is_loopback(request):
            await ctx.verify_project_access(cookie, public_pool.server_url(), pid)
    if req.collaboration_mode is not None and req.collaboration_mode != eng.settings.collaboration_mode:
        if eng.db.meta_get("source_mode") in ("host_link", "local_folder"):
            raise HTTPException(409, "本机目录的交付位置在接入时确定，请从启动台选择新的项目工作区")
        if req.collaboration_mode not in ("git", "files"):
            raise HTTPException(422, "未知的协作方式")
        if ctx.is_planning(slot) or ctx.is_scheduler_running(slot) or eng.db.list_milestones():
            raise HTTPException(409, "请在立项前选择协作方式，已有任务需要沿用原有交付记录")
        if req.collaboration_mode == "git":
            await asyncio.to_thread(gitops.ensure_repo, eng.ws.root, eng.settings.base_branch)
    if req.loopforge_project_id is not None:
        ctx.bind_slot(slot, pid)
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
            setattr(eng.settings, field, val.strip())
    if eng.settings.runtime_backend == "loopforge" and eng.settings.effective_loopforge_token() and eng.settings.loopforge_project_id:
        from autodev.server import app as app_mod

        try:
            await app_mod.ensure_vernal_loopforge_assets(ctx.lf_client(slot), eng.settings.loopforge_project_id, eng.settings)
        except LoopForgeError:
            pass
    ctx.persist_config(eng)
    eng.reset_harness()
    eng.db.add_event(
        "runtime.configured",
        f"运行底座配置已更新（backend={eng.settings.runtime_backend}, workflow={eng.settings.loopforge_workflow_id or '-'}）",
        actor=effective_actor(),
    )
    return {"ok": True, "runtime_backend": eng.settings.runtime_backend, "workflow_id": eng.settings.loopforge_workflow_id}


@router.post("/vernal-api/runtime/bind")
async def api_runtime_bind(request: Request) -> dict[str, Any]:
    ctx = get_ctx(request)
    slot = ctx.active_slot(request)
    eng = slot.engine
    cookie = request.headers.get("cookie", "")
    client = ctx.lf_client(slot)
    try:
        me = await client.auth_current_with_cookie(cookie)
    except LoopForgeError as exc:
        raise HTTPException(status_code=exc.status_code or 502, detail=str(exc)) from exc
    user = me.get("user") if isinstance(me.get("user"), dict) else me
    login = str((user or {}).get("loginName") or (user or {}).get("login") or "")
    if not login or (user or {}).get("isGuest"):
        raise HTTPException(status_code=401, detail="尚未登录平台，请先登录后再绑定")
    if cookie:
        _, native = await ctx.access_scope(cookie)
        if eng.settings.loopforge_project_id:
            await ctx.verify_project_access(cookie, public_pool.server_url(), eng.settings.loopforge_project_id)
        elif native:
            ctx.bind_slot(slot, next(iter(native)))
        else:
            raise HTTPException(403, "请先创建原生项目")
    previously_bound = eng.db.meta_get("runtime_bound") == "1"
    try:
        assets = await LoopForgeBridge(eng.settings, eng.db).ensure_project_and_assets()
    except LoopForgeError as exc:
        raise HTTPException(exc.status_code or 502, str(exc)) from exc
    ctx.bind_slot(slot, str(assets["project_id"]))
    eng.settings.runtime_backend = "loopforge"
    eng.reset_harness()
    ctx.persist_config(eng)
    eng.db.meta_set("runtime_bound", "1")
    return {"bound": True, "reused": previously_bound, "login": login,
            "project_id": assets["project_id"], "workflow_id": assets["selected_workflow_id"]}


@router.get("/vernal-api/runner/installer")
def runner_installer(component: str = "runner") -> FileResponse:
    scripts = {"runner": "start-runner.sh", "binder": "wait-and-bind-runner.sh"}
    if component not in scripts:
        raise HTTPException(404, "未知的 Runner 组件")
    script = Path(__file__).resolve().parents[3] / "deploy/loopforge" / scripts[component]
    return FileResponse(script, media_type="text/plain")


@router.post("/vernal-api/runner/connect")
async def runner_connect(request: Request) -> dict:
    ctx = get_ctx(request)
    cookie = request.headers.get("cookie", "")
    lf = public_pool.server_url()
    login = await ctx.verify_cookie_user(cookie, lf)
    origin = request.headers.get("origin") or str(request.base_url).rstrip("/")
    if urlsplit(origin).scheme not in ("https", "http") or urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "浏览器来源不匹配")
    async with httpx.AsyncClient(base_url=lf, trust_env=False, timeout=10) as client:
        resp = await client.post("/api/runners/bind-codes", headers={"Cookie": cookie, "Origin": lf}, json={})
    if resp.status_code >= 400 or not (code := resp.json().get("code")):
        raise HTTPException(502, "无法签发个人执行机绑定码")
    key = hashlib.sha256(f"{origin}:{login}".encode()).hexdigest()[:16]
    target = '"$HOME/.local/share/vernal/runners/' + key + '"'
    from autodev.loopforge.bridge import sign_context_sig
    from urllib.parse import urlencode
    import time
    url = shlex.quote(origin.rstrip("/"))
    eng = ctx.active_slot(request).engine
    expires = int(time.time()) + 600
    def installer(component):
        pid = eng.settings.loopforge_project_id
        sig = sign_context_sig(eng.settings.project_signing_key(), pid, f"installer:{component}:{expires}")
        query = urlencode({"project": pid, "component": component, "expires": expires, "sig": sig})
        return shlex.quote(f"{origin.rstrip('/')}/vernal-api/runner/installer?{query}")
    cmd = (
        f"(umask 077 && mkdir -p {target} && cd {target} && curl -fsS "
        f"{installer('runner')} -o start-runner.sh && "
        f"curl -fsS {installer('binder')} -o wait-and-bind-runner.sh && "
        f"chmod 700 start-runner.sh wait-and-bind-runner.sh && "
        f"VERNAL_URL={url} LOOPFORGE_URL={url} VERNAL_BIND_CODE={shlex.quote(code)} ./start-runner.sh)"
    )
    return {"command": cmd}


@router.post("/vernal-api/runner/bind-code")
async def api_runner_bind_code(request: Request, format: str = "json") -> Response:
    if not is_runner_launcher(request):
        raise HTTPException(status_code=403, detail="执行机自动绑定码仅限宿主机本机调用")
    eng = get_ctx(request).active_slot(request).engine
    token = eng.settings.effective_loopforge_token()
    if not token:
        raise HTTPException(status_code=401, detail="当前项目尚未绑定登录态，请先在浏览器登录 http://127.0.0.1:8787")
    client = LoopForgeClient(eng.settings.loopforge_url, token)
    try:
        res = await client.create_runner_bind_code()
    except LoopForgeError as exc:
        raise HTTPException(status_code=exc.status_code or 502, detail=str(exc)) from exc
    code = str(res.get("code") or "")
    if not code:
        raise HTTPException(status_code=502, detail="未返回绑定码")
    if format == "raw":
        return Response(content=code, media_type="text/plain")
    return JSONResponse({"code": code})


@router.post("/vernal-api/credentials/codebuddy")
async def api_save_codebuddy_key(request: Request, req: CodeBuddyKeyRequest) -> dict[str, Any]:
    ctx = get_ctx(request)
    eng = ctx.active_slot(request).engine
    api_key = req.api_key.strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="请输入有效的 CodeBuddy API Key")
    env_val = req.internet_environment.strip()
    if env_val not in ("", "internal", "ioa"):
        raise HTTPException(status_code=422, detail="网络环境仅支持国际站（留空）、国内站（internal）或 iOA（ioa）")
    cookie = request.headers.get("cookie", "")
    client = LoopForgeClient(eng.settings.loopforge_url, eng.settings.effective_loopforge_token())
    try:
        me = await (client.auth_current_with_cookie(cookie) if cookie else client.auth_current())
    except LoopForgeError as exc:
        raise HTTPException(status_code=401, detail=f"获取当前登录用户失败：{exc}") from exc
    user_obj = me.get("user") if isinstance(me.get("user"), dict) else me
    uid = (user_obj or {}).get("id")
    if not uid:
        raise HTTPException(status_code=401, detail="未能识别当前登录用户，请先在网页完成登录")
    try:
        await client.save_user_codebuddy_key(uid, api_key, env_val, cookie_header=cookie)
    except LoopForgeError as exc:
        raise HTTPException(status_code=exc.status_code or 502, detail=f"保存 CodeBuddy API Key 失败：{exc}") from exc
    ctx.runner_rt_cache.clear()
    return {"ok": True, "codebuddy_api_key_configured": True, "internet_environment": env_val}


@router.post("/vernal-api/credentials/personal-cli")
async def api_save_personal_cli_key(request: Request, req: PersonalCliKeyRequest) -> dict[str, Any]:
    from autodev.loopforge.client import SessionCookie

    ctx = get_ctx(request)
    provider = req.provider.strip().lower()
    if provider not in ("dsh", "opencode", "both"):
        raise HTTPException(422, "仅支持配置 dsh、opencode 或 both 个人凭据")
    api_key = req.api_key.strip()
    base_url = req.base_url.strip()
    models = [m.strip() for m in (req.models or []) if isinstance(m, str) and m.strip()]
    if req.use_pool_key and not api_key:
        await _require_pool_admin(request)
        pool_cfg = public_pool.values()
        if pool_cfg.get("deepseek_api_key"):
            api_key = pool_cfg["deepseek_api_key"]
            base_url = base_url or pool_cfg.get("deepseek_base_url") or "https://api.deepseek.com"
            if not models:
                models = list(dict.fromkeys(x for x in (
                    pool_cfg.get("default_model") or "deepseek-chat",
                    pool_cfg.get("reasoning_model") or "deepseek-reasoner",
                ) if x))
        elif provider in ("opencode", "both") and pool_cfg.get("openai_api_key"):
            api_key = pool_cfg["openai_api_key"]
            base_url = base_url or pool_cfg.get("openai_base_url") or "https://api.openai.com/v1"
            if not models:
                models = list(dict.fromkeys(x for x in (
                    pool_cfg.get("default_model"),
                    pool_cfg.get("reasoning_model"),
                ) if x))
    if not api_key:
        raise HTTPException(400, "请输入有效的 API Key，或先保存公用池 DeepSeek Key")
    if not base_url:
        base_url = "https://api.deepseek.com"
    parsed = urlsplit(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HTTPException(422, "模型端点需要 HTTP(S) 地址且不能包含凭据或查询参数")
    if not models:
        models = ["deepseek-chat", "deepseek-reasoner"]
    cookie = request.headers.get("cookie", "")
    if not cookie:
        raise HTTPException(401, "写入个人凭据需要先在浏览器登录平台")
    client = LoopForgeClient(public_pool.server_url(), SessionCookie(cookie))
    try:
        me = await client.auth_current()
    except LoopForgeError as exc:
        raise HTTPException(401, f"获取当前登录用户失败：{exc}") from exc
    user_obj = me.get("user") if isinstance(me.get("user"), dict) else me
    uid = (user_obj or {}).get("id")
    if not uid:
        raise HTTPException(401, "未能识别当前登录用户")
    targets = ["opencode", "dsh"] if provider == "both" else [provider]
    cred_entry = {
        "apiKey": api_key,
        "baseUrl": base_url,
        "protocol": "chat",
        "models": [{"id": m} for m in models],
    }
    payload = {"agentCliCredentials": {target: dict(cred_entry) for target in targets}}
    try:
        await client._req("PUT", f"/api/users/{int(uid)}/credentials", json_body=payload)
    except LoopForgeError as exc:
        raise HTTPException(exc.status_code or 502, f"写入原生个人凭据失败：{exc}") from exc
    ctx.runner_rt_cache.clear()
    return {"ok": True, "provider": provider, "providers": targets, "configured": True, "base_url": base_url, "models": models}


@router.post("/vernal-api/agent/rescan")
async def api_agent_rescan(request: Request) -> dict[str, Any]:
    ctx = get_ctx(request)
    eng = ctx.active_slot(request).engine
    runtimes, runner_summary = await ctx.fetch_unified_runtimes(eng, cookie=request.headers.get("cookie", ""), refresh=True)
    return {
        "ok": True,
        "runtimes": runtimes,
        "runner_summary": runner_summary,
        "preferred_harness": choose_preferred_harness_from_runtimes(runtimes, eng.settings),
    }


@router.post("/vernal-api/agent/config")
async def api_agent_config(request: Request, req: AgentConfigRequest) -> dict[str, Any]:
    ctx = get_ctx(request)
    slot = ctx.active_slot(request)
    eng = slot.engine
    pool_changing = req.execution_pool is not None and req.execution_pool != eng.settings.execution_pool
    if req.execution_pool is not None:
        if req.execution_pool not in public_pool.POOLS:
            raise HTTPException(422, "无效的执行池")
        if pool_changing:
            busy_sched = ctx.is_scheduler_running(slot) and bool(getattr(slot.scheduler, "running_tasks", None))
            if busy_sched or ctx.is_planning(slot):
                raise HTTPException(409, "当前有任务正在执行或立项中，请先点击顶栏「暂停调度」后再切换执行池")
    if (req.execution_pool or eng.settings.execution_pool) == "public_docker":
        state = public_pool.status()
        allowed = {state["default_model"], state["reasoning_model"], ""}
        for name in ("model","pm_model","director_model","coder_model","qa_model"):
            proposed = getattr(req,name)
            if (proposed if proposed is not None else getattr(eng.settings,name)).strip() not in allowed:
                raise HTTPException(422, "角色模型未列入公用池白名单")
    if req.native_executor_user_id is not None or req.native_approver_user_id is not None:
        user, projects = await ctx.access_scope(request.headers.get("cookie",""))
        project = projects.get(eng.settings.loopforge_project_id) or {}
        from autodev.server.deps import require_engineering_role
        require_engineering_role(slot, user, project, ("admin",), manage=True)
        members = {int(m["userId"]) for m in (project.get("members") or []) if m.get("role") != "viewer"}
        members.add(project.get("ownerId"))
        executor = req.native_executor_user_id if req.native_executor_user_id is not None else eng.settings.native_executor_user_id
        reviewer = req.native_approver_user_id if req.native_approver_user_id is not None else eng.settings.native_approver_user_id
        if (executor or reviewer) and not (executor and reviewer and executor != reviewer and {executor, reviewer} <= members):
            raise HTTPException(422, "执行人与审批人须为两个不同的可写项目成员")
        eng.settings.native_executor_user_id, eng.settings.native_approver_user_id = executor, reviewer
    valid_h = {"", "auto", *SUPPORTED_HARNESSES}
    for hfield in ("harness", "pm_harness", "director_harness", "coder_harness", "qa_harness"):
        raw_val = getattr(req, hfield)
        if raw_val is not None:
            norm_val = normalize_harness_name(raw_val)
            if norm_val not in valid_h:
                raise HTTPException(422, f"不支持的执行器: {raw_val}")
            if hfield == "harness" and not norm_val:
                norm_val = "auto"
            setattr(eng.settings, hfield, norm_val)
            if hfield == "director_harness" and req.pm_harness is None:
                eng.settings.pm_harness = norm_val

    for mfield in ("model", "pm_model", "director_model", "coder_model", "qa_model"):
        mval = getattr(req, mfield)
        if mval is not None:
            cleaned = mval.strip()
            setattr(eng.settings, mfield, cleaned)
            if mfield == "director_model":
                if req.pm_model is None:
                    eng.settings.pm_model = cleaned
                eng.settings.planner_model = cleaned
            elif mfield == "qa_model":
                eng.settings.reviewer_model = cleaned

    if req.max_concurrency is not None:
        if not (1 <= req.max_concurrency <= 16):
            raise HTTPException(422, "max_concurrency 需在 1 ~ 16 之间")
        eng.settings.max_concurrency = req.max_concurrency
        eng.db.ensure_slots(req.max_concurrency)

    if pool_changing and req.execution_pool is not None:
        eng.settings.execution_pool = req.execution_pool
        eng.settings.selected_pool = ""
    if req.process_template is not None:
        if req.process_template not in ("legacy", "standard", "feature", "bugfix", "doc_reverse", "quick-prototype"):
            raise HTTPException(422, f"不支持的流程模板: {req.process_template}")
        eng.settings.process_template = req.process_template
    if req.review_policy is not None:
        if req.review_policy not in ("off", "milestone", "risk", "all"):
            raise HTTPException(422, f"不支持的代码审查策略: {req.review_policy}")
        eng.settings.review_policy = req.review_policy
    if req.doc_profile is not None:
        if req.doc_profile not in ("default", "gbt8567"):
            raise HTTPException(422, f"不支持的文档规范体系: {req.doc_profile}")
        eng.settings.doc_profile = req.doc_profile
    if req.ci_provider is not None:
        if req.ci_provider not in ("none", "gitea", "gitlab", "jenkins", "webhook"):
            raise HTTPException(422, f"不支持的 CI 提供商: {req.ci_provider}")
        eng.settings.ci_provider = req.ci_provider
    ctx.runner_rt_cache.clear()
    eng.reset_harness()
    ctx.persist_config(eng)

    synced_loopforge = False
    sync_error = ""
    synced_workflow_id = eng.settings.loopforge_workflow_id
    synced_workflow_version: int | None = None
    if eng.settings.runtime_backend == "loopforge" and eng.settings.effective_loopforge_token() and eng.settings.loopforge_project_id:
        from autodev.server import app as app_mod

        try:
            assets = await app_mod.ensure_vernal_loopforge_assets(ctx.lf_client(slot), eng.settings.loopforge_project_id, eng.settings)
            synced_loopforge = True
            if isinstance(assets, dict):
                synced_workflow_id = str(assets.get("selected_workflow_id") or synced_workflow_id)
                try:
                    wfs = await ctx.lf_client(slot).list_workflows(eng.settings.loopforge_project_id)
                    matched_wf = next((w for w in wfs if isinstance(w, dict) and w.get("id") == synced_workflow_id), None)
                    if matched_wf and matched_wf.get("version") is not None:
                        synced_workflow_version = int(matched_wf["version"])
                except LoopForgeError:
                    pass
        except LoopForgeError as exc:
            sync_error = str(exc)

    eff_dir = eng.settings.harness_for("director")
    eff_cod = eng.settings.harness_for("coder")
    eff_qa = eng.settings.harness_for("reviewer")
    eng.db.add_event(
        "agent.configured",
        f"已更新智能体编队配置（池={eng.settings.execution_pool}, 总监={eff_dir}, 开发={eff_cod}, 审查={eff_qa}, 并发={eng.settings.max_concurrency}）"
        + (f" · 工作流同步失败：{sync_error}" if sync_error else ""),
        actor=effective_actor(),
    )
    return {
        "ok": True,
        "synced_loopforge": synced_loopforge,
        "sync_error": sync_error,
        "workflow_id": synced_workflow_id,
        "workflow_version": synced_workflow_version,
        "effective": {
            "execution_pool": eng.settings.execution_pool,
            "director_harness": eff_dir,
            "coder_harness": eff_cod,
            "qa_harness": eff_qa,
            "max_concurrency": eng.settings.max_concurrency,
        },
    }


@router.get("/vernal-api/inbox")
def project_inbox(request: Request):
    eng = get_ctx(request).active_slot(request).engine
    snap = eng.snapshot()
    gates = [dict(r) for r in eng.db._query(
        "SELECT g.record_id,g.gate_id,g.scope_id FROM gate_records g WHERE g.status='PENDING' AND NOT EXISTS "
        "(SELECT 1 FROM gate_records n WHERE n.gate_id=g.gate_id AND n.scope_type=g.scope_type "
        "AND n.scope_id=g.scope_id AND n.round>g.round)")]
    reviews = [dict(r) for r in eng.db._query("SELECT review_id,scope_id FROM code_reviews WHERE status='OPEN'")]
    fused = [t for t in snap["tasks"] if t["status"] == "FUSED"]
    milestones = [m for m in snap["milestones"] if m["status"] == "AWAITING_MERGE"]
    plan = snap["status"] == "PLANNED"
    return {"project_id": eng.settings.loopforge_project_id or eng.ws.root.name,
            "count": int(plan) + len(gates) + len(reviews) + len(fused) + len(milestones),
            "plan_pending": plan, "process_gates": gates, "reviews": reviews,
            "fused_tasks": fused, "awaiting_milestones": milestones}


@router.get("/vernal-api/runtime/approvals")
async def api_runtime_approvals(request: Request) -> dict[str, Any]:
    ctx = get_ctx(request)
    slot = ctx.active_slot(request)
    eng = slot.engine
    snap = eng.snapshot()
    lf_items: list[dict[str, Any]] = []
    lf_err = ""
    if eng.settings.effective_loopforge_token():
        try:
            lf_items = await ctx.lf_client(slot).list_pending_approvals(project_id=eng.settings.loopforge_project_id)
        except LoopForgeError as exc:
            lf_err = str(exc)
    return {
        "plan_pending": snap["status"] == "PLANNED",
        "fused_tasks": [t for t in snap["tasks"] if t["status"] == "FUSED"],
        "awaiting_milestones": [m for m in snap["milestones"] if m["status"] == "AWAITING_MERGE"],
        "loopforge_gates": lf_items,
        "loopforge_error": lf_err,
    }


@router.get("/vernal-api/link-node/{job_id}", include_in_schema=False)
def delegate_tick(job_id: str, request: Request, sig: str):
    from autodev.harnesses.link_node import node_tick
    return node_tick(get_ctx(request).active_slot(request).engine, job_id, sig)


async def project_run(ctx, slot, run_id, *, client=None):
    try:
        return await (client or ctx.lf_client(slot)).project_run(run_id, slot.engine.settings.loopforge_project_id)
    except LoopForgeError as exc:
        raise HTTPException(exc.status_code or 503, str(exc)) from exc


async def authorize_run_decision(ctx, request, run, node_id=""):
    from autodev.server.deps import require_engineering_role, current_user_var
    context = run.get("executionContext") or {}
    pid = run_project_id(run)
    wf = run.get("workflowSnapshot") or {}
    nodes = {n["id"]: n for n in wf.get("nodes", []) if n.get("id")}
    active = [nid for nid, state in (run.get("nodes") or {}).items() if state.get("status") == "waiting_approval"]
    if node_id and active and node_id not in active:
        raise HTTPException(409, "审批节点已变化")
    signoff = bool(pid and run.get("workflowId") == pid + "-vernal-signoff")
    pending = [node_id] if node_id else active
    human = signoff or any(nodes.get(n, {}).get("type") == "approval" for n in pending)
    uid = context.get("responsibleUserId")
    slot = ctx.find_or_open_slot(pid, create=False) if pid else None
    constraints = []
    if slot and pending:
        from autodev.process.runner import load_template
        template = load_template(slot.engine.ws,slot.engine.settings)
        for stage in template.stages:
            if stage.enabled:
                constraints.extend(tuple(g.approver_roles) for g in stage.gates if g.approver_roles and g.id in pending)
        custom = slot.engine.db.meta_get("approval_roles:" + str(run.get("workflowId") or ""))
        if custom:
            constraints.append(tuple(json.loads(custom)))
    if not (human or constraints):
        return
    cookie = request.headers.get("cookie", "")
    if not cookie:
        if signoff or uid or constraints:
            raise HTTPException(401, "指定节点签字需要个人登录会话")
        return
    user, projects = await ctx.access_scope(cookie)
    project = projects.get(pid)
    if not project or (human and uid and str(user.get("id")) != str(uid)) or (signoff and not uid):
        raise HTTPException(403, "当前会话无权代签原生审批")
    if slot and constraints:
        token = current_user_var.set(user.get("login") or user.get("loginName") or "")
        try:
            for roles in constraints:
                require_engineering_role(slot,user,project,roles)
        finally:
            current_user_var.reset(token)


@router.post("/vernal-api/runtime/runs/{run_id}/approve")
async def api_lf_approve(run_id: str, request: Request, req: RunApproveRequest) -> Any:
    ctx = get_ctx(request)
    slot = ctx.active_slot(request)
    eng = slot.engine
    run = await project_run(ctx, slot, run_id)
    await authorize_run_decision(ctx, request, run, req.node_id)
    actor = effective_actor()
    matched_cp = next(
        (cp for cp in eng.db.list_checkpoints() if cp.get("loopforge_run_id") == run_id and cp.get("status") != "APPLIED"),
        None,
    )
    if matched_cp is not None:
        try:
            return await approve_steering_checkpoint(eng, int(matched_cp["checkpoint_id"]), actor=actor, comment=req.comment)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except LoopForgeError as exc:
            raise HTTPException(status_code=exc.status_code or 502, detail=str(exc)) from exc
    try:
        res = await ctx.lf_client(slot).approve_run(run_id, comment=req.comment, node_id=req.node_id)
        eng.db.add_event("loopforge.approved", f"已批准运行节点 {run_id}", actor=actor)
        return res
    except LoopForgeError as exc:
        raise HTTPException(status_code=exc.status_code or 502, detail=str(exc)) from exc


@router.post("/vernal-api/runtime/runs/{run_id}/reject")
async def api_lf_reject(run_id: str, request: Request, req: RunRejectRequest) -> Any:
    ctx = get_ctx(request)
    slot = ctx.active_slot(request)
    eng = slot.engine
    run = await project_run(ctx, slot, run_id)
    await authorize_run_decision(ctx, request, run, req.node_id)
    actor = effective_actor()
    matched_cp = next(
        (cp for cp in eng.db.list_checkpoints() if cp.get("loopforge_run_id") == run_id and cp.get("status") != "APPLIED"),
        None,
    )
    if matched_cp is not None and (not req.target_node or req.target_node == "director_plan"):
        try:
            cp = await reject_steering_checkpoint(eng, int(matched_cp["checkpoint_id"]), actor=actor, reason=req.reason)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except LoopForgeError as exc:
            raise HTTPException(status_code=exc.status_code or 502, detail=str(exc)) from exc
        ctx.ensure_steering_poller(slot, int(cp["checkpoint_id"]))
        return cp
    try:
        res = await ctx.lf_client(slot).reject_run(run_id, reason=req.reason, target_node=req.target_node, node_id=req.node_id)
        eng.db.add_event("loopforge.rejected", f"已打回运行节点 {run_id}：{req.reason}", actor=actor)
        return res
    except LoopForgeError as exc:
        raise HTTPException(status_code=exc.status_code or 502, detail=str(exc)) from exc


@router.post("/vernal-api/runtime/runs/{run_id}/retry")
async def api_lf_retry(run_id: str, request: Request, node_id: str = "") -> Any:
    ctx = get_ctx(request)
    from autodev.loopforge.dispatch import reserve, record
    slot = ctx.active_slot(request)
    run = await project_run(ctx, slot, run_id)
    if run.get("status") not in ("failed", "paused", "waiting", "cancelled"):
        raise HTTPException(409, "当前运行状态不可重试")
    nodes = run.get("nodes") or {}
    if not node_id:
        failed = [k for k, n in nodes.items() if n.get("status") == "failed"]
        node_id = failed[0] if len(failed) == 1 else ""
    if not node_id or node_id not in nodes:
        raise HTTPException(422, "请选择本次运行中需要重试的节点")
    db = slot.engine.db
    with db.transaction():
        rows = db._query("SELECT * FROM tasks WHERE loopforge_run_id=?", (run_id,))
        task = rows[0] if rows else None
        seq = int(task["exec_seq"] + 1) if task else 0
        receipt, fresh = reserve(db, task["task_id"] if task else "run:" + run_id, seq,
                                 task["retry_count"] + 2 if task else 1, slot.engine.settings.execution_pool,
                                 effective_actor(), (run.get("executionContext") or {}).get("issueId"), command=f"retry:{run_id}:{node_id}:{run.get('updatedAt','')}")
        if not fresh:
            if receipt["state"] in ("DISPATCHING", "NEEDS_CONFIRMATION"):
                raise HTTPException(409, "原生重试尚待确认")
            return {"dispatch_id": receipt["dispatch_id"], "state": receipt["state"]}
        record(db, receipt, "DISPATCHING", lf_run_id=run_id)
        if task:
            db._exec("UPDATE tasks SET exec_seq=?,status='FUSED',failure_reason=? WHERE task_id=?",
                     (seq, "等待原生重试确认", task["task_id"]))
    try:
        await ctx.lf_client(slot).retry_run(run_id, node_id=node_id)
    except LoopForgeError as exc:
        record(db, receipt, "NEEDS_CONFIRMATION")
        raise HTTPException(exc.status_code or 503, str(exc)) from exc
    with db.transaction():
        record(db, receipt, "RUNNING", lf_run_id=run_id)
        if task:
            db._exec("UPDATE tasks SET status='READY',retry_count=retry_count+1,failure_reason=NULL "
                     "WHERE task_id=? AND exec_seq=?", (task["task_id"], seq))
    return {"dispatch_id": receipt["dispatch_id"], "run_id": run_id, "state": "RUNNING"}


@router.get("/vernal-api/runtime/runs/{run_id}/diff")
async def api_lf_diff(run_id: str, request: Request) -> Any:
    ctx = get_ctx(request)
    await project_run(ctx, ctx.active_slot(request), run_id)
    try:
        return await ctx.lf_client(ctx.active_slot(request)).get_run_diff(run_id)
    except LoopForgeError as exc:
        raise HTTPException(status_code=exc.status_code or 502, detail=str(exc)) from exc


_DEFAULT_NODE_TITLES = {
    "implement": "编码与自测",
    "package": "打包校验交付清单",
    "review": "独立契约与测试审查",
    "verify": "集成验证",
    "approve": "人工确认签字",
}
_STALL_THRESHOLD_SECONDS = 900


def _iso_age_seconds(iso_str: str | None) -> int | None:
    if not iso_str:
        return None
    from datetime import datetime, timezone

    try:
        text = str(iso_str).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = int((datetime.now(timezone.utc) - dt).total_seconds())
        return max(0, delta)
    except Exception:
        return None


def _extract_ordered_nodes(run: dict[str, Any]) -> list[dict[str, Any]]:
    node_states = run.get("nodes") or {}
    wf_nodes = (run.get("workflowSnapshot") or {}).get("nodes") or []
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for spec in wf_nodes:
        if not isinstance(spec, dict) or not spec.get("id"):
            continue
        nid = str(spec["id"])
        seen.add(nid)
        st = node_states.get(nid) if isinstance(node_states.get(nid), dict) else {}
        status = str(st.get("status") or "pending")
        started = str(st.get("startedAt") or "")
        finished = str(st.get("finishedAt") or "")
        elapsed = None
        if started and finished:
            s_age = _iso_age_seconds(started)
            f_age = _iso_age_seconds(finished)
            if s_age is not None and f_age is not None:
                elapsed = max(0, s_age - f_age)
        elif started:
            elapsed = _iso_age_seconds(started)
        ordered.append(
            {
                "id": nid,
                "title": str(spec.get("title") or _DEFAULT_NODE_TITLES.get(nid, nid)),
                "type": str(spec.get("type") or ""),
                "status": status,
                "provider": str(st.get("provider") or ""),
                "model": str(st.get("model") or ""),
                "tool_calls": int(st.get("toolCalls") or 0),
                "activity": str(st.get("activity") or ""),
                "output_preview": str(st.get("output") or "").strip()[:240],
                "started_at": started,
                "finished_at": finished,
                "elapsed_sec": elapsed,
                "error": str(st.get("error") or ""),
            }
        )
    for nid, st in node_states.items():
        if nid in seen or not isinstance(st, dict):
            continue
        started = str(st.get("startedAt") or "")
        ordered.append(
            {
                "id": str(nid),
                "title": _DEFAULT_NODE_TITLES.get(str(nid), str(nid)),
                "type": "",
                "status": str(st.get("status") or "pending"),
                "provider": str(st.get("provider") or ""),
                "model": str(st.get("model") or ""),
                "tool_calls": int(st.get("toolCalls") or 0),
                "activity": str(st.get("activity") or ""),
                "output_preview": str(st.get("output") or "").strip()[:240],
                "started_at": started,
                "finished_at": str(st.get("finishedAt") or ""),
                "elapsed_sec": _iso_age_seconds(started) if started else None,
                "error": str(st.get("error") or ""),
            }
        )
    return ordered


@router.get("/vernal-api/runtime/live-progress")
async def api_runtime_live_progress(request: Request) -> dict[str, Any]:
    ctx = get_ctx(request)
    slot = ctx.active_slot(request)
    eng = slot.engine
    snap = eng.snapshot()
    milestones = snap.get("milestones") or []
    tasks = snap.get("tasks") or []

    done_statuses = {"APPROVED", "MERGED"}
    active_statuses = {"IN_PROGRESS", "VERIFYING", "REVIEWING"}
    review_statuses = {"AWAITING_HUMAN_REVIEW"}

    ms_done = sum(1 for m in milestones if m.get("status") == "MERGED")
    current_ms = next((m for m in milestones if m.get("status") != "MERGED"), milestones[-1] if milestones else None)

    tasks_done = sum(1 for t in tasks if t.get("status") in done_statuses)
    tasks_active = sum(1 for t in tasks if t.get("status") in active_statuses)
    tasks_ready = sum(1 for t in tasks if t.get("status") == "READY")
    tasks_review = sum(1 for t in tasks if t.get("status") in review_statuses)
    tasks_fused = sum(1 for t in tasks if t.get("status") == "FUSED")
    tasks_blocked = sum(1 for t in tasks if t.get("status") == "BLOCKED")
    tasks_total = len(tasks)

    dispatch_by_task: dict[str, dict[str, Any]] = {}
    for row in eng.db._query(
        "SELECT d.* FROM dispatch_records d "
        "WHERE d.dispatch_id = (SELECT MAX(x.dispatch_id) FROM dispatch_records x WHERE x.task_id = d.task_id)"
    ):
        dispatch_by_task[str(row["task_id"])] = dict(row)

    focus_tasks = [t for t in tasks if t.get("status") in (active_statuses | {"FUSED", "AWAITING_HUMAN_REVIEW"})]

    bridge = None
    if eng.settings.runtime_backend == "loopforge" and eng.settings.effective_loopforge_token():
        bridge = LoopForgeBridge(eng.settings, eng.db)
        bridge.client = ctx.lf_client(slot)

    async def _inspect_task(t: dict[str, Any]) -> dict[str, Any]:
        tid = str(t["task_id"])
        disp = dispatch_by_task.get(tid) or {}
        run_id = str(t.get("loopforge_run_id") or disp.get("lf_run_id") or "")
        pool_name = str(
            disp.get("pool")
            or eng.db.meta_get(f"pool.run:{run_id}")
            or eng.settings.selected_pool
            or eng.settings.execution_pool
            or "auto"
        )
        latest = t.get("latest_activity") or {}
        nodes: list[dict[str, Any]] = []
        run_status = ""
        started_at = str(t.get("active_started_at") or disp.get("created_at") or "")
        updated_at = str(latest.get("created_at") or disp.get("updated_at") or started_at)
        fetch_error = ""

        if run_id and bridge is not None:
            try:
                try:
                    client = bridge.client_for_run(run_id)
                except LoopForgeError:
                    client = bridge.client
                lf_run = await asyncio.wait_for(client.get_run(run_id), timeout=4.0)
                if isinstance(lf_run, dict):
                    run_status = str(lf_run.get("status") or "")
                    started_at = str(lf_run.get("startedAt") or lf_run.get("createdAt") or started_at)
                    updated_at = str(lf_run.get("updatedAt") or updated_at)
                    nodes = _extract_ordered_nodes(lf_run)
            except Exception as exc:
                fetch_error = str(exc)

        current_node = next(
            (n for n in nodes if n["status"] in ("running", "waiting", "waiting_approval", "failed")),
            None,
        )
        if current_node is None and nodes:
            current_node = nodes[-1]

        elapsed_sec = _iso_age_seconds(started_at)
        node_elapsed = current_node.get("elapsed_sec") if current_node else None
        stalled = False
        stalled_reason = ""

        if t.get("status") in active_statuses:
            if run_status in ("failed", "cancelled"):
                stalled = True
                stalled_reason = f"底层运行已结束（{run_status}），可一键重试此任务"
            elif current_node and current_node["status"] in ("running", "waiting") and (node_elapsed or 0) >= _STALL_THRESHOLD_SECONDS:
                mins = int((node_elapsed or 0) // 60)
                stalled = True
                stalled_reason = f"节点「{current_node['title']}」已持续 {mins} 分钟无进展，疑似卡滞"
            elif not nodes and (elapsed_sec or 0) >= _STALL_THRESHOLD_SECONDS:
                mins = int((elapsed_sec or 0) // 60)
                stalled = True
                stalled_reason = f"任务已持续 {mins} 分钟未推进状态，疑似卡滞"
        elif t.get("status") == "FUSED":
            stalled = True
            stalled_reason = str(t.get("failure_reason") or "任务已触发熔断，可检查日志后重试")

        return {
            "task_id": tid,
            "title": str(t.get("title") or ""),
            "milestone_id": str(t.get("milestone_id") or ""),
            "squad": str(t.get("squad_name") or t.get("squad") or t.get("module") or ""),
            "task_status": str(t.get("status") or ""),
            "retry_count": int(t.get("retry_count") or 0),
            "max_retries": int(t.get("max_retries") or 0),
            "failure_reason": str(t.get("failure_reason") or ""),
            "run_id": run_id,
            "issue_id": str(t.get("loopforge_issue_id") or disp.get("issue_id") or ""),
            "pool": pool_name,
            "run_status": run_status,
            "started_at": started_at,
            "updated_at": updated_at,
            "elapsed_sec": elapsed_sec,
            "stalled": stalled,
            "stalled_reason": stalled_reason,
            "current_node_id": current_node["id"] if current_node else "",
            "current_node_title": current_node["title"] if current_node else "",
            "latest_activity": str(latest.get("message") or ""),
            "latest_activity_at": str(latest.get("created_at") or ""),
            "fetch_error": fetch_error,
            "nodes": nodes,
        }

    active_runs = list(await asyncio.gather(*(_inspect_task(t) for t in focus_tasks))) if focus_tasks else []
    scheduler_running = ctx.is_scheduler_running(slot)
    planning_now = ctx.is_planning(slot)
    proj_status = eng.status.value

    waiting_run = next(
        (
            r for r in active_runs
            if any(n.get("status") == "waiting_approval" for n in (r.get("nodes") or []))
            or r.get("run_status") == "waiting"
        ),
        None,
    )
    stalled_run = next((r for r in active_runs if r.get("stalled")), None)
    awaiting_ms = next((m for m in milestones if m.get("status") == "AWAITING_MERGE"), None)

    if planning_now:
        next_action = {
            "bucket": "running",
            "who": "system",
            "label": "正在执行架构圆桌与立项拆解",
            "hint": "多智能体正在评审规格并生成研发蓝图，完成后将暂停等待你批准。",
            "action_kind": "planning",
        }
    elif proj_status == "PLANNED":
        next_action = {
            "bucket": "waiting_approval",
            "who": "human",
            "label": "等待你批准立项蓝图",
            "hint": f"蓝图已拆解 {tasks_total} 项任务，请审核架构与任务拆解后点击「批准并启动研发」。",
            "action_kind": "approve_plan",
        }
    elif waiting_run is not None:
        next_action = {
            "bucket": "waiting_approval",
            "who": "human",
            "label": f"{waiting_run['task_id']} 等待人工审批门禁",
            "hint": "底层审查节点需要你确认放行或打回返工（点击前往审查或底层运行审批）。",
            "action_kind": "approve_gate",
            "task_id": waiting_run["task_id"],
            "run_id": waiting_run.get("run_id") or "",
        }
    elif tasks_review > 0:
        next_action = {
            "bucket": "waiting_approval",
            "who": "human",
            "label": f"{tasks_review} 项任务等待人工 Code Review",
            "hint": "请在「审查与交付」工作台核对代码 Diff 并签字通过。",
            "action_kind": "human_review",
        }
    elif awaiting_ms is not None:
        next_action = {
            "bucket": "waiting_approval",
            "who": "human",
            "label": f"里程碑 {awaiting_ms['milestone_id']} 等待验收合入",
            "hint": "本里程碑任务已通过门禁，请前往「审查与交付」签字合入交付物。",
            "action_kind": "merge_milestone",
            "milestone_id": awaiting_ms["milestone_id"],
        }
    elif stalled_run is not None:
        next_action = {
            "bucket": "stalled",
            "who": "human",
            "label": f"{stalled_run['task_id']} 异常或疑似卡滞",
            "hint": stalled_run.get("stalled_reason") or "可展开节点查看实时日志或一键重试。",
            "action_kind": "retry_stalled",
            "task_id": stalled_run["task_id"],
        }
    elif proj_status == "COMPLETED":
        next_action = {
            "bucket": "completed",
            "who": "none",
            "label": "全部里程碑已交付完成",
            "hint": "可下载交付产物或发起新一轮阶段变更需求。",
            "action_kind": "completed",
        }
    elif tasks_active > 0 and scheduler_running:
        next_action = {
            "bucket": "running",
            "who": "system",
            "label": f"{tasks_active} 项任务正在自动执行中",
            "hint": "展开下方任务及 SOP 节点即可实时查看 AI 思考链与命令流。",
            "action_kind": "running",
        }
    elif proj_status in ("APPROVED", "RUNNING", "STALLED") and not scheduler_running and tasks_total > tasks_done:
        next_action = {
            "bucket": "waiting_approval",
            "who": "human",
            "label": "调度器当前已暂停",
            "hint": "点击「恢复自动追踪」继续推进剩余任务。",
            "action_kind": "resume_scheduler",
        }
    else:
        next_action = {
            "bucket": "ready",
            "who": "human",
            "label": "待发起立项评审",
            "hint": "确认需求说明后，点击右上角「开始立项评审」启动架构设计与蓝图拆解。",
            "action_kind": "start_plan",
        }

    return {
        "project_id": eng.settings.loopforge_project_id or slot.project_key,
        "project_status": proj_status,
        "scheduler_running": scheduler_running,
        "planning": planning_now,
        "auto_resumable": bool(proj_status in ("APPROVED", "RUNNING", "STALLED") and not scheduler_running and tasks_total > tasks_done),
        "next_action": next_action,
        "milestone_summary": {
            "current_milestone_id": str(current_ms.get("milestone_id") or "") if current_ms else "",
            "current_milestone_title": str(current_ms.get("title") or "") if current_ms else "",
            "current_milestone_status": str(current_ms.get("status") or "") if current_ms else "",
            "milestones_done": ms_done,
            "milestones_total": len(milestones),
            "tasks_done": tasks_done,
            "tasks_total": tasks_total,
            "tasks_active": tasks_active,
            "tasks_ready": tasks_ready,
            "tasks_review": tasks_review,
            "tasks_fused": tasks_fused,
            "tasks_blocked": tasks_blocked,
            "percent": int(round(100 * tasks_done / tasks_total)) if tasks_total else 0,
        },
        "active_runs": active_runs,
    }


@router.get("/vernal-api/runtime/analytics")
async def api_lf_analytics(request: Request) -> dict[str, Any]:
    ctx = get_ctx(request)
    slot = ctx.active_slot(request)
    eng = slot.engine
    snap = eng.snapshot()
    lf_analytics: dict[str, Any] = {}
    lf_ok = False
    lf_error = ""
    if eng.settings.effective_loopforge_token():
        try:
            lf_analytics = await ctx.lf_client(slot).get_analytics(eng.settings.loopforge_project_id)
            lf_ok = True
        except LoopForgeError as exc:
            lf_analytics = {}
            lf_error = str(exc)
    else:
        lf_error = "当前项目尚未绑定执行底座令牌"
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
