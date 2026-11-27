"""Batch B contracts at the standalone script, SQLite and native dispatch boundaries."""
import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from autodev.harnesses.loopforge_harness import LoopForgeHarness
from autodev.loopforge.bootstrap import build_lite_delivery_workflow, build_lite_plan_workflow
from autodev.loopforge.client import LoopForgeClient
from autodev.models import ClarificationItem, LitePlanBundle, strict_json_schema
from autodev.process.runner import latest_gate_record
from autodev.scheduler import Scheduler
from autodev.server.app import create_app
from autodev.speckit import adapter, compiler, missions, service
from test_missions import project, advance, mock_bundles
from test_speckit import BRIEF, PLAN, SPEC, work_packages

FEATURE = "001-save"


def bundle(mode="READY_FOR_APPROVAL"):
    value = dict(mode=mode, risk="low", rationale="单文件本地修改", clarifications=[], prerequisites=[],
                 experience_items=[], spec_markdown="", plan_markdown="", work_packages=[], handoff_brief=BRIEF)
    if mode == "READY_FOR_APPROVAL":
        value.update(spec_markdown=SPEC, plan_markdown=PLAN, work_packages=work_packages())
        value["work_packages"][0]["verify_cmds"] = ["test -s src/save.py"]
        value["work_packages"][0]["steps"][0]["verify_cmds"] = []
    elif mode == "NEEDS_CLARIFICATION":
        value["clarifications"] = [ClarificationItem(question_key="Q-01", question="保存多久？").model_dump()]
    else:
        value.update(risk="medium", rationale="需要跨模块改造")
    return value


@pytest.mark.parametrize("case", ["ready", "clarify", "escalate", "risk", "question_count", "question_kind",
    "unexpected_questions", "unexpected_packages", "no_package", "too_many", "duplicate_req", "bad_header",
    "no_scenarios", "no_checks", "empty_chapter", "p0", "protected", "bad_command", "cycle", "symlink", "two", "low_escalate", "low_no_boundary"])
def test_native_lite_contract(case, tmp_path):
    mode = "NEEDS_CLARIFICATION" if case in ("clarify","question_count","question_kind") else (
        "ESCALATE_STANDARD" if case in ("escalate","unexpected_packages","low_escalate","low_no_boundary") else "READY_FOR_APPROVAL")
    value = bundle(mode)
    schema = {**strict_json_schema(LitePlanBundle), "x-vernal-step":"lite_bundle",
              "x-vernal-protected-paths":["contracts/*"]}
    if case.startswith("low_"):
        value.update(risk="low", rationale="虽风险较低，但需要三个可独立验收的工作包" if case == "low_escalate" else "风险低")
    if case == "risk": value["risk"] = "high"
    if case == "question_count": value["clarifications"] = []
    if case == "question_kind": value["clarifications"][0]["severity"] = "DEFAULTED"
    if case == "unexpected_questions": value["clarifications"] = bundle("NEEDS_CLARIFICATION")["clarifications"]
    if case == "unexpected_packages": value["work_packages"] = work_packages()
    if case == "no_package": value["work_packages"] = []
    if case == "too_many": value["work_packages"] *= 3
    if case == "duplicate_req": value["spec_markdown"] = SPEC.replace("## 边界情况", SPEC.split("## 需求\n")[1])
    if case == "bad_header": value["spec_markdown"] = SPEC.replace("REQ-001", "REQ-X")
    if case == "no_scenarios": value["spec_markdown"] = SPEC.replace("- 用户提交", "用户提交")
    if case == "no_checks": value["spec_markdown"] = SPEC.replace("- 重新打开", "重新打开")
    if case == "empty_chapter": value["plan_markdown"] = PLAN.replace("保存接口接收文本。","")
    if case == "p0": value["spec_markdown"] = SPEC.replace("## 边界情况","### NFR-002 [P0] 兼容\n要求：兼容\n验收：\n- 可用\n## 边界情况")
    if case == "protected": value["work_packages"][0]["allowed_paths"] = ["contracts/api"]
    if case == "bad_command": value["work_packages"][0]["verify_cmds"] = ["echo '"]
    if case == "cycle": value["work_packages"][0]["depends_on_packages"] = ["save"]
    if case == "symlink": (tmp_path / "src").symlink_to(tmp_path.parent, target_is_directory=True)
    if case == "two":
        second = work_packages()[0]
        second.update(package_key="tests", allowed_paths=["tests"], depends_on_packages=["save"])
        second["steps"][0]["task_key"] = "T002"
        value["work_packages"].append(second)
    (tmp_path / ".vernal-schema.json").write_text(json.dumps(schema))
    output = tmp_path / "vernal-result.json"
    results = []
    for exe, suffix in ((sys.executable, "py"), (shutil.which("node"), "cjs")):
        assert exe
        output.write_text(json.dumps(value))
        script = Path(__file__).resolve().parents[1] / "autodev/speckit" / ("validate_output." + suffix)
        p = subprocess.run([exe, script], cwd=tmp_path, capture_output=True, timeout=10)
        results.append((p.returncode, p.stderr.decode(), output.read_bytes()))
    assert results[0] == results[1]
    assert (results[0][0] == 0) == (case in ("ready","clarify","escalate","two","low_escalate")), results[0][:2]


