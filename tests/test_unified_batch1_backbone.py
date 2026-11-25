"""Batch 1 contracts with isolated workspaces and a controlled native REST transport."""
import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from autodev.config import EXECUTION_IDENTITY
from autodev.engine import Engine, init_workspace
from autodev.governance.audit import append_audit_log, platform_db_path_for
from autodev.harnesses.link_worker import LINK_USER
from autodev.loopforge import dispatch
from autodev.loopforge.client import LoopForgeClient, LoopForgeError
from autodev.scheduler import Scheduler
from autodev.server.app import create_app
from autodev.server.deps import current_user_var
from test_steering_and_global_rules import _seed_steering_project


_native_request = LoopForgeClient._req


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    monkeypatch.setenv("VERNAL_LOOPFORGE_URL", "http://trusted.invalid")
    ws = init_workspace(tmp_path / "workspaces" / "alpha", collaboration_mode="files")
    cfg = ws.load_settings()
    cfg.loopforge_url = "http://untrusted.invalid"
    cfg.loopforge_project_id = "alpha"
    cfg.loopforge_token = "deployment-fixture"
    cfg.runtime_backend = "loopforge"
    eng = Engine(ws, settings=cfg)
    app = create_app(eng)
    projects = [{"id": "alpha", "ownerId": 1, "members": [{"userId": 2, "role": "member"}]},
                {"id": "secret", "ownerId": 3}]
    calls = []

    async def me(client, cookie):
        assert client.base_url == "http://trusted.invalid"
        uid = {"alice": 1, "bob": 2, "carol": 3}[cookie.split("=")[1]]
        return {"user": {"id": uid, "loginName": cookie.split("=")[1]}}

    async def project_list(client, cookie):
        assert client.base_url == "http://trusted.invalid"
        return projects

    async def req(client, method, path, **kwargs):
        assert client.base_url == "http://trusted.invalid"
        calls.append((method, path, client._headers()))
        if path == "/api/projects":
            return projects
        if path.startswith("/api/runs/") and method == "GET":
            return {"id": path.split("/")[-1], "status": "failed", "updatedAt": "t",
                    "executionContext": {"projectId": "secret" if path.endswith("/foreign") else "alpha",
                                         "issueId": "issue"}, "nodes": {"dev": {"status": "failed"}}}
        if method == "POST" and path.startswith("/api/runs/"):
            return {"ok": True}
        raise AssertionError((method, path))
    monkeypatch.setattr(LoopForgeClient, "auth_current_with_cookie", me)
    monkeypatch.setattr(LoopForgeClient, "list_projects_with_cookie", project_list)
    monkeypatch.setattr(LoopForgeClient, "_req", req)
    with TestClient(app, client=("192.0.2.4", 1010)) as browser:
        yield eng, app.state.ctx, browser, projects, calls
    eng.db.close()


def headers(who="alice", project="alpha"):
    return {"Cookie": "session=" + who, "X-Vernal-Project": project}


@pytest.mark.parametrize("path", ["state", "metrics", "inbox", "history/workspaces", "docs", "runtime/analytics"])
def test_all_project_reads_require_native_membership(deployment, path):
    _, _, client, _, _ = deployment
    assert client.get("/vernal-api/" + path).status_code == 401
    assert client.get("/vernal-api/" + path, headers=headers("carol")).status_code == 403


def test_fixed_auth_source_role_ceiling_and_disk_secrets(deployment):
    eng, _, client, _, _ = deployment
    response = client.post("/vernal-api/runtime/config", headers=headers(),
                           json={"loopforge_url": "http://other.invalid", "loopforge_token": "replacement"})
    assert response.status_code == 403
    response = client.post("/vernal-api/rbac/members", headers=headers("bob"),
                           json={"user_id": "bob", "role": "admin"})
    assert response.status_code == 403 and not eng.db._query("SELECT * FROM members")
    secret = eng.ws.state_dir / "credentials.json"
    assert len(bytes.fromhex(json.loads(secret.read_text())["signing_secret"])) == 32
    assert secret.stat().st_mode & 0o777 == 0o600
    assert eng.settings.project_signing_key() != eng.settings.service_token()


