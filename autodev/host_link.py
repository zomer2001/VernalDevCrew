"""Portable outbound client for a project-scoped Vernal link."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import ntpath
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit

import httpx

from autodev import gitops

LIMIT = 200 * 1024 * 1024
RESERVED = {".git", ".autodev", ".vernal"}
IGNORED = RESERVED | {".ssh", ".aws", ".gnupg", ".venv", "node_modules", "__pycache__", ".pytest_cache", "dist", "build"}
HEX = re.compile(r"[a-f0-9]{40}(?:[a-f0-9]{24})?")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def windows_reserved(part):
    # ntpath works on the server as well as on Windows. Python 3.11/3.12 use pathlib.
    if hasattr(ntpath, "isreserved"):
        return ntpath.isreserved(part)
    return PureWindowsPath(part).is_reserved()


def valid_name(name):
    parts = name.split("/")
    if ("\\" in name or any(not part or part in (".", "..") or part.casefold() in RESERVED
           or part.endswith((" ", ".")) or windows_reserved(part)
           or re.search(r'[<>:"|?*\x00-\x1f]', part) for part in parts)):
        raise ValueError(f"不支持的项目路径 {name!r}")
    return name


def target_path(root, name):
    target = root / valid_name(name)
    for component in [target, *target.parents]:
        if component == root:
            break
        if component.is_symlink() or getattr(component, "is_junction", lambda: False)():
            raise ValueError(f"路径包含链接 {name}")
    return target


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError(f"拒绝写入符号链接 {path.name}")
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as fp:
        tmp = Path(fp.name)
        fp.write(data)
    try:
        if path.is_file():
            tmp.chmod(stat.S_IMODE(path.stat().st_mode))
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def zip_members(z):
    entries = z.infolist()
    if len(entries) > 100000 or sum(i.file_size for i in entries) > LIMIT:
        raise ValueError("项目文件超过 200 MiB 或条目过多")
    seen = set()
    for i in entries:
        if i.is_dir():
            continue
        name = valid_name(i.filename)
        if name.casefold() in seen or stat.S_ISLNK(i.external_attr >> 16):
            raise ValueError(f"路径重名或包含链接 {name}")
        seen.add(name.casefold())
        yield name, i


def unpack_zip(archive, root):
    with zipfile.ZipFile(archive) as z:
        for name, info in zip_members(z):
            target = target_path(root, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, target.open("xb") as dst:
                shutil.copyfileobj(src, dst)
            if info.external_attr >> 16 & 0o100:
                target.chmod(0o755)


def plain_baseline(root, archive):
    # A separate index honors nested .gitignore files without adding .git to the user's folder.
    with tempfile.TemporaryDirectory() as tmp:
        gitops.git(["init", "-q", "--bare", tmp], root)
        names = subprocess.check_output(["git", "--git-dir", tmp, "--work-tree", str(root),
                                         "ls-files", "-z", "--others", "--exclude-standard"], cwd=root)
    manifest = {}
    total = 0
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for raw in names.split(b"\0"):
            if not raw:
                continue
            name = raw.decode("utf-8")
            parts = name.split("/")
            if any(p.casefold() in IGNORED or p == ".env" or p.startswith(".env.") or p.endswith((".pem", ".key")) for p in parts):
                continue
            path = target_path(root, name)
            if not path.is_file():
                continue
            total += path.stat().st_size
            if total > LIMIT:
                raise ValueError("目录基线超过 200 MiB")
            data = path.read_bytes()
            z.writestr(zipfile.ZipInfo.from_file(path, name), data, zipfile.ZIP_DEFLATED)
            manifest[name] = digest(data)
    return manifest


def git_baseline(root, archive):
    gitops.git(["bundle", "create", str(archive), "HEAD"], root)


def sync_folder(root, archive, manifest, milestone, version):
    if not re.fullmatch(r"[\w.-]+", milestone) or milestone in (".", ".."):
        raise ValueError("里程碑标识无效")
    conflict_root = root / ".vernal" / "conflicts" / f"{milestone}-v{int(version)}"
    for parent in (root / ".vernal", conflict_root.parent, conflict_root):
        if parent.is_symlink() or getattr(parent, "is_junction", lambda: False)():
            raise ValueError("冲突目录包含链接")
    updated = {}
    conflicts = []
    with zipfile.ZipFile(archive) as z:
        members = dict(zip_members(z))
        removed = manifest.keys() - members.keys()
        # Removing tracked children first also handles a directory becoming a file.
        for name in [*sorted(removed), *sorted(members)]:
            data = z.read(members[name]) if name in members else None
            new = digest(data) if data is not None else None
            if new is not None:
                updated[name] = new
            if new == manifest.get(name):
                continue
            try:
                path = target_path(root, name)
                if path.exists() and not path.is_file():
                    raise ValueError("路径类型不同")
                current = digest(path.read_bytes()) if path.is_file() else None
                if current == new:
                    continue
                if current != manifest.get(name):
                    raise ValueError("本机文件已修改")
                if data is None:
                    path.unlink()
                    parent = path.parent
                    while parent != root:
                        try:
                            parent.rmdir()
                        except OSError:
                            break
                        parent = parent.parent
                else:
                    atomic_write(path, data)
            except (ValueError, NotADirectoryError, IsADirectoryError, FileExistsError):
                conflicts.append(name)
                bucket = "incoming" if data is not None else "deletions"
                dest = target_path(conflict_root, bucket + "/" + name)
                atomic_write(dest, data if data is not None else b"")
    return updated, conflicts


def sync_git(root, bundle, delivery):
    sha = delivery["commit_sha"]
    if not HEX.fullmatch(sha):
        raise ValueError("交付提交无效")
    branch = delivery["rolling_branch"]
    archive = delivery["archive_branch"]
    refs = [f"refs/heads/{n}" for n in (branch, archive)]
    for ref in refs:
        gitops.git(["check-ref-format", ref], root)
        if not ref.startswith("refs/heads/vernal/"):
            raise ValueError("交付目标不在 Vernal 命名空间")
    advertised = gitops.git(["bundle", "list-heads", str(bundle), refs[1]], root).stdout.split()
    if advertised != [sha, refs[1]]:
        raise ValueError("下载的版本与账本提交不一致")
    gitops.git(["fetch", "--no-tags", "--no-write-fetch-head", "--", str(bundle), refs[1]], root)
    checked = gitops.git(["worktree", "list", "--porcelain"], root).stdout.splitlines()
    conflicts = []
    for ref, previous in ((refs[1], ""), (refs[0], delivery.get("prev_commit_sha") or "")):
        head = gitops.git(["rev-parse", "--verify", "--quiet", ref], root, check=False).stdout.strip()
        if head == sha:
            continue
        if head != previous or "branch " + ref in checked:
            if ref == refs[1]:
                raise ValueError("归档分支已被本机占用")
            conflicts.append(branch)
            continue
        gitops.git(["update-ref", ref, sha, previous], root)
    return conflicts


def capabilities():
    found = {}
    for key, exe in (("codex", "codex"), ("claude-code", "claude"), ("opencode", "opencode"), ("dsh", "dsh")):
        binary = shutil.which(exe)
        if not binary and sys.platform == "win32" and os.environ.get("APPDATA"):
            candidate = Path(os.environ["APPDATA"]) / "npm" / (exe + ".cmd")
            binary = str(candidate) if candidate.is_file() else None
        if binary:
            found[key] = binary
    return found


def secure_url(url):
    parts = urlsplit(url)
    if (not parts.hostname or parts.username or parts.password or parts.query or parts.fragment or
        (parts.scheme != "https" and not (parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1", "::1")))):
        raise ValueError("跨机连接请使用 HTTPS，本机回环地址也可使用 HTTP")
    return url.rstrip("/")


def transient_error(exc):
    if isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and (
        exc.response.status_code in (408, 429) or 500 <= exc.response.status_code < 600
    )


def require(response):
    response.raise_for_status()
    return response


async def download(client, route, path, limit=LIMIT + 8*1024*1024):
    async with client.stream("GET", route) as response:
        response.raise_for_status()
        with path.open("wb") as fp:
            async for block in response.aiter_bytes():
                if fp.tell() + len(block) > limit:
                    raise ValueError("下载超出容量限制")
                fp.write(block)


async def stream_file(path):
    with path.open("rb") as fp:
        while block := fp.read(1024 * 1024):
            yield block


async def supervise_runner(client, route, state_dir):
    os_name = {"darwin": "darwin", "linux": "linux"}[sys.platform]
    arch = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64", "amd64": "amd64"}[platform.machine().lower()]
    programs = {}
    for name in ("agentflow-runner", "agentflow-gate-mcp"):
        filename = name if name == "agentflow-runner" else f"{name}-{os_name}-{arch}"
        installed = shutil.which(filename) or shutil.which(name)
        path = Path(installed) if installed else state_dir / filename
        if not path.is_file():
            temporary = path.with_suffix(".tmp")
            await download(client, f"/runners/{name}?os={os_name}&arch={arch}", temporary)
            temporary.chmod(0o700)
            temporary.replace(path)
        programs[name] = str(path)
    workspace = state_dir / "workspaces"
    workspace.mkdir(exist_ok=True)
    state = state_dir / "runner.json"
    pairing = state_dir / "runner-pairing"
    while True:
        if state.is_file() and state.stat().st_size:
            pairing.unlink(missing_ok=True)
            code = "session-reused"
        else:
            if not pairing.is_file():
                response = require(await client.post(route + "/runner-code"))
                atomic_write(pairing, response.json()["code"].encode())
            code = pairing.read_text(encoding="utf-8")
        proc = await asyncio.create_subprocess_exec(programs["agentflow-runner"], "-server", str(client.base_url).rstrip("/"), "-token", code,
                "-state", str(state), "-workspace", str(workspace), "-auto-update=false",
                env={**os.environ, "AGENTFLOW_GATE_MCP_BINARY": programs["agentflow-gate-mcp"],
                     "PATH": str(state_dir) + os.pathsep + os.environ.get("PATH", "")})
        try:
            await proc.wait()
        finally:
            if proc.returncode is None:
                proc.terminate()
                await proc.wait()
        await asyncio.sleep(2)


async def run_link(url, code, directory, with_worker=True):
    from autodev.harnesses.link_worker import serve_jobs
    root = directory.resolve(strict=True)
    url = secure_url(url)
    if not root.is_dir():
        raise ValueError("请选择本机项目目录")
    state_dir = Path.home() / ".vernal" / "links" / digest((url + "\0" + str(root)).encode())
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_file = state_dir / "session.json"
    state = json.loads(state_file.read_text()) if state_file.is_file() else {}
    if state and code and state["project"] != code.rsplit(".", 1)[0]:
        raise ValueError("该目录已连接到另一个项目。请为新项目选择独立目录")
    bins = capabilities() if with_worker else {}
    async with httpx.AsyncClient(base_url=url, timeout=120) as client:
        if not state:
            if not code or "." not in code:
                raise ValueError("首次绑定请从工作台取得配对码")
            project = code.rsplit(".", 1)[0]
            git = (root / ".git").exists()
            hello = {"code": code, "kind": "git" if git else "folder", "platform": sys.platform,
                     "clis": list(bins), "codex_login": (Path.home()/".codex"/"auth.json").is_file()}
            pair = require(await client.post("/vernal-api/link/exchange", params={"project": project}, json=hello)).json()
            state = {**pair, "kind": hello["kind"], "cursor": 0, "ready": False}
            atomic_write(state_file, json.dumps(state).encode())
        client.headers.update({"Authorization": "Bearer " + state["link_token"], "X-Vernal-Project": state["project"]})
        route = "/vernal-api/link/" + state["link_id"]
        if not state["ready"]:
            bundle = state_dir / "baseline"
            if not bundle.exists() or "manifest" not in state:
                temp = bundle.with_suffix(".tmp")
                temp.unlink(missing_ok=True)
                manifest = plain_baseline(root, temp) if state["kind"] == "folder" else git_baseline(root, temp)
                temp.replace(bundle)
                state["manifest"] = manifest or {}
                atomic_write(state_file, json.dumps(state).encode())
            while True:
                try:
                    require(await client.post(route + "/baseline", content=stream_file(bundle)))
                    break
                except httpx.HTTPError as exc:
                    if not transient_error(exc):
                        raise
                    print(f"基线上传中断，正在重试：{exc}", flush=True)
                    await asyncio.sleep(2)
            state["ready"] = True
            atomic_write(state_file, json.dumps(state).encode())
            bundle.unlink(missing_ok=True)
        print(f"已连接项目 {state['project']}，正在监听交付", flush=True)
        async def run_worker():
            while True:
                try:
                    if sys.platform == "win32":
                        await serve_jobs(client, route, bins)
                    else:
                        await supervise_runner(client, route, state_dir)
                    return
                except httpx.HTTPError as exc:
                    if transient_error(exc):
                        print(f"执行机连接中断，正在重连：{exc}", flush=True)
                        await asyncio.sleep(2)
                        continue
                    print(f"本机执行机已停止：{exc}", flush=True)
                    return
                except (OSError, ValueError) as exc:
                    print(f"本机执行机已停止：{exc}", flush=True)
                    return

        if sys.platform == "win32":
            require(await client.post(route + "/capabilities", json={
                "clis": list(bins), "codex_login": (Path.home() / ".codex" / "auth.json").is_file()
            }))
        worker = None
        if bins and (sys.platform == "win32" or state.get("native_runner", True)):
            worker = asyncio.create_task(run_worker())
        try:
            while True:
                try:
                    response = require(await client.get(route + "/events", params={"after": state["cursor"]}))
                    for delivery in response.json()["deliveries"]:
                        with tempfile.TemporaryDirectory() as tmp:
                            package = Path(tmp) / "release"
                            await download(client, route + f"/deliveries/{delivery['delivery_id']}", package)
                            if state["kind"] == "git":
                                conflicts = sync_git(root, package, delivery)
                            else:
                                manifest, conflicts = sync_folder(root, package, state["manifest"], delivery["milestone_id"], delivery["version_no"])
                            if conflicts:
                                print("保留本机改动：" + "、".join(conflicts), flush=True)
                                require(await client.post(route + "/conflicts", json={"delivery": delivery["delivery_id"], "paths": conflicts}))
                            if state["kind"] == "folder":
                                state["manifest"] = manifest
                            state["cursor"] = delivery["delivery_id"]
                            atomic_write(state_file, json.dumps(state).encode())
                except httpx.HTTPError as exc:
                    if not transient_error(exc):
                        raise
                    print(f"连接中断，正在重试：{exc}", flush=True)
                await asyncio.sleep(2)
        finally:
            if worker:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
