"""Private Gitea mirror for mounted projects. Credentials live outside project trees."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from urllib.parse import quote, urlsplit

import httpx

from autodev import gitops, public_pool
from autodev.local_git import LeaseConflict


def initialize_directory(source: Path, branch: str, staging: Path) -> None:
    if gitops.git(["rev-parse", "--show-toplevel"], source, check=False).returncode == 0:
        raise ValueError("该目录属于另一个 Git 仓库，请选择仓库根目录")
    gitops.git(["check-ref-format", "--branch", branch], staging)
    # Build the complete index elsewhere, so an interrupted import leaves the user's files intact.
    with TemporaryDirectory(dir=staging) as temp:
        repo = Path(temp)
        gitops.git(["init", "-q", "-b", branch], repo)
        dotgit = repo / ".git"
        (dotgit / "info/exclude").write_text(gitops.GITIGNORE + "\n.env\n.env.*\n*.pem\n*.key\n")
        args = [f"--git-dir={dotgit}", f"--work-tree={source}", "-c", "core.hooksPath=/dev/null"]
        gitops.git([*args, "add", "-A", "--", "."], source)
        gitops.git([*args, "-c", "user.name=Vernal", "-c", "user.email=vernal@localhost",
                    "commit", "--allow-empty", "-qm", "Vernal initial project baseline"], source)
        # Mounted sources can be on another filesystem. Stage on that filesystem before publishing .git.
        with TemporaryDirectory(dir=source, prefix=".vernal-init-") as staging_dir:
            local = Path(staging_dir) / "repository"
            shutil.copytree(dotgit, local)
            if os.path.lexists(source / ".git"):
                raise ValueError("目录已被初始化，请重新选择分支")
            local.rename(source / ".git")


def password_path() -> Path:
    return public_pool.directory() / "gitea-password"


def validate_https_remote(url: str) -> None:
    from autodev.security import validate_external_git_url

    parts = urlsplit(url)
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment or any(c.isspace() for c in url)):
        raise ValueError("请输入无内嵌凭据的 HTTPS 仓库地址")
    validate_external_git_url(url)


def remote_git(cwd: Path, url: str, username: str, token: str, args: list[str]) -> str:
    validate_https_remote(url)
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": "https", "GIT_CONFIG_COUNT": "0"}
    if token:
        auth = base64.b64encode(f"{username or 'oauth2'}:{token}".encode()).decode()
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0=f"http.{url}.extraHeader",
                   GIT_CONFIG_VALUE_0="Authorization: Basic " + auth)
    try:
        proc = subprocess.run(["git", "-c", "credential.helper=", "-c", "http.followRedirects=false",
                               "-c", "core.hooksPath=/dev/null", *args], cwd=cwd, env=env,
                              capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise gitops.GitError("远端 Git 操作超时，请检查仓库可达性后重试") from None
    if proc.returncode:
        raise gitops.GitError("远端 Git 操作失败，请检查仓库、分支及令牌权限")
    return proc.stdout.strip()


def save_remote_access(url: str, username: str, token: str) -> str:
    root = public_pool.directory() / "remote-access"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with NamedTemporaryFile("w", dir=root, delete=False) as tmp:
        json.dump({"url": url, "username": username, "token": token}, tmp)
    access_id = secrets.token_hex(16)
    Path(tmp.name).replace(root / f"{access_id}.json")
    return access_id


def _push_with_lease(call, url, ref, commit, expected):
    def current():
        line = call(["ls-remote", "--refs", "--", url, ref]).split()
        return line[0] if line else ""
    head = current()
    if head == commit:
        return
    if head != expected:
        raise LeaseConflict("远端已有同名交付分支，未覆盖现有提交")
    try:
        call(["push", f"--force-with-lease={ref}:{expected}", "--", url, f"{commit}:{ref}"])
    except gitops.GitError:
        head = current()
        if head == commit:
            return
        if head != expected:
            raise LeaseConflict("交付期间远端分支已被修改") from None
        raise


def push_remote_branch(root: Path, url: str, username: str, token: str, ref: str, commit: str, expected="") -> None:
    _push_with_lease(lambda args: remote_git(root, url, username, token, args), url, ref, commit, expected)


def publish_private_remote(root: Path, access_id: str, branch: str, commit: str, expected="") -> None:
    publish_private_ref(root, access_id, "refs/heads/" + branch, commit, expected)


def publish_private_ref(root: Path, access_id: str, ref: str, commit: str, expected="") -> None:
    if not re.fullmatch(r"[a-f0-9]{32}", access_id):
        raise gitops.GitError("该项目的仓库凭据记录无效")
    access = json.loads((public_pool.directory() / "remote-access" / f"{access_id}.json").read_text())
    push_remote_branch(root, access["url"], access["username"], access["token"], ref, commit, expected)


def urls(project: str) -> tuple[str, str]:
    path = f"/vernal-local/{quote(project, safe='')}.git"
    return (os.getenv("VERNAL_GITEA_URL", "http://127.0.0.1:3000").rstrip("/") + path,
            os.getenv("VERNAL_GITEA_HOST_URL", "http://127.0.0.1:3000").rstrip("/") + path)


def mirror_git(workspace: Path, args: list[str]) -> str:
    secret = password_path().read_text().strip()
    auth = base64.b64encode(f"vernal-local:{secret}".encode()).decode()
    env = {**os.environ, "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader",
           "GIT_CONFIG_VALUE_0": "Authorization: Basic " + auth, "GIT_TERMINAL_PROMPT": "0"}
    p = subprocess.run(["git", "-c", "http.followRedirects=false", "-c", "core.hooksPath=/dev/null", *args],
                       cwd=workspace, env=env, capture_output=True, text=True)
    if p.returncode:
        raise gitops.GitError("无法同步私有 Gitea 仓库，请检查服务状态、访问权限及目标分支是否冲突")
    return p.stdout.strip()


class MirrorOwnershipConflict(ValueError):
    pass


async def ensure_private_repo(project: str, marker: str = "") -> None:
    base = os.getenv("VERNAL_GITEA_URL", "http://127.0.0.1:3000").rstrip("/")
    async with httpx.AsyncClient(base_url=base, auth=("vernal-local", password_path().read_text().strip()),
                                 trust_env=False, timeout=12) as client:
        route = f"/api/v1/repos/vernal-local/{quote(project, safe='')}"
        check = await client.get(route)
        if check.status_code == 404:
            new = await client.post("/api/v1/user/repos", json={"name": project, "private": True, "auto_init": False, "description": marker})
            if new.status_code not in (201, 409, 422):
                raise ValueError(f"Gitea 创建镜像失败（HTTP {new.status_code}）")
            check = await client.get(route)
        if check.status_code != 200 or check.json().get("private") is not True:
            raise ValueError("Gitea 私有仓库不可用")
        if marker and check.json().get("description") != marker:
            raise MirrorOwnershipConflict("Gitea 任务仓库名称已被占用")


async def prepare(project: str, workspace: Path, branch: str) -> str:
    await ensure_private_repo(project)
    internal, external = urls(project)
    ref = f"refs/heads/{branch}"
    await asyncio.to_thread(push_once, workspace, internal, gitops.head_sha(workspace), ref)
    return external


def push_once(workspace: Path, url: str, commit: str, ref: str) -> None:
    _push_with_lease(lambda args: mirror_git(workspace, args), url, ref, commit, "")


def deliver(project: str, workspace: Path, branch: str, commit: str, expected="") -> None:
    internal, _ = urls(project)
    ref = f"refs/heads/{branch}"
    _push_with_lease(lambda args: mirror_git(workspace, args), internal, ref, commit, expected)
    mirror_git(workspace, ["fetch", "--no-tags", internal, ref])
    if gitops.head_sha(workspace, "FETCH_HEAD") != commit:
        raise gitops.GitError("Gitea 返回的交付分支与本次里程碑不一致")


from contextlib import asynccontextmanager


@asynccontextmanager
async def mirror_lock(db, key):
    import fcntl
    folder = db.path.parent / "mirror-locks"
    folder.mkdir(exist_ok=True)
    with (folder / key).open("a") as guard:
        while True:
            try:
                fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.1)
        yield


async def task_remote(workspace, settings, db, task_id: str, snapshot: str) -> str:
    from autodev.loopforge.bridge import sign_context_sig

    project = settings.loopforge_project_id
    key = hashlib.sha256(f"{project}:{task_id}".encode()).hexdigest()
    async with mirror_lock(db,key):
        repo = db.meta_get(f"task_mirror:{task_id}") or "vernal-task-" + key[:32]
        try:
            await ensure_private_repo(repo, marker=f"Vernal task {key}")
        except MirrorOwnershipConflict:
            if db.meta_get(f"gitea_task:{repo}") != task_id or repo.endswith("-v2"):
                raise
            # Older unmarked repositories may contain human data. Leave their existing leases intact.
            repo += "-v2"
            await ensure_private_repo(repo, marker=f"Vernal task {key}")
        db.meta_set(f"task_mirror:{task_id}", repo)
        upstream, _ = urls(repo)
        await asyncio.to_thread(push_once, workspace, upstream, snapshot, f"refs/heads/base/{snapshot}")
        db.meta_set(f"gitea_task:{repo}", task_id)
        expires = int(time.time()) + max(3600, settings.agent_timeout_sec + 900)
        db.meta_set(f"gitea_expiry:{repo}", str(expires))
        sig = sign_context_sig(settings.project_signing_key(), project, f"gitea:{repo}:{expires}")
        base = settings.vernal_public_url.rstrip("/")
        return f"{base}/vernal-api/git-bridge/{project}/{repo}/{expires}/{sig}"


async def collect_task_mirrors(engine):
    if engine.db.meta_get("gitea_mirror") != "1":
        return
    db = engine.db
    project = engine.settings.loopforge_project_id
    base = os.environ.get("VERNAL_GITEA_URL", "http://127.0.0.1:3000").rstrip("/")
    for task in db.list_tasks():
        if task.status not in ("MERGED", "CANCELLED"):
            continue
        key = hashlib.sha256(f"{project}:{task.task_id}".encode()).hexdigest()
        repo = db.meta_get(f"task_mirror:{task.task_id}") or "vernal-task-" + key[:32]
        if db.meta_get(f"gitea_task:{repo}") != task.task_id:
            continue
        expiry = db.meta_get(f"gitea_expiry:{repo}")
        if not expiry:
            db.meta_set(f"gitea_expiry:{repo}", str(int(time.time()) + max(3600, engine.settings.agent_timeout_sec + 900)))
            continue
        async with mirror_lock(db,key):
            expiry = db.meta_get(f"gitea_expiry:{repo}")
            if not expiry or db.meta_get(f"gitea_task:{repo}") != task.task_id:
                continue
            if int(expiry) >= time.time():
                continue
            async with httpx.AsyncClient(base_url=base, auth=("vernal-local", password_path().read_text().strip()), trust_env=False) as client:
                route = "/api/v1/repos/vernal-local/" + repo
                info = await client.get(route)
                if info.status_code != 404:
                    info.raise_for_status()
                    if info.json().get("description") != "Vernal task " + key or info.json().get("private") is not True:
                        continue
                    # A renewed execution or lease supersedes this collection pass.
                    if db.get_task(task.task_id).status not in ("MERGED","CANCELLED") or db.meta_get(f"gitea_expiry:{repo}") != expiry:
                        continue
                    result = await client.delete(route)
                    if result.status_code != 404:
                        result.raise_for_status()
                db.meta_set(f"gitea_task:{repo}", "")


def restore_command(remote: str, snapshot: str) -> str:
    url = shlex.quote(remote)
    return (
        'if previous=$(git -C vernal-source rev-parse --verify refs/vernal/base 2>/dev/null); then '
        f'test "$previous" = {snapshot} && git -C vernal-source remote set-url vernal-task {url}; else '
        f'git clone -q --depth=1 --single-branch --no-tags --branch base/{snapshot} --origin vernal-task '
        f'-- {url} vernal-source && test "$(git -C vernal-source rev-parse HEAD)" = {snapshot} && '
        f'git -C vernal-source update-ref refs/vernal/base {snapshot}; fi'
    )


async def relay(request, project: str, repo: str, expires: int, sig: str, operation: str, engine):
    from fastapi import HTTPException
    from fastapi.responses import StreamingResponse
    from starlette.background import BackgroundTask
    from autodev.loopforge.bridge import verify_context_sig

    if (project != engine.settings.loopforge_project_id or expires < time.time()
            or not any(verify_context_sig(key, project, f"gitea:{repo}:{expires}", sig) for key in engine.settings.verification_keys())
            or not engine.db.meta_get(f"gitea_task:{repo}")):
        raise HTTPException(403, "Git 任务仓库票据无效或过期")
    allowed = ("git-upload-pack", "git-receive-pack")
    params = {}
    if request.method == "GET" and operation == "info/refs" and request.query_params.get("service") in allowed:
        params["service"] = request.query_params["service"]
    elif request.method != "POST" or operation not in allowed:
        raise HTTPException(404)
    internal, _ = urls(repo)
    headers = {k: request.headers[k] for k in ("content-type", "git-protocol") if k in request.headers}
    client = httpx.AsyncClient(auth=("vernal-local", password_path().read_text().strip()), trust_env=False, timeout=120)
    try:
        req = client.build_request(request.method, internal + "/" + operation, headers=headers, params=params,
                                   content=request.stream() if request.method == "POST" else None)
        response = await client.send(req, stream=True)
    except httpx.HTTPError:
        await client.aclose()
        raise HTTPException(502, "Gitea Git 传输失败") from None
    if response.status_code != 200:
        await response.aclose()
        await client.aclose()
        raise HTTPException(502, "Gitea 拒绝任务仓库传输")
    async def close():
        await response.aclose()
        await client.aclose()
    return StreamingResponse(response.aiter_bytes(), media_type=response.headers.get("content-type"),
                             headers={"Cache-Control": "no-store"}, background=BackgroundTask(close))


async def export_release(project: str, milestone: str, files: dict[str, str], url: str = "",
                         username: str = "", token: str = "") -> str:
    from autodev.artifacts import materialize

    branch = f"vernal/{project}/{milestone}"
    ref = f"refs/heads/{branch}"
    if url:
        validate_https_remote(url)
    else:
        if not password_path().is_file():
            raise ValueError("内置 Gitea 尚未初始化")
        await ensure_private_repo(project)
    with TemporaryDirectory() as tmp:
        root = Path(tmp) / "release"
        materialize(files, root)
        gitops.git(["check-ref-format", ref], root)
        gitops.git(["init", "-q", "-b", "main"], root)
        gitops.git(["add", "--force", "--all"], root)
        env = {**os.environ, "GIT_AUTHOR_DATE":"2000-01-01T00:00:00Z", "GIT_COMMITTER_DATE":"2000-01-01T00:00:00Z",
               "GIT_TERMINAL_PROMPT":"0", "GIT_CONFIG_GLOBAL":os.devnull, "GIT_CONFIG_NOSYSTEM":"1"}
        subprocess.run(["git", "-c", "user.name=Vernal", "-c", "user.email=vernal@localhost", "-c", "core.hooksPath=/dev/null",
                        "commit", "--allow-empty", "-qm", branch], cwd=root, env=env, check=True)
        commit = gitops.head_sha(root)
        if not url:
            push_once(root, urls(project)[0], commit, ref)
            return branch
        push_remote_branch(root, url, username, token, ref, commit)
    return branch
