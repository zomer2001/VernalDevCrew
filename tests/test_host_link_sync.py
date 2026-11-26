"""Remote pairing, real Git transfers, binary folders and the Windows npm CLI contract."""
import asyncio
import io
import json
import os
import zipfile

import httpx
import pytest

from autodev import gitops, host_link
from autodev.engine import Engine, init_workspace
from autodev.harnesses.link_worker import execute_job
from autodev.loopforge.client import LoopForgeClient
from autodev.models import MilestoneStatus, ProjectStatus, TaskStatus
from autodev.server.app import create_app


def gateway(tmp_path, monkeypatch):
    engine = Engine(init_workspace(tmp_path / "workspaces" / "demo", collaboration_mode="files"))
    engine.settings.loopforge_project_id = "demo"

    async def who(self, cookie):
        return {"user": {"id": 1, "loginName": "alice"}}

    async def projects(self, cookie):
        return [{"id": "demo", "ownerId": 1}]

    monkeypatch.setattr(LoopForgeClient, "auth_current_with_cookie", who)
    monkeypatch.setattr(LoopForgeClient, "list_projects_with_cookie", projects)
    transport = httpx.ASGITransport(app=create_app(engine), client=("192.0.2.15", 43000))
    return engine, httpx.AsyncClient(transport=transport, base_url="https://gateway.example")


async def connect(client, kind, worker=False):
    path = "/vernal-api/link/create-code"
    assert (await client.post(path, json={})).status_code == 401
    res = await client.post(path, headers={"Cookie": "session=example"}, json={})
    assert res.status_code == 200, res.text
    hello = dict(code=res.json()["code"], kind=kind, platform="win32" if worker else "darwin",
                 clis=["codex"] if worker else [])
    auth = await client.post("/vernal-api/link/exchange", json=hello)
    assert auth.status_code == 200, auth.text
    assert (await client.post("/vernal-api/link/exchange", json=hello)).status_code == 403
    route = "/vernal-api/link/" + auth.json()["link_id"]
    assert (await client.get(route + "/events")).status_code == 403
    client.headers.update({"X-Vernal-Project": "demo", "Authorization": "Bearer " + auth.json()["link_token"]})
    return route


def deliver(engine, edits):
    root = engine.ws.root
    history = engine.db.list_milestone_deliveries("M1")
    branch = "cycle" + str(len(history) + 1)
    gitops.git(["checkout", "-b", branch], root)
    for name, content in edits.items():
        target = root / name
        if content is None:
            target.unlink()
        else:
            target.write_bytes(content)
    gitops.commit_all(root, "delivery " + branch)
    gitops.git(["checkout", engine.settings.base_branch], root)
    if not history:
        engine.db._exec("INSERT INTO milestones(milestone_id,seq_order,title,goal,status) VALUES('M1',1,'test','','PENDING')")
    engine.db.update_milestone("M1", status=MilestoneStatus.AWAITING_MERGE, branch_name=branch)
    engine.merge_milestone("M1", "alice")


