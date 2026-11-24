"""Personal Windows CLI jobs, leased to one paired device and verified by the scheduler."""
from __future__ import annotations

import asyncio
import base64
import contextvars
import json
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse

from autodev import gitops
from autodev.harnesses.base import AgentResult, Harness, extract_json
from autodev.host_link import LIMIT, digest, download, require, transient_error, unpack_zip
from autodev.loopforge.client import LoopForgeClient

LINK_USER = contextvars.ContextVar("link_user", default="")


def personal_worker(engine):
    pool = engine.settings.execution_pool
    if pool not in ("local_runner", "auto") or not LINK_USER.get() or engine.db.meta_get("source_mode") != "host_link":
        return None
    rows = engine.db._query("SELECT * FROM host_links WHERE platform='win32' AND baseline_hash IS NOT NULL "
                            "AND owner=? AND clis!='[]' ORDER BY seen DESC LIMIT 1", (LINK_USER.get(),))
    if not rows:
        return None
    link = dict(rows[0])
    if pool == "auto":
        required = {engine.settings.harness, engine.settings.pm_harness, engine.settings.director_harness,
                    engine.settings.coder_harness, engine.settings.qa_harness} - {"", "auto"}
        if link["seen"] < time.time() - 30 or not required.issubset(json.loads(link["clis"])):
            return None
    return link


async def track_issue(engine, task_id, status):
    task = engine.db.get_task(task_id)
    s = engine.settings
    if s.runtime_backend != "loopforge" or not task:
        return
    client = LoopForgeClient(s.loopforge_url, s.effective_loopforge_token())
    fields = dict(project_id=s.loopforge_project_id, title=f"[{task_id}] {task.title}", description=task.description)
    if not task.loopforge_issue_id:
        issue = await client.create_issue(**fields)
        engine.db.update_task(task_id, loopforge_issue_id=issue["id"])
    await client.update_issue(issue_id=engine.db.get_task(task_id).loopforge_issue_id, status=status, **fields)


class LinkWorkerHarness(Harness):
    name = "link-worker"

    def __init__(self, engine, link):
        super().__init__()
        self.engine = engine
        self.link = link

    async def health_check(self):
        return self.link["seen"] > time.time() - 30, "Windows 本机 CLI"

    async def run_structured_readonly(self, prompt, *, schema_path, cwd, log_dir, role):
        return await self._submit(prompt, cwd, schema_path, role, True)

    async def run_workspace_task(self, prompt, *, cwd, log_dir, schema_path, role, **options):
        return await self._submit(prompt, cwd, schema_path, role, False)

    async def resume_workspace_task(self, session_id, prompt, **options):
        return await self.run_workspace_task(prompt, **options)

    async def _submit(self, prompt, cwd, schema_path, role, readonly):
        db = self.engine.db
        if not (await self.health_check())[0]:
            return AgentResult(False, 1, None, "", error="个人 Windows Worker 离线")
        clis = json.loads(self.link["clis"])
        name = self.engine.settings.harness_for(role)
        if name not in clis:
            if self.engine.settings.harness in ("", "auto"):
                name = clis[0]
            else:
                return AgentResult(False, 1, None, "", error=f"本机没有 {name}")
        job_id = secrets.token_hex(16)
        jobs = self.engine.ws.state_dir / "link-jobs"
        jobs.mkdir(exist_ok=True)
        source = jobs / job_id
        try:
            gitops.git(["archive", "--format=zip", "-o", str(source), "HEAD"], cwd)
            payload = {"role": role, "cli": name, "readonly": readonly, "prompt": prompt,
                       "schema": json.loads(Path(schema_path).read_text()) if schema_path else {}}
            if not readonly:
                from autodev.engine import current_task
                task = db.get_task(current_task.get() or "")
                payload["verify_cmds"] = [re.sub(r"^python3(?=\s|$)", "python", cmd)
                                          for cmd in task.verify_cmds] if task else []
            expires = time.time() + self.engine.settings.agent_timeout_sec
            db._exec("INSERT INTO link_jobs(id,link_id,state,payload,expires) VALUES(?,?,'pending',?,?)",
                     (job_id, self.link["id"], json.dumps(payload), expires))
            while time.time() < expires:
                rows = db._query("SELECT result FROM link_jobs WHERE id=?", (job_id,))
                if rows and rows[0]["result"]:
                    output = json.loads(rows[0]["result"])
                    if not output.get("ok"):
                        return AgentResult(False, 1, None, "", error=str(output.get("error", "Windows CLI 执行失败")))
                    if not readonly and output.get("patch"):
                        patch = base64.b64decode(output["patch"], validate=True)
                        if digest(patch) != output.get("digest"):
                            raise ValueError("Windows Worker 返回的补丁校验失败")
                        proc = subprocess.run(["git", "apply", "--binary", "--whitespace=nowarn", "-"],
                                              cwd=cwd, input=patch, capture_output=True)
                        if proc.returncode:
                            raise ValueError("Windows Worker 补丁与任务基线不匹配")
                    return AgentResult(True, 0, None, "", structured=output["structured"])
                await asyncio.sleep(0.2)
            return AgentResult(False, 1, None, "", error="Windows Worker 等待超时")
        finally:
            db._exec("DELETE FROM link_jobs WHERE id=?", (job_id,))
            source.unlink(missing_ok=True)