def test_cookie_actor_and_run_ownership(deployment):
    eng, _, client, _, calls = deployment
    for who in ("alice", "bob"):
        res = client.post("/vernal-api/runtime/runs/local/approve", headers=headers(who), json={"comment": who})
        assert res.status_code == 200
        assert calls[-1][2]["Cookie"] == "session=" + who and "Authorization" not in calls[-1][2]
    for action in ("approve", "reject", "retry"):
        before = len(calls)
        res = client.post(f"/vernal-api/runtime/runs/foreign/{action}", headers=headers("bob"),
                          json={"reason": "review", "comment": "review"})
        assert res.status_code == 403
        assert [m for m, _, _ in calls[before:]] == ["GET"]
    assert client.post("/vernal-api/runtime/runs/local/retry", headers=headers("bob")).is_success
    assert client.post("/vernal-api/runtime/runs/local/retry", headers=headers("bob")).is_success
    assert len([c for c in calls if c[1].endswith("/retry")]) == 1
    assert eng.db._query("SELECT actor FROM dispatch_records")[0]["actor"] == "bob"
    assert "session=" not in (eng.ws.state_dir / "credentials.json").read_text()


def test_background_is_bound_without_request_context(deployment, monkeypatch):
    eng, ctx, client, *_ = deployment
    async def scenario():
        EXECUTION_IDENTITY.set((eng.settings.loopforge_url, "alice-personal"))
        LINK_USER.set("alice")
        current_user_var.set("alice")
        async def inspect():
            return EXECUTION_IDENTITY.get(), LINK_USER.get(), current_user_var.get(), eng.settings.effective_loopforge_token()
        result = await ctx.start_background(ctx.initial_slot, "fixture", inspect())
        assert result == (None, "", "", "deployment-fixture")
        assert eng.db.events_after(0)[-1]["actor"] == "alice"
    asyncio.run(scenario())

    monkeypatch.delenv("AGENTFLOW_TOKEN", raising=False)
    monkeypatch.setenv("VERNAL_SERVICE_TOKEN_FILE", str(eng.ws.root / "missing-deployment.token"))
    eng.settings.loopforge_token = ""
    res = client.post("/vernal-api/scheduler/start", headers=headers("alice"))
    assert res.status_code == 409 and "服务身份" in res.json()["detail"]
    assert not ctx.initial_slot.scheduler_task


def test_aggregate_scope_and_unavailable_directory(deployment, monkeypatch):
    eng, ctx, client, projects, _ = deployment
    pdb = platform_db_path_for(ctx.workspaces_parent())
    for pid in ("alpha", "secret"):
        append_audit_log(pdb, project=pid, actor="owner", actor_ip="", action="fixture", target=pid, detail={})
    rows = client.get("/vernal-api/audit", headers=headers("bob")).json()
    assert [r["project"] for r in rows] == ["alpha"]
    sibling = init_workspace(ctx.workspaces_parent() / "secret", collaboration_mode="files")
    settings = sibling.load_settings()
    settings.loopforge_project_id = "secret"
    from autodev.security import persist_workspace_settings
    persist_workspace_settings(sibling, settings)
    portfolio = client.get("/vernal-api/metrics/portfolio", headers=headers("bob"))
    assert portfolio.is_success and "secret" not in portfolio.text
    assert client.get("/vernal-api/audit/verify", headers=headers()).status_code == 403
    projects.clear()
    ctx.proj_perm_cache.clear()
    assert client.get("/vernal-api/inbox", headers=headers()).status_code == 403
    ctx.proj_perm_cache.clear()
    monkeypatch.setattr(LoopForgeClient, "list_projects_with_cookie", AsyncMock(side_effect=LoopForgeError("down")))
    assert client.get("/vernal-api/inbox", headers=headers()).status_code == 503


@pytest.mark.parametrize("explicit", [False, True])
def test_device_loses_access_on_removal_or_revocation(deployment, explicit):
    eng, _, client, projects, _ = deployment
    code = client.post("/vernal-api/link/create-code", headers=headers("bob"), json={}).json()["code"]
    result = client.post("/vernal-api/link/exchange?project=alpha", json={"code": code, "kind": "folder"}).json()
    link = result["link_id"]
    device = {"Authorization": "Bearer " + result["link_token"], "X-Vernal-Project": "alpha"}
    url = "/vernal-api/link/" + link
    if explicit:
        assert client.delete(url, headers=headers("bob")).is_success
    else:
        projects[0]["members"] = []
        eng.db._exec("UPDATE host_links SET verified_at=?", (time.time() - 61,))
    assert client.get(url + "/events", headers=device).status_code == 403