def forbid(*args, **kwargs):
    pytest.fail("轻量规划或单包交付调用了标准串行路径")


def no_serial(monkeypatch):
    for target, name in ((adapter,"run_step"), (Scheduler,"run"), (Scheduler,"_dispatch_ready")):
        monkeypatch.setattr(target, name, forbid)


def mock_plan(project, monkeypatch, bundles):
    values = iter(bundles)
    def output(stage):
        assert stage == "lite_bundle"
        return next(values)
    mock_bundles(project, monkeypatch, output)
    missions.save(project, FEATURE, "在本地保存回答", "feature", 0, actor="owner")


def test_clarify_then_approve_rollback(project, monkeypatch):
    no_serial(monkeypatch)
    mock_plan(project, monkeypatch, [bundle("NEEDS_CLARIFICATION"), bundle()])
    advance(project)
    mission = missions.view(project, missions.get(project, FEATURE))
    assert mission["state"] == "WAITING_ANSWERS" and not service.latest(project, FEATURE)
    service.answer_batch(project, FEATURE, [dict(clarify_id=mission["questions"][0]["clarify_id"], answer="30 天")],
                         expected_rev_id=mission["spec_rev_id"], actor="owner")
    advance(project)
    view = missions.view(project, missions.get(project, FEATURE))
    assert view["state"] == "WAITING_APPROVAL" and not project.db.list_tasks()
    assert "30 天" in service.latest(project, FEATURE)["content_text"]
    assert len(project.db.list_runs("_speckit_001-save_lite_bundle")) == 2
    gate = latest_gate_record(project.db,"srs_approval","project","project")
    with monkeypatch.context() as m:
        def conflict(*args, **kwargs):
            raise ValueError("冻结冲突")
        m.setattr(compiler,"publish_tasks",conflict)
        with pytest.raises(ValueError, match="冻结冲突"):
            missions.decide_and_freeze(project,gate["record_id"],"pass",actor="owner")
    assert latest_gate_record(project.db,"srs_approval","project","project")["status"] == "PENDING"
    assert service.latest(project,FEATURE)["status"] == "DRAFT"
    frozen = missions.decide_and_freeze(project,gate["record_id"],"pass",actor="owner")["frozen_task_ids"]
    assert compiler.assert_package_current(project.db,project.db.get_task(frozen[0]))
    assert all(service.latest(project,FEATURE,k)["status"] == "APPROVED" for k in ("spec","plan","tasks"))
    assert "frozen_task_ids" not in missions.decide_and_freeze(project,gate["record_id"],"pass",actor="owner")


def wait_for_lite_delivery(web):
    task = web.app.state.ctx.active_slot().scheduler_task
    web.portal.call(asyncio.wait_for, task, 10)


