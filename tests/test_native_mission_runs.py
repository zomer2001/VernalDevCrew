import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from autodev.engine import Engine
from autodev.loopforge import reconciler
from autodev.loopforge.bootstrap import build_standard_plan_workflow
from autodev.loopforge.client import LoopForgeClient
from autodev.loopforge.protocol import FailureCategory, native_progress
from autodev.server.app import create_app
from autodev.speckit import adapter, missions, preflight, service
from test_missions import assessed, mock_bundles, native_result, project, review_result
from test_speckit import PLAN, BRIEF, SPEC, work_packages, save


@pytest.mark.parametrize("stage,ids,agent", [
    ("std_spec", ["prepare", "draft_spec", "validate_spec", "quality_check"], "planner-pm"),
    ("std_plan", ["prepare", "draft_plan_and_packages", "validate_and_compile", "consistency_review"], "planner"),
])
def test_standard_combo_nodes(stage, ids, agent):
    wf = build_standard_plan_workflow("p", stage, "sandbox")
    assert wf["id"] == f"p-vernal-{stage.replace('_', '-')}-sop"
    assert wf["sandboxProfileId"] == "sandbox"
    assert [n["id"] for n in wf["nodes"]] == ids
    assert [(e["source"], e["target"]) for e in wf["edges"]] == list(zip(ids, ids[1:]))
    prepare, draft, validate, qa = wf["nodes"]
    assert prepare["type"] == validate["type"] == "script"
    assert draft["agentId"] == "p-vernal-" + agent and qa["agentId"] == "p-vernal-planner-qa"
    assert all(n["rejectTo"] == ids[1] and n["onFailure"] == "reject" and n["maxReworks"] == 2 for n in (validate, qa))
    assert {item["name"] for item in qa["artifactOutputs"]} == {"vernal-quality.json", "review-report.md"}


@pytest.mark.parametrize("status,node_status,node,count,expected,category", [
    ("running", "running", "draft_spec", 0, "RUNNING", None),
    ("running", "running", "draft_spec", 1, "REWORKING", None),
    ("running", "succeeded", "draft_spec", 2, "RUNNING", None),
    ("waiting", "waiting_approval", "signoff", 0, "WAITING_DECISION", FailureCategory.USER_DECISION_REQUIRED),
    ("failed", "failed", "validate_spec", 2, "REWORK_EXHAUSTED", FailureCategory.ARTIFACT_DEFECT),
    ("failed", "failed", "prepare", 0, "EXECUTION_FAILED", FailureCategory.ENV_OR_CONFLICT_BLOCKED),
    ("failed", "failed", "draft_spec", 0, "EXECUTION_FAILED", FailureCategory.TRANSIENT_TECH_ERROR),
])
def test_display_uses_only_active_native_facts(status, node_status, node, count, expected, category):
    run = {"status": status, "nodes": {node: {"status": node_status, "reworkCount": count, "error": "本轮检查报告"}},
           "workflowSnapshot": build_standard_plan_workflow("p", "std_spec")}
    state, failure, detail = native_progress(run)
    assert (state, failure) == (expected, category)
    if expected == "REWORK_EXHAUSTED":
        assert detail == "本轮检查报告"


@pytest.mark.parametrize("stage,defect", [
    ("std_spec", ""), ("std_spec", "requirements"), ("std_spec", "constitution"),
    ("std_spec", "question"), ("std_spec", "clarity"), ("std_spec", "subjective"), ("std_plan", ""), ("std_plan", "uncovered"),
    ("std_plan", "cycle"), ("std_plan", "commands"), ("std_plan", "paths"), ("std_plan", "chapter"),
])
def test_standard_validator_parity(project, tmp_path, stage, defect):
    service.enable(project, "vernal-core")
    save(project, "spec", SPEC, "example")
    mission = missions.save(project, "example", "保存记录", "feature", 0, actor="owner")
    _, schema, _ = adapter.planning_request(project, mission, stage)
    data = (dict(assessed(), constitution_markdown="交付须验收") if stage == "std_spec" else
            dict(plan_markdown=PLAN, work_packages=work_packages(), handoff_brief=BRIEF))
    if defect == "requirements": data["proposed_spec"] = SPEC.replace("REQ-001", "REQ-X")
    if defect == "constitution": data["constitution_markdown"] = " "
    if defect == "clarity": data["goal_clear"] = False
    if defect == "subjective": data["experience_items"] = [{"description": "验收", "core_acceptance": True}]
    if defect == "question": data["questions"] = [{}]
    if defect == "uncovered": data["work_packages"][0]["steps"][0]["requirement_ids"] = ["REQ-999"]
    if defect == "cycle": data["work_packages"][0]["depends_on_packages"] = [data["work_packages"][0]["package_key"]]
    if defect == "commands": data["work_packages"][0]["verify_cmds"] = ["echo '"]
    if defect == "paths": data["work_packages"][0]["allowed_paths"] = ["../other"]
    if defect == "chapter": data["plan_markdown"] = "缺少章节"
    (tmp_path / ".vernal-schema.json").write_text(json.dumps(schema))
    output = tmp_path / "vernal-result.json"
    scripts = Path(adapter.__file__).parent
    results = []
    for exe, ext in ((sys.executable, "py"), (shutil.which("node"), "cjs")):
        output.write_text(json.dumps(data))
        proc = subprocess.run([exe, scripts / f"validate_output.{ext}"], cwd=tmp_path, capture_output=True, timeout=10)
        results.append((proc.returncode, proc.stderr, output.read_bytes()))
    assert results[0] == results[1]
    assert (results[0][0] == 0) == (not defect)


