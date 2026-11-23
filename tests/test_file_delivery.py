import asyncio
import io
import json
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from autodev.artifacts import FileExchange, apply_output
from autodev.engine import Engine, init_workspace
from autodev.harnesses.mock import MockHarness
from autodev.loopforge.client import LoopForgeClient, use_environment_proxy
from autodev.models import PlanMilestone, PlanModule, PlanTask, ProjectPlan, ProjectStatus
from autodev.scheduler import Scheduler
from autodev.server.app import create_app


def plan():
    def task(tid, path, depends):
        return PlanTask(task_id=tid, milestone_id="M1", module="game", role="coder",
                        title=tid, description=path, acceptance_criteria=["可打开"],
                        depends_on=depends, doc_slice_ids=[], allowed_paths=[path],
                        verify_cmds=[f"test -s {path}"], priority=10)
    return ProjectPlan(summary="飞船", tech_stack="js", agents_md="test",
                       modules=[PlanModule(name="game",path="game/",description="demo")],
                       milestones=[PlanMilestone(milestone_id="M1",title="交付",goal="可玩",
                                                integration_cmd="test -s web/index.html && test -s engine/game.js")],
                       tasks=[task("T-001","engine/game.js",[]),task("T-002","web/index.html",["T-001"])])


@pytest.mark.parametrize("backend", ["local", "loopforge"])
def test_files_handoff_to_verified_release(tmp_path, monkeypatch, backend):
    ws = init_workspace(tmp_path / "space", collaboration_mode="files")
    def coder(prompt, cwd, call):
        if "web/index.html" in prompt:
            assert (cwd / "engine/game.js").read_text() == "engine"
            path, text = "web/index.html", "play"
        else:
            path, text = "engine/game.js", "engine"
        (cwd / path).parent.mkdir(exist_ok=True)
        (cwd / path).write_text(text)
        return {"status":"DONE","summary":"ok"}
    engine = Engine(ws, harness=MockHarness(coder=coder))
    engine.settings.runtime_backend = backend
    engine.settings.loopforge_token = "test"
    engine.settings.loopforge_project_id = "demo"
    engine.settings.require_merge_approval = True
    issues = []
    async def create(_, **body):
        issues.append(body)
        if len(issues) == 2:
            assert '"engine/game.js": "engine"' in body["description"]
        assert body["repository_id"] == ""
        return {"id":str(len(issues))}
    async def dispatch(_, issue_id):
        return {"latestRunId":issue_id}
    async def get_run(_, rid):
        files = {"engine/game.js":"engine"} if rid == "1" else {"web/index.html":"play"}
        return {"id":rid, "status":"succeeded", "artifacts":[
            {"name":"vernal-files.json", "content":json.dumps({"files":files})}]}
    monkeypatch.setattr(LoopForgeClient,"create_issue",create)
    monkeypatch.setattr(LoopForgeClient,"dispatch_issue",dispatch)
    monkeypatch.setattr(LoopForgeClient,"get_run",get_run)
    try:
        engine.db.save_plan(plan(),3)
        engine.set_status(ProjectStatus.APPROVED)
        asyncio.run(Scheduler(engine).run())
        m=engine.db.get_milestone("M1")
        assert m.status == "AWAITING_MERGE", [(t.status,t.failure_reason) for t in engine.db.list_tasks()]
        engine.merge_milestone("M1", actor="tester")
        assert engine.status == ProjectStatus.COMPLETED and not (ws.root / ".git").exists()
        assert all(t.artifact_digest and not t.candidate_commit_sha for t in engine.db.list_tasks())
        with TestClient(create_app(engine)) as client:
            result=client.get("/vernal-api/milestones/M1/files.zip")
            assert result.status_code == 200
            with zipfile.ZipFile(io.BytesIO(result.content)) as archive:
                assert archive.read("web/index.html")==b"play"
        assert bool(issues) == (backend == "loopforge")
    finally:
        engine.db.close()