def test_one_approval_starts_one_delivery_and_records_candidate(project, monkeypatch, tmp_path):
    """Exercise native script bodies, HTTP dispatch, gate API and the real candidate verifier."""
    no_serial(monkeypatch)
    from autodev import public_pool
    checked = []
    sandbox_check = public_pool.check_workflow_sandbox
    async def check(settings, client, wid, pool):
        checked.append(wid)
        return await sandbox_check(settings, client, wid, pool)
    monkeypatch.setattr(public_pool, "check_workflow_sandbox", check)
    workflows, issues, runs = {}, [], {}
    registered = {}
    from autodev.ci import service as ci
    from autodev.ci.providers.base import CIHandle, CIStatus
    class CI:
        name = "gitea"
        async def trigger(self, pid, ref, sha, context):
            return CIHandle(self.name, pid, ref, sha, external_id="ci-job")
        async def poll(self, handle):
            return CIStatus("SUCCESS")
    monkeypatch.setattr(ci, "build_ci_provider", lambda *args, **kwargs: CI())

    def native(request):
        path = request.url.path
        def response(value): return httpx.Response(200, json=value)
        if path == "/api/runners":
            return response([dict(id="runner",status="online",isDefault=True,runtimes=[{"provider":"codex"}])])
        if path == "/api/sandboxes": return response([{"id":"local"}])
        if path in ("/api/workflows","/api/agents","/api/rules","/api/skills","/api/mcp-servers"):
            catalog = workflows if path == "/api/workflows" else registered.setdefault(path,{})
            if request.method == "GET": return response(list(catalog.values()))
            obj = json.loads(request.content)
            catalog[obj["id"]] = obj
            return response(obj)
        if path.endswith("/publish"): return response({"status":"published"})
        if path == "/api/issues" and request.method == "POST":
            issue = json.loads(request.content)
            assert issue["workflowId"] == checked[-1]
            issues.append(issue)
            return response({"id":str(len(issues))})
        if path.endswith("/dispatch"):
            index = int(path.split("/")[-2])
            assert index == len(issues) and index in (1,2)
            wf = workflows[issues[index-1]["workflowId"]]
            nodes = wf["nodes"]
            folder = tmp_path / f"native-{index}"
            folder.mkdir()
            def script(node):
                p = subprocess.run(["sh","-c",node["script"]],cwd=folder,capture_output=True,text=True,timeout=20)
                assert p.returncode == 0, p.stderr
            script(nodes[0])
            if index == 1:
                assert [n["id"] for n in nodes] == ["prepare","draft_lite_bundle","validate_lite_bundle"]
                value = bundle()
                (folder/"vernal-result.json").write_text(json.dumps(value))
                script(nodes[2])
                artifacts = [{"name":"vernal-result.json","nodeId":"validate_lite_bundle","content":(folder/"vernal-result.json").read_text()}]
            else:
                assert wf["id"].endswith("-vernal-lite-deliver-files-sop")
                assert (folder/".vernal-baseline.json").exists()
                (folder/"src").mkdir(exist_ok=True)
                (folder/"src/save.py").write_text("def save(value):\n    return value\n")
                script(nodes[2])
                artifacts = [{"name":name, "content":(folder/name).read_text()} for name in
                             ("vernal-files.json","vernal-review-context.md")]
                artifacts += [{"name":"handoff-brief.json","nodeId":n,"content":json.dumps(BRIEF)} for n in ("implement","review")]
                artifacts += [{"name":"review-report.md","nodeId":"review","content":"已核对保存行为和验证结果。"}]
            rid = f"native-{index}"
            runs[rid] = {"id":rid,"projectId":project.settings.loopforge_project_id,"issueId":str(index),"status":"succeeded","artifacts":artifacts}
            return response({"id":f"task-{index}", "issueId":str(index)})
        if path.startswith("/api/tasks/task-"):
            i = path.split("-")[-1]
            if i == "1":
                assert not project.harness._assets_lock.locked()
                assert not project.harness._dispatch_lock.locked()
            return response({"id":"task-"+i, "issueId":i, "runId":"native-"+i})
        if path in ("/api/runs/native-1", "/api/runs/native-2"): return response(runs[path.split("/")[-1]])
        return response({})

    transport = httpx.MockTransport(native)
    original = LoopForgeClient.__init__
    def bind(self, *args, **kwargs):
        kwargs["transport"] = transport
        original(self, *args, **kwargs)
    monkeypatch.setattr(LoopForgeClient,"__init__",bind)
    project._harness = LoopForgeHarness(project.settings,project.db,project.ws,poll_interval=.001)
    missions.save(project,FEATURE,"保存本地回答","feature",0,actor="owner")
    advance(project)
    assert len(issues) == 1
    assert missions.view(project,missions.get(project,FEATURE))["state"] == "WAITING_APPROVAL"
    gate = latest_gate_record(project.db,"srs_approval","project","project")
    with TestClient(create_app(project, proxy_transport=transport)) as web:
        response = web.post(f"/vernal-api/gates/{gate['record_id']}/decide",json={
            "decision":"pass","actor":"owner","expected_sha":gate["target_sha"], "expected_round":gate["round"]})
        assert response.status_code == 200, response.text
        task_id, = response.json()["frozen_task_ids"]
        wait_for_lite_delivery(web)
        task = project.db.get_task(task_id)
        assert task.loopforge_run_id == "native-2", (task.failure_reason, project.db._query("SELECT message FROM events ORDER BY event_id DESC LIMIT 5"))
        assert task.artifact_digest and task.status in ("APPROVED","AWAITING_HUMAN_REVIEW"), (task.status, task.failure_reason, task.last_feedback)
        assert set(json.loads(task.handoff_brief_json)) == {"implement","review"}
        assert project.db._query("SELECT 1 FROM quality_results WHERE task_id=?", (task_id,))
        job, = ci.list_ci_runs(project.db, task.milestone_id)
        assert project.status == "APPROVED"
        polled = web.post(f"/vernal-api/ci/runs/{job['ci_run_id']}/poll")
        assert polled.status_code == 200, polled.text
        assert project.db.get_milestone(task.milestone_id).status == "AWAITING_MERGE"
        review, = project.db._query("SELECT review_id,head_ref FROM code_reviews WHERE scope_type='milestone' AND status='OPEN'")
        merge_url = f"/vernal-api/milestones/{task.milestone_id}/merge"
        assert web.post(merge_url, json={"actor":"owner"}).status_code != 200
        signed = web.post(f"/vernal-api/reviews/{review['review_id']}/decision", json={
            "actor":"owner", "decision":"approve", "expected_head":review["head_ref"]})
        assert signed.status_code == 200, signed.text
        merged = web.post(merge_url, json={"actor":"owner"})
        assert merged.status_code == 200, merged.text
        assert project.status == "COMPLETED"
        assert len(issues) == len(runs) == 2 and all("执行下面这行" not in i["description"] for i in issues)
        assert not project.db._query("SELECT 1 FROM gate_records WHERE gate_id='plan_approval'")
        assert len(project.db._query("SELECT 1 FROM dispatch_records WHERE state='FINISHED'")) == 2


