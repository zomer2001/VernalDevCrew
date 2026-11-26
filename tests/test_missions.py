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
from autodev.process.runner import GateConflictError, load_template, is_gate_passed
from autodev.server.app import create_app
from autodev.speckit import adapter, compiler, missions, preflight, service, triage
from autodev.speckit.parser import render_tasks
from test_speckit import SPEC, PLAN, BRIEF, approve, draft, save


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


def responses(engine,monkeypatch,assessments):
    calls=[]
    values=iter(assessments)
    async def run(prompt,*,schema_path,**kwargs):
        step=schema_path.name.split("_",2)[1]
        calls.append(step)
        if step=="intake":
            result=next(values)
        else:
            content={"constitution":"# 项目宪章\n交付必须有可复跑依据。", "checklist":"# 质量检查\n验收条件明确。",
                     "plan":PLAN,"tasks":render_tasks([draft()]),"analyze":"# 一致性检查\n全部需求已覆盖。"}[step]
            result=dict(step=step,content=content,clarifications=[],findings=[],handoff_brief=BRIEF)
        return SimpleNamespace(ok=True,structured=result,session_id="model-run",usage=SimpleNamespace(input_tokens=1,cached_tokens=0,output_tokens=1,reasoning_tokens=0))
    monkeypatch.setattr(engine.harness,"run_structured_readonly",run)
    return calls


def advance(engine,feature="001-save"):
    mission=missions.get(engine,feature)
    asyncio.run(missions.advance(engine,feature,missions.reserve(engine,feature,mission["revision"]),actor="owner"))


@pytest.mark.parametrize("mode,origin",[("scratch","greenfield"),("local_folder","existing_local"),("git_repo","existing_git")])
def test_source_and_clarity_are_independent_and_zero_questions_are_valid(project,monkeypatch,mode,origin):
    project.db.meta_set("source_mode",mode)
    calls=responses(project,monkeypatch,[assessed()])
    missions.save(project,"001-save","可靠保存回答，重新打开后仍可读取。","feature",0,actor="owner")
    advance(project)
    view=missions.view(project,missions.get(project,"001-save"))
    assert view["route"]["source_origin"]==origin
    assert view["route"]["clarity_level"]=="READY_LITE" and view["questions"]==[]
    assert view["state"]=="WAITING_APPROVAL"
    assert view["actual_preset"]=="vernal-lite" and load_template(project.ws).id=="quick-prototype"
    assert calls==["intake","constitution","checklist"]
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
    assert calls==["intake","constitution","intake","checklist"]
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


def test_lease_cas_and_safe_recovery(project):
    missions.save(project,"001-save","目标","feature",0,actor="owner")
    token=missions.reserve(project,"001-save",1)
    with pytest.raises(ValueError):missions.reserve(project,"001-save",1)
    with pytest.raises(ValueError):missions.save(project,"001-save","新目标","feature",1,actor="owner")
    missions.release(project,"wrong")
    assert project.db.meta_get("mission.lease")
    missions.recover(project,actor="owner")
    assert not project.db.meta_get("mission.lease")
    with pytest.raises(ValueError):missions.reserve(project,"001-save",2)
    missions.release(project,token)
    token=missions.reserve(project,"001-save",1)
    from autodev.loopforge.dispatch import reserve
    reserve(project.db,"pending-intake",1,1,"local_runner","owner","")
    with pytest.raises(ValueError,match="原生执行"):
        missions.recover(project,actor="owner")
    assert project.db.meta_get("mission.lease")


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
    missions.save(project,"001-save","旧目标","feature",0,actor="owner")
    async def run(*args,**kwargs):
        project.db.meta_set("source_mode","git_repo")
        return SimpleNamespace(ok=True,structured=assessed(),session_id="old")
    monkeypatch.setattr(project.harness,"run_structured_readonly",run)
    with pytest.raises(ValueError,match="旧结果"):
        advance(project)
    assert not service.latest(project,"001-save") and not project.db.meta_get("mission.lease")


