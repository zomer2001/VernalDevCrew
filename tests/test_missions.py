"""P2 intake, clarification and frozen publication across real persistence boundaries."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from autodev.engine import Engine, init_workspace
from autodev.config import Settings
from autodev.models import ClarificationItem
from autodev.loopforge import dispatch, reconciler
from autodev.process.runner import GateConflictError, load_template, is_gate_passed
from autodev.server.app import create_app
from autodev.speckit import compiler, missions, preflight, service, triage, execution
from test_speckit import SPEC, PLAN, BRIEF, approve, work_packages


def assessed(**changes):
    return dict(goal_clear=True, scope_clear=True, acceptance_clear=True, local_impact=True,
                change_type="feature", risk="low", rationale="目标、边界和持久化验收已经明确。",
                questions=[], prerequisites=[], experience_items=[], proposed_spec=SPEC, handoff_brief=BRIEF, **changes)


@pytest.fixture
def project(tmp_path):
    engine=Engine(init_workspace(tmp_path/"project",collaboration_mode="files"),settings=Settings(
        collaboration_mode="files",runtime_backend="loopforge",loopforge_project_id="mission-test",loopforge_token="test-token",loopforge_url="http://lf.test"))
    yield engine
    engine.db.close()


def review_result(stage):
    return dict(step="checklist" if stage == "std_spec" else "analyze", content="# 独立审查\n验收依据完整。",
                findings=[], clarifications=[], work_packages=[], handoff_brief=BRIEF)


def native_result(stage, data, review=None):
    validator = {"std_spec": "validate_spec", "std_plan": "validate_and_compile",
                 "lite_bundle": "validate_lite_bundle"}[stage]
    artifacts = [dict(nodeId=validator, name="vernal-result.json", content=json.dumps(data))]
    if stage != "lite_bundle":
        node = "quality_check" if stage == "std_spec" else "consistency_review"
        artifacts.extend([dict(nodeId=node, name="vernal-quality.json", content=json.dumps(review or review_result(stage))),
                          dict(nodeId=node, name="review-report.md", content="# PASS\n已核对需求与验收。")])
    return artifacts


def mock_bundles(engine, monkeypatch, output):
    from autodev.loopforge.client import LoopForgeClient
    runs = {}
    async def submit(receipt, prompt, schema, role):
        rid = "run-" + str(receipt["dispatch_id"])
        issue = "issue-" + rid
        runs[rid] = dict(id=rid, issueId=issue, projectId=engine.settings.loopforge_project_id, status="succeeded",
                         artifacts=native_result(receipt["stage"], output(receipt["stage"])))
        dispatch.record(engine.db, receipt, "RUNNING", issue_id=issue, lf_run_id=rid)
        return rid
    client = LoopForgeClient("http://lf.test", transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json=runs[req.url.path.split("/")[-1]])))
    monkeypatch.setattr(engine.harness, "dispatch_bundle", submit)
    monkeypatch.setattr(reconciler, "receipt_client", lambda *_: client)
    return runs


def responses(engine, monkeypatch, assessments, *, lite=False):
    if not lite and not service.enabled(engine):
        service.enable(engine, "vernal-core")
    calls = []
    values = iter(assessments)
    def output(stage):
        calls.append(stage)
        if stage == "lite_bundle":
            from test_lite_bundle import bundle
            return bundle()
        if stage == "std_spec":
            value = dict(next(values), constitution_markdown="# 项目宪章\n交付必须有可复跑依据。")
            value["proposed_spec"] = value["proposed_spec"] or SPEC
            return value
        return dict(plan_markdown=PLAN, work_packages=work_packages(), handoff_brief=BRIEF)
    mock_bundles(engine, monkeypatch, output)
    return calls


def advance(engine, feature="001-save"):
    mission = missions.get(engine, feature)
    async def run():
        await reconciler.start(engine, feature, mission["revision"], actor="owner")
        await reconciler.reconcile_once(None, SimpleNamespace(engine=engine))
    asyncio.run(run())
    if error := missions.get(engine, feature).get("last_error"):
        raise ValueError(error)


def test_planning_stream_is_scoped_and_cursor_resets_when_run_changes(project, monkeypatch):
    from autodev.server.routers import speckit as routes
    missions.save(project, "001-save", "保存回答", "feature", 0, actor="owner")
    execution.record(project.db, "001-save", {"run_id": "run-2", "step": "tasks", "role": "director"})
    execution.record(project.db, "other", {"run_id": "run-3", "step": "plan"})
    seen = []

    async def project_run(rid, pid):
        assert rid == "run-2" and pid == project.settings.loopforge_project_id
        return {"status": "running", "nodes": {"draft": {"status": "running", "model": "sol"}}}

    async def logs(rid, **kwargs):
        seen.append(kwargs["after"])
        return {"logs": [{"id": "event-1", "sequence": 3, "body": "检查任务", "attributes": {"category": "tool"},
                          "context": {"nodeId": "draft", "attempt": 2}}], "hasMore": False}

    lf = SimpleNamespace(project_run=project_run, get_run_logs=logs)
    monkeypatch.setattr(routes, "_engine", lambda request: project)
    monkeypatch.setattr(routes, "get_ctx", lambda request: SimpleNamespace(active_slot=lambda _: None, lf_client=lambda _: lf))
    result = asyncio.run(routes.mission_stream("001-save", None, after=50, cursor_run_id="run-1"))
    assert seen == [0] and result["after"] == 3
    assert len(result["runs"]) == 1 and result["nodes"][0]["items"][0]["body"] == "检查任务"
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        asyncio.run(routes.mission_stream("001-save", None, run_id="run-3", after=0))
    assert exc.value.status_code == 404


def test_completed_plan_reconciles_only_matching_native_receipt(project):
    pid = project.settings.loopforge_project_id
    receipts = {}
    for case in ("success", "waiting", "wrong-issue", "failed"):
        receipt, _ = dispatch.reserve(project.db, f"plan:{pid}:{case}", 1, 1, "local_runner",
                                      "test", "issue-" + case, "run-" + case)
        dispatch.record(project.db, receipt, "NEEDS_CONFIRMATION")
        receipts[case] = receipt["dispatch_id"]

    async def project_run(rid, project_id):
        assert project_id == pid
        case = rid.removeprefix("run-")
        return {"id": rid, "status": {"waiting": "waiting", "failed": "failed"}.get(case, "succeeded"),
                "executionContext": {"issueId": "other" if case == "wrong-issue" else "issue-" + case}}

    for _ in range(2):
        asyncio.run(dispatch.reconcile_completed_plans(project.db, pid, SimpleNamespace(project_run=project_run), "owner"))
    for case, dispatch_id in receipts.items():
        row = project.db._query("SELECT state FROM dispatch_records WHERE dispatch_id=?", (dispatch_id,))[0]
        assert row["state"] == ("FINISHED" if case == "success" else "NEEDS_CONFIRMATION")
    assert len(project.db._query("SELECT 1 FROM events WHERE event_type='dispatch.reconciled'")) == 1


@pytest.mark.parametrize("mode,origin",[("scratch","greenfield"),("local_folder","existing_local"),("git_repo","existing_git")])
def test_source_and_clarity_are_independent_and_zero_questions_are_valid(project,monkeypatch,mode,origin):
    project.db.meta_set("source_mode",mode)
    calls=responses(project,monkeypatch,[assessed()], lite=True)
    missions.save(project,"001-save","可靠保存回答，重新打开后仍可读取。","feature",0,actor="owner")
    advance(project)
    view=missions.view(project,missions.get(project,"001-save"))
    assert view["route"]["source_origin"]==origin
    assert view["route"]["clarity_level"]=="READY_LITE" and view["questions"]==[]
    assert view["state"]=="WAITING_APPROVAL"
    assert view["actual_preset"]=="vernal-lite" and load_template(project.ws).id=="quick-prototype"
    assert calls==["lite_bundle"]
    assert not project.db.list_tasks() and not is_gate_passed(project.db,"srs_approval","project","project")


def test_answers_restart_reassessment_approval_and_frozen_publication(project,monkeypatch):
    q=ClarificationItem(question_key="Q-01",category="scope",severity="BLOCKING",question="保存多久？",options=["主动删除前","30天"])
    initial=assessed();initial.update(scope_clear=False,questions=[q.model_dump()])
    calls=responses(project,monkeypatch,[initial,{**assessed(),"proposed_spec":""}])
    missions.save(project,"001-save","做回答存储功能","feature",0,actor="owner")
    advance(project)
    assert missions.view(project,missions.get(project,"001-save"))["state"]=="WAITING_ANSWERS"
    with pytest.raises(GateConflictError): approve(project)
    question=service.list_clarifications(project,"001-save")[0]
    old=service.latest(project,"001-save")
    service.answer_clarification(project,question["clarify_id"],"主动删除前",expected_rev_id=old["rev_id"],actor="owner",supplement="删除前需明确确认")
    assert "删除前需明确确认" in service.latest(project,"001-save")["content_text"]
    with pytest.raises(GateConflictError): approve(project)
    # Persistence is used, not a cursor in the browser or a cached Python object.
    reopened=Engine(project.ws,settings=project.settings)
    try:
        assert missions.get(reopened,"001-save")["brief"]=="做回答存储功能"
        assert service.list_clarifications(reopened,"001-save")[0]["status"]=="ANSWERED"
    finally: reopened.db.close()
    advance(project)
    assert calls==["std_spec","std_spec"]
    approve(project)
    advance(project)
    view=missions.view(project,missions.get(project,"001-save"))
    assert view["state"]=="READY_TO_PUBLISH"
    hashes=[p["compiled_task_hash"] for p in view["packages"]]
    ids=compiler.publish_tasks(project,"001-save",expected_hashes=hashes,actor="owner")
    assert missions.view(project,missions.get(project,"001-save"))["state"]=="PUBLISHED"
    frozen=compiler.assert_package_current(project.db,project.db.get_task(ids[0]))
    assert frozen.spec_rev_id==service.latest(project,"001-save")["rev_id"]
    # Repeated continuation cannot regenerate or replace an approved package.
    count=len(calls);advance(project);assert len(calls)==count
    assert compiler.publish_tasks(project,"001-save",expected_hashes=hashes,actor="owner")==ids


@pytest.mark.parametrize("mutation",[
    {"scope_clear":False},
    {"questions":[dict(question_key="Q-01",category="acceptance",severity="DEFAULTED",question="数据可丢失吗？",options=["可以","不可以"],default_option="可以",rationale="省事")]},
    {"experience_items":[dict(description="检查所有回答不丢失",core_acceptance=True)]},
    {"questions":[dict(question_key=f"Q-0{i}",category="scope",severity="BLOCKING",question="问题",options=[]) for i in range(1,5)]},
])
def test_unsafe_assessment_cannot_weaken_commitments(mutation):
    value=assessed();value.update(mutation)
    with pytest.raises(ValueError):triage.Assessment.model_validate(value)


def test_prerequisites_remain_blocking_until_evidence_and_new_assessment(project,monkeypatch):
    item=dict(key="P01",description="测试硬件尚未连接",evidence_needed="记录设备型号与连通性测试结果")
    value=assessed();value.update(prerequisites=[item])
    responses(project,monkeypatch,[value,{**assessed(),"proposed_spec":""}])
    missions.save(project,"001-save","验证设备回答持久化","feature",0,actor="owner");advance(project)
    assert missions.view(project,missions.get(project,"001-save"))["state"]=="WAITING_PREREQUISITES"
    assert "测试硬件" in service.latest(project,"001-save")["content_text"]
    with pytest.raises(GateConflictError):approve(project)
    missions.prerequisite_evidence(project,"001-save","P01","设备 A 的连通性日志在报告中",1,actor="owner")
    with pytest.raises(GateConflictError):approve(project)
    advance(project);approve(project)
    assert is_gate_passed(project.db,"srs_approval","project","project")


def test_preflight_never_provisions_or_exposes_credentials(project,monkeypatch):
    from autodev import public_pool
    from autodev.loopforge.client import LoopForgeClient
    async def runners(self):return []
    async def user(self):return {"user":{"id":1}}
    monkeypatch.setattr(LoopForgeClient,"list_runners",runners);monkeypatch.setattr(LoopForgeClient,"auth_current",user)
    monkeypatch.setattr(public_pool,"status",lambda:dict(image_ready=False,key_configured=False,identity_configured=False,runtimes={}))
    project.settings.execution_pool="local_runner"
    result=asyncio.run(preflight.inspect(project))
    assert not result["ready"] and any(c["key"]=="runner" and not c["ok"] for c in result["checks"])
    assert "test-token" not in json.dumps(result)


def test_answer_batch_is_atomic_and_feature_scoped(project,monkeypatch):
    questions=[ClarificationItem(question_key=f"Q-0{i}",category="scope",severity="BLOCKING",question=f"问题 {i}",options=["是","否"]).model_dump() for i in (1,2)]
    value=assessed();value.update(questions=questions)
    responses(project,monkeypatch,[value])
    missions.save(project,"001-save","回答持久化","feature",0,actor="owner");advance(project)
    revision=service.latest(project,"001-save")["rev_id"]
    qs=service.list_clarifications(project,"001-save")
    async def unavailable(engine):return {"ready":False,"checks":[],"verification":"未执行"}
    monkeypatch.setattr(preflight,"inspect",unavailable)
    with TestClient(create_app(project,proxy_transport=httpx.MockTransport(lambda r:httpx.Response(200,json={})))) as client:
        path="/vernal-api/missions/001-save/answers"
        body={"expected_rev_id":revision,"answers":[{"clarify_id":qs[0]["clarify_id"],"answer":"是"},{"clarify_id":qs[1]["clarify_id"],"answer":"无效"}]}
        assert client.post(path,json=body).status_code==422
        assert all(q["status"]=="OPEN" for q in service.list_clarifications(project,"001-save"))
        body["answers"][1]["answer"]="否"
        service.assert_source_current(project,service.latest(project,"001-save"))
        result=client.post(path,json=body)
        assert result.status_code==200,result.text
        assert result.json()["saved"] and not result.json()["started"] and "确认：是" in result.json()["diff"]
        assert client.post(path,json=body).status_code==409


def test_existing_custom_workflow_is_preserved(project,monkeypatch):
    from autodev.process.runner import load_builtin_template,save_custom_template
    custom=load_builtin_template("feature");custom.name="团队的既有门禁"
    save_custom_template(project.ws,custom.model_dump_json())
    responses(project,monkeypatch,[assessed()])
    missions.save(project,"001-save","保存回答并复读验证","feature",0,actor="owner");advance(project)
    assert load_template(project.ws).name=="团队的既有门禁"


def test_model_result_from_old_source_is_rejected(project,monkeypatch):
    service.enable(project,"vernal-core")
    missions.save(project,"001-save","旧目标","feature",0,actor="owner")
    def stale_result(stage):
        project.db.meta_set("source_mode","git_repo")
        return dict(assessed(), constitution_markdown="项目宪章")
    mock_bundles(project, monkeypatch, stale_result)
    with pytest.raises(ValueError,match="旧结果"):
        advance(project)
    assert not service.latest(project,"001-save")


def test_intake_uses_native_sop_dispatch_receipts_and_frozen_execution(project, monkeypatch):
    """Only the remote HTTP service is simulated; adapters and receipts are real."""
    from autodev.harnesses.loopforge_harness import LoopForgeHarness
    from autodev.loopforge.client import LoopForgeClient
    from autodev.loopforge.bridge import LoopForgeBridge
    current={"step":"intake"};issues=[];runs=[];native_assets={}
    def handler(req):
        p=req.url.path
        if p=="/api/runners":return httpx.Response(200,json=[{"id":"runner-test","status":"online","isDefault":True,"runtimes":[{"provider":"codex"}]}])
        if p=="/api/sandboxes":return httpx.Response(200,json=[{"id":"local"}])
        if p in ("/api/rules","/api/agents","/api/skills","/api/mcp-servers","/api/workflows"):
            if req.method == "GET":
                return httpx.Response(200, json=list(native_assets.get(p, {}).values()))
            value = json.loads(req.content)
            native_assets.setdefault(p, {})[value["id"]] = value
            return httpx.Response(200, json={**value, "status": "draft"})
        if p.endswith("/publish"):return httpx.Response(200,json={"status":"published"})
        if p=="/api/issues" and req.method=="POST":
            body=json.loads(req.content);issues.append(body)
            if "std-" in body["workflowId"]:
                current["step"]="std_spec" if "-std-spec-" in body["workflowId"] else "std_plan"
            return httpx.Response(201,json={"id":f"i{len(issues)}",**body})
        if p.endswith("/dispatch"):
            rid=f"run-{len(runs)+1}";runs.append((rid,current["step"]))
            return httpx.Response(200,json={"latestRunId":rid})
        if p.startswith("/api/runs/"):
            step=dict(runs)[p.rsplit("/",1)[-1]]
            if step=="execute":
                artifacts=[{"name":"handoff-brief.json","nodeId":node,"content":json.dumps(BRIEF)} for node in ("implement","review")]
            else:
                value = dict(assessed(), constitution_markdown="# 项目宪章\n保留验收依据。") if step == "std_spec" else dict(
                    plan_markdown=PLAN, work_packages=work_packages(), handoff_brief=BRIEF)
                artifacts = native_result(step, value)
            index = p.rsplit("-",1)[-1]
            return httpx.Response(200,json={"id":p.rsplit("/",1)[-1],"projectId":project.settings.loopforge_project_id,
                                           "issueId":"i"+index,"status":"succeeded","artifacts":artifacts})
        return httpx.Response(200,json={})
    client=LoopForgeClient(project.settings.loopforge_url,project.settings.loopforge_token,transport=httpx.MockTransport(handler))
    monkeypatch.setattr(reconciler, "receipt_client", lambda *_: client)
    project._harness=LoopForgeHarness(project.settings,project.db,project.ws,poll_interval=.001,client=client)
    service.enable(project,"vernal-core")
    missions.save(project,"001-save","实现回答保存，重启后可重新读取。","feature",0,actor="owner")
    advance(project);approve(project);advance(project)
    packages=compiler.compile_tasks(project,"001-save")
    ids=compiler.publish_tasks(project,"001-save",expected_hashes=[p.compiled_task_hash for p in packages],actor="owner")
    project.approve_plan()
    receipts=[dict(r) for r in project.db._query("SELECT * FROM dispatch_records")]
    assert [r["stage"] for r in receipts] == ["std_spec", "std_plan"]
    assert len(receipts)==2 and all(r["lf_run_id"] and r["asset_versions_json"]!="{}" for r in receipts)
    assert receipts[-1]["spec_sha"]==packages[0].spec_sha
    bridge=LoopForgeBridge(project.settings,project.db);bridge.client=client;bridge.selected_execution=("host_link",client)
    current["step"]="execute"
    ok,message=asyncio.run(bridge.execute_task(project.db.get_task(ids[0]),"core","slot-test",asyncio.Event(),inputs={}))
    assert ok,message
    assert set(json.loads(project.db.get_task(ids[0]).handoff_brief_json))=={"implement","review"}


def test_answer_can_reconcile_requirement_text_but_cannot_erase_user_confirmation(project,monkeypatch):
    q=ClarificationItem(question_key="Q-01",category="scope",severity="BLOCKING",question="保存多久？",options=["30天","永久"]).model_dump()
    first={**assessed(),"questions":[q],"scope_clear":False}
    updated=SPEC.replace("可靠保存用户回答。","保存用户回答 30 天。")+"\n## 澄清 Q-01 · 1\n模型试图替换的内容\n"
    responses(project,monkeypatch,[first,{**assessed(),"proposed_spec":updated}])
    missions.save(project,"001-save","保存用户回答","feature",0,actor="owner");advance(project)
    question=service.list_clarifications(project,"001-save")[0]
    service.answer_batch(project,"001-save",[{"clarify_id":question["clarify_id"],"answer":"30天","supplement":"期满删除前提醒负责人"}],
                         expected_rev_id=service.latest(project,"001-save")["rev_id"],actor="owner")
    advance(project)
    spec=service.latest(project,"001-save")["content_text"]
    assert "保存用户回答 30 天" in spec and "期满删除前提醒负责人" in spec
    assert "模型试图替换" not in spec and "确认：30天" in spec
    assert not is_gate_passed(project.db,"srs_approval","project","project")


def test_guided_launcher_saves_mission_without_starting_legacy_council(tmp_path):
    from unittest.mock import patch
    from test_project_launcher_and_runner_sync import _make_mock_lf
    state={};transport=httpx.MockTransport(_make_mock_lf(state));real_async=httpx.AsyncClient
    engine=Engine(init_workspace(tmp_path/"workspaces/default",collaboration_mode="files"),settings=Settings(
        collaboration_mode="files",runtime_backend="loopforge",loopforge_project_id="default",loopforge_token="test-token",loopforge_url="http://lf.test"))
    try:
        app=create_app(engine,proxy_transport=transport)
        with patch("autodev.loopforge.client.httpx.AsyncClient",lambda **kw:real_async(transport=transport)):
            with TestClient(app,client=("127.0.0.1",50000)) as client:
                response=client.post("/vernal-api/project/create",json={"mode":"scratch","name":"Guided","wish":"保存回答","guided_delivery":True,"intent":"feature"})
                assert response.status_code==200,response.text
                assert response.json()["mission_saved"] and not response.json()["council_started"]
                data=client.get("/vernal-api/specifications",headers={"X-Vernal-Project":"guided"}).json()
                assert data["missions"][0]["brief"]=="保存回答" and data["missions"][0]["state"]=="NEEDS_ASSESSMENT"
                assert not data["running"] and not data["enabled"]
    finally:engine.db.close()


def test_mission_view_does_not_claim_ready_when_checklist_has_critical_or_missing_metadata(project, monkeypatch):
    responses(project, monkeypatch, [assessed()])
    missions.save(project, "001-save", "保存回答", "feature", 0, actor="owner")
    advance(project)
    check = service.latest(project, "001-save", "checklist")
    assert missions.view(project, missions.get(project, "001-save"))["state"] == "WAITING_APPROVAL"

    project.db.meta_set(
        f"speckit.analysis:{check['rev_id']}",
        json.dumps([{"severity": "CRITICAL", "message": "验收不可测", "requirement_ids": ["REQ-001"]}], ensure_ascii=False),
    )
    v_crit = missions.view(project, missions.get(project, "001-save"))
    assert v_crit["state"] == "CHECKLIST_BLOCKED"
    assert any(b["code"] == "CHECKLIST_CRITICAL_FINDINGS" for b in v_crit["approval_blockers"])

    project.db.meta_set(f"speckit.analysis:{check['rev_id']}", None)
    v_missing = missions.view(project, missions.get(project, "001-save"))
    assert v_missing["state"] == "CHECKLIST_BLOCKED"
    assert any(b["code"] == "CHECKLIST_FINDINGS_MISSING" for b in v_missing["approval_blockers"])


def test_r5_a09_unicode_codepoint_boundary_and_verbatim_confirmation_in_answer_batch(project, monkeypatch):
    q = ClarificationItem(question_key="Q-01", category="scope", severity="BLOCKING", question="是否保留？", options=["保留", "不保留"])
    first = {**assessed(), "scope_clear": False, "questions": [q.model_dump()]}
    responses(project, monkeypatch, [first])
    missions.save(project, "001-save", "回答保存边界测试", "feature", 0, actor="owner")
    advance(project)
    rev_id = service.latest(project, "001-save")["rev_id"]
    clarify_id = service.list_clarifications(project, "001-save")[0]["clarify_id"]

    async def unavailable(engine):
        return {"ready": False, "checks": [], "verification": "未执行"}

    monkeypatch.setattr(preflight, "inspect", unavailable)
    emoji_5001 = "😀" * 5001
    emoji_10001 = "😀" * 10001
    with TestClient(create_app(project, proxy_transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))) as client:
        path = "/vernal-api/missions/001-save/answers"
        # 10001 Unicode codepoints rejected (422)
        too_long = client.post(path, json={"expected_rev_id": rev_id, "answers": [{"clarify_id": clarify_id, "answer": "保留", "supplement": emoji_10001}]})
        assert too_long.status_code == 422
        assert service.list_clarifications(project, "001-save")[0]["status"] == "OPEN"

        # 5001 emojis (5001 codepoints <= 10000, despite 10002 UTF-16 code units) accepted and preserved verbatim
        ok = client.post(path, json={"expected_rev_id": rev_id, "answers": [{"clarify_id": clarify_id, "answer": "保留", "supplement": emoji_5001}]})
        assert ok.status_code == 200, ok.text
        assert emoji_5001 in service.latest(project, "001-save")["content_text"]

        # Stale expected_rev_id conflict detection preserved (409)
        stale = client.post(path, json={"expected_rev_id": rev_id, "answers": [{"clarify_id": clarify_id, "answer": "保留", "supplement": "重复旧修订"}]})
        assert stale.status_code == 409