@pytest.mark.parametrize("startup", [False, True], ids=["manual-start","restart-recovery"])
def test_scheduler_entry_reuses_queued_lite_receipt(project, monkeypatch, startup):
    from autodev.loopforge import dispatch
    from threading import Event
    no_serial(monkeypatch)
    mock_plan(project, monkeypatch, [bundle()])
    advance(project)
    gate = latest_gate_record(project.db, "srs_approval", "project", "project")
    task_id, = missions.decide_and_freeze(project,gate["record_id"],"pass",actor="owner")["frozen_task_ids"]
    project.db.update_task(task_id,status="IN_PROGRESS",exec_seq=1)
    task = project.db.get_task(task_id)
    package = compiler.assert_package_current(project.db,task)
    receipt, _ = dispatch.reserve(project.db,task_id,1,1,"local_runner","deployment-service","issue")
    dispatch.bind_baseline(project.db,receipt,dict(spec_sha=package.spec_sha,contract_sha=package.contract_sha,
                          compiled_task_hash=package.compiled_task_hash,asset_versions=package.asset_versions))
    dispatch.record(project.db,receipt,"QUEUED",lf_task_id="accepted")
    if startup:
        project.db.meta_set("status","RUNNING")
    bound = Event()
    async def native_task(self, identifier):
        assert identifier == "accepted"
        return {"id":identifier,"issueId":"issue","runId":"original-run"}
    async def execute_remote(sched, task, *args, **kwargs):
        assert task.loopforge_run_id == "original-run"
        assert sched.stop_event is project.planning_stop
        assert not sched.stop_event.is_set()
        if not bound.is_set():
            bound.set()
            await asyncio.wait_for(sched.stop_event.wait(), timeout=5)
            project.db.update_task(task_id, status="READY")
        else:
            project.db.update_task(task_id, status="AWAITING_HUMAN_REVIEW")
    monkeypatch.setattr(LoopForgeClient,"get_task",native_task)
    monkeypatch.setattr(LoopForgeClient,"dispatch_issue",forbid)
    monkeypatch.setattr(Scheduler,"_execute_remote",execute_remote)
    with TestClient(create_app(project,proxy_transport=httpx.MockTransport(lambda req:httpx.Response(200,json={})))) as web:
        if not startup:
            assert web.post("/vernal-api/scheduler/start").status_code == 200
        assert bound.wait(3)
        assert web.post("/vernal-api/scheduler/start").status_code == 200
        assert len(project.db._query("SELECT 1 FROM events WHERE event_type='background.started' AND message='_lite_delivery'")) == 1
        assert web.post("/vernal-api/scheduler/stop").status_code == 200
        wait_for_lite_delivery(web)
        assert project.planning_stop.is_set()
        assert project.status == "STALLED"

        ctx = web.app.state.ctx
        enqueue = ctx.start_background
        stop_flags_at_enqueue = []
        def check_restart(slot, operation, coroutine):
            # Inspect the flag before the generic background launcher clears it.
            stop_flags_at_enqueue.append(slot.scheduler.stop_event.is_set())
            return enqueue(slot, operation, coroutine)
        monkeypatch.setattr(ctx, "start_background", check_restart)
        assert web.post("/vernal-api/scheduler/start").status_code == 200
        wait_for_lite_delivery(web)
        assert stop_flags_at_enqueue == [False]
        assert project.db.get_task(task_id).status == "AWAITING_HUMAN_REVIEW"
        assert project.status == "APPROVED"
    assert len(project.db._query("SELECT 1 FROM dispatch_records WHERE task_id=?", (task_id,))) == 1