def test_native_queue_survives_restart_and_remote_history_is_not_reposted(deployment, monkeypatch):
    eng, _, _, _, _ = deployment
    _seed_steering_project(eng)
    _calls = []
    async def native(req):
        _calls.append((req.method, req.url.path))
        if req.method == "POST":
            return httpx.Response(201, json={"id": "task004", "issueId": "iss-3", "workflowId": "wf", "status": "queued"})
        if req.url.path == "/api/tasks/task004":
            return httpx.Response(200, json={"id": "task004", "issueId": "iss-3", "runId": "v2"})
        return httpx.Response(200, json={"id": "v2", "status": "succeeded", "executionContext": {"projectId": "alpha"}})
    monkeypatch.setattr(LoopForgeClient, "_req", _native_request)
    native_client = LoopForgeClient("http://fixture", transport=httpx.MockTransport(native))
    async def scenario():
        row, fresh = dispatch.reserve(eng.db, "T-003", 1, 1, "local_runner", "service", "iss-3")
        assert fresh
        response = await native_client.dispatch_issue("iss-3")
        stop = asyncio.Event()
        stop.set()
        with pytest.raises(LoopForgeError, match="暂停"):
            await dispatch.accept_task(eng.db, row, native_client, response, stop=stop)
        assert eng.db._query("SELECT state FROM dispatch_records")[0]["state"] == "QUEUED"
        recovered = Engine(eng.ws, settings=eng.settings)
        try:
            monkeypatch.setattr(LoopForgeClient, "get_task", lambda self, tid: native_client._req("GET", "/api/tasks/" + tid))
            monkeypatch.setattr(LoopForgeClient, "get_run", lambda self, rid: native_client._req("GET", "/api/runs/" + rid))
            await Scheduler(recovered)._recover_remote_dispatches()
            again, fresh = dispatch.reserve(recovered.db, "T-003", 2, 1, "local_runner", "service", "iss-3")
            assert not fresh and again["lf_run_id"] == "v2"
            advanced, fresh = dispatch.reserve(recovered.db, "T-003", 3, 2, "local_runner", "service", "iss-3")
            assert not fresh and advanced["dispatch_id"] == again["dispatch_id"]
            assert recovered.db.get_task("T-003").loopforge_run_id == "v2"
        finally:
            recovered.db.close()
    asyncio.run(scenario())
    assert sum(m == "POST" for m, _ in _calls) == 1


def test_native_project_selection_inbox_and_dom_contract(deployment):
    eng, _, client, projects, _ = deployment
    projects.append({"id": "native", "ownerId": 1})
    res = client.post("/vernal-api/project/select", headers=headers(), json={"project_id": "native"})
    assert res.json()["needs_binding"] and not (eng.ws.root.parent / "native").exists()
    inbox = client.get("/vernal-api/inbox", headers=headers()).json()
    assert inbox["count"] == int(inbox["plan_pending"]) + sum(len(inbox[k]) for k in
        ("process_gates", "reviews", "fused_tasks", "awaiting_milestones"))
    pinned = json.loads((Path(__file__).parent / "fixtures/loopforge_040_contract.json").read_text())
    static = Path(__file__).resolve().parents[1] / "autodev/server/static"
    adapter = (static / "vernal-overlay.js").read_text()
    assert all(a in adapter for a in ("#platformRail", "issueDetailTabs", "runDetail", "state.issueDetailData", "dataset.runId"))
    assert "vernal-mode" not in adapter and "vernal-show-engine" not in (static / "vernal-overlay.css").read_text()
    if root := os.getenv("VERNAL_040_CONTRACT_ROOT"):
        for name, expected in pinned.items():
            source = (Path(root) / name).read_bytes()
            assert hashlib.sha256(source).hexdigest() == expected["sha256"]
            assert all(marker.encode() in source for marker in expected["markers"])