@pytest.mark.parametrize("queued", [False, True], ids=["immediate-run", "accepted-task"])
def test_service_restart_collects_run_once_without_dispatch(project, monkeypatch, queued):
    service.enable(project, "vernal-core")
    mission = missions.save(project, "feature", "保存记录", "feature", 0, actor="owner")
    stage = "std_spec"
    value = dict(assessed(), constitution_markdown="验收须留存依据")
    quality = review_result(stage)
    quality["clarifications"] = [dict(question_key="Q-01", question="是否离线保存？", category="scope",
                                      default_option="", rationale="", options=["是", "否"], severity="BLOCKING")]
    wf = build_standard_plan_workflow("mission-test", stage)
    run = dict(id="run-R", projectId="mission-test", issueId="issue-I", status="running",
               nodes={"draft_spec": {"status": "running", "reworkCount": 1}}, workflowSnapshot=wf)
    assets, posts = {}, []
    monkeypatch.setattr(project.settings, "effective_loopforge_token", lambda: "request-session-token")
    def native(req):
        path = req.url.path
        if path == "/api/runners":
            data = [dict(id="runner", status="online", isDefault=True, runtimes=[{"provider":"codex"}])]
        elif path == "/api/sandboxes":
            data = [{"id": "local"}]
        elif path in ("/api/workflows", "/api/agents", "/api/rules", "/api/skills", "/api/mcp-servers"):
            items = assets.setdefault(path, {})
            if req.method == "GET": data = list(items.values())
            else:
                data = json.loads(req.content)
                items[data["id"]] = data
        elif path.endswith("/publish"): data = {"status": "published"}
        elif path == "/api/issues":
            assert req.headers["authorization"] == "Bearer " + project.settings.service_token()
            posts.append(path)
            assert json.loads(req.content)["workflowId"] == wf["id"]
            data = {"id": "issue-I"}
        elif path.endswith("/dispatch"):
            posts.append(path)
            data = {"issueId": "issue-I", **({"id": "queued-T"} if queued else {"runId": "run-R"})}
        elif path == "/api/tasks/queued-T":
            data = {"id": "queued-T", "issueId": "issue-I", "runId": "run-R"}
        elif path == "/api/runs/run-R":
            assert req.headers["authorization"] == "Bearer " + project.settings.service_token()
            data = run
        else: data = {}
        return httpx.Response(200, json=data)
    transport = httpx.MockTransport(native)
    init = LoopForgeClient.__init__
    def with_transport(self, *args, **kw):
        init(self, *args, **{**kw, "transport": transport})
    monkeypatch.setattr(LoopForgeClient, "__init__", with_transport)
    async def ready(_): return {"ready": True}
    monkeypatch.setattr(preflight, "inspect", ready)
    with TestClient(create_app(project, proxy_transport=transport)) as web:
        endpoint = "/vernal-api/missions/feature/continue"
        for _ in range(2):
            response = web.post(endpoint, json={"expected_revision": mission["revision"]})
            assert response.status_code == 200, response.text
            assert response.json()["run_id"] == "run-R"
        assert web.app.state.ctx.active_slot().plan_task is None
        assert web.post("/vernal-api/missions/recover").is_success
        assert web.get("/vernal-api/specifications").json()["missions"][0]["display_state"] == "REWORKING"
    assert not service.latest(project, "feature")
    run.update(status="succeeded", artifacts=native_result(stage, value, quality))
    reopened = Engine(project.ws, settings=project.settings)
    try:
        with TestClient(create_app(reopened, proxy_transport=transport)) as web:
            deadline = time.monotonic() + 6
            while not service.latest(reopened, "feature") and time.monotonic() < deadline:
                time.sleep(.02)
            view = web.get("/vernal-api/specifications").json()["missions"][0]
            assert (view["state"], view["display_state"]) == ("WAITING_ANSWERS", "WAITING_DECISION")
            assert web.post("/vernal-api/missions/recover").is_success
        receipt, = reopened.db._query("SELECT * FROM dispatch_records")
        assert (receipt["stage"], receipt["lf_run_id"], receipt["state"]) == (stage, "run-R", "FINISHED")
        assert receipt["expected_baseline_sha"]
        assert len(reopened.db._query("SELECT 1 FROM spec_revisions WHERE source_kind='spec'")) == 1
        assert len(reopened.db._query("SELECT 1 FROM events WHERE event_type='mission.completed'")) == 1
        assert posts == ["/api/issues", "/api/issues/issue-I/dispatch"]
    finally:
        reopened.db.close()