def test_two_packages_freeze_in_one_decision_without_direct_single_dispatch(project, monkeypatch):
    from autodev.server.routers.speckit import start_lite_delivery
    no_serial(monkeypatch)
    value = bundle()
    second = work_packages()[0]
    second.update(package_key="checks", allowed_paths=["tests"], depends_on_packages=["save"])
    second["steps"][0]["task_key"] = "T002"
    value["work_packages"].append(second)
    mock_plan(project, monkeypatch, [value])
    advance(project)
    gate = latest_gate_record(project.db, "srs_approval", "project", "project")
    ids = missions.decide_and_freeze(project,gate["record_id"],"pass",actor="owner")["frozen_task_ids"]
    assert len(ids) == 2
    assert project.db.get_task(ids[1]).depends_on == [ids[0]]
    assert not start_lite_delivery(None, None, ids)
    assert project.status == "APPROVED"
    assert not project.db._query("SELECT 1 FROM gate_records WHERE gate_id IN ('architecture_approval','plan_approval') AND status='PENDING'")


def test_failed_bundle_publication_rolls_back_sources_and_files(project, monkeypatch):
    no_serial(monkeypatch)
    mock_plan(project, monkeypatch, [bundle(), bundle()])
    publish = service.publish_revision
    def fail_on_tasks(*args, **kwargs):
        if args[2] == "tasks":
            raise OSError("任务发布失败")
        return publish(*args, **kwargs)
    with monkeypatch.context() as m:
        m.setattr(service, "publish_revision", fail_on_tasks)
        with pytest.raises(ValueError, match="任务发布失败"): advance(project)
    assert not project.db._query("SELECT 1 FROM spec_revisions")
    assert not (project.ws.root / "docs/SRS.md").exists()
    assert not (project.ws.root / "specs" / FEATURE / "spec.md").exists()
    advance(project)
    assert missions.view(project,missions.get(project,FEATURE))["state"] == "WAITING_APPROVAL"


def test_escalation_hands_off_to_standard(project, monkeypatch):
    mock_plan(project, monkeypatch, [bundle("ESCALATE_STANDARD")])
    advance(project)
    mission = missions.get(project, FEATURE)
    assert mission["route"]["clarity_level"] == "READY_STANDARD"
    assert missions.next_stage(project, mission) == "std_spec"
    assert not project.db.list_tasks() and not service.latest(project, FEATURE)


@pytest.mark.parametrize("mode,local", [("files",False), ("git",False), ("git",True)])
def test_lite_delivery_rejects_before_packaging(tmp_path, mode, local):
    wf = build_lite_delivery_workflow("p", mode, "local", restore_cmd=":", verify_cmds=["exit 7"], local_git=local)
    assert [n["id"] for n in wf["nodes"]] == ["prepare","implement","package_and_verify","review"]
    assert [(e["source"],e["target"]) for e in wf["edges"]] == [(a["id"],b["id"]) for a,b in zip(wf["nodes"],wf["nodes"][1:])]
    for n in wf["nodes"][-2:]:
        assert (n["rejectTo"],n["onFailure"],n["maxReworks"]) == ("implement","reject",2)
    (tmp_path/"vernal-source").mkdir()
    assert subprocess.run(["sh","-c",wf["nodes"][2]["script"]],cwd=tmp_path).returncode == 7
    assert not any(tmp_path.glob("*.json"))
    plan = build_lite_plan_workflow("p")
    assert [n["id"] for n in plan["nodes"]] == ["prepare","draft_lite_bundle","validate_lite_bundle"]
    assert plan["nodes"][2]["rejectTo"] == "draft_lite_bundle"