def register_jobs(app, slot, authorize):
    @app.post("/vernal-api/link/{link_id}/tasks/claim")
    def claim(link_id: str, request: Request):
        link = authorize(request, link_id)
        db = slot().engine.db
        if link["platform"] != "win32" or not link["baseline_hash"]:
            raise HTTPException(403, "此设备未启用 Windows Worker")
        lease = request.headers.get("X-Vernal-Claim") or secrets.token_hex(16)
        if not re.fullmatch(r"[a-f0-9]{32}", lease):
            raise HTTPException(400, "领单请求标识无效")
        with db.transaction():
            # A retry with the same key receives the job whose HTTP reply was lost.
            rows = db._query("SELECT * FROM link_jobs WHERE link_id=? AND expires>? AND "
                             "(state='pending' OR (state='claimed' AND claim=?)) "
                             "ORDER BY (state='claimed') DESC, rowid LIMIT 1", (link_id, time.time(), lease))
            if not rows:
                return {"job": None}
            job = rows[0]
            db._exec("UPDATE link_jobs SET state='claimed',claim=? WHERE id=?", (lease, job["id"]))
            return {"job": {**json.loads(job["payload"]), "id": job["id"], "claim": lease}}

    @app.get("/vernal-api/link/{link_id}/tasks/{job_id}/input")
    def job_input(link_id: str, job_id: str, request: Request):
        authorize(request, link_id)
        engine = slot().engine
        if not engine.db._query("SELECT 1 FROM link_jobs WHERE id=? AND link_id=? AND state='claimed' AND expires>?",
                                (job_id, link_id, time.time())):
            raise HTTPException(404, "任务不存在")
        return FileResponse(engine.ws.state_dir / "link-jobs" / job_id)

    @app.post("/vernal-api/link/{link_id}/tasks/complete")
    async def complete(link_id: str, request: Request):
        authorize(request, link_id)
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > LIMIT:
                raise HTTPException(413, "补丁超出限制")
            raw.extend(chunk)
        try:
            body = json.loads(raw)
            result = json.dumps(body["result"])
        except (TypeError, KeyError, ValueError) as exc:
            raise HTTPException(400, "任务结果格式错误") from exc
        db = slot().engine.db
        with db.transaction():
            rows = db._query("SELECT state,result FROM link_jobs WHERE id=? AND link_id=? AND claim=? AND expires>?",
                             (body.get("id"), link_id, body.get("claim"), time.time()))
            if not rows or rows[0]["state"] == "done" and rows[0]["result"] != result:
                raise HTTPException(409, "任务租约已失效")
            db._exec("UPDATE link_jobs SET state='done',result=? WHERE id=?", (result, body["id"]))
        return {"ok": True}