@pytest.mark.parametrize("fault,display,category", [
    ("exhausted", "REWORK_EXHAUSTED", FailureCategory.ARTIFACT_DEFECT),
    ("old_turn", "EXECUTION_FAILED", FailureCategory.ARTIFACT_DEFECT),
    ("unrelated_issue", "EXECUTION_FAILED", FailureCategory.ENV_OR_CONFLICT_BLOCKED),
    ("denied", "EXECUTION_FAILED", FailureCategory.ENV_OR_CONFLICT_BLOCKED),
])
def test_terminal_failures_do_not_publish_or_dispatch_again(project, monkeypatch, fault, display, category):
    service.enable(project, "vernal-core")
    mission = missions.save(project, "example", "保存记录", "feature", 0, actor="owner")
    runs = mock_bundles(project, monkeypatch, lambda _: dict(assessed(), constitution_markdown="验收留痕"))
    async def execute():
        started = await reconciler.start(project, "example", mission["revision"], actor="owner")
        run = runs[started["run_id"]]
        run["workflowSnapshot"] = build_standard_plan_workflow("p", "std_spec")
        if fault == "old_turn":
            run["nodes"] = {"quality_check": {"reworkCount": 1, "attempt": 2, "status": "succeeded"}}
        elif fault == "unrelated_issue":
            run["issueId"] = "another-issue"
        elif fault == "exhausted":
            run.update(status="failed", nodes={"validate_spec": {"status":"failed", "reworkCount":2, "error":"缺少 P0"}})
        else:
            run.update(status="waiting", nodes={"draft_spec": {"status": "waiting", "completionCategory":"CALLBACK_PERMISSION_DENIED"}})
        await reconciler.reconcile_once(None, SimpleNamespace(engine=project))
        with pytest.raises(service.SpecConflictError):
            await reconciler.start(project, "example", mission["revision"], actor="owner")
    asyncio.run(execute())
    view = missions.view(project, missions.get(project, "example"))
    assert (view["display_state"], view["failure_category"]) == (display, category)
    if fault == "exhausted":
        assert "缺少 P0" in view["last_error"]
    assert len(runs) == 1 and not service.latest(project, "example")


def test_concurrent_reconcilers_share_one_workspace_lease(project, monkeypatch):
    service.enable(project, "vernal-core")
    missions.save(project, "example", "保存记录", "feature", 0, actor="owner")
    mock_bundles(project, monkeypatch, lambda _: dict(assessed(), constitution_markdown="验收依据"))
    calls = []
    real = reconciler.reconcile_plan
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed(engine, receipt):
        calls.append(receipt["dispatch_id"])
        entered.set()
        await release.wait()
        await real(engine, receipt)
    monkeypatch.setattr(reconciler, "reconcile_plan", delayed)
    second = Engine(project.ws, settings=project.settings)
    async def run():
        await reconciler.start(project, "example", 1, actor="owner")
        first = asyncio.create_task(reconciler.reconcile_once(None, SimpleNamespace(engine=project)))
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(reconciler.reconcile_once(None, SimpleNamespace(engine=second)), 2)
        release.set()
        await first
        await reconciler.reconcile_once(None, SimpleNamespace(engine=second))
    try:
        asyncio.run(run())
        assert len(calls) == 1
        assert len(second.db._query("SELECT 1 FROM events WHERE event_type='mission.completed'")) == 1
    finally:
        second.db.close()