def test_proxy_login_wish_and_explicit_runtime(tmp_path, monkeypatch):
    ws = init_workspace(tmp_path/"new", collaboration_mode="files")
    eng=Engine(ws)
    def upstream(req):
        if req.url.path == "/api/auth/current":
            return httpx.Response(200, json={"cookie": req.headers.get("cookie")})
        if req.url.path == "/api/projects":
            assert req.headers["origin"]=="http://127.0.0.1:8090"
            return httpx.Response(200,json=[],headers=[("Set-Cookie","a=x"),("Set-Cookie","b=y")])
        return httpx.Response(200,text="<html><head><title>LoopForge</title></head><body><h1>LoopForge</h1></body></html>",
                              headers={"content-type":"text/html"})
    try:
        with TestClient(create_app(eng, httpx.MockTransport(upstream))) as client:
            assert "vernal-overlay.js" in client.get("/").text
            assert "<h1>Vernal</h1>" in client.get("/login.html").text
            assert client.get("/vernal-static/vernal-overlay.js").status_code == 200
            assert client.post("/api/projects",headers={"Origin":"http://testserver"}).headers.get_list("set-cookie")==["a=x","b=y"]
            client.cookies.clear()
            assert client.get("/api/auth/current").json()["cookie"] is None
            assert client.post("/vernal-api/scheduler/start").status_code==409
            assert client.post("/vernal-api/wish",json={"title":"射击游戏","content":"创建飞船游戏"}).json()["slices"]==1
            assert client.get("/vernal-api/openapi.json").status_code==200
            monkeypatch.setattr(LoopForgeClient,"auth_current_with_cookie",AsyncMock(return_value={"loginName":"pilot"}))
            token=AsyncMock(return_value={"token":"test-value"})
            monkeypatch.setattr(LoopForgeClient,"create_token_with_cookie",token)
            monkeypatch.setattr(LoopForgeClient,"list_projects",AsyncMock(return_value=[{"id":"orbital"}]))
            client.cookies.set("session","test-session")
            assert client.post("/vernal-api/runtime/bind").json()["project_id"]=="orbital"
            assert "session=test-session" in token.await_args.args[0]
            assert client.post("/vernal-api/runtime/bind").json()["reused"]
            assert ws.load_settings().collaboration_mode=="files"
            assert ws.load_settings().loopforge_project_id=="orbital"
    finally:
        eng.db.close()


def test_local_services_bypass_host_proxy():
    assert not use_environment_proxy("http://127.0.0.1:8090")
    assert not use_environment_proxy("http://localhost:3000")
    assert not use_environment_proxy("http://[::1]:8090")
    assert use_environment_proxy("https://loopforge.example.com")


def test_bridge_auto_passes_submit_gate_result_waiting_node(tmp_path, monkeypatch):
    from autodev.loopforge.bridge import LoopForgeBridge

    ws = init_workspace(tmp_path / "autopass", collaboration_mode="files")
    eng = Engine(ws)
    eng.settings.runtime_backend = "loopforge"
    eng.settings.loopforge_token = "tok"
    eng.settings.loopforge_project_id = "proj"
    eng.settings.loopforge_workflow_id = "proj-vernal-file-sop"
    try:
        eng.db.save_plan(plan(), 3)
        eng.db.ensure_slots(1)
        task = eng.db.get_task("T-001")
        eng.db.update_task("T-001", loopforge_issue_id="iss-1", loopforge_run_id="run-1")

        calls = []
        states = [
            {
                "id": "run-1",
                "status": "waiting",
                "workflowSnapshot": {"nodes": [{"id": "implement", "type": "agent"}]},
                "nodes": {
                    "implement": {
                        "status": "waiting_approval",
                        "attempt": 1,
                        "reworkCount": 0,
                        "error": "未收到 submit_gate_result，恢复 CLI session 后仍未提交",
                    }
                },
            },
            {"id": "run-1", "status": "succeeded", "nodes": {"implement": {"status": "succeeded"}}},
        ]

        async def fake_get_run(_, rid):
            return states.pop(0)

        async def fake_approve(_, rid, comment="", node_id=""):
            calls.append((rid, node_id))
            return {"ok": True}

        monkeypatch.setattr(LoopForgeClient, "get_run", fake_get_run)
        monkeypatch.setattr(LoopForgeClient, "approve_run", fake_approve)
        ok, _ = asyncio.run(LoopForgeBridge(eng.settings, eng.db).execute_task(eng.db.get_task("T-001"), "组", "slot-1", asyncio.Event()))
        assert ok is True
        assert calls == [("run-1", "implement")]
    finally:
        eng.db.close()