def command_prefix(binary, cli):
    # npm .cmd shims invoke cmd.exe. Launch the installed Node entry point directly so project text
    # and paths are never interpreted by the Windows command shell.
    if Path(binary).suffix.lower() not in (".cmd", ".bat"):
        return [binary]
    packages = {"codex": "@openai/codex", "claude-code": "@anthropic-ai/claude-code",
                "opencode": "opencode-ai", "dsh": "@deepseek-ai/dsh"}
    package = Path(binary).parent / "node_modules" / packages[cli]
    entry = json.loads((package / "package.json").read_text(encoding="utf-8"))["bin"]
    entry = entry if isinstance(entry, str) else entry[Path(binary).stem]
    return [shutil.which("node") or "node", str(package / entry)]


async def execute_job(job, archive, binaries):
    with tempfile.TemporaryDirectory() as folder:
        tmp = Path(folder)
        repo = tmp / "project"
        repo.mkdir()
        unpack_zip(archive, repo)
        gitops.git(["init", "-q"], repo)
        # The gateway patch must use the exact blob bytes even with Git for Windows defaults.
        gitops.git(["config", "core.autocrlf", "false"], repo)
        gitops.git(["add", "-f", "--all"], repo)
        gitops.git(["-c", "user.name=Vernal", "-c", "user.email=vernal@localhost", "commit", "--allow-empty",
                    "-qm", "Task baseline"], repo)
        schema = tmp / "schema.json"
        schema.write_text(json.dumps(job["schema"]), encoding="utf-8")
        output = tmp / "answer.json"
        cli = job["cli"]
        args = command_prefix(binaries[cli], cli)
        prompt = job["prompt"] + "\nReturn the final report as JSON matching this schema:\n" + schema.read_text()
        if not job["readonly"] and job.get("verify_cmds"):
            checks = "\n".join(job["verify_cmds"])
            prompt += f"\nRun the task checks in this Windows workspace before returning the report:\n{checks}"
        if cli == "codex":
            args += ["exec", "--sandbox", "read-only" if job["readonly"] else "workspace-write",
                     "--output-schema", str(schema), "-o", str(output), "-"]
        elif cli == "claude-code":
            args += ["-p", "--permission-mode", "plan" if job["readonly"] else "acceptEdits", "--output-format", "text"]
        elif cli == "opencode":
            args += ["run", "--agent", "plan" if job["readonly"] else "build"]
        else:
            args += ["--profile", "headless", "--permission-mode", "read-only" if job["readonly"] else "workspace-write", prompt]
        proc = await asyncio.create_subprocess_exec(*args, cwd=repo, stdin=subprocess.PIPE,
                                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(prompt.encode() if cli != "dsh" else b""), 1800)
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        if proc.returncode:
            raise ValueError(stderr.decode(errors="replace")[-1200:])
        structured = extract_json(output.read_text(encoding="utf-8") if output.exists() else stdout.decode(errors="replace"))
        if structured is None:
            raise ValueError("CLI 未返回 JSON 报告")
        gitops.git(["add", "-A"], repo)
        patch = subprocess.check_output(["git", "diff", "--cached", "--binary", "--full-index", "HEAD"], cwd=repo)
        if job["readonly"] and patch:
            raise ValueError("只读评审修改了工作目录")
        return {"ok": True, "structured": structured, "patch": base64.b64encode(patch).decode(), "digest": digest(patch)}


async def serve_jobs(client, route, binaries):
    lease = secrets.token_hex(16)
    while True:
        try:
            job = require(await client.post(route + "/tasks/claim", headers={"X-Vernal-Claim": lease})).json()["job"]
        except httpx.HTTPError as exc:
            if not transient_error(exc):
                raise
            job = None
        if not job:
            await asyncio.sleep(2)
            continue
        lease = secrets.token_hex(16)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                archive = Path(tmp) / "input.zip"
                await download(client, route + "/tasks/" + job["id"] + "/input", archive)
                result = await execute_job(job, archive, binaries)
        except (OSError, KeyError, ValueError, gitops.GitError, httpx.HTTPError, TimeoutError) as exc:
            result = {"ok": False, "error": str(exc)}
        while True:
            try:
                response = await client.post(route + "/tasks/complete", json={"id": job["id"], "claim": job["claim"], "result": result})
                if response.status_code != 409:
                    require(response)
                break
            except httpx.HTTPError as exc:
                if not transient_error(exc):
                    raise
                await asyncio.sleep(2)