def test_delivery_reconciler_binds_queued_run_and_waits_for_terminal(project, monkeypatch):
    from autodev.loopforge import dispatch
    from autodev.server.routers import speckit as routes
    from autodev.speckit import compiler
    from autodev.process.runner import latest_gate_record
    from test_lite_bundle import bundle
    from test_missions import advance
    missions.save(project, "feature", "保存记录", "feature", 0, actor="owner")
    mock_bundles(project, monkeypatch, lambda _: bundle())
    advance(project, "feature")
    gate = latest_gate_record(project.db, "srs_approval", "project", "project")
    task_id, = missions.decide_and_freeze(project, gate["record_id"], "pass", actor="owner")["frozen_task_ids"]
    package = compiler.assert_package_current(project.db, project.db.get_task(task_id))
    receipt, _ = dispatch.reserve(project.db, task_id, 1, 1, "local_runner", "owner", "issue")
    dispatch.record(project.db, receipt, "QUEUED", lf_task_id="accepted")
    project.db._exec("UPDATE dispatch_records SET stage='lite_delivery', feature_id='feature', expected_baseline_sha=? "
                     "WHERE dispatch_id=?", (package.compiled_task_hash, receipt["dispatch_id"]))
    native = dict(id="R", status="running", projectId=project.settings.loopforge_project_id, issueId="issue",
                  nodes={"implement": {"status": "running", "reworkCount": 1}})
    def remote(req):
        assert req.method == "GET"
        if req.url.path == "/api/tasks/accepted":
            return httpx.Response(200, json={"id": "accepted", "issueId": "issue", "runId": "R"})
        assert req.url.path == "/api/runs/R"
        return httpx.Response(200, json=native)
    client = LoopForgeClient("http://test", transport=httpx.MockTransport(remote))
    monkeypatch.setattr(reconciler, "receipt_client", lambda *_: client)
    calls = []
    monkeypatch.setattr(routes, "start_lite_delivery", lambda ctx, slot, ids: calls.append(ids))
    slot = SimpleNamespace(engine=project)
    ctx = SimpleNamespace(is_scheduler_running=lambda _: bool(calls))
    asyncio.run(reconciler.reconcile_once(ctx, slot))
    assert missions.view(project, missions.get(project, "feature"))["display_state"] == "REWORKING"
    assert not calls
    native["status"] = "succeeded"
    asyncio.run(reconciler.reconcile_once(ctx, slot))
    asyncio.run(reconciler.reconcile_once(ctx, slot))
    assert calls == [[task_id]]
    row, = project.db._query("SELECT lf_run_id FROM dispatch_records WHERE stage='lite_delivery'")
    assert row["lf_run_id"] == "R"

def test_reconcile_after_lost_http_request_at_queue_binding(project, monkeypatch):
    from autodev.loopforge import dispatch
    service.enable(project, "vernal-core")
    missions.save(project, "example", "保存记录", "feature", 0, actor="owner")
    count = []
    async def submit(receipt, *args):
        count.append(receipt["dispatch_id"])
        dispatch.record(project.db, receipt, "QUEUED", lf_task_id="native-task", issue_id="issue")
        raise asyncio.CancelledError
    monkeypatch.setattr(project.harness, "dispatch_bundle", submit)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(reconciler.start(project, "example", 1, actor="owner"))
    def native(req):
        assert req.method == "GET"
        if req.url.path == "/api/tasks/native-task":
            return httpx.Response(200, json={"id":"native-task","issueId":"issue","runId":"R"})
        assert req.url.path == "/api/runs/R"
        value = dict(assessed(), constitution_markdown="验收依据")
        return httpx.Response(200, json=dict(id="R", issueId="issue", projectId=project.settings.loopforge_project_id,
                                             status="succeeded", artifacts=native_result("std_spec", value)))
    client = LoopForgeClient("http://test", transport=httpx.MockTransport(native))
    monkeypatch.setattr(reconciler, "receipt_client", lambda *_: client)
    second = Engine(project.ws, settings=project.settings)
    try:
        asyncio.run(reconciler.reconcile_once(None, SimpleNamespace(engine=second)))
        assert missions.view(second, missions.get(second, "example"))["state"] == "WAITING_APPROVAL"
        row, = second.db._query("SELECT * FROM dispatch_records")
        assert count == [row["dispatch_id"]] and (row["lf_run_id"], row["state"]) == ("R", "FINISHED")
    finally:
        second.db.close()