def test_file_validation_and_parallel_conflict(tmp_path):
    ws=init_workspace(tmp_path/"new",collaboration_mode="files")
    eng=Engine(ws)
    try:
        eng.db.save_plan(plan(),3)
        store=FileExchange(ws,eng.db)
        task=eng.db.get_task("T-001")
        with pytest.raises(ValueError):
            store.patch(task,{},{"web/index.html":"escape"},[])
        with pytest.raises(ValueError):
            apply_output({},'{"files":{"../outside":"oops"}}')
        for task in eng.db.list_tasks():
            eng.db.update_task(task.task_id, depends_on=[], artifact_digest=store.put({"web/x":[None,task.task_id]}))
        with pytest.raises(ValueError,match="同一文件"):
            store.baseline(["T-001","T-002"])
    finally:
        eng.db.close()


def test_single_issue_reused_on_retry_and_runner_offline_fuses(tmp_path, monkeypatch):
    ws = init_workspace(tmp_path / "rerun-check", collaboration_mode="files")
    eng = Engine(ws)
    eng.settings.runtime_backend = "loopforge"
    eng.settings.loopforge_token = "tok"
    eng.settings.loopforge_project_id = "proj"
    eng.settings.loopforge_workflow_id = "proj-vernal-file-sop"
    created_issues = []
    updated_issues = []
    dispatches = []

    async def fake_runners(_):
        return [{"id": "r1", "name": "Mac", "status": "online"}]

    async def fake_create(_, **kw):
        created_issues.append(kw["title"])
        return {"id": f"iss-{len(created_issues)}"}

    async def fake_update(_, **kw):
        updated_issues.append(kw["issue_id"])
        return {"id": kw["issue_id"]}

    async def fake_dispatch(_, issue_id, rerun=False):
        dispatches.append((issue_id, rerun))
        return {"latestRunId": f"run-{len(dispatches)}"}

    async def fake_get_run(_, rid):
        if rid == "run-1":
            # 第 1 次普通业务测试失败 -> 应触发原单重跑，而不是新建第 2 条需求
            return {"id": rid, "status": "failed", "nodes": {"implement": {"error": "assertion failed in game.js"}}}
        if rid == "run-2":
            return {"id": rid, "status": "succeeded", "artifacts": [{"name": "vernal-files.json", "content": '{"files":{"engine/game.js":"ok"}}'}]}
        # T-002 模拟执行器离线 -> 应立即熔断，不再连刷 3 次重试
        return {"id": rid, "status": "failed", "nodes": {"implement": {"error": "执行器 Mac 当前离线，无法路由 codex 任务"}}}

    monkeypatch.setattr(LoopForgeClient, "list_runners", fake_runners)
    monkeypatch.setattr(LoopForgeClient, "create_issue", fake_create)
    monkeypatch.setattr(LoopForgeClient, "update_issue", fake_update)
    monkeypatch.setattr(LoopForgeClient, "dispatch_issue", fake_dispatch)
    monkeypatch.setattr(LoopForgeClient, "get_run", fake_get_run)
    try:
        eng.db.save_plan(plan(), 3)
        eng.set_status(ProjectStatus.APPROVED)
        asyncio.run(Scheduler(eng).run())
        t1 = eng.db.get_task("T-001")
        t2 = eng.db.get_task("T-002")
        assert t1.status.value == "APPROVED"
        assert t1.loopforge_issue_id == "iss-1"
        assert updated_issues == ["iss-1"]
        assert dispatches[:2] == [("iss-1", False), ("iss-1", True)]
        # T-002 只派发了 1 次就因离线熔断，retry_count 为 0（未浪费 3 次重试）
        assert t2.status.value == "FUSED"
        assert t2.retry_count == 0
        assert created_issues == ["[T-001] T-001", "[T-002] T-002"]
    finally:
        eng.db.close()


def test_planning_prompts_enforce_concise_plain_language():
    from autodev.config import Settings
    from autodev.prompts import build_blueprint_prompt, build_wave_prompt

    bp_prompt = build_blueprint_prompt([], {"python3": "3.14"}, Settings())
    assert "精简敏捷档" in bp_prompt
    assert "严禁“文书核账" in bp_prompt
    assert "说人话" in bp_prompt

    wave_prompt = build_wave_prompt(
        {"milestone_id": "M2", "title": "核心", "goal": "g"},
        [],
        [],
        [],
        [],
        "",
        [],
        4,
        Settings(),
    )
    assert "严格控制单阶段卡片数量" in wave_prompt
    assert "说人话" in wave_prompt