@pytest.mark.parametrize("kind", ["folder", "git"])
def test_pair_and_receive_two_versions(tmp_path, monkeypatch, kind):
    engine, client = gateway(tmp_path, monkeypatch)
    root = tmp_path / "personal"
    root.mkdir()
    (root / "auth.txt").write_bytes(b"base")
    (root / "other.txt").write_bytes(b"unchanged")
    large = os.urandom(5 * 1024 * 1024)
    (root / "binary.dat").write_bytes(large)
    bundle = tmp_path / "source"
    manifest = {}
    if kind == "git":
        gitops.ensure_repo(root, "main")
        original = gitops.head_sha(root)
        (root / "other.txt").write_bytes(b"dirty")
        host_link.git_baseline(root, bundle)
    else:
        (root / ".gitignore").write_text("ignored.bin\n")
        (root / "ignored.bin").write_bytes(b"omit")
        manifest = host_link.plain_baseline(root, bundle)
        assert "ignored.bin" not in manifest

    async def scenario():
        nonlocal manifest
        async with client:
            route = await connect(client, kind)
            for _ in range(2):
                res = await client.post(route + "/baseline", content=host_link.stream_file(bundle))
                assert res.status_code == 200, res.text
            assert (engine.ws.root / "binary.dat").read_bytes() == large
            assert engine.status == ProjectStatus.INGESTED and engine.db.list_slices()
            assert engine.ingest() and not (root / "inputs/00-existing-codebase.md").exists()
            locked_mode = await client.post("/vernal-api/runtime/config", headers={"Cookie": "session=example"},
                                            json={"collaboration_mode": "files"})
            assert locked_mode.status_code == 409
            cursor = 0
            for index in (1, 2):
                if index == 2:
                    (root / "other.txt").write_bytes(b"manual")
                    (root / "auth.txt").write_bytes(b"human change")
                edits = {"auth.txt": str(index).encode(), "new.bin": bytes([0, index])}
                deliver(engine, edits)
                res = await client.get(route + "/events", params={"after": cursor})
                release, = res.json()["deliveries"]
                cursor = release["delivery_id"]
                assert release["version_no"] == index
                package = tmp_path / f"v{index}"
                await host_link.download(client, route + f"/deliveries/{cursor}", package)
                if kind == "git":
                    fetch_head = root / ".git/FETCH_HEAD"
                    fetch_head.write_text("owner fetch state")
                    assert not host_link.sync_git(root, package, release)
                    assert fetch_head.read_text() == "owner fetch state"
                    assert gitops.head_sha(root, release["archive_branch"]) == release["commit_sha"]
                    assert gitops.head_sha(root, release["rolling_branch"]) == release["commit_sha"]
                    assert gitops.head_sha(root) == original and gitops.current_branch(root) == "main"
                else:
                    manifest, conflicts = host_link.sync_folder(root, package, manifest, "M1", index)
                    assert (root / "new.bin").read_bytes() == edits["new.bin"]
                    assert conflicts == ([] if index == 1 else ["auth.txt"])
                    if conflicts:
                        assert (root / ".vernal/conflicts/M1-v2/incoming/auth.txt").read_bytes() == b"2"
                        response = await client.post(route + "/conflicts", json={"delivery": cursor, "paths": conflicts})
                        assert response.status_code == 200
                        assert engine.db.events_after(0)[-1]["event_type"] == "link.conflict"
            assert (root / "other.txt").read_bytes() == b"manual"
            assert (root / "auth.txt").read_bytes() == b"human change"
    try:
        asyncio.run(scenario())
    finally:
        engine.db.close()