def test_native_dispatch_to_run_through_bridge(deployment, monkeypatch):
    from autodev import public_pool
    from autodev.loopforge.bridge import LoopForgeBridge
    from autodev.models import TaskStatus

    eng, *_ = deployment
    _seed_steering_project(eng)
    eng.settings.loopforge_workflow_id = "wf"
    eng.db.update_task("T-003", loopforge_issue_id=None, status=TaskStatus.IN_PROGRESS)
    bridge = LoopForgeBridge(eng.settings, eng.db)
    calls = []
    async def native(req):
        calls.append(req.url.path)
        if req.url.path == "/api/issues":
            assert req.method == "POST"
            return httpx.Response(201, json={"id": "issue1"})
        if req.url.path == "/api/issues/issue1/dispatch":
            assert eng.db._query("SELECT state FROM dispatch_records")[0]["state"] == "DISPATCHING"
            return httpx.Response(201, json={"id": "native-task", "issueId": "issue1", "workflowId": "wf", "status": "queued"})
        if req.url.path == "/api/tasks/native-task":
            assert eng.db._query("SELECT state FROM dispatch_records")[0]["state"] == "QUEUED"
            return httpx.Response(200, json={"id": "native-task", "issueId": "issue1", "runId": "native-run"})
        assert req.url.path == "/api/runs/native-run"
        return httpx.Response(200, json={"id": "native-run", "executionContext": {"projectId": "alpha"},
                                        "status": "succeeded", "nodes": {}})
    monkeypatch.setattr(LoopForgeClient, "_req", _native_request)
    bridge.client.transport = httpx.MockTransport(native)
    monkeypatch.setattr(public_pool, "resolve", AsyncMock(return_value=("local_runner", bridge.client)))
    ok, message = asyncio.run(bridge.execute_task(eng.db.get_task("T-003"), "squad", "slot-1", asyncio.Event()))
    assert ok, message
    row = eng.db._query("SELECT * FROM dispatch_records")[0]
    assert (row["lf_task_id"], row["lf_run_id"], row["state"]) == ("native-task", "native-run", "FINISHED")
    assert calls.count("/api/issues/issue1/dispatch") == 1


def test_binding_repository_failure_resume_and_cold_pool(tmp_path, monkeypatch):
    from autodev import gitops
    from autodev.security import persist_workspace_settings
    from autodev.server.project_launcher import load_project_registry

    seed = Engine(init_workspace(tmp_path / "workspaces/default", collaboration_mode="files"))
    seed.settings.runtime_backend = "loopforge"
    seed.settings.loopforge_token = "service"
    repo = tmp_path / "source"
    repo.mkdir()
    (repo / "keep.txt").write_text("original")
    gitops.ensure_repo(repo, "main")
    gitops.commit_all(repo, "seed")
    projects = {}
    monkeypatch.setattr(LoopForgeClient, "list_projects", AsyncMock(side_effect=lambda: list(projects.values())))
    async def create(**args):
        assert args["project_id"] not in projects
        projects[args["project_id"]] = {"id": args["project_id"]}
        return projects[args["project_id"]]
    monkeypatch.setattr(LoopForgeClient, "create_project", AsyncMock(side_effect=create))
    monkeypatch.setattr(LoopForgeClient, "put_repository", AsyncMock(side_effect=LoopForgeError("repository unavailable")))
    monkeypatch.setattr(LoopForgeClient, "put_project", AsyncMock(return_value={}))
    monkeypatch.setattr("autodev.server.app.ensure_vernal_loopforge_assets", AsyncMock(return_value={}))
    app = create_app(seed)
    with TestClient(app) as browser:
        creation = browser.post("/vernal-api/project/create", json={
            "mode": "git_repo", "name": "recover", "git_url": str(repo), "branch": "main"})
        assert creation.status_code == 503, creation.text
        mapping = load_project_registry(app.state.ctx.workspaces_parent())["recover"]
        assert mapping["bind_state"] == "PENDING_UPSTREAM" and "repository unavailable" in mapping["last_error"]
        assert app.state.ctx.find_or_open_slot("recover") is None
        assert (repo / "keep.txt").read_text() == "original"
        monkeypatch.setattr(LoopForgeClient, "put_repository", AsyncMock(return_value={"id": "recover-repo"}))
        resumed = browser.post("/vernal-api/project/resume", json={"project_id": "recover"})
        assert resumed.status_code == 200, resumed.text
        ready = app.state.ctx.find_or_open_slot("recover").engine
        ready.settings.execution_pool = "local_runner"
        persist_workspace_settings(ready.ws, ready.settings)
    with TestClient(create_app(seed)) as browser:
        assert browser.app.state.ctx.find_or_open_slot("recover").engine.settings.execution_pool == "local_runner"
    seed.db.close()


