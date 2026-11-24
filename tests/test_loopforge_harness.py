"""测试 LoopForgeHarness 统一规划执行、上下文外部化、离线熔断、绑定码接口与校验脚本。"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from autodev.config import Settings
from autodev.engine import Engine, init_workspace
from autodev.harnesses.loopforge_harness import LoopForgeHarness
from autodev.loopforge.bootstrap import VALIDATE_PLAN_JSON_SCRIPT, plan_workflow_id
from autodev.server.app import create_app


@pytest.mark.parametrize("public_engine", [None, "dsh", "opencode"])
def test_loopforge_harness_dispatches_auto_passes_reuses_issue_and_parses_result(tmp_path: Path, monkeypatch, public_engine) -> None:
    ws = init_workspace(tmp_path / "demo", name="demo", collaboration_mode="files")
    settings = Settings(
        collaboration_mode="files",
        runtime_backend="loopforge",
        loopforge_url="http://lf.test",
        loopforge_token="afp_test_tok",
        loopforge_project_id="proj-plan",
        vernal_public_url="http://127.0.0.1:8787",
    )
    shared_credentials = {}
    registered_sandboxes = []
    if public_engine:
        from autodev import public_pool
        monkeypatch.setenv("VERNAL_POOL_DIR", str(tmp_path / "pool"))
        monkeypatch.setenv("VERNAL_POOL_TOKEN", "shared-test-token")
        monkeypatch.setenv("VERNAL_LOOPFORGE_URL", settings.loopforge_url)
        public_pool.save({"deepseek_api_key": "not-for-the-runner"})
        (public_pool.directory() / "image-ready").write_text("dsh and OpenCode")
        settings.execution_pool = "public_docker"
        settings.harness = public_engine
    eng = Engine(ws, settings=settings)
    assert isinstance(eng.harness, LoopForgeHarness)

    created_issues: list[dict] = []
    updated_issues: list[dict] = []
    dispatches: list[dict] = []
    approved_nodes: list[str] = []
    poll_count = {"n": 0}

    def _handler(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if public_engine:
            if p == "/api/dockerfiles":
                assert json.loads(req.content)["content"].startswith("FROM autodev-runtime-sandbox")
                return httpx.Response(200, json={"ok": True})
            if p == "/api/sandboxes" and req.method == "PUT":
                registered_sandboxes.append(json.loads(req.content))
                return httpx.Response(200, json={"ok": True})
            if p == "/api/sandboxes":
                return httpx.Response(200, json=[{"id": "local"}, *registered_sandboxes])
            if p == "/api/workflows" and req.method == "PUT":
                assert json.loads(req.content)["sandboxProfileId"] == registered_sandboxes[0]["id"]
            public_identity = req.headers.get("authorization") == "Bearer shared-test-token"
            if p == "/api/runners" and public_identity:
                return httpx.Response(200, json=[])
            if p == "/api/runners/default":
                pytest.fail("public dispatch attempted to change the personal default Runner")
            if p == "/api/projects":
                return httpx.Response(200, json=[{"id": settings.loopforge_project_id}])
            if p == "/api/users/32/credentials":
                assert public_identity and b"not-for-the-runner" not in req.content
                shared_credentials.update(json.loads(req.content)["agentCliCredentials"])
                return httpx.Response(200, json={})
            if p == "/api/auth/current":
                creds = {k: {"apiKeyConfigured": True, "baseUrl": v["baseUrl"]} for k, v in shared_credentials.items()}
                return httpx.Response(200, json={"user": {"id": 32 if public_identity else 12, "agentCliCredentials": creds}})
            if p == "/api/issues/iss-plan-1" and req.method == "GET":
                assert created_issues[0]["responsibleUserId"] == 32
                return httpx.Response(200, json={"issue": created_issues[0]})
            if p.startswith(("/api/issues", "/api/runs")):
                assert public_identity
            if p == "/api/agents" and req.method == "PUT":
                assert json.loads(req.content)["executorId"] == public_engine
        if p == "/api/runners":
            return httpx.Response(200, json=[{"id": "r-1", "name": "Mac", "status": "online", "isDefault": True, "runtimes": [{"provider": "codex"}]}])
        if p == "/api/runners/default":
            return httpx.Response(200, json={"ok": True})
        if p == "/api/sandboxes":
            return httpx.Response(200, json=[{"id": "local"}])
        if p in ("/api/rules", "/api/skills", "/api/mcp-servers"):
            if req.method == "GET":
                return httpx.Response(200, json=[])
            return httpx.Response(200, json={"ok": True})
        if p == "/api/agents":
            return httpx.Response(200, json=[] if req.method == "GET" else json.loads(req.content))
        if p == "/api/workflows":
            if req.method == "GET":
                return httpx.Response(200, json=[])
            body = json.loads(req.content)
            return httpx.Response(200, json={**body, "status": "draft"})
        if p.endswith("/publish") and req.method == "POST":
            return httpx.Response(200, json={"status": "published"})
        if p == "/api/issues" and req.method == "POST":
            body = json.loads(req.content)
            created_issues.append(body)
            return httpx.Response(201, json={"id": "iss-plan-1", **body})
        if p == "/api/issues" and req.method == "PUT":
            body = json.loads(req.content)
            updated_issues.append(body)
            return httpx.Response(200, json=body)
        if p == "/api/issues/iss-plan-1/dispatch" and req.method == "POST":
            body = json.loads(req.content)
            dispatches.append(body)
            poll_count["n"] = 0
            return httpx.Response(200, json={"latestRunId": f"run-plan-{len(dispatches)}"})
        if p.startswith("/api/runs/run-plan-") and p.endswith("/approve"):
            body = json.loads(req.content)
            approved_nodes.append(body.get("nodeId", ""))
            return httpx.Response(200, json={"ok": True})
        if p == "/api/runs/run-plan-1/artifacts/art-plan-1":
            return httpx.Response(
                200,
                json={
                    "headline": "PM 建议先做 10 种排序脚本",
                    "proposals": ["统一函数签名"],
                    "concerns": ["避免外部依赖"],
                    "questions": [],
                    "recommended_sandboxes": ["local"],
                },
            )
        if p.startswith("/api/runs/run-plan-"):
            poll_count["n"] += 1
            if poll_count["n"] == 1:
                return httpx.Response(
                    200,
                    json={
                        "id": p.rsplit("/", 1)[-1],
                        "status": "waiting",
                        "workflowSnapshot": {"nodes": [{"id": "draft", "type": "agent"}]},
                        "nodes": {
                            "draft": {
                                "status": "waiting",
                                "attempt": 1,
                                "reworkCount": 0,
                                "error": "未收到 submit_gate_result",
                            }
                        },
                    },
                )
            rid = p.rsplit("/", 1)[-1]
            if rid == "run-plan-1":
                return httpx.Response(
                    200,
                    json={
                        "id": rid,
                        "status": "succeeded",
                        "usage": {"inputTokens": 120, "cacheReadTokens": 40, "outputTokens": 55},
                        "artifacts": [{"id": "art-plan-1", "name": "vernal-result.json"}],
                    },
                )
            return httpx.Response(
                200,
                json={
                    "id": rid,
                    "status": "succeeded",
                    "usage": {"inputTokens": 120, "cacheReadTokens": 40, "outputTokens": 55},
                    "artifacts": [
                        {
                            "name": "vernal-result.json",
                            "content": json.dumps(
                                {
                                    "headline": "PM 建议先做 10 种排序脚本",
                                    "proposals": ["统一函数签名"],
                                    "concerns": ["避免外部依赖"],
                                    "questions": [],
                                    "recommended_sandboxes": ["local"],
                                },
                                ensure_ascii=False,
                            ),
                        }
                    ],
                },
            )
        return httpx.Response(200, json={})

    real_async_client = httpx.AsyncClient
    transport = httpx.MockTransport(_handler)
    harness = LoopForgeHarness(settings, eng.db, ws, poll_interval=0.01)

    with patch("autodev.loopforge.client.httpx.AsyncClient", lambda **kw: real_async_client(transport=transport)):
        # 第 1 次：新建规划 Issue + 自动放行 submit_gate_result + 读取 vernal-result.json
        res1 = asyncio.run(
            harness.run_structured_readonly(
                "请审阅排序算法需求",
                schema_path=eng.schemas["council_opinion"],
                cwd=ws.root,
                log_dir=ws.runs_dir / "_council" / "r1-pm",
                role="pm",
            )
        )
        assert res1.ok and res1.structured is not None
        assert res1.structured["headline"] == "PM 建议先做 10 种排序脚本"
        assert res1.usage.input_tokens == 120 and res1.usage.output_tokens == 55
        assert approved_nodes == ["draft"]
        assert len(created_issues) == 1
        assert created_issues[0]["workflowId"] == plan_workflow_id("proj-plan", "pm")

        # 第 2 次同角色同阶段：复用原 Issue（update + rerun=True），不刷屏新建
        res2 = asyncio.run(
            harness.run_structured_readonly(
                "补充：加上基数排序",
                schema_path=eng.schemas["council_opinion"],
                cwd=ws.root,
                log_dir=ws.runs_dir / "_council" / "r1-pm-2",
                role="pm",
            )
        )
        assert res2.ok
        assert len(created_issues) == 1
        assert len(updated_issues) == 1
        assert dispatches[-1]["rerun"] is True
    eng.db.close()


def test_loopforge_harness_externalizes_large_context_and_fuses_offline_runner(tmp_path: Path) -> None:
    ws = init_workspace(tmp_path / "big", name="big", collaboration_mode="files")
    (ws.inputs_dir / "spec.md").write_text("# Huge Spec\n" + ("算法说明 " * 5000), "utf-8")
    settings = Settings(
        collaboration_mode="files",
        runtime_backend="loopforge",
        loopforge_url="http://lf.test",
        loopforge_token="afp_test_tok",
        loopforge_project_id="proj-big",
        vernal_public_url="http://10.0.0.5:8787",
    )
    eng = Engine(ws, settings=settings)
    harness = LoopForgeHarness(settings, eng.db, ws, poll_interval=0.01)

    desc = harness._build_issue_description("blueprint", "请阅读 inputs/ 中的大文档", eng.schemas["project_blueprint"], "director")
    assert "http://10.0.0.5:8787/vernal-api/plan-context/blueprint.json" in desc
    assert (ws.state_dir / "baselines" / "plan-blueprint.json").is_file()

    # 测试 REST 接口能够提供规划上下文与 Runner 绑定码
    def _lf_api(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/runners":
            return httpx.Response(200, json=[{"id": "r-off", "name": "OfflineMac", "status": "offline"}])
        if req.url.path == "/api/runners/bind-codes" and req.method == "POST":
            return httpx.Response(200, json={"code": "bind-xyz-999"})
        return httpx.Response(200, json={})

    real_async_client = httpx.AsyncClient
    transport = httpx.MockTransport(_lf_api)
    app = create_app(eng, proxy_transport=transport)
    with patch("autodev.loopforge.client.httpx.AsyncClient", lambda **kw: real_async_client(transport=transport)):
        # 离线 runner 预检应立刻失败且不建单
        res = asyncio.run(
            harness.run_structured_readonly(
                "hi",
                schema_path=eng.schemas["project_blueprint"],
                cwd=ws.root,
                log_dir=ws.runs_dir / "_planner" / "attempt-1",
                role="director",
            )
        )
        assert not res.ok and "离线" in res.error

        with TestClient(app) as client:
            ctx_resp = client.get("/vernal-api/plan-context/blueprint.json")
            assert ctx_resp.status_code == 200
            assert "PLAN_PROMPT.md" in ctx_resp.json()["files"]
            assert ".vernal-schema.json" in ctx_resp.json()["files"]

            raw_code = client.post("/vernal-api/runner/bind-code?format=raw")
            assert raw_code.status_code == 200 and raw_code.text == "bind-xyz-999"

            json_code = client.post("/vernal-api/runner/bind-code")
            assert json_code.status_code == 200 and json_code.json()["code"] == "bind-xyz-999"
    eng.db.close()


def test_validate_plan_json_script_enforces_schema_and_rejects_bad_json(tmp_path: Path) -> None:
    (tmp_path / ".vernal-schema.json").write_text(json.dumps({"type": "object", "required": ["headline", "proposals"]}), "utf-8")

    # 1. 缺少文件 -> 失败
    r1 = subprocess.run([sys.executable, "-c", VALIDATE_PLAN_JSON_SCRIPT], cwd=tmp_path, capture_output=True, text=True)
    assert r1.returncode != 0 and "缺少 vernal-result.json" in (r1.stderr + r1.stdout)

    # 2. 缺必填字段 -> 失败
    (tmp_path / "vernal-result.json").write_text('{"headline": "only"}', "utf-8")
    r2 = subprocess.run([sys.executable, "-c", VALIDATE_PLAN_JSON_SCRIPT], cwd=tmp_path, capture_output=True, text=True)
    assert r2.returncode != 0 and "proposals" in (r2.stderr + r2.stdout)

    # 3. 合法 JSON -> 通过
    (tmp_path / "vernal-result.json").write_text('{"headline": "ok", "proposals": ["a"]}', "utf-8")
    r3 = subprocess.run([sys.executable, "-c", VALIDATE_PLAN_JSON_SCRIPT], cwd=tmp_path, capture_output=True, text=True)
    assert r3.returncode == 0


@pytest.mark.parametrize(
    "run_statuses, expected_run, dispatch_count",
    [
        ([200], "run-1234", 1),
        ([404], "run-ok", 2),
        ([503, 200], "run-1234", 1),
    ],
)
def test_reconcile_deadlocked_dispatch(tmp_path, run_statuses, expected_run, dispatch_count):
    from autodev.loopforge.client import LoopForgeClient, LoopForgeError

    ws = init_workspace(tmp_path / "retry", collaboration_mode="files")
    settings = Settings(loopforge_project_id="demo", collaboration_mode="files", runtime_backend="loopforge")
    eng = Engine(ws, settings=settings)
    attempts = []
    remaining_statuses = iter(run_statuses)

    def handle(request):
        path = request.url.path
        if path == "/api/issues":
            return httpx.Response(201, json={"id": "issue1"})
        if path == "/api/issues/issue1/dispatch":
            attempts.append(path)
            if len(attempts) == 1:
                error = "runs upsert run-1234: runs write: Error 1213 (40001): Deadlock found when trying to get lock"
                return httpx.Response(500, json={"error": error})
            return httpx.Response(200, json={"latestRunId": "run-ok"})
        if path == "/api/issues/issue1":
            latest = "" if run_statuses == [404] else "run-1234"
            return httpx.Response(200, json={"issue": {"id": "issue1", "latestRunId": latest}})
        if path == "/api/runs/run-1234":
            status = next(remaining_statuses)
            if status == 200:
                return httpx.Response(200, json={"id": "run-1234", "executionContext": {"issueId": "issue1"}})
            return httpx.Response(status, json={"error": "lookup failed"})
        pytest.fail(f"Unexpected request {path}")

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handle)

    def dispatch():
        harness = LoopForgeHarness(settings, eng.db, ws)
        client = LoopForgeClient("http://lf.test", "fixture")
        return asyncio.run(harness._dispatch_plan(
            client, "demo", "council_pm", "PM", "review", "sop", "", "signature"
        ))

    try:
        with patch("autodev.loopforge.client.httpx.AsyncClient", lambda **kw: real_client(transport=transport)):
            if run_statuses[0] == 503:
                with pytest.raises(LoopForgeError):
                    dispatch()
                assert len(attempts) == 1
            assert dispatch() == ("issue1", expected_run)
            assert len(attempts) == dispatch_count
            assert not eng.db.meta_get("plan_issue:demo:council_pm:pending_run")
    finally:
        eng.db.close()