def test_windows_worker_real_cli_patch_and_gate(tmp_path, monkeypatch):
    engine, client = gateway(tmp_path, monkeypatch)
    fixture = tmp_path / "npm"
    package = fixture / "node_modules/@openai/codex"
    package.mkdir(parents=True)
    shim = fixture / "codex.cmd"
    shim.write_text("@echo off")
    (package / "package.json").write_text(json.dumps({"bin": {"codex": "cli.js"}}))
    (package / "cli.js").write_text("""
const fs = require('fs');
const args = process.argv;
if (args.includes('workspace-write')) {
  fs.writeFileSync('feature.py', 'VALUE = 2\\n');
  const input = fs.readFileSync(0, 'utf8');
  if (!input.includes('python -c')) process.exit(2);
  require('child_process').execFileSync(process.env.VERNAL_TEST_PYTHON, ['-B','-c','from feature import VALUE; assert VALUE == 2']);
  fs.writeFileSync(args[args.indexOf('-o')+1], JSON.stringify({status:'DONE',summary:'coded',files_changed:['feature.py'],tests_added:[],notes:''}));
} else {
  fs.writeFileSync(args[args.indexOf('-o')+1], JSON.stringify({verdict:'PASS',summary:'reviewed',criteria_check:[],blocking_issues:[]}));
}
""")
    import sys
    monkeypatch.setenv("VERNAL_TEST_PYTHON", sys.executable)
    states = []
    async def create(self, **fields):
        return {"id": "windows-issue"}
    async def update(self, **fields):
        states.append(fields["status"])
        return fields
    monkeypatch.setattr(LoopForgeClient, "create_issue", create)
    monkeypatch.setattr(LoopForgeClient, "update_issue", update)
    workflows, receipts = {}, {}
    async def put(_self, workflow):
        workflows[workflow["id"]] = workflow
        return workflow
    async def publish(_self, wid):
        return workflows[wid]
    async def dispatch(_self, issue, *, workflow_id):
        return {"id":"task-"+workflow_id, "issueId":issue, "runId":workflow_id}
    async def get_run(_self, rid):
        import re
        url = re.search(r"urlopen\('([^']+)'", workflows[rid]["nodes"][0]["script"]).group(1)
        read = await client.get(url)
        assert read.is_success, read.text
        if not read.json().get("waiting"):
            receipts[rid] = read.json()
        return {"id":rid,"status":"succeeded" if rid in receipts else "running",
                "executionContext":{"projectId":engine.settings.loopforge_project_id}}
    async def artifact(_self, run, filename, *, node_id):
        assert filename == "link-receipt.json" and node_id == "delegate"
        return json.dumps(receipts[run["id"]])
    for method, impl in (("put_workflow",put),("publish_workflow",publish),("dispatch_issue",dispatch),
                         ("get_run",get_run),("named_artifact",artifact)):
        monkeypatch.setattr(LoopForgeClient,method,impl)

    async def scenario():
        async with client:
            route = await connect(client, "folder", worker=True)
            body = io.BytesIO()
            with zipfile.ZipFile(body, "w") as archive:
                archive.writestr("feature.py", "VALUE = 1\n")
            res = await client.post(route + "/baseline", content=body.getvalue())
            assert res.status_code == 200, res.text
            db = engine.db
            db._exec("INSERT INTO milestones(milestone_id,seq_order,title,goal,status) VALUES('M1',1,'feature','','PENDING')")
            db._exec("INSERT INTO tasks(task_id,milestone_id,module,role,title,description,acceptance_criteria_json,"
                     "depends_on_json,doc_slice_ids_json,allowed_paths_json,verify_cmds_json,status) "
                     "VALUES('T-001','M1','core','coder','feature','change', '[]','[]','[]','[\"feature.py\"]',?,'READY')",
                     (json.dumps(['python3 -c "from feature import VALUE; assert VALUE == 2"']),))
            engine.settings.runtime_backend = "loopforge"
            engine.settings.execution_pool = "local_runner"
            engine.settings.loopforge_token = "test"
            engine.settings.harness = "codex"
            engine.settings.require_merge_approval = True
            engine.set_status(ProjectStatus.APPROVED)
            async def current_identity(self):
                return {"user": {"id": 1, "loginName": "alice"}}
            monkeypatch.setattr(LoopForgeClient, "auth_current", current_identity)
            owner = {"Cookie": "session=example"}
            started = await client.post("/vernal-api/scheduler/start?watch=false", headers=owner)
            assert started.status_code == 200, started.text
            try:
                for _ in range(2):
                    async with asyncio.timeout(15):
                        claim = {"X-Vernal-Claim": os.urandom(16).hex()}
                        while not (job := (await client.post(route + "/tasks/claim", headers=claim)).json()["job"]):
                            if engine.status == ProjectStatus.STALLED:
                                raise AssertionError(db.events_after(0))
                            await asyncio.sleep(0.05)
                    assert (await client.post(route + "/tasks/claim", headers=claim)).json()["job"] == job
                    assert (await client.post(route + "/tasks/claim")).json()["job"] is None
                    archive = tmp_path / (job["id"] + ".zip")
                    await host_link.download(client, route + "/tasks/" + job["id"] + "/input", archive)
                    result = await execute_job(job, archive, {"codex": str(shim)})
                    payload = dict(id=job["id"], claim=job["claim"], result=result)
                    assert (await client.post(route + "/tasks/complete", json=dict(payload, claim="wrong"))).status_code == 409
                    assert (await client.post(route + "/tasks/complete", json=payload)).status_code == 200
                async with asyncio.timeout(10):
                    while db.get_milestone("M1").status != MilestoneStatus.AWAITING_MERGE:
                        if engine.status == ProjectStatus.STALLED:
                            raise AssertionError(db.events_after(0))
                        await asyncio.sleep(0.05)
                assert db.get_task("T-001").status == TaskStatus.APPROVED
                assert db.get_milestone("M1").status == MilestoneStatus.AWAITING_MERGE
                assert states == ["in_progress", "done"]
                engine.merge_milestone("M1", "alice")
                assert (engine.ws.root / "feature.py").read_text() == "VALUE = 2\n"
            finally:
                await client.post("/vernal-api/scheduler/stop", headers=owner)
    try:
        asyncio.run(scenario())
    finally:
        engine.db.close()


def test_link_codes_require_membership_and_expire(tmp_path, monkeypatch):
    async def scenario():
        engine, client = gateway(tmp_path, monkeypatch)
        try:
            async with client:
                route = "/vernal-api/link/create-code"
                cookie = {"Cookie": "session=owner"}
                res = await client.post(route, headers=cookie, json={})
                code = res.json()["code"]
                engine.db._exec("UPDATE link_codes SET expires=0")
                expired = await client.post("/vernal-api/link/exchange", json={"code": code, "kind": "git"})
                assert expired.status_code == 403

                async def no_projects(self, cookie):
                    return []
                monkeypatch.setattr(LoopForgeClient, "list_projects_with_cookie", no_projects)
                assert (await client.post(route, headers={"Cookie": "session=nonmember"}, json={})).status_code == 403
        finally:
            engine.db.close()
    asyncio.run(scenario())


