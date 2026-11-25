"""测试 Vernal 统一网关：同源 LoopForge 前端注入、自动装配标准角色/SOP、多项目切换与远端运行可观测接口。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from autodev.artifacts import FileExchange
from autodev.config import Settings
from autodev.engine import Engine, init_workspace
from autodev.harnesses.mock import MockHarness
from autodev.server.app import create_app

MOCK_LOOPFORGE_INDEX = """<!doctype html>
<html lang="zh-CN">
<head><title>LoopForge · Agent 编排平台</title></head>
<body>
  <aside id="platformRail"><nav class="rail-nav"></nav></aside>
  <div class="shell-main"><main><iframe aria-label="LoopForge 工作台"></iframe></main></div>
</body>
</html>"""

MOCK_LOOPFORGE_LOGIN = """<!doctype html>
<html lang="zh-CN">
<head><title>LoopForge · 登录</title></head>
<body class="login-page"><main class="login-card"><h1>LoopForge</h1><p>登录你的交付工作台</p></main></body>
</html>"""


def test_server_endpoints_and_proxy_injection(tmp_path: Path) -> None:
    ws = init_workspace(tmp_path / "workspaces" / "web-demo", name="web-demo", collaboration_mode="files")
    (ws.inputs_dir / "arch.md").write_text("# Web Demo\n\n架构说明。\n", "utf-8")

    plan_dict = {
        "summary": "Web API 测试计划",
        "tech_stack": "Python 3",
        "agents_md": "# Demo",
        "modules": [{"name": "contracts", "path": "contracts/", "description": "契约"}],
        "milestones": [{"milestone_id": "M1", "title": "M1", "goal": "g", "integration_cmd": ""}],
        "tasks": [
            {
                "task_id": "T-001",
                "milestone_id": "M1",
                "module": "contracts",
                "role": "contract",
                "title": "契约定义",
                "description": "写契约",
                "acceptance_criteria": ["ok"],
                "depends_on": [],
                "doc_slice_ids": ["DOC01-S01"],
                "allowed_paths": ["contracts/", "tests/"],
                "verify_cmds": ["python3 -c 'print(1)'"],
                "priority": 10,
            }
        ],
    }

    seen_origins: list[str] = []

    def _proxy_handler(req: httpx.Request) -> httpx.Response:
        if req.headers.get("origin"):
            seen_origins.append(req.headers["origin"])
        if req.url.path == "/":
            return httpx.Response(200, text=MOCK_LOOPFORGE_INDEX, headers={"content-type": "text/html; charset=utf-8"})
        if req.url.path == "/login.html":
            return httpx.Response(200, text=MOCK_LOOPFORGE_LOGIN, headers={"content-type": "text/html; charset=utf-8"})
        if req.url.path == "/api/auth/password":
            return httpx.Response(
                200,
                json={"ok": True},
                headers=[("set-cookie", "af_session=signed123; Path=/; HttpOnly")],
            )
        return httpx.Response(404, json={"error": "not found"})

    eng = Engine(
        ws,
        settings=Settings(require_plan_approval=True, collaboration_mode="files"),
        harness=MockHarness(planner=lambda *_: plan_dict),
    )
    app = create_app(eng, proxy_transport=httpx.MockTransport(_proxy_handler))

    try:
        with TestClient(app) as client:
            # 1. 原版首页与登录页无侵入重写 + 蓝图样式与挂载脚本注入
            r = client.get("/")
            assert r.status_code == 200
            assert "<title>Vernal · 多智能体架构研发平台</title>" in r.text
            assert "/vernal-static/vernal-overlay.css" in r.text
            assert "/vernal-static/vernal-overlay.js" in r.text

            r_login = client.get("/login.html")
            assert r_login.status_code == 200
            assert "<h1>Vernal</h1>" in r_login.text
            assert "/vernal-static/vernal-overlay.css" in r_login.text

            # 2. 同源 Origin 重写与 Set-Cookie 原样透传
            r_auth = client.post("/api/auth/password", json={"login": "admin", "password": "pw"}, headers={"Origin": "http://testserver"})
            assert r_auth.status_code == 200
            assert seen_origins == ["http://127.0.0.1:8090"]
            assert "af_session=signed123" in r_auth.headers.get("set-cookie", "")

            client.cookies.clear()  # Exercise the offline CLI after testing cookie forwarding.
            # 3. Vernal 大脑接口与统一 Diff 渲染
            r = client.post("/vernal-api/ingest")
            assert r.status_code == 200
            assert r.json()["slices"] == 1

            slices = client.get("/vernal-api/slices").json()
            assert len(slices) == 1
            assert slices[0]["slice_id"] == "DOC01-S01"

            asyncio.run(eng.plan())
            st = client.get("/vernal-api/state").json()
            assert st["status"] == "PLANNED"
            assert len(st["tasks"]) == 1

            r = client.post("/vernal-api/plan/approve", json={"actor": "bob"})
            assert r.status_code == 200
            assert r.json()["status"] == "APPROVED"

            ex = FileExchange(eng.ws, eng.db)
            digest = ex.patch(eng.db.get_task("T-001"), {}, {"contracts/api.py": "VERSION = 1\n"}, [])
            eng.db.update_task("T-001", artifact_digest=digest)

            diff_res = client.get("/vernal-api/tasks/T-001/diff").json()
            assert diff_res["source"] == "files"
            assert "+VERSION = 1" in diff_res["diff"]

            trace_res = client.get("/vernal-api/tasks/T-001/trace").json()
            assert "steps" in trace_res
    finally:
        eng.db.close()


def test_runtime_bind_provisions_loopforge_assets_and_project_switch(tmp_path: Path) -> None:
    ws_root = tmp_path / "workspaces"
    ws = init_workspace(ws_root / "alpha", name="Alpha", collaboration_mode="files")
    saved_agents: list[str] = []
    saved_workflows: list[str] = []
    published: list[str] = []

    def _lf_api(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p == "/api/auth/current":
            assert "af_session=ok" in req.headers.get("cookie", "")
            return httpx.Response(200, json={"user": {"loginName": "zomer", "isGuest": False}})
        if p == "/api/auth/tokens" and req.method == "POST":
            return httpx.Response(201, json={"token": "afp_test_auto_token"})
        if p == "/api/projects":
            return httpx.Response(200, json=[{"id": "proj-alpha", "name": "Alpha"}])
        if p == "/api/sandboxes":
            return httpx.Response(200, json=[{"id": "local", "name": "Local"}])
        if p == "/api/rules" and req.method == "PUT":
            return httpx.Response(200, json=json.loads(req.content))
        if p == "/api/agents":
            if req.method == "GET":
                return httpx.Response(200, json=[])
            payload = json.loads(req.content)
            saved_agents.append(payload["id"])
            return httpx.Response(200, json=payload)
        if p == "/api/workflows":
            if req.method == "GET":
                return httpx.Response(200, json=[])
            payload = json.loads(req.content)
            saved_workflows.append(payload["id"])
            return httpx.Response(200, json={**payload, "status": "draft"})
        if p.endswith("/publish") and req.method == "POST":
            wid = p.split("/")[-2]
            published.append(wid)
            return httpx.Response(200, json={"id": wid, "status": "published"})
        if p == "/api/runs/run-99":
            return httpx.Response(200, json={"id": "run-99", "status": "running", "nodes": {"implement": {"status": "running", "output": "writing game.js"}}})
        if p == "/api/runs/run-99/logs":
            return httpx.Response(200, json=[{"nodeId": "implement", "message": "npm test passed"}])
        return httpx.Response(200, json=[])

    real_async_client = httpx.AsyncClient
    mock_transport = httpx.MockTransport(_lf_api)

    eng = Engine(ws, settings=Settings(collaboration_mode="files", runtime_backend="loopforge"))
    app = create_app(eng, proxy_transport=mock_transport)

    with patch("autodev.loopforge.client.httpx.AsyncClient", lambda **kw: real_async_client(transport=mock_transport)):
        with TestClient(app) as client:
            r = client.post("/vernal-api/runtime/bind", headers={"Cookie": "af_session=ok"})
            assert r.status_code == 200
            body = r.json()
            assert body["bound"] is True
            assert body["project_id"] == "proj-alpha"
            assert body["workflow_id"] == "proj-alpha-vernal-file-sop"
            assert "proj-alpha-vernal-coder" in saved_agents
            assert "proj-alpha-vernal-reviewer" in saved_agents
            assert "proj-alpha-vernal-file-sop" in saved_workflows
            assert "proj-alpha-vernal-file-sop" in published

            # A purely native project can be selected without creating a local workspace.
            sw = client.post("/vernal-api/project/select", json={"project_id": "proj-beta", "project_name": "Beta"})
            assert sw.status_code == 200
            assert sw.json()["switched"] is True
            assert sw.json()["needs_binding"] is True
            assert not (ws_root / "proj-beta").exists()
            st_beta = client.get("/vernal-api/state").json()
            assert st_beta["name"] == "Alpha"
