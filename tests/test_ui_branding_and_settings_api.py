"""Regression tests for Vernal branding proxy, Gitea host URL, public-pool authorization, and personal CLI credential sync."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from autodev import public_pool
from autodev.config import Settings
from autodev.engine import Engine, init_workspace
from autodev.server.app import create_app
from autodev.server.proxy import OVERLAY_VERSION, transform_loopforge_html


def test_proxy_injects_brand_script_into_embedded_assistant_frames():
    root_html = '<html><head><title>LoopForge · Agent 编排平台</title></head><body><iframe aria-label="LoopForge 工作台"></iframe></body></html>'
    transformed_root = transform_loopforge_html("/", root_html)
    assert "Vernal · 多智能体架构研发平台" in transformed_root
    assert 'aria-label="Vernal 研发助手"' in transformed_root
    assert f"vernal-overlay.js?v={OVERLAY_VERSION}" in transformed_root
    assert "vernal-brand.js" not in transformed_root

    chat_html = "<html><head><title>LoopForge</title></head><body><main>LoopForge</main></body></html>"
    transformed_chat = transform_loopforge_html("/native-chat/", chat_html)
    assert "<title>Vernal</title>" in transformed_chat
    assert f"vernal-overlay.css?v={OVERLAY_VERSION}" in transformed_chat
    assert f"vernal-brand.js?v={OVERLAY_VERSION}" in transformed_chat


def test_runtime_status_returns_host_gitea_url_and_repo_link(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("VERNAL_GITEA_HOST_URL", "http://127.0.0.1:3000")
    monkeypatch.setenv("VERNAL_POOL_DIR", str(tmp_path / "pool"))
    monkeypatch.setenv("VERNAL_SERVICE_TOKEN_FILE", str(tmp_path / "missing.token"))
    ws = init_workspace(tmp_path / "workspaces" / "demo", name="Demo", collaboration_mode="git")
    eng = Engine(ws, settings=Settings(
        collaboration_mode="git",
        runtime_backend="local",
        loopforge_project_id="demo-proj",
        gitea_url="http://gitea:3000",
    ))
    eng.db.meta_set("gitea_mirror", "1")
    app = create_app(eng)
    try:
        with TestClient(app) as client:
            resp = client.get("/vernal-api/runtime/status")
            assert resp.status_code == 200, resp.text
            gt = resp.json()["gitea"]
            assert gt["url"] == "http://127.0.0.1:3000"
            assert gt["internal_url"] == "http://gitea:3000"
            assert gt["repo_url"] == "http://127.0.0.1:3000/vernal-local/demo-proj"
            assert gt["admin_user"] == "vernal-local"
    finally:
        eng.db.close()


def test_public_pool_authorize_project_and_personal_cli_sync(tmp_path: Path, monkeypatch):
    pool_dir = tmp_path / "pool"
    pool_dir.mkdir(parents=True)
    (pool_dir / "identity.json").write_text(json.dumps({"login": "vernalpooltest", "token": "pool-tok", "user_id": 99}))
    (pool_dir / "image-ready").write_text("0.2.2\n")
    monkeypatch.setenv("VERNAL_POOL_DIR", str(pool_dir))
    monkeypatch.setenv("VERNAL_LOOPFORGE_URL", "http://lf.test")
    monkeypatch.setenv("VERNAL_REQUIRE_AUTH", "1")
    monkeypatch.delenv("VERNAL_POOL_ADMINS", raising=False)
    public_pool.save({"deepseek_api_key": "sk-pool-secret", "default_model": "deepseek-flash", "reasoning_model": "deepseek-v4-pro"})

    project = {"id": "alpha", "name": "Alpha", "ownerId": None, "ownerName": None, "members": None}
    saved_creds: dict[int, dict] = {}
    users = {
        "session=alice": {"id": 1, "loginName": "alice", "isPlatformAdmin": True, "platformRole": "admin"},
        "session=bob": {"id": 2, "loginName": "bob", "isPlatformAdmin": False, "platformRole": ""},
    }

    def handler(req: httpx.Request) -> httpx.Response:
        auth = req.headers.get("authorization", "")
        cookie = req.headers.get("cookie", "")
        if req.url.path == "/api/auth/current":
            # Real LoopForge 0.4.0: Bearer token without Cookie always returns guest id=0.
            if auth and not cookie:
                return httpx.Response(200, json={"user": {"id": 0, "loginName": "guest", "isGuest": True}})
            u = users.get(cookie)
            if not u:
                return httpx.Response(401, json={"error": "unauthenticated"})
            return httpx.Response(200, json={"user": {**u, "agentCliCredentials": saved_creds.get(u["id"], {})}})
        if req.url.path == "/api/users":
            return httpx.Response(200, json=[
                {"id": 1, "loginName": "alice", "platformRole": "admin"},
                {"id": 2, "loginName": "bob", "platformRole": ""},
                {"id": 99, "loginName": "vernalpooltest", "platformRole": ""},
            ])
        if req.url.path == "/api/projects":
            if req.method == "GET":
                return httpx.Response(200, json=[project])
            if req.method == "PUT":
                body = json.loads(req.content)
                project["members"] = body.get("members", project["members"])
                return httpx.Response(200, json=project)
        if req.url.path.startswith("/api/users/") and req.url.path.endswith("/credentials") and req.method == "PUT":
            uid = int(req.url.path.split("/")[3])
            body = json.loads(req.content)
            for k, v in (body.get("agentCliCredentials") or {}).items():
                saved_creds.setdefault(uid, {})[k] = {**v, "apiKeyConfigured": bool(v.get("apiKey"))}
            return httpx.Response(200, json={"ok": True})
        if req.url.path == "/api/runners":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr("autodev.loopforge.client.create_async_http_client", lambda *a, **kw: httpx.AsyncClient(transport=transport))

    ws = init_workspace(tmp_path / "workspaces" / "alpha", name="Alpha", collaboration_mode="files")
    eng = Engine(ws, settings=Settings(
        collaboration_mode="files",
        runtime_backend="loopforge",
        loopforge_url="http://lf.test",
        loopforge_project_id="alpha",
        loopforge_token="svc-tok",
    ))
    app = create_app(eng)
    try:
        with TestClient(app) as client:
            # Non-manager Bob cannot authorize public pool for the project
            r_bob = client.post("/vernal-api/runtime/public-pool/authorize-project", headers={"Cookie": "session=bob", "X-Vernal-Project": "alpha"})
            assert r_bob.status_code == 403

            # Owner/Admin Alice authorizes public pool for ownerless project alpha (keeps Alice as maintainer too)
            r_alice = client.post("/vernal-api/runtime/public-pool/authorize-project", headers={"Cookie": "session=alice", "X-Vernal-Project": "alpha"})
            assert r_alice.status_code == 200, r_alice.text
            assert r_alice.json()["project_authorized"] is True
            assert r_alice.json()["service_user_id"] == 99
            assert any(m.get("userId") == 99 and m.get("role") == "member" for m in project["members"])
            assert any(m.get("userId") == 1 and m.get("role") == "maintainer" for m in project["members"])

            # Grant Bob member access to alpha so we can test Bob's personal CLI credential boundaries
            project["members"].append({"userId": 2, "loginName": "bob", "role": "member"})
            app.state.ctx.proj_perm_cache.clear()

            # Non-admin Bob cannot copy deployment pool key into personal credentials
            r_leak = client.post(
                "/vernal-api/credentials/personal-cli",
                headers={"Cookie": "session=bob", "X-Vernal-Project": "alpha"},
                json={"provider": "dsh", "use_pool_key": True},
            )
            assert r_leak.status_code == 403

            # Bob can save his own explicit personal key
            r_own = client.post(
                "/vernal-api/credentials/personal-cli",
                headers={"Cookie": "session=bob", "X-Vernal-Project": "alpha"},
                json={"provider": "dsh", "api_key": "sk-bob-own"},
            )
            assert r_own.status_code == 200, r_own.text
            assert saved_creds[2]["dsh"]["apiKey"] == "sk-bob-own"
            assert saved_creds[2]["dsh"]["baseUrl"] == "https://api.deepseek.com"

            # Admin Alice can sync from pool key cleanly for both OpenCode and dsh in one click
            r_sync = client.post(
                "/vernal-api/credentials/personal-cli",
                headers={"Cookie": "session=alice", "X-Vernal-Project": "alpha"},
                json={"provider": "both", "use_pool_key": True},
            )
            assert r_sync.status_code == 200, r_sync.text
            assert r_sync.json()["providers"] == ["opencode", "dsh"]
            for prov in ("opencode", "dsh"):
                assert saved_creds[1][prov]["apiKey"] == "sk-pool-secret"
                assert saved_creds[1][prov]["models"] == [{"id": "deepseek-flash"}, {"id": "deepseek-v4-pro"}]
    finally:
        eng.db.close()


def test_sqlite_journal_mode_uses_delete_for_docker_mounts(tmp_path: Path, monkeypatch):
    from autodev.db import Database, sqlite_journal_mode

    monkeypatch.setenv("VERNAL_SQLITE_JOURNAL_MODE", "DELETE")
    assert sqlite_journal_mode() == "DELETE"
    db = Database(tmp_path / "durable.db")
    try:
        mode = db._conn.execute("PRAGMA journal_mode").fetchone()[0].upper()
        assert mode == "DELETE"
        db.meta_set("status", "PLANNED")
        assert not (tmp_path / "durable.db-wal").exists()
    finally:
        db.close()


def test_large_candidate_artifact_spillover_and_resolution(tmp_path: Path, monkeypatch):
    import hashlib
    from urllib.parse import urlsplit

    from autodev.context_bundle import resolve_candidate_artifact
    from autodev.loopforge.bootstrap import candidate_upload_url
    from autodev.native_review_context import spill_large_candidates

    monkeypatch.setenv("VERNAL_POOL_DIR", str(tmp_path / "pool"))
    monkeypatch.setenv("VERNAL_SERVICE_TOKEN_FILE", str(tmp_path / "missing.token"))
    ws = init_workspace(tmp_path / "workspaces" / "bigproj", name="BigProj", collaboration_mode="files")
    eng = Engine(ws, settings=Settings(
        collaboration_mode="files",
        runtime_backend="loopforge",
        loopforge_project_id="bigproj",
        vernal_public_url="http://vernal:8787",
    ))
    app = create_app(eng)
    try:
        upload_url = candidate_upload_url(eng.settings, "bigproj")
        upload_path = urlsplit(upload_url).path
        big_payload = {"files": {f"src/file_{i}.py": ("x = " + "a" * 2048 + "\n") for i in range(45)}}
        raw_bytes = json.dumps(big_payload).encode("utf-8")
        assert len(raw_bytes) > 80_000

        with TestClient(app) as client:
            # Invalid signature must be rejected
            bad_path = upload_path.rsplit("/", 1)[0] + "/" + ("0" * 64)
            assert client.post(bad_path, content=raw_bytes).status_code == 403

            # Valid signed upload stores file and returns SHA-256 reference
            resp = client.post(upload_path, content=raw_bytes, headers={"Content-Type": "application/json"})
            assert resp.status_code == 200, resp.text
            data = resp.json()
            digest = hashlib.sha256(raw_bytes).hexdigest()
            assert data["vernal_ref"] == digest
            assert data["bytes"] == len(raw_bytes)

            # Resolver reconstructs full payload from tiny stub
            stub = json.dumps({"vernal_candidate_ref": digest, "sha256": digest, "bytes": len(raw_bytes)})
            assert len(stub) < 256
            assert json.loads(resolve_candidate_artifact(ws, stub)) == big_payload

            # Tampered or missing ref fails closed
            bad_stub = json.dumps({"vernal_candidate_ref": "f" * 64, "sha256": "f" * 64})
            try:
                resolve_candidate_artifact(ws, bad_stub)
                raise AssertionError("expected ValueError for missing candidate ref")
            except ValueError:
                pass

            # Spillover helper rewrites >48 KB artifact to small pointer stub via upload endpoint
            run_dir = tmp_path / "sandbox_run"
            run_dir.mkdir()
            (run_dir / "vernal-files.json").write_bytes(raw_bytes)

            class _FakeResp:
                def __init__(self, body: bytes):
                    self._body = body
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    return False
                def read(self) -> bytes:
                    return self._body

            class _FakeOpener:
                def open(self, req, timeout=30):
                    r = client.post(urlsplit(req.full_url).path, content=req.data, headers={"Content-Type": "application/json"})
                    assert r.status_code == 200
                    return _FakeResp(r.content)

            monkeypatch.setattr("urllib.request.build_opener", lambda *a, **kw: _FakeOpener())
            monkeypatch.setenv("VERNAL_CANDIDATE_UPLOAD_URL", upload_url)
            monkeypatch.chdir(run_dir)
            spill_large_candidates(max_inline_bytes=48_000)
            rewritten = (run_dir / "vernal-files.json").read_text("utf-8")
            assert len(rewritten.encode("utf-8")) < 300
            assert json.loads(rewritten)["vernal_candidate_ref"] == digest
            assert json.loads(resolve_candidate_artifact(ws, rewritten)) == big_payload
    finally:
        eng.db.close()


def test_live_progress_nodes_stall_detection_and_task_restart(tmp_path: Path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from autodev.models import MilestoneStatus, PlanTask, ProjectStatus, TaskStatus

    monkeypatch.setenv("VERNAL_POOL_DIR", str(tmp_path / "pool"))
    monkeypatch.setenv("VERNAL_SERVICE_TOKEN_FILE", str(tmp_path / "missing.token"))
    monkeypatch.setenv("VERNAL_DISABLE_AUTO_RESUME", "1")
    monkeypatch.setenv("VERNAL_LOOPFORGE_URL", "http://lf.test")

    ws = init_workspace(tmp_path / "workspaces" / "stallproj", name="StallProj", collaboration_mode="files")
    eng = Engine(ws, settings=Settings(
        collaboration_mode="files",
        runtime_backend="loopforge",
        loopforge_url="http://lf.test",
        loopforge_project_id="stallproj",
        loopforge_token="svc-tok",
    ))
    eng.db._exec(
        "INSERT INTO milestones(milestone_id, seq_order, title, goal, integration_cmd, status, planned) "
        "VALUES('M1', 1, '第一阶段', '完成核心链路', 'pytest', ?, 1)",
        (MilestoneStatus.PENDING.value,),
    )
    eng.db.insert_single_task(PlanTask(
        task_id="T-001", milestone_id="M1", squad="core", module="core", role="coder",
        title="核心解析与打包", description="测试卡滞恢复", acceptance_criteria=["ok"],
        depends_on=[], doc_slice_ids=[], allowed_paths=["src/"], verify_cmds=["pytest"],
        priority=1,
    ), max_retries=3)
    eng.db.update_task("T-001", status=TaskStatus.IN_PROGRESS, loopforge_run_id="run-zombie", loopforge_issue_id="issue-1")
    eng.db._exec(
        "INSERT INTO dispatch_records(task_id,exec_seq,attempt_no,pool,actor,issue_id,lf_task_id,lf_run_id,state,created_at,updated_at) "
        "VALUES('T-001',1,1,'public_docker','owner','issue-1','lt-1','run-zombie','RUNNING','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')"
    )
    eng.set_status(ProjectStatus.RUNNING)

    old_ts = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat()
    paused_calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/runs/run-zombie" and req.method == "GET":
            return httpx.Response(200, json={
                "id": "run-zombie",
                "status": "running",
                "startedAt": old_ts,
                "updatedAt": old_ts,
                "workflowSnapshot": {
                    "nodes": [
                        {"id": "implement", "title": "模块编码与自测", "type": "agent"},
                        {"id": "package", "title": "打包校验交付清单", "type": "shell"},
                        {"id": "review", "title": "独立审查", "type": "agent"},
                    ]
                },
                "nodes": {
                    "implement": {"status": "succeeded", "startedAt": old_ts, "finishedAt": old_ts},
                    "package": {"status": "running", "startedAt": old_ts},
                    "review": {"status": "pending"},
                },
            })
        if req.url.path == "/api/runs/run-zombie/pause" and req.method == "POST":
            paused_calls.append("run-zombie")
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr("autodev.loopforge.client.create_async_http_client", lambda *a, **kw: httpx.AsyncClient(transport=transport))
    app = create_app(eng)
    try:
        with TestClient(app) as client:
            resp = client.get("/vernal-api/runtime/live-progress")
            assert resp.status_code == 200, resp.text
            lp = resp.json()
            assert lp["project_status"] == "RUNNING"
            assert lp["auto_resumable"] is True
            assert lp["milestone_summary"]["tasks_total"] == 1
            assert lp["milestone_summary"]["tasks_active"] == 1
            assert len(lp["active_runs"]) == 1
            run_info = lp["active_runs"][0]
            assert [n["id"] for n in run_info["nodes"]] == ["implement", "package", "review"]
            assert [n["status"] for n in run_info["nodes"]] == ["succeeded", "running", "pending"]
            assert run_info["stalled"] is True
            assert "打包校验交付清单" in run_info["stalled_reason"]

            r_restart = client.post("/vernal-api/tasks/T-001/restart?start_scheduler=false")
            assert r_restart.status_code == 200, r_restart.text
            assert paused_calls == ["run-zombie"]
            task_after = eng.db.get_task("T-001")
            assert task_after is not None
            assert task_after.status == TaskStatus.READY
            assert not task_after.loopforge_run_id
            assert task_after.exec_seq >= 1
            d_row = eng.db._query("SELECT state FROM dispatch_records WHERE task_id='T-001'")[0]
            assert d_row["state"] == "FAILED"
    finally:
        eng.db.close()


def test_startup_auto_resume_restarts_running_project_scheduler(tmp_path: Path, monkeypatch):
    from autodev.models import MilestoneStatus, PlanTask, ProjectStatus, TaskStatus

    monkeypatch.setenv("VERNAL_POOL_DIR", str(tmp_path / "pool"))
    monkeypatch.setenv("VERNAL_SERVICE_TOKEN_FILE", str(tmp_path / "missing.token"))
    monkeypatch.delenv("VERNAL_DISABLE_AUTO_RESUME", raising=False)

    ws = init_workspace(tmp_path / "workspaces" / "resumable", name="Resumable", collaboration_mode="files")
    eng = Engine(ws, settings=Settings(
        collaboration_mode="files",
        runtime_backend="local",
        loopforge_project_id="resumable",
    ))
    eng.db._exec(
        "INSERT INTO milestones(milestone_id, seq_order, title, goal, integration_cmd, status, planned) "
        "VALUES('M1', 1, 'M1', 'Goal', '', ?, 1)",
        (MilestoneStatus.PENDING.value,),
    )
    eng.db.insert_single_task(PlanTask(
        task_id="T-001", milestone_id="M1", squad="core", module="core", role="coder",
        title="Active Task", description="desc", acceptance_criteria=["ok"],
        depends_on=[], doc_slice_ids=[], allowed_paths=["src/"], verify_cmds=[],
        priority=1,
    ), max_retries=3)
    eng.db.update_task("T-001", status=TaskStatus.IN_PROGRESS)
    eng.set_status(ProjectStatus.RUNNING)

    resumed_keys: list[str] = []

    def fake_start(ctx, slot, *, watch=True):
        resumed_keys.append(slot.project_key)
        return True

    monkeypatch.setattr("autodev.server.routers.planning.start_slot_scheduler", fake_start)
    app = create_app(eng)
    try:
        with TestClient(app):
            assert "resumable" in resumed_keys
            events = [e["event_type"] for e in eng.db.events_after(0)]
            assert "scheduler.auto_resumed" in events
    finally:
        eng.db.close()


def test_task_stream_parses_loopforge_node_thoughts_and_tools(tmp_path: Path, monkeypatch):
    from autodev.models import MilestoneStatus, PlanTask, ProjectStatus, TaskStatus

    monkeypatch.setenv("VERNAL_POOL_DIR", str(tmp_path / "pool"))
    monkeypatch.setenv("VERNAL_SERVICE_TOKEN_FILE", str(tmp_path / "missing.token"))
    monkeypatch.setenv("VERNAL_DISABLE_AUTO_RESUME", "1")
    monkeypatch.setenv("VERNAL_LOOPFORGE_URL", "http://lf.test")

    ws = init_workspace(tmp_path / "workspaces" / "streamproj", name="StreamProj", collaboration_mode="files")
    eng = Engine(ws, settings=Settings(
        collaboration_mode="files",
        runtime_backend="loopforge",
        loopforge_url="http://lf.test",
        loopforge_project_id="streamproj",
        loopforge_token="svc-tok",
    ))
    eng.db._exec(
        "INSERT INTO milestones(milestone_id, seq_order, title, goal, integration_cmd, status, planned) "
        "VALUES('M1', 1, '阶段1', '目标', 'pytest', ?, 1)",
        (MilestoneStatus.PENDING.value,),
    )
    eng.db.insert_single_task(PlanTask(
        task_id="T-001", milestone_id="M1", squad="core", module="core", role="coder",
        title="实现契约并自测", description="测试思考链解析", acceptance_criteria=["ok"],
        depends_on=[], doc_slice_ids=[], allowed_paths=["contracts/"], verify_cmds=["pytest"],
        priority=1,
    ), max_retries=3)
    eng.db.update_task("T-001", status=TaskStatus.IN_PROGRESS, loopforge_run_id="run-stream-1")
    eng.set_status(ProjectStatus.RUNNING)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/runs/run-stream-1" and req.method == "GET":
            return httpx.Response(200, json={
                "id": "run-stream-1",
                "status": "running",
                "toolCalls": 3,
                "workflowSnapshot": {
                    "nodes": [
                        {"id": "implement", "title": "模块编码与自测", "type": "agent"},
                        {"id": "package", "title": "打包校验交付清单", "type": "shell"},
                        {"id": "review", "title": "独立审查", "type": "agent"},
                    ]
                },
                "nodes": {
                    "implement": {"status": "succeeded", "provider": "codex", "toolCalls": 3, "output": "已完成 11 项契约自测"},
                    "package": {"status": "running", "provider": "terminal", "toolCalls": 0},
                    "review": {"status": "pending"},
                },
            })
        if req.url.path == "/api/runs/run-stream-1/logs" and req.method == "GET":
            return httpx.Response(200, json={
                "logs": [
                    {
                        "id": "l1", "sequence": 1, "timestamp": "2026-11-25T06:03:26Z",
                        "level": "info", "stream": "platform", "body": "沙箱镜像已就绪",
                        "source": {"layer": "sandbox"}, "context": {"nodeId": "implement"},
                    },
                    {
                        "id": "l2", "sequence": 2, "timestamp": "2026-11-25T06:03:41Z",
                        "level": "info", "stream": "worker", "body": "我会先编写 OpenAPI 契约与样本。",
                        "source": {"layer": "executor", "provider": "codex"},
                        "context": {"nodeId": "implement", "executorId": "codex"},
                        "attributes": {"category": "output", "eventName": "agent.output", "phase": "finish"},
                    },
                    {
                        "id": "l3", "sequence": 3, "timestamp": "2026-11-25T06:03:45Z",
                        "level": "info", "stream": "worker",
                        "body": "/bin/zsh -lc 'python3 -m unittest discover' /bin/zsh -lc 'python3 -m unittest discover'",
                        "source": {"layer": "executor", "provider": "codex"},
                        "context": {"nodeId": "implement", "executorId": "codex"},
                        "attributes": {"category": "tool", "eventName": "tool.call", "phase": "start", "status": "in_progress"},
                    },
                    {
                        "id": "l4", "sequence": 4, "timestamp": "2026-11-25T06:03:49Z",
                        "level": "info", "stream": "worker",
                        "body": "/bin/zsh -lc 'python3 -m unittest discover'",
                        "source": {"layer": "executor", "provider": "codex"},
                        "context": {"nodeId": "implement", "executorId": "codex"},
                        "attributes": {"category": "tool", "eventName": "tool.call", "phase": "finish", "status": "completed"},
                    },
                    {
                        "id": "l5", "sequence": 5, "timestamp": "2026-11-25T06:04:01Z",
                        "level": "info", "stream": "stdout", "body": "已打包 42 个交付文件至 vernal-files.json",
                        "source": {"layer": "executor", "provider": "terminal"},
                        "context": {"nodeId": "package"},
                        "attributes": {"category": "process", "eventName": "process.stdout"},
                    },
                ]
            })
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr("autodev.loopforge.client.create_async_http_client", lambda *a, **kw: httpx.AsyncClient(transport=transport))
    app = create_app(eng)
    try:
        with TestClient(app) as client:
            resp = client.get("/vernal-api/tasks/T-001/stream")
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["source"] == "loopforge"
            assert data["tool_calls_count"] == 3
            assert [n["id"] for n in data["nodes"]] == ["implement", "package", "review"]
            impl = data["nodes"][0]
            assert impl["provider"] == "codex"
            assert impl["system_count"] == 1
            kinds = [it["kind"] for it in impl["items"]]
            assert kinds == ["system", "message", "tool"]
            # Deduplicated echoed command and collapsed start+finish into completed
            assert impl["items"][2]["body"] == "python3 -m unittest discover"
            assert impl["items"][2]["status"] == "completed"
            assert "运行测试" in impl["items"][2]["title"]
            pkg = data["nodes"][1]
            assert pkg["items"][0]["kind"] == "stdout"
            assert "42 个交付文件" in pkg["items"][0]["body"]
    finally:
        eng.db.close()


def test_scratch_project_creation_respects_files_mode_and_explicit_route(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("VERNAL_POOL_DIR", str(tmp_path / "pool"))
    monkeypatch.setenv("VERNAL_SERVICE_TOKEN_FILE", str(tmp_path / "missing.token"))
    monkeypatch.setenv("VERNAL_DISABLE_AUTO_RESUME", "1")

    ws_seed = init_workspace(tmp_path / "workspaces" / "seed", name="Seed", collaboration_mode="git")
    seed_eng = Engine(ws_seed, settings=Settings(
        collaboration_mode="git",
        runtime_backend="local",
        execution_pool="local_runner",
        harness="codex",
        model="gpt-5.4",
        coder_harness="codex",
        coder_model="gpt-5.4",
        director_harness="codex",
        director_model="gpt-5.4",
        loopforge_project_id="seed",
    ))
    app = create_app(seed_eng)
    try:
        with TestClient(app) as client:
            resp = client.post(
                "/vernal-api/project/create",
                json={
                    "mode": "scratch",
                    "name": "DocOnly",
                    "collaboration_mode": "files",
                    "execution_pool": "public_docker",
                    "harness": "dsh",
                    "auto_start": False,
                },
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["collaboration_mode"] == "files"
            assert body["execution_pool"] == "public_docker"
            assert body["harness"] == "dsh"
            created_cfg = Settings.load(Path(body["workspace_root"]) / ".autodev" / "config.toml")
            assert created_cfg.collaboration_mode == "files"
            assert created_cfg.execution_pool == "public_docker"
            assert created_cfg.harness == "dsh"
            # Must NOT silently inherit seed_eng's codex/gpt-5.4 overrides!
            assert created_cfg.model == ""
            assert created_cfg.coder_harness == ""
            assert created_cfg.coder_model == ""
            assert created_cfg.director_harness == ""
            assert created_cfg.director_model == ""
    finally:
        seed_eng.db.close()


def test_gitea_credentials_admin_only_and_no_store(tmp_path: Path, monkeypatch):
    pool_dir = tmp_path / "pool"
    pool_dir.mkdir(parents=True)
    (pool_dir / "gitea-password").write_text("gt-auto-pass-999\n", encoding="utf-8")
    monkeypatch.setenv("VERNAL_POOL_DIR", str(pool_dir))
    monkeypatch.setenv("VERNAL_GITEA_HOST_URL", "http://127.0.0.1:3000")
    monkeypatch.setenv("VERNAL_LOOPFORGE_URL", "http://lf.test")
    monkeypatch.setenv("VERNAL_REQUIRE_AUTH", "1")
    monkeypatch.delenv("VERNAL_POOL_ADMINS", raising=False)

    users = {
        "session=admin": {"id": 1, "loginName": "admin", "platformRole": "admin", "isPlatformAdmin": True},
        "session=bob": {"id": 2, "loginName": "bob", "platformRole": "", "isPlatformAdmin": False},
    }

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/auth/current":
            u = users.get(req.headers.get("cookie", ""))
            if not u:
                return httpx.Response(401, json={"error": "unauthenticated"})
            return httpx.Response(200, json={"user": u})
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr("autodev.loopforge.client.create_async_http_client", lambda *a, **kw: httpx.AsyncClient(transport=transport))

    ws = init_workspace(tmp_path / "workspaces" / "gtproj", name="GTProj", collaboration_mode="git")
    eng = Engine(ws, settings=Settings(collaboration_mode="git", runtime_backend="local", loopforge_project_id="gtproj"))
    app = create_app(eng)
    try:
        with TestClient(app) as client:
            r_bob = client.post("/vernal-api/runtime/gitea/credentials", headers={"Cookie": "session=bob"})
            assert r_bob.status_code == 403

            r_adm = client.post("/vernal-api/runtime/gitea/credentials", headers={"Cookie": "session=admin"})
            assert r_adm.status_code == 200, r_adm.text
            assert r_adm.headers.get("cache-control") == "no-store"
            data = r_adm.json()
            assert data["username"] == "vernal-local"
            assert data["password"] == "gt-auto-pass-999"
            assert data["login_url"] == "http://127.0.0.1:3000/user/login"
    finally:
        eng.db.close()


def test_agent_config_surfaces_workflow_sync_failure(tmp_path: Path, monkeypatch):
    from autodev.loopforge.client import LoopForgeError

    monkeypatch.setenv("VERNAL_POOL_DIR", str(tmp_path / "pool"))
    monkeypatch.setenv("VERNAL_SERVICE_TOKEN_FILE", str(tmp_path / "missing.token"))
    ws = init_workspace(tmp_path / "workspaces" / "syncfail", name="SyncFail", collaboration_mode="files")
    eng = Engine(ws, settings=Settings(
        collaboration_mode="files",
        runtime_backend="loopforge",
        loopforge_url="http://lf.test",
        loopforge_project_id="syncfail",
        loopforge_token="svc-tok",
        execution_pool="local_runner",
        harness="codex",
    ))

    async def boom(*_a, **_kw):
        raise LoopForgeError("模板校验失败：sandboxProfileId 无效", status_code=422)

    monkeypatch.setattr("autodev.server.app.ensure_vernal_loopforge_assets", boom)
    app = create_app(eng)
    try:
        with TestClient(app) as client:
            resp = client.post(
                "/vernal-api/agent/config",
                json={"execution_pool": "local_runner", "harness": "codex", "sync_loopforge": True},
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["ok"] is True
            assert body["synced_loopforge"] is False
            assert "模板校验失败" in body["sync_error"]
    finally:
        eng.db.close()


def test_loopforge_plan_dispatch_backfills_terminal_state(tmp_path: Path, monkeypatch):
    import asyncio
    from autodev.harnesses.loopforge_harness import LoopForgeHarness
    from autodev.loopforge.client import LoopForgeClient

    monkeypatch.setenv("VERNAL_POOL_DIR", str(tmp_path / "pool"))
    ws = init_workspace(tmp_path / "workspaces" / "planbf", name="PlanBF", collaboration_mode="files")
    settings = Settings(
        collaboration_mode="files",
        runtime_backend="loopforge",
        loopforge_url="http://lf.test",
        loopforge_project_id="planbf",
        loopforge_token="svc-tok",
        execution_pool="local_runner",
        harness="codex",
    )
    eng = Engine(ws, settings=settings)

    run_counter = {"n": 0}
    run_modes = {1: "succeeded", 2: "failed"}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/api/runners":
            return httpx.Response(200, json=[{
                "id": "r1",
                "status": "online",
                "runtimes": [{"provider": "codex", "binaryPath": "/usr/local/bin/codex"}],
            }])
        if path == "/api/projects":
            return httpx.Response(200, json=[{"id": "planbf", "name": "PlanBF", "ownerId": 1, "members": []}])
        if path in ("/api/agents", "/api/workflows", "/api/sandboxes"):
            return httpx.Response(200, json=[])
        if path.startswith("/api/workflows/") and req.method == "GET":
            return httpx.Response(200, json={"id": path.split("/")[-1], "sandboxProfileId": "", "version": 1})
        if path == "/api/issues" and req.method == "POST":
            return httpx.Response(200, json={"id": "iss-plan-1"})
        if path == "/api/issues" and req.method == "PUT":
            return httpx.Response(200, json={"id": "iss-plan-1"})
        if path.startswith("/api/issues/") and path.endswith("/dispatch") and req.method == "POST":
            run_counter["n"] += 1
            return httpx.Response(200, json={"id": f"lt-{run_counter['n']}", "issueId": "iss-plan-1", "runId": f"run-plan-{run_counter['n']}"})
        if path.startswith("/api/issues/") and req.method == "GET":
            return httpx.Response(200, json={"issue": {"id": "iss-plan-1", "projectId": "planbf"}})
        if path.startswith("/api/runs/run-plan-"):
            idx = int(path.rsplit("-", 1)[-1])
            st = run_modes[idx]
            if st == "succeeded":
                return httpx.Response(200, json={
                    "id": f"run-plan-{idx}",
                    "status": "succeeded",
                    "artifacts": [{"name": "vernal-result.json", "content": '{"ok": true}'}],
                })
            return httpx.Response(200, json={"id": f"run-plan-{idx}", "status": "failed", "error": "boom"})
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr("autodev.loopforge.client.create_async_http_client", lambda *a, **kw: httpx.AsyncClient(transport=transport))

    schema_path = tmp_path / "council_consensus.schema.json"
    schema_path.write_text('{"type":"object","properties":{"ok":{"type":"boolean"}},"required":["ok"]}', encoding="utf-8")
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    async def _run():
        harness = LoopForgeHarness(settings, eng.db, ws, poll_interval=0.05, client=LoopForgeClient("http://lf.test", "svc-tok"))
        r1 = await harness.run_structured_readonly("prompt ok", schema_path=schema_path, cwd=ws.root, log_dir=log_dir, role="director")
        assert r1.ok is True, r1.error
        r2 = await harness.run_structured_readonly("prompt fail", schema_path=schema_path, cwd=ws.root, log_dir=log_dir, role="director")
        assert r2.ok is False

    try:
        asyncio.run(_run())
        rows = eng.db._query("SELECT lf_run_id, state FROM dispatch_records ORDER BY dispatch_id ASC")
        states = {r["lf_run_id"]: r["state"] for r in rows}
        assert states == {"run-plan-1": "FINISHED", "run-plan-2": "FAILED"}
    finally:
        eng.db.close()


def test_public_pool_smoke_test_and_next_action_banner(tmp_path: Path, monkeypatch):
    from autodev.models import ProjectStatus

    pool_dir = tmp_path / "pool"
    pool_dir.mkdir(parents=True)
    (pool_dir / "identity.json").write_text(json.dumps({"login": "vernalpooltest", "token": "pool-tok", "user_id": 99}))
    (pool_dir / "image-ready").write_text("0.2.2\n")
    monkeypatch.setenv("VERNAL_POOL_DIR", str(pool_dir))
    monkeypatch.setenv("VERNAL_LOOPFORGE_URL", "http://lf.test")
    monkeypatch.setenv("VERNAL_DISABLE_AUTO_RESUME", "1")
    public_pool.save({"deepseek_api_key": "sk-pool-secret", "default_model": "deepseek-flash", "reasoning_model": "deepseek-v4-pro"})

    saved_workflows: dict[str, dict] = {}
    saved_sandboxes: dict[str, dict] = {}
    saved_dockerfiles: dict[str, dict] = {}
    saved_creds: dict[str, dict] = {}
    saved_issue: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/api/users":
            return httpx.Response(200, json=[
                {"id": 1, "loginName": "alice", "platformRole": "admin"},
                {
                    "id": 99,
                    "loginName": "vernalpooltest",
                    "platformRole": "",
                    "agentCliCredentials": {
                        k: {
                            "apiKeyConfigured": True,
                            "baseUrl": v.get("baseUrl", ""),
                            "protocol": v.get("protocol", "chat"),
                            "models": v.get("models", []),
                        }
                        for k, v in saved_creds.items()
                    },
                },
            ])
        if path == "/api/projects":
            return httpx.Response(200, json=[{
                "id": "smokeproj",
                "name": "SmokeProj",
                "ownerId": 1,
                "members": [{"userId": 99, "loginName": "vernalpooltest", "role": "member"}],
            }])
        if path.startswith("/api/users/") and path.endswith("/credentials") and req.method == "PUT":
            body = json.loads(req.content)
            saved_creds.update(body.get("agentCliCredentials") or {})
            return httpx.Response(200, json={"ok": True})
        if path == "/api/runners":
            return httpx.Response(200, json=[])
        if path == "/api/workflows" and req.method == "GET":
            return httpx.Response(200, json=list(saved_workflows.values()))
        if path == "/api/workflows" and req.method == "PUT":
            body = json.loads(req.content)
            wf_id = str(body.get("id") or "")
            saved_workflows[wf_id] = {**body, "id": wf_id, "version": 3}
            return httpx.Response(200, json=saved_workflows[wf_id])
        if path.startswith("/api/workflows/") and req.method == "PUT":
            wf_id = path.split("/")[-1]
            body = json.loads(req.content)
            saved_workflows[wf_id] = {**body, "id": wf_id, "version": 3}
            return httpx.Response(200, json=saved_workflows[wf_id])
        if path.startswith("/api/workflows/") and req.method == "GET":
            wf_id = path.split("/")[-1]
            return httpx.Response(200, json=saved_workflows.get(wf_id, {"id": wf_id, "sandboxProfileId": "smokeproj-vernal-docker-dsh", "version": 3}))
        if path == "/api/agents":
            return httpx.Response(200, json=[])
        if path == "/api/sandboxes":
            if req.method == "PUT":
                body = json.loads(req.content)
                sid = str(body.get("id") or "")
                saved_sandboxes[sid] = body
                return httpx.Response(200, json=body)
            return httpx.Response(200, json=list(saved_sandboxes.values()))
        if path == "/api/dockerfiles":
            if req.method == "PUT":
                body = json.loads(req.content)
                did = str(body.get("id") or "")
                saved_dockerfiles[did] = body
                return httpx.Response(200, json=body)
            return httpx.Response(200, json=list(saved_dockerfiles.values()))
        if path == "/api/issues" and req.method in ("POST", "PUT"):
            body = json.loads(req.content)
            saved_issue.clear()
            saved_issue.update({**body, "id": "iss-smoke-1"})
            return httpx.Response(200, json=saved_issue)
        if path == "/api/issues/iss-smoke-1" and req.method == "GET":
            return httpx.Response(200, json={"issue": saved_issue})
        if path == "/api/issues/iss-smoke-1/dispatch" and req.method == "POST":
            return httpx.Response(200, json={"id": "lt-smoke-1", "issueId": "iss-smoke-1", "runId": "run-smoke-1"})
        if path == "/api/runs/run-smoke-1":
            return httpx.Response(200, json={
                "id": "run-smoke-1",
                "status": "succeeded",
                "executionContext": {"executionMode": "cloud", "executionRunnerId": "", "responsibleUserId": 99},
                "workflowSnapshot": {"sandboxProfileId": "smokeproj-vernal-docker-dsh"},
                "artifacts": [{"name": "vernal-result.json", "content": '{"ok": true, "harness": "dsh", "marker": "vernal-smoke-ok"}'}],
            })
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr("autodev.loopforge.client.create_async_http_client", lambda *a, **kw: httpx.AsyncClient(transport=transport))

    ws = init_workspace(tmp_path / "workspaces" / "smokeproj", name="SmokeProj", collaboration_mode="files")
    eng = Engine(ws, settings=Settings(
        collaboration_mode="files",
        runtime_backend="loopforge",
        loopforge_url="http://lf.test",
        loopforge_project_id="smokeproj",
        loopforge_token="svc-tok",
        execution_pool="local_runner",
        harness="codex",
    ))
    eng.set_status(ProjectStatus.PLANNED)
    app = create_app(eng)
    try:
        with TestClient(app) as client:
            r_smoke = client.post("/vernal-api/runtime/public-pool/smoke-test", json={"harness": "dsh"})
            assert r_smoke.status_code == 200, r_smoke.text
            res = r_smoke.json()
            assert res["ok"] is True, f"{res.get('error')} | {res.get('steps')}"
            assert res["run_id"] == "run-smoke-1"
            assert res["sandbox_id"] == "smokeproj-vernal-docker-dsh"
            assert [s["key"] for s in res["steps"]] == ["authorize", "sandbox_sync", "model_run", "verify_isolation"]
            assert all(s["ok"] for s in res["steps"])
            # Temporary smoke flag must be cleaned up after run
            assert not eng.db.meta_get("public_pool.smoke_active_until")
            # State endpoint surfaces last_smoke on public_docker pool
            st = client.get("/vernal-api/state").json()
            assert st["runner_summary"]["pools"]["public_docker"]["last_smoke"]["run_id"] == "run-smoke-1"
            # Live-progress computes clear next_action banner for PLANNED state
            lp = client.get("/vernal-api/runtime/live-progress").json()
            assert lp["next_action"]["bucket"] == "waiting_approval"
            assert lp["next_action"]["who"] == "human"
            assert lp["next_action"]["action_kind"] == "approve_plan"
    finally:
        eng.db.close()
