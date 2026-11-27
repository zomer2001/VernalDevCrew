"""项目创建、切换、本地目录接管与文件夹导入路由。"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request

from autodev import gitops, local_git, local_git_bridge
from autodev.artifacts import MAX_TOTAL as FILE_EXCHANGE_MAX_TOTAL, FileExchange, checked_name
from autodev.config import Workspace
from autodev.engine import Engine, OpsError, init_workspace
from autodev.loopforge.bootstrap import ensure_vernal_loopforge_assets
from autodev.loopforge.client import LoopForgeClient, LoopForgeError, SessionCookie
from autodev.security import validate_external_git_url
from autodev.server.deps import PROJECT_ID_RE, current_slot_var, effective_actor, get_ctx, is_loopback
from autodev.server.project_launcher import (
    list_host_directories,
    load_project_registry,
    register_project_mapping,
    resolve_safe_host_dir,
    scan_workspace_source_files_detailed,
    slugify_project_id,
    write_codebase_overview_doc,
)
from autodev.server.schemas import ImportFolderRequest, ProjectCreateRequest, ProjectSelectRequest

router = APIRouter()


async def _require_native_binding_manager(ctx, cookie, pid):
    from autodev.public_pool import server_url
    await ctx.verify_project_access(cookie, server_url(), pid)
    user, projects = await ctx.access_scope(cookie)
    project = projects[pid]
    uid = str(user["id"])
    if not (user.get("isPlatformAdmin") is True or str(project.get("ownerId")) == uid or
            any(str(m.get("userId")) == uid and m.get("role") == "maintainer"
                for m in (project.get("members") or []))):
        raise HTTPException(403, "绑定现有原生项目需要项目管理权限")


def _require_host_access(request):
    if hasattr(request.state, "allowed_projects") and not request.state.platform_admin:
        raise HTTPException(403, "服务器目录仅供平台管理员接入。个人项目请使用设备配对或上传目录。")


async def _guard_project_target(
    request: Request,
    pid: str,
    target_dir: Path,
    *,
    allow_same_dir: bool = False,
) -> None:
    ctx = get_ctx(request)
    parent = ctx.workspaces_parent()
    reg = load_project_registry(parent)
    resolved_target = target_dir.resolve()
    if any(key != pid and info.get("root") and Path(info["root"]).resolve() == resolved_target
           for key, info in reg.items() if isinstance(info, dict)):
        raise HTTPException(409, "该目录已绑定其他项目")
    if pid in reg or pid in ctx.slots or Workspace(resolved_target).config_path.exists():
        raise HTTPException(409, "工作区已有登记，请打开已有绑定，待恢复的登记可重试绑定")
    if not allow_same_dir and target_dir.exists() and any(target_dir.iterdir()):
        raise HTTPException(status_code=409, detail=f"目标工作区目录「{pid}」已存在且非空，请更换项目名称避免覆盖已有文件")
    cookie = request.headers.get("cookie", "")
    eng = ctx.active_slot(request).engine
    if eng.settings.runtime_backend != "loopforge":
        return
    if getattr(request.state, "bind_existing", False):
        if cookie:
            await _require_native_binding_manager(ctx, cookie, pid)
        return
    if cookie:
        from autodev.public_pool import server_url
        try:
            existing = await LoopForgeClient(server_url()).list_projects_with_cookie(cookie)
        except LoopForgeError:
            return  # The journaled provisioning step retries the native lookup.
        if any(str(p.get("id")) == pid for p in existing):
            raise HTTPException(409, "平台中已存在同名项目，请从原生项目接入")


async def _finalize_launched_project(
    request: Request,
    *,
    ws: Workspace,
    pid: str,
    display_name: str,
    collab_mode: str,
    source_mode: str,
    seed_eng: Engine,
    git_source: Path | None = None,
    private_remote: str = "",
    source_commit: str = "",
    needs_wish: bool = False,
    git_url: str = "",
    branch: str = "",
    wish: str = "",
    docs: list[dict[str, str]] | None = None,
    process_template: str = "",
    execution_pool: str = "",
    harness: str = "",
    start_council: bool = False,
    guided_delivery: bool = False,
    intent: str = "feature",
    actor: str = "owner",
) -> dict[str, Any]:
    from autodev import public_pool
    from autodev.config import SUPPORTED_HARNESSES, normalize_harness_name

    ctx = get_ctx(request)
    new_settings = ws.load_settings()
    explicit_pool = (execution_pool or "").strip()
    explicit_harness = normalize_harness_name(harness or "") if (harness or "").strip() else ""
    if explicit_pool and explicit_pool not in public_pool.POOLS:
        raise HTTPException(422, f"无效的执行池：{explicit_pool}")
    if explicit_harness and explicit_harness not in ("auto", *SUPPORTED_HARNESSES):
        raise HTTPException(422, f"不支持的执行器：{harness}")

    if explicit_pool or explicit_harness:
        # User explicitly chose a route on creation: never inherit unrelated overrides from another active project.
        new_settings.execution_pool = explicit_pool or "auto"
        new_settings.harness = explicit_harness or "auto"
        for field_name in (
            "model",
            "pm_harness",
            "pm_model",
            "director_harness",
            "director_model",
            "planner_model",
            "coder_harness",
            "coder_model",
            "qa_harness",
            "qa_model",
            "reviewer_model",
        ):
            setattr(new_settings, field_name, "")
    else:
        for field_name in (
            "harness",
            "model",
            "pm_harness",
            "pm_model",
            "director_harness",
            "director_model",
            "planner_model",
            "coder_harness",
            "coder_model",
            "qa_harness",
            "qa_model",
            "reviewer_model",
        ):
            setattr(new_settings, field_name, getattr(seed_eng.settings, field_name))
        new_settings.execution_pool = seed_eng.settings.execution_pool
    new_settings.runtime_backend = seed_eng.settings.runtime_backend
    new_settings.loopforge_url = seed_eng.settings.loopforge_url
    new_settings.gitea_url = seed_eng.settings.gitea_url
    new_settings.vernal_public_url = seed_eng.settings.vernal_public_url
    # Browser credentials are request scoped. Background work uses the deployment identity.
    new_settings.loopforge_project_id = pid
    new_settings.collaboration_mode = collab_mode
    if process_template.strip():
        new_settings.process_template = process_template.strip()
    if start_council:
        new_settings.planning_mode = "goal"
        new_settings.enable_council = True
    if branch:
        new_settings.base_branch = branch

    new_eng = Engine(ws, settings=new_settings)
    new_eng.db.meta_set("source_mode", source_mode)
    if git_source is not None:
        new_eng.db.meta_set("git_delivery_source", str(git_source))
        new_eng.db.meta_set("git_source_commit", source_commit)
        new_eng.db.meta_set("delivery_branch_prefix", "vernal/milestone-")
    if private_remote:
        new_eng.db.meta_set("private_remote", private_remote)
    elif git_source is not None and git_url:
        new_eng.db.meta_set("gitea_mirror", "1")
    if git_url:
        new_eng.db.meta_set("git_url", git_url)
    if branch:
        new_eng.db.meta_set("git_branch", branch)

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

    mission = None
    if guided_delivery and (wish.strip() or docs):
        from autodev.speckit import missions
        mission = missions.save(new_eng, "001-initial", wish.strip() or "根据附带的需求文档评估首版范围与验收条件。",
                                intent, 0, actor=effective_actor(actor))

    ctx.persist_config(new_eng)
    new_eng.db.meta_set("binding_existing", "1" if getattr(request.state, "bind_existing", False) else "0")
    new_eng.db.meta_set("binding_owner", effective_actor(actor))
    new_eng.db.meta_set("binding_display_name", display_name)
    new_eng.db.meta_set("binding_git_url", git_url)
    try:
        lf_synced, lf_repo_id = await _complete_binding(ctx, new_eng, ctx.initial_engine.settings.service_token() if not request.headers.get("cookie") and is_loopback(request) else "")
    except Exception:
        new_eng.db.close()
        raise
    slot = ctx.register_slot(new_eng)
    ctx.update_cli_default_if_unscoped(request, slot)
    tok_ctx = current_slot_var.set(slot)
    try:
        if guided_delivery and mission and start_council:
            from autodev.speckit import missions, preflight
            check = await preflight.inspect(new_eng)
            if check["ready"]:
                from autodev.loopforge.reconciler import start
                await start(new_eng, mission["feature_id"], mission["revision"], actor=effective_actor(actor))
            else:
                new_eng.db.add_event("mission.preflight_blocked", "目标已保存，执行资源未就绪，请在需求面板查看", actor=effective_actor(actor))
        elif not guided_delivery and start_council and slices > 0 and not needs_wish:
            async def _auto_plan() -> None:
                try:
                    await new_eng.plan(extra_instructions="", mode="goal")
                except Exception as exc:
                    new_eng.db.add_event("plan.failed", f"自动启动立项评审失败：{exc}")

            slot.plan_task = ctx.start_background(slot, "_auto_plan", _auto_plan())
    finally:
        current_slot_var.reset(tok_ctx)

    new_eng.db.add_event(
        "project.launched",
        f"已初始化项目「{display_name}」（方式={source_mode}，协作={collab_mode}，切片={slices}）",
        actor=effective_actor(actor),
    )
    return {
        "ok": True,
        "project_id": pid,
        "name": display_name,
        "workspace_root": str(ws.root),
        "delivery_branch_prefix": new_eng.db.meta_get("delivery_branch_prefix") or "",
        "source_mode": source_mode,
        "collaboration_mode": collab_mode,
        "execution_pool": new_settings.execution_pool,
        "harness": new_settings.harness,
        "branch": branch or new_eng.settings.base_branch,
        "slices": slices,
        "loopforge_synced": lf_synced,
        "repository_id": lf_repo_id,
        "council_started": bool(not guided_delivery and start_council and slices > 0 and not needs_wish),
        "mission_saved": bool(mission),
        "needs_wish": needs_wish,
    }


def _native_repository_payload(
    repo_id: str,
    project_id: str,
    name: str,
    *,
    clone_url: str,
    repo_url: str,
    branch: str,
    gitea_mirror: bool = False,
) -> dict[str, Any]:
    from urllib.parse import urlsplit

    raw = (repo_url or clone_url or "").strip()
    parts = urlsplit(raw)
    is_ssh = raw.startswith(("git@", "ssh://"))
    is_github = parts.scheme in ("http", "https") and (parts.hostname or "").lower() == "github.com"
    if not gitea_mirror and (is_github or is_ssh):
        return {
            "id": repo_id,
            "projectId": project_id,
            "name": name,
            "cloneUrl": clone_url,
            "repoUrl": raw,
            "defaultBranch": branch,
        }
    path = parts.path.strip("/") if parts.scheme in ("http", "https") else ""
    if path.endswith(".git"):
        path = path[:-4]
    repo_path = path or (f"vernal-local/{project_id}" if gitea_mirror else f"local/{project_id}")
    payload: dict[str, Any] = {
        "id": repo_id,
        "projectId": project_id,
        "name": name,
        "provider": "gongfeng",
        "gongfengProjectId": repo_path,
        "cloneUrl": clone_url,
        "defaultBranch": branch,
    }
    if parts.scheme in ("http", "https"):
        payload["webUrl"] = raw[:-4] if raw.endswith(".git") else raw
    return payload


async def _complete_binding(ctx, eng, local_service=""):
    from autodev.public_pool import server_url
    from autodev.server import app as app_mod
    cfg, db = eng.settings, eng.db
    pid = cfg.loopforge_project_id
    name = db.meta_get("binding_display_name") or pid
    url = db.meta_get("binding_git_url") or ""
    mode = db.meta_get("source_mode") or "scratch"
    branch = db.meta_get("git_branch") or cfg.base_branch
    repo = ""
    def record(state, error=""):
        db.meta_set("bind_state", state)
        register_project_mapping(ctx.workspaces_parent(), pid, eng.ws.root, source_mode=mode, git_url=url,
                                 branch=branch, bind_state=state, last_error=error,
                                 delivery_target=db.meta_get("git_delivery_source") or url or mode)
    record("PENDING_UPSTREAM")
    if cfg.runtime_backend == "loopforge":
        try:
            token = cfg.effective_loopforge_token() or local_service
            if not token:
                raise LoopForgeError("请配置部署服务身份或登录后重试")
            client = LoopForgeClient(server_url(), token)
            project = next((p for p in await client.list_projects() if p.get("id") == pid), None)
            if project is not None:
                if isinstance(token, SessionCookie):
                    await ctx.verify_project_access(str(token), server_url(), pid)
            adopting = db.meta_get("binding_existing") == "1"
            if adopting and project is not None and isinstance(token, SessionCookie):
                await _require_native_binding_manager(ctx, str(token), pid)
            if adopting and project is None:
                raise LoopForgeError("待接入的原生项目已不存在", 409)
            if project is not None and not db.meta_get("upstream_create_intent") and not adopting:
                raise LoopForgeError("原生项目已存在，请从原生项目接入", 409)
            if project is None:
                db.meta_set("upstream_create_intent", pid)
                options = {}
                if cfg.execution_pool == "public_docker":
                    from autodev import public_pool
                    await public_pool.provision(client)
                    company = LoopForgeClient(server_url(), public_pool.service_token())
                    member = await public_pool.resolve_pool_user(company)
                    if not member.get("id") or public_pool.is_admin_user(member):
                        raise LoopForgeError("公用执行身份必须是原生普通账号", 409)
                    options["members"] = [{"userId": member["id"], "role": "member"}]
                project = await client.create_project(project_id=pid, name=name, description=f"Vernal {mode}", **options)
            if project.get("id") != pid:
                raise LoopForgeError("上游未确认项目 ID")
            if url:
                repo = f"{pid}-repo"
                is_mirror = bool(db.meta_get("gitea_mirror"))
                clone = local_git_bridge.urls(pid)[0] if is_mirror else url
                await client.put_repository(
                    _native_repository_payload(
                        repo,
                        pid,
                        name,
                        clone_url=clone,
                        repo_url=url,
                        branch=branch,
                        gitea_mirror=is_mirror,
                    )
                )
                if not db.meta_get("git_delivery_source"):
                    await client.put_project({**project, "defaultRepositoryId": repo})
                cfg.loopforge_repository_id = repo
            await app_mod.ensure_vernal_loopforge_assets(client, pid, cfg)
            ctx.proj_perm_cache.clear()
        except LoopForgeError as exc:
            record("PENDING_UPSTREAM", str(exc))
            raise HTTPException(409 if exc.status_code == 409 else 503, {"bind_state": "PENDING_UPSTREAM", "project_id": pid,
                                      "retry": "/vernal-api/project/resume", "error": str(exc)}) from exc
    ctx.persist_config(eng)
    record("READY")
    return cfg.runtime_backend == "loopforge", repo


@router.post("/vernal-api/project/resume")
async def resume_binding(request: Request, req: ProjectSelectRequest):
    ctx = get_ctx(request)
    info = load_project_registry(ctx.workspaces_parent()).get(req.project_id, {})
    if info.get("bind_state") != "PENDING_UPSTREAM":
        raise HTTPException(404, "没有待恢复的绑定")
    eng = Engine(Workspace(Path(info["root"])))
    try:
        if eng.db.meta_get("binding_owner") != effective_actor():
            raise HTTPException(403, "仅原登记人可恢复")
        local_service = ctx.initial_engine.settings.service_token() if is_loopback(request) and not request.headers.get("cookie") else ""
        await _complete_binding(ctx, eng, local_service)
    except BaseException:
        eng.db.close()
        raise
    slot = ctx.register_slot(eng)
    ctx.update_cli_default_if_unscoped(request, slot)
    return {"project_id": req.project_id, "name": eng.db.meta_get("binding_display_name") or req.project_id,
            "bind_state": "READY", "collaboration_mode": eng.settings.collaboration_mode, "branch": eng.settings.base_branch}


@router.get("/vernal-api/projects")
async def native_projects(request: Request):
    ctx = get_ctx(request)
    try:
        if hasattr(request.state, "allowed_projects"):
            _, native = await ctx.access_scope(request.headers["cookie"])
            return list(native.values())
        return await ctx.lf_client().list_projects()
    except LoopForgeError as exc:
        raise HTTPException(exc.status_code or 503, str(exc)) from exc


@router.post("/vernal-api/project/select")
async def api_select_project(request: Request, req: ProjectSelectRequest) -> dict[str, Any]:
    ctx = get_ctx(request)
    pid = req.project_id.strip()
    if not pid or not PROJECT_ID_RE.fullmatch(pid) or pid in (".", ".."):
        raise HTTPException(status_code=400, detail="非法的项目标识")

    cur_slot = ctx.active_slot(request)
    cookie = request.headers.get("cookie", "")
    if cookie or not is_loopback(request):
        await ctx.verify_project_access(cookie, cur_slot.engine.settings.loopforge_url, pid)

    if not cookie and cur_slot.engine.settings.runtime_backend == "loopforge" and cur_slot.engine.settings.service_token():
        try:
            native = await ctx.lf_client(cur_slot).list_projects()
        except LoopForgeError as exc:
            raise HTTPException(exc.status_code or 503, "无法读取原生目录") from exc
        if not any(p.get("id") == pid for p in native):
            raise HTTPException(404, "原生项目不存在")

    if cur_slot.engine.settings.loopforge_project_id == pid:
        return {"switched": False, "project_id": pid, "workspace": cur_slot.engine.ws.root.name}

    target_slot = ctx.find_or_open_slot(pid)
    if target_slot is None:
        return {"switched": True, "project_id": pid, "workspace_initialized": False, "needs_binding": True}
    target_eng = target_slot.engine
    ctx.update_cli_default_if_unscoped(request, target_slot)
    return {
        "switched": True,
        "project_id": pid,
        "workspace": target_eng.ws.root.name,
        "workflow_id": target_eng.settings.loopforge_workflow_id,
    }


@router.get("/vernal-api/host/dirs")
def api_host_dirs(request: Request, path: str = "") -> dict[str, Any]:
    _require_host_access(request)
    try:
        return list_host_directories(path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/vernal-api/project/create")
async def api_create_project(request: Request, req: ProjectCreateRequest) -> dict[str, Any]:
    if req.bind_existing and not req.project_id.strip():
        raise HTTPException(422, "请选择要绑定的原生项目")
    request.state.bind_existing = req.bind_existing
    ctx = get_ctx(request)
    mode = (req.mode or "scratch").strip().lower()
    if mode not in ("scratch", "local_folder", "git_repo"):
        raise HTTPException(status_code=422, detail=f"不支持的创建方式：{mode}")

    seed_eng = ctx.active_slot(request).engine
    parent = ctx.workspaces_parent()
    parent.mkdir(parents=True, exist_ok=True)

    if mode == "scratch":
        display_name = req.name.strip() or "新项目"
        pid = req.project_id.strip() or slugify_project_id(display_name, fallback="proj")
        if not PROJECT_ID_RE.fullmatch(pid) or pid in (".", ".."):
            raise HTTPException(status_code=400, detail="项目标识仅支持字母、数字、点、下划线和短横线")
        collab = req.collaboration_mode.strip().lower() or "files"
        if collab not in ("files", "git"):
            raise HTTPException(status_code=422, detail="协作方式仅支持 files 或 git")
        ws_dir = parent / pid
        await _guard_project_target(request, pid, ws_dir, allow_same_dir=False)
        mirror_url = ""
        if collab == "git" and local_git_bridge.password_path().is_file():
            try:
                with TemporaryDirectory(dir=parent) as tmp:
                    stage = Path(tmp) / "repo"
                    stage.mkdir()
                    gitops.ensure_repo(stage, "main")
                    mirror_url = await local_git_bridge.prepare(pid, stage, "main")
                    stage.rename(ws_dir)
            except (ValueError, OSError, gitops.GitError, httpx.HTTPError) as exc:
                raise HTTPException(422, f"新项目 Git 基线初始化失败：{exc}") from exc
        ws = init_workspace(ws_dir, name=display_name, collaboration_mode=collab)
        return await _finalize_launched_project(
            request,
            ws=ws,
            pid=pid,
            display_name=display_name,
            collab_mode=collab,
            source_mode="scratch",
            seed_eng=seed_eng,
            git_source=ws.root if collab == "git" else None,
            git_url=mirror_url,
            source_commit=gitops.head_sha(ws.root) if collab == "git" else "",
            needs_wish=not req.wish.strip() and not req.docs,
            wish=req.wish,
            docs=req.docs,
            process_template=req.process_template,
            execution_pool=req.execution_pool,
            harness=req.harness,
            start_council=req.start_council,
            guided_delivery=req.guided_delivery,
            intent=req.intent,
            actor=req.actor,
        )

    if mode == "local_folder":
        _require_host_access(request)
        if not req.local_path.strip():
            raise HTTPException(status_code=400, detail="请选择或填写本机项目文件夹路径")
        try:
            folder = resolve_safe_host_dir(req.local_path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        display_name = req.name.strip() or folder.name
        pid = req.project_id.strip() or slugify_project_id(display_name, fallback="local")
        if not PROJECT_ID_RE.fullmatch(pid) or pid in (".", ".."):
            raise HTTPException(status_code=400, detail="非法的项目标识")
        is_git = (folder / ".git").exists()
        detected_branch = gitops.current_branch(folder) if is_git else ""
        collab = req.collaboration_mode.strip().lower() or ("git" if is_git else "files")
        if collab not in ("files", "git"):
            collab = "git" if is_git else "files"
        if collab == "files":
            _, needs_git, _ = await asyncio.to_thread(
                scan_workspace_source_files_detailed, folder, max_total_bytes=FILE_EXCHANGE_MAX_TOTAL, require_lossless=True
            )
            if needs_git:
                collab = "git"
        target_branch = req.branch.strip() or detected_branch or ("main" if not is_git and collab == "git" else "")
        mirror_url = ""
        empty_source = not any(p.name != ".git" for p in folder.iterdir())
        source_commit = ""
        workspace = folder
        if collab == "git":
            if not target_branch:
                raise HTTPException(422, "Git 接管需要一个已有提交的本地分支，请选择分支或使用文件模式")
            workspace = parent / ".local-git" / pid
            await _guard_project_target(request, pid, workspace)
            if workspace.exists() or pid in (".", ".."):
                raise HTTPException(409, "该 Git 工作区已存在，请选择另一个项目名称")
            if any(p.exists() and p.samefile(folder) for p in (workspace.parent, *workspace.parent.parents)):
                raise HTTPException(422, "请选择独立的项目仓库，平台工作区应位于源仓库之外")
            workspace.parent.mkdir(exist_ok=True)
            try:
                if not is_git:
                    local_git_bridge.initialize_directory(folder, target_branch, workspace.parent)
                with TemporaryDirectory(dir=workspace.parent) as tmp:
                    clone = Path(tmp) / "repo"
                    local_git.clone_source(folder, clone, target_branch)
                    source_commit = gitops.head_sha(clone)
                    if local_git_bridge.password_path().is_file():
                        mirror_url = await local_git_bridge.prepare(pid, clone, target_branch)
                        existing = gitops.git(["remote", "get-url", "vernal-bridge"], folder, check=False)
                        if existing.returncode:
                            gitops.git(["remote", "add", "vernal-bridge", mirror_url], folder)
                    clone.rename(workspace)
            except (ValueError, gitops.GitError, OSError, httpx.HTTPError) as exc:
                raise HTTPException(422, f"无法创建本机项目工作副本：{exc}") from exc
        else:
            await _guard_project_target(request, pid, folder, allow_same_dir=True)
        ws = init_workspace(workspace, name=display_name, collaboration_mode=collab, base_branch=target_branch or None)
        return await _finalize_launched_project(
            request,
            ws=ws,
            pid=pid,
            display_name=display_name,
            collab_mode=collab,
            source_mode="local_folder",
            seed_eng=seed_eng,
            git_source=folder if collab == "git" else None,
            git_url=mirror_url,
            needs_wish=empty_source and not req.wish.strip() and not req.docs,
            source_commit=source_commit,
            branch=target_branch,
            wish=req.wish,
            docs=req.docs,
            process_template=req.process_template,
            execution_pool=req.execution_pool,
            harness=req.harness,
            start_council=req.start_council,
            guided_delivery=req.guided_delivery,
            intent=req.intent,
            actor=req.actor,
        )

    git_url = req.git_url.strip()
    if not git_url:
        raise HTTPException(status_code=400, detail="请填写 Git 仓库地址")
    try:
        validate_external_git_url(
            git_url,
            allow_local_path=is_loopback(request),
            allowed_internal_urls=(seed_eng.settings.gitea_url,),
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if req.git_token and not git_url.lower().startswith("https://"):
        raise HTTPException(422, "使用访问令牌的仓库需要 HTTPS")
    repo_basename = git_url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git") or "git-project"
    display_name = req.name.strip() or repo_basename
    pid = req.project_id.strip() or slugify_project_id(display_name, fallback="repo")
    if not PROJECT_ID_RE.fullmatch(pid) or pid in (".", ".."):
        raise HTTPException(status_code=400, detail="非法的项目标识")
    target_dir = parent / pid
    await _guard_project_target(request, pid, target_dir, allow_same_dir=False)
    branch = req.branch.strip()
    clone_args = ["clone", "--no-local"]
    access_id = ""
    if branch:
        clone_args.extend(["--branch", branch])
    try:
        with TemporaryDirectory(dir=parent) as tmp:
            stage = Path(tmp) / "repo"
            clone_args.extend(["--", git_url, str(stage)])
            if req.git_token:
                await asyncio.to_thread(local_git_bridge.remote_git, parent, git_url, req.git_username, req.git_token, clone_args)
            else:
                await asyncio.to_thread(gitops.git, clone_args, parent)
            local_git.validate_checkout(stage)
            stage.rename(target_dir)
            if req.git_token:
                try:
                    access_id = local_git_bridge.save_remote_access(git_url, req.git_username, req.git_token)
                except OSError:
                    target_dir.rename(stage)
                    raise
    except (ValueError, OSError, gitops.GitError) as exc:
        raise HTTPException(400, f"克隆仓库失败：{exc}") from exc

    actual_branch = branch
    try:
        actual_branch = gitops.current_branch(target_dir)
    except Exception:
        pass
    ws = init_workspace(target_dir, name=display_name, collaboration_mode="git", base_branch=actual_branch or None)
    return await _finalize_launched_project(
        request,
        ws=ws,
        pid=pid,
        display_name=display_name,
        collab_mode="git",
        source_mode="git_repo",
        seed_eng=seed_eng,
        git_source=ws.root if req.git_token else None,
        private_remote=access_id,
        source_commit=gitops.head_sha(ws.root) if req.git_token else "",
        git_url=git_url,
        branch=actual_branch,
        wish=req.wish,
        docs=req.docs,
        process_template=req.process_template,
        execution_pool=req.execution_pool,
        harness=req.harness,
        start_council=req.start_council,
        guided_delivery=req.guided_delivery,
        intent=req.intent,
        actor=req.actor,
    )


@router.post("/vernal-api/project/import-folder")
async def api_import_folder(request: Request, req: ImportFolderRequest) -> dict[str, Any]:
    if req.bind_existing and not req.project_id.strip():
        raise HTTPException(422, "请选择要绑定的原生项目")
    request.state.bind_existing = req.bind_existing
    ctx = get_ctx(request)
    display_name = req.name.strip() or "导入项目"
    pid = req.project_id.strip() or slugify_project_id(display_name, fallback="import")
    if not PROJECT_ID_RE.fullmatch(pid) or pid in (".", ".."):
        raise HTTPException(status_code=400, detail="非法的项目标识")
    if not req.files:
        raise HTTPException(status_code=400, detail="未收到任何文件")
    if len(req.files) > 500:
        raise HTTPException(status_code=400, detail="单次浏览器导入最多支持 500 个文本文件，大型仓库请使用「接管本机文件夹」或「接入 Git 仓库」")

    parent = ctx.workspaces_parent()
    target_dir = parent / pid
    await _guard_project_target(request, pid, target_dir, allow_same_dir=False)

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
                f"超限受影响文件：{sample}。请精简大文件或改用「接入 Git 仓库」/「接管本机 Git 目录」。"
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

    ws = init_workspace(target_dir, name=display_name, collaboration_mode="files")
    result = await _finalize_launched_project(
        request,
        ws=ws,
        pid=pid,
        display_name=display_name,
        collab_mode="files",
        source_mode="import_folder",
        seed_eng=ctx.active_slot(request).engine,
        wish=req.wish,
        execution_pool=req.execution_pool,
        harness=req.harness,
        start_council=req.start_council,
        guided_delivery=req.guided_delivery,
        intent=req.intent,
        actor=req.actor,
    )
    result["imported_files"] = len(staged)
    return result