def test_daemon_resumes_after_offline_delivery(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    root = tmp_path / "personal"
    root.mkdir()
    (root / "auth.txt").write_text("original")

    async def scenario():
        engine, client = gateway(tmp_path, monkeypatch)
        asgi = client._transport
        failures = {"baseline": 1, "deliveries": 1}

        async def intermittent(request):
            path = request.url.path
            phase = "baseline" if path.endswith("/baseline") else "deliveries" if "/deliveries/" in path else ""
            if failures.get(phase, 0):
                failures[phase] -= 1
                return httpx.Response(503)
            return await asgi.handle_async_request(request)

        transport = httpx.MockTransport(intermittent)
        client_type = httpx.AsyncClient
        monkeypatch.setattr(host_link.httpx, "AsyncClient", lambda **kw: client_type(transport=transport, **kw))

        async def wait_for(predicate, process):
            async with asyncio.timeout(12):
                while not predicate():
                    if process.done():
                        process.result()
                    await asyncio.sleep(0.05)

        async def stop(process):
            process.cancel()
            await asyncio.gather(process, return_exceptions=True)

        try:
            async with client:
                res = await client.post("/vernal-api/link/create-code", headers={"Cookie": "session=owner"}, json={})
                daemon = asyncio.create_task(host_link.run_link("https://gateway.example", res.json()["code"], root, False))
                try:
                    await wait_for(lambda: engine.db.meta_get("source_mode") == "host_link", daemon)
                    deliver(engine, {"auth.txt": b"first"})
                    await wait_for(lambda: (root / "auth.txt").read_bytes() == b"first", daemon)
                    assert failures == {"baseline": 0, "deliveries": 0}
                finally:
                    await stop(daemon)
                (root / "auth.txt").write_text("user")
                deliver(engine, {"auth.txt": b"second", "later.txt": b"delivered"})
                resumed = asyncio.create_task(host_link.run_link("https://gateway.example", "", root, False))
                try:
                    await wait_for(lambda: any(e["event_type"] == "link.conflict" for e in engine.db.events_after(0)), resumed)
                    assert (root / "later.txt").read_bytes() == b"delivered"
                    assert (root / "auth.txt").read_text() == "user"
                    assert (root / ".vernal/conflicts/M1-v2/incoming/auth.txt").read_bytes() == b"second"
                finally:
                    await stop(resumed)
        finally:
            engine.db.close()
    asyncio.run(scenario())


def test_folder_file_directory_replacements(tmp_path):
    root = tmp_path / "local"
    root.mkdir()
    (root / "entry").write_bytes(b"one")
    (root / "untouched").write_bytes(b"user")
    manifest = {"entry": host_link.digest(b"one")}
    for version, name in enumerate(("entry/nested", "entry"), start=1):
        archive = tmp_path / f"release-{version}.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr(name, str(version))
        manifest, conflicts = host_link.sync_folder(root, archive, manifest, "M1", version)
        assert not conflicts and (root / name).read_text() == str(version)
        assert (root / "untouched").read_bytes() == b"user"


def test_link_retries_interrupted_import(tmp_path, monkeypatch):
    from fastapi import HTTPException

    async def scenario():
        engine, client = gateway(tmp_path, monkeypatch)
        try:
            async with client:
                route = await connect(client, "folder")
                source = io.BytesIO()
                with zipfile.ZipFile(source, "w") as z:
                    z.writestr("resume.txt", "initial")
                add_event = engine.db.add_event
                with monkeypatch.context() as scoped:
                    def interrupted(event, *args, **kwargs):
                        if event == "link.connected":
                            raise HTTPException(503, "interrupted before receipt")
                        return add_event(event, *args, **kwargs)
                    scoped.setattr(engine.db, "add_event", interrupted)
                    response = await client.post(route + "/baseline", content=source.getvalue())
                    assert response.status_code == 503
                    assert engine.db.meta_get("source_mode") is None
                head = gitops.head_sha(engine.ws.root)
                response = await client.post(route + "/baseline", content=source.getvalue())
                assert response.status_code == 200, response.text
                assert head == gitops.head_sha(engine.ws.root)
                assert (engine.ws.root / "resume.txt").read_text() == "initial"
                assert len([e for e in engine.db.events_after(0) if e["event_type"] == "link.connected"]) == 1
        finally:
            engine.db.close()
    asyncio.run(scenario())