def test_bind_existing_project_preserves_identity_and_rejects_reinitialization(deployment, monkeypatch):
    eng, ctx, client, projects, calls = deployment
    projects.append({"id": "native-id", "ownerId": 1, "members": [{"userId": 2, "role": "member"}]})
    payload = {"mode": "scratch", "project_id": "native-id", "bind_existing": True, "name": "Different Name"}
    path = "/vernal-api/project/create"
    monkeypatch.setattr("autodev.server.app.ensure_vernal_loopforge_assets", AsyncMock())
    assert client.post(path, headers=headers("bob", "native-id"), json=payload).status_code == 403
    response = client.post(path, headers=headers("alice", "native-id"), json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["project_id"] == "native-id"
    directory = client.get("/api/projects", headers=headers("alice")).json()
    assert {p["id"] for p in directory} == {"alpha", "native-id"}
    assert ctx.find_or_open_slot("native-id").engine.db.meta_get("bind_state") == "READY"
    assert client.post(path, headers=headers("alice", "native-id"), json=payload).status_code == 409
    assert not any(method == "POST" and target == "/api/projects" for method, target, _ in calls)
    assert client.get("/vernal-api/host/dirs", headers=headers("bob")).status_code == 403


def test_rotated_service_token_is_never_a_signing_key(deployment):
    eng, *_ = deployment
    initial = eng.settings.service_token()
    assert initial in eng.settings.verification_keys()
    eng.settings.loopforge_token = "new-independent-service-identity"
    assert eng.settings.verification_keys() == (eng.settings.project_signing_key(),)


def test_unmarked_historical_browser_token_cannot_become_service_identity(tmp_path, monkeypatch):
    from autodev.security import persist_workspace_settings
    monkeypatch.delenv("AGENTFLOW_TOKEN", raising=False)
    monkeypatch.setenv("VERNAL_SERVICE_TOKEN_FILE", str(tmp_path / "missing-deployment.token"))
    ws = init_workspace(tmp_path / "legacy", collaboration_mode="files")
    old = ws.state_dir / "credentials.json"
    old.write_text(json.dumps({"loopforge_token": "old-personal-session"}))
    eng = Engine(ws)
    try:
        assert not eng.settings.service_token()
        assert not eng.settings.effective_loopforge_token()
        assert "old-personal-session" in eng.settings.verification_keys()
        eng.settings.loopforge_token = "dedicated-service"
        persist_workspace_settings(ws, eng.settings)
        restored = ws.load_settings()
        assert restored.service_token() == "dedicated-service"
        assert "dedicated-service" not in restored.verification_keys()
        assert restored.project_signing_key() not in ("dedicated-service", "old-personal-session")
    finally:
        eng.db.close()


def test_native_repository_payload_uses_provider_for_gitea_and_self_hosted():
    from autodev.server.routers.projects import _native_repository_payload

    mirror = _native_repository_payload(
        "latex-word-repo",
        "latex-word",
        "latex转换成word",
        clone_url="http://gitea:3000/vernal-local/latex-word.git",
        repo_url="http://127.0.0.1:3000/vernal-local/latex-word.git",
        branch="main",
        gitea_mirror=True,
    )
    assert "repoUrl" not in mirror
    assert mirror["provider"] == "gongfeng"
    assert mirror["gongfengProjectId"] == "vernal-local/latex-word"
    assert mirror["webUrl"] == "http://127.0.0.1:3000/vernal-local/latex-word"
    assert mirror["cloneUrl"] == "http://gitea:3000/vernal-local/latex-word.git"

    corp = _native_repository_payload(
        "corp-repo",
        "corp",
        "Corp",
        clone_url="https://git.corp.internal/team/service.git",
        repo_url="https://git.corp.internal/team/service.git",
        branch="main",
        gitea_mirror=False,
    )
    assert "repoUrl" not in corp
    assert corp["gongfengProjectId"] == "team/service"
    assert corp["webUrl"] == "https://git.corp.internal/team/service"

    github = _native_repository_payload(
        "gh-repo",
        "gh",
        "GH",
        clone_url="https://github.com/acme/demo.git",
        repo_url="https://github.com/acme/demo.git",
        branch="main",
        gitea_mirror=False,
    )
    assert github["repoUrl"] == "https://github.com/acme/demo.git"
    assert "gongfengProjectId" not in github