def test_intake_uses_native_sop_dispatch_receipts_and_frozen_execution(project):
    """Only the remote HTTP service is simulated; adapters and receipts are real."""
    import re
    from autodev.harnesses.loopforge_harness import LoopForgeHarness
    from autodev.loopforge.client import LoopForgeClient
    from autodev.loopforge.bridge import LoopForgeBridge
    current={"step":"intake"};issues=[];runs=[]
    outputs={"constitution":"# 项目宪章\n交付必须有验收依据。", "checklist":"# 规格检查\n验收条件明确。", "plan":PLAN,
             "tasks":render_tasks([draft()]),"analyze":"# 一致性检查\n全部需求已覆盖。"}
    def handler(req):
        p=req.url.path
        if p=="/api/runners":return httpx.Response(200,json=[{"id":"runner-test","status":"online","isDefault":True,"runtimes":[{"provider":"codex"}]}])
        if p=="/api/sandboxes":return httpx.Response(200,json=[{"id":"local"}])
        if p in ("/api/rules","/api/agents","/api/skills","/api/mcp-servers","/api/workflows"):
            return httpx.Response(200,json=[] if req.method=="GET" else {**json.loads(req.content),"status":"draft"})
        if p.endswith("/publish"):return httpx.Response(200,json={"status":"published"})
        if p=="/api/issues" and req.method=="POST":
            body=json.loads(req.content);issues.append(body)
            match=re.search(r"speckit_([a-z]+)_",body.get("description",""))
            if match:current["step"]=match[1]
            return httpx.Response(201,json={"id":f"i{len(issues)}",**body})
        if p.endswith("/dispatch"):
            rid=f"run-{len(runs)+1}";runs.append((rid,current["step"]))
            return httpx.Response(200,json={"latestRunId":rid})
        if p.startswith("/api/runs/"):
            step=dict(runs)[p.rsplit("/",1)[-1]]
            if step=="execute":
                artifacts=[{"name":"handoff-brief.json","nodeId":node,"content":json.dumps(BRIEF)} for node in ("implement","review")]
            else:
                value=assessed() if step=="intake" else dict(step=step,content=outputs[step],clarifications=[],findings=[],handoff_brief=BRIEF)
                artifacts=[{"name":"vernal-result.json","content":json.dumps(value)}]
            return httpx.Response(200,json={"id":p.rsplit("/",1)[-1],"status":"succeeded","artifacts":artifacts})
        return httpx.Response(200,json={})
    client=LoopForgeClient(project.settings.loopforge_url,project.settings.loopforge_token,transport=httpx.MockTransport(handler))
    project._harness=LoopForgeHarness(project.settings,project.db,project.ws,poll_interval=.001,client=client)
    missions.save(project,"001-save","实现回答保存，重启后可重新读取。","feature",0,actor="owner")
    advance(project);approve(project);advance(project)
    packages=compiler.compile_tasks(project,"001-save")
    ids=compiler.publish_tasks(project,"001-save",expected_hashes=[p.compiled_task_hash for p in packages],actor="owner")
    project.approve_plan()
    receipts=[dict(r) for r in project.db._query("SELECT * FROM dispatch_records")]
    assert len(receipts)==6 and all(r["lf_run_id"] and r["asset_versions_json"]!="{}" for r in receipts)
    assert receipts[-1]["spec_sha"]==packages[0].spec_sha
    bridge=LoopForgeBridge(project.settings,project.db);bridge.client=client;bridge.selected_execution=("host_link",client)
    current["step"]="execute"
    ok,message=asyncio.run(bridge.execute_task(project.db.get_task(ids[0]),"core","slot-test",asyncio.Event(),inputs={}))
    assert ok,message
    assert set(json.loads(project.db.get_task(ids[0]).handoff_brief_json))=={"implement","review"}


def test_answer_can_reconcile_requirement_text_but_cannot_erase_user_confirmation(project,monkeypatch):
    q=dict(question_key="Q-01",category="scope",severity="BLOCKING",question="保存多久？",options=["30天","永久"])
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
