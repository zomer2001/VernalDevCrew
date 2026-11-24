"""Project bound pairing, baseline import and delivery transport."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from autodev import gitops, local_git
from autodev.ingest import write_catalog
from autodev.server.project_launcher import write_codebase_overview_doc
from autodev.host_link import LIMIT, digest, unpack_zip, valid_name
from autodev.loopforge.client import LoopForgeClient
from autodev.models import ProjectStatus


def tree_files(repo, revision):
    entries = {}
    for line in subprocess.check_output(["git", "ls-tree", "-r", "-z", revision], cwd=repo).split(b"\0"):
        if not line:
            continue
        meta, name = line.split(b"\t", 1)
        mode, kind, sha = meta.decode().split()
        if mode not in ("100644", "100755") or kind != "blob":
            raise ValueError("桥接基线包含符号链接或子模块")
        entries[valid_name(name.decode())] = (sha, mode)
    return entries


def prepare_baseline(engine, link, file, fingerprint, canonical):
    ws = engine.ws
    allowed = {".git", ".autodev", ".gitignore", ".opencode", "inputs", "docs"}
    if any(p.name not in allowed or p.is_symlink() for p in ws.root.iterdir()):
        raise HTTPException(409, "该项目已有本机文件，请选择空项目")
    if any(p.is_file() and p.name != ".gitkeep" for d in (ws.inputs_dir, ws.root / "docs") for p in d.rglob("*")):
        raise HTTPException(409, "该槽位已有项目资料")

    with tempfile.TemporaryDirectory(dir=ws.state_dir) as tmp:
        repo = Path(tmp) / "source"
        repo.mkdir()
        gitops.git(["init", "-q", "-b", "link-base"], repo)
        gitops.git(["config", "user.name", "Vernal"], repo)
        gitops.git(["config", "user.email", "vernal@localhost"], repo)
        if link["kind"] == "git":
            gitops.git(["fetch", "--no-tags", "--", str(file), "HEAD"], repo)
            head = gitops.head_sha(repo, "FETCH_HEAD")
            tree_files(repo, head)
            gitops.git(["reset", "--hard", head], repo)
        else:
            unpack_zip(file, repo)
            gitops.git(["add", "-f", "--all"], repo)
            gitops.git(["commit", "--allow-empty", "-qm", "Linked folder baseline"], repo)
            head = gitops.head_sha(repo)
        local_git.validate_checkout(repo)
        write_codebase_overview_doc(repo, source_mode="host_link", inputs_dir=ws.state_dir / "link-context")

        gitops.ensure_repo(ws.root, engine.settings.base_branch)
        if (ws.inputs_dir / ".gitkeep").is_file():
            gitops.commit_paths(ws.root, ["inputs/.gitkeep"], "Vernal workspace inputs")
        if not gitops.is_clean(ws.root):
            raise HTTPException(409, "工作区含有未提交修改")
        start = gitops.head_sha(ws.root)
        gitops.git(["fetch", "--no-tags", "--", str(ws.root), start], repo)
        gitops.git(["checkout", "-qb", "platform-import", start], repo)
        gitops.git(["merge", "--allow-unrelated-histories", "--no-edit", "-X", "theirs", head], repo)
        gitops.git(["config", "vernal.importOwner", link["id"]], repo)
        gitops.git(["config", "vernal.importHash", fingerprint], repo)
        gitops.git(["config", "vernal.importStart", start], repo)
        repo.rename(canonical)


def install_baseline(engine, link, file, fingerprint):
    db, ws = engine.db, engine.ws
    with db.transaction():
        prior = db._query("SELECT baseline_hash FROM host_links WHERE id=?", (link["id"],))[0][0]
        if prior:
            if prior != fingerprint:
                raise HTTPException(409, "此连接已上传其他基线")
            engine.settings.collaboration_mode = "git"
            return
        if (db.meta_get("source_mode") or db.list_tasks() or db.list_milestones() or engine.status != ProjectStatus.CREATED):
            raise HTTPException(409, "请选择空项目槽位来接入本机目录")

        # Publish the prepared repository atomically before changing the platform checkout.
        # A retry can finish the database transaction even after the checkout advanced.
        canonical = ws.state_dir / "link-source"
        if not canonical.exists():
            prepare_baseline(engine, link, file, fingerprint, canonical)
        owner = gitops.git(["config", "--get", "vernal.importOwner"], canonical, check=False).stdout.strip()
        stored_hash = gitops.git(["config", "--get", "vernal.importHash"], canonical, check=False).stdout.strip()
        if (owner, stored_hash) != (link["id"], fingerprint):
            raise HTTPException(409, "该项目已有其他基线")
        start = gitops.git(["config", "--get", "vernal.importStart"], canonical).stdout.strip()
        imported = gitops.head_sha(canonical, "platform-import")
        head = gitops.head_sha(canonical, "link-base")
        if (gitops.current_branch(ws.root) != engine.settings.base_branch
                or gitops.head_sha(ws.root) not in (start, imported) or not gitops.is_clean(ws.root)):
            raise HTTPException(409, "工作区在接入期间有人工改动")
        exclude = ws.root / ".git" / "info" / "exclude"
        exclusions = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
        with exclude.open("a", encoding="utf-8") as fp:
            for name in ("/.autodev/", "/.vernal/"):
                if name not in exclusions:
                    fp.write(f"\n{name}\n")
        if gitops.head_sha(ws.root) != imported:
            gitops.git(["fetch", "--no-tags", "--", str(canonical), "refs/heads/platform-import"], ws.root)
            gitops.git(["merge", "--ff-only", imported], ws.root)
        db.meta_set("source_mode", "host_link")
        db.meta_set("git_delivery_source", str(canonical))
        db.meta_set("git_source_commit", head)
        db.meta_set("link_platform_base", gitops.head_sha(ws.root))
        db.meta_set("delivery_branch_prefix", f"vernal/{engine.settings.loopforge_project_id or ws.root.name}/")
        context = engine.link_context_slices()
        for item in context:
            db.upsert_slice(item)
        write_catalog(context, ws.catalog_path)
        engine.set_status(ProjectStatus.INGESTED)
        engine.settings.collaboration_mode = "git"
        db._exec("UPDATE host_links SET baseline_hash=? WHERE id=?", (fingerprint, link["id"]))
        db.add_event("link.connected", f"{link['owner']} 已连接本机项目目录")


def release_file(engine, link, delivery, output):
    repo = Path(engine.db.meta_get("git_delivery_source"))
    head = delivery["commit_sha"]
    if link["kind"] == "git":
        args = ["bundle", "create", str(output), "refs/heads/" + delivery["archive_branch"]]
        prev = delivery["prev_commit_sha"] or engine.db.meta_get("git_source_commit")
        if prev != head and gitops.is_ancestor(repo, prev, head):
            args.append("^" + prev)
        gitops.git(args, repo)
        return
    files = tree_files(repo, head)
    origin = tree_files(repo, engine.db.meta_get("git_source_commit"))
    scaffold = tree_files(repo, engine.db.meta_get("link_platform_base"))
    total = 0
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, (sha, mode) in files.items():
            if name not in origin and scaffold.get(name) == (sha, mode):
                continue
            total += int(gitops.git(["cat-file", "-s", sha], repo).stdout)
            if total > LIMIT:
                raise ValueError("交付超过 200 MiB")
            archive.writestr(name, subprocess.check_output(["git", "cat-file", "blob", sha], cwd=repo))


def register_link(app, slot, find_slot, verify_user, verify_project, user_token, persist):
    import asyncio
    from autodev.harnesses.link_worker import register_jobs

    def authorize(request, link_id):
        if not re.fullmatch("[a-f0-9]{32}", link_id):
            raise HTTPException(403, "连接凭据无效")
        db = slot().engine.db
        rows = db._query("SELECT * FROM host_links WHERE id=?", (link_id,))
        header = request.headers.get("authorization", "")
        if not rows or not header.startswith("Bearer ") or not hmac.compare_digest(rows[0]["token_hash"], digest(header[7:].encode())):
            raise HTTPException(403, "连接凭据无效")
        db._exec("UPDATE host_links SET seen=? WHERE id=?", (time.time(), link_id))
        return dict(rows[0])

    @app.post("/vernal-api/link/create-code")
    async def create_code(request: Request, body: dict):
        engine = slot().engine
        pid = request.state.requested_project_id or engine.settings.loopforge_project_id or slot().project_key
        lf = engine.settings.loopforge_url
        cookie = request.headers.get("cookie", "")
        owner = await verify_user(cookie, lf)
        await verify_project(cookie, lf, pid, strict=True)
        target = find_slot(pid, create=True).engine
        if target.db.meta_get("source_mode") or target.db.list_milestones():
            raise HTTPException(409, "请在尚未导入项目的槽位生成配对码")
        runner_code = None
        if body.get("native_runner"):
            token = await user_token(cookie, target.settings)
            runner_code = (await LoopForgeClient(lf, token).create_runner_bind_code()).get("code")
        code = pid + "." + secrets.token_urlsafe(18)
        expiry = time.time() + 600
        target.db._exec("DELETE FROM link_codes WHERE expires<=?", (time.time(),))
        target.db._exec("INSERT INTO link_codes VALUES(?,?,?,?)", (digest(code.encode()), owner, expiry, runner_code))
        return {"code": code, "expires_at": expiry, "project": pid}

    @app.post("/vernal-api/link/exchange")
    def exchange(body: dict):
        engine = slot().engine
        db = engine.db
        code = str(body.get("code", ""))
        kind = body.get("kind")
        if kind not in ("folder", "git"):
            raise HTTPException(400, "未知目录类型")
        with db.transaction():
            key = digest(code.encode())
            rows = db._query("SELECT * FROM link_codes WHERE code_hash=? AND expires>?", (key, time.time()))
            if not rows:
                raise HTTPException(403, "配对码无效或已过期")
            owner = rows[0]["owner"]
            db._exec("DELETE FROM link_codes WHERE code_hash=?", (key,))
            link_id = secrets.token_hex(16)
            token = hmac.digest(secrets.token_bytes(32), f"{slot().project_key}:{owner}:{link_id}".encode(), "sha256").hex()
            clis = [k for k in ("codex", "claude-code", "opencode", "dsh") if k in body.get("clis", [])]
            db._exec("INSERT INTO host_links(id,owner,token_hash,kind,platform,clis,seen,runner_code) VALUES(?,?,?,?,?,?,?,?)",
                     (link_id, owner, digest(token.encode()), kind, str(body.get("platform", ""))[:20], json.dumps(clis), time.time(), rows[0]["runner_code"]))
            db._exec("UPDATE host_links SET codex_login=? WHERE id=?", (int(body.get("codex_login") is True), link_id))
        return {"project": slot().project_key, "link_id": link_id, "link_token": token,
                "native_runner": bool(rows[0]["runner_code"])}

    @app.post("/vernal-api/link/{link_id}/capabilities")
    def capabilities(link_id: str, request: Request, body: dict):
        link = authorize(request, link_id)
        if link["platform"] != "win32":
            raise HTTPException(409, "此连接使用官方 Runner")
        enabled = [k for k in ("codex", "claude-code", "opencode", "dsh") if k in body.get("clis", [])]
        slot().engine.db._exec("UPDATE host_links SET clis=?,codex_login=? WHERE id=?",
                               (json.dumps(enabled), int(body.get("codex_login") is True), link_id))
        return {"ok": True}

    @app.post("/vernal-api/link/{link_id}/baseline")
    async def baseline(link_id: str, request: Request):
        link = authorize(request, link_id)
        engine = slot().engine
        hasher = hashlib.sha256()
        try:
            with tempfile.TemporaryDirectory(dir=engine.ws.state_dir) as tmp:
                path = Path(tmp) / "baseline"
                with path.open("wb") as fp:
                    async for block in request.stream():
                        if fp.tell() + len(block) > LIMIT + 8*1024*1024:
                            raise HTTPException(413, "基线超过上传限制")
                        hasher.update(block)
                        fp.write(block)
                await asyncio.to_thread(install_baseline, engine, link, path, hasher.hexdigest())
                persist()
        except (ValueError, gitops.GitError, zipfile.BadZipFile) as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"ready": True}

    @app.get("/vernal-api/link/{link_id}/events")
    def events(link_id: str, request: Request, after: int = 0):
        link = authorize(request, link_id)
        db = slot().engine.db
        if not link["baseline_hash"]:
            raise HTTPException(409, "请先上传基线")
        return {"deliveries": [dict(r) for r in db._query("SELECT * FROM milestone_deliveries WHERE delivery_id>? "
                                                         "AND collab_mode='git' ORDER BY delivery_id LIMIT 32", (after,))]}

    @app.post("/vernal-api/link/{link_id}/conflicts")
    def conflicts(link_id: str, request: Request, body: dict):
        link = authorize(request, link_id)
        paths = [str(p)[:400] for p in body.get("paths", [])[:100]]
        db = slot().engine.db
        rows = db._query("SELECT milestone_id,version_no FROM milestone_deliveries WHERE delivery_id=?",
                         (body.get("delivery"),))
        if not rows or not link["baseline_hash"]:
            raise HTTPException(404, "未找到该版本")
        name = f"{rows[0]['milestone_id']} v{rows[0]['version_no']}"
        db.add_event("link.conflict", f"{name} 本机交付冲突，已保留人工修改：{'、'.join(paths)}",
                     actor=link["owner"], payload={"delivery_id": body["delivery"], "paths": paths})
        return {"ok": True}

    @app.get("/vernal-api/link/{link_id}/deliveries/{delivery_id}")
    def release(link_id: str, delivery_id: int, request: Request):
        link = authorize(request, link_id)
        engine = slot().engine
        rows = engine.db._query("SELECT * FROM milestone_deliveries WHERE delivery_id=? AND collab_mode='git'", (delivery_id,))
        if not rows or not link["baseline_hash"]:
            raise HTTPException(404, "未找到该版本")
        folder = Path(tempfile.mkdtemp(dir=engine.ws.state_dir))
        path = folder / "release"
        try:
            release_file(engine, link, dict(rows[0]), path)
            return FileResponse(path, background=BackgroundTask(shutil.rmtree, folder))
        except BaseException:
            shutil.rmtree(folder)
            raise

    @app.post("/vernal-api/link/{link_id}/runner-code")
    def runner_code(link_id: str, request: Request):
        db = slot().engine.db
        with db.transaction():
            link = authorize(request, link_id)
            if not link["runner_code"] or not link["baseline_hash"]:
                raise HTTPException(409, "请在启动台为本机 Runner 签发绑定码")
            db._exec("UPDATE host_links SET runner_code=NULL WHERE id=?", (link_id,))
            return {"code": link["runner_code"]}

    register_jobs(app, slot, authorize)
