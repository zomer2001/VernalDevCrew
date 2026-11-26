"""P1 source authority, revision races, compiler boundaries and offline validation."""
import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from autodev.engine import Engine, init_workspace
from autodev.config import Settings
from autodev.models import ClarificationItem, SpecTaskDraft, SpecStepResult, strict_json_schema
from autodev.process.runner import decide_gate, latest_gate_record, is_gate_passed, GateConflictError
from autodev.speckit import service, compiler, adapter, assets
from autodev.speckit.parser import parse_spec, render_tasks

SPEC = """# 功能规格
## 目标
可靠保存用户回答。
## 用户场景
- 用户提交回答后重新打开仍能读取。
## Non-Goals
- 本轮不提供多人编辑。
## 需求
### REQ-001 [P0] 保存回答
要求：当用户提交回答时，系统应持久保存并返回确认。
验收：
- 重新打开后可读取同一回答。
## 边界情况
- 空回答返回校验错误。
"""
PLAN = "# 技术方案\n## 需求映射\nREQ-001 由存储模块实现。\n## 接口与数据\n保存接口接收文本。\n## 实施边界\n只修改 src/save.py 和 tests/test_save.py。\n## 验证\n执行 pytest。\n"
BRIEF = dict(delivered="已整理保存回答的规格。",integration="批准后可生成技术方案。",boundaries="本轮不做多人编辑。",verification="格式校验通过，尚未执行功能测试。")


@pytest.fixture
def eng(tmp_path):
    settings=Settings(collaboration_mode="files",runtime_backend="loopforge",loopforge_project_id="spec-test",loopforge_url="http://lf.test",loopforge_token="test-token")
    e=Engine(init_workspace(tmp_path/"project",collaboration_mode="files"),settings=settings)
    service.enable(e,"vernal-core")
    yield e
    e.db.close()


def save(e,kind,content,feature="001-save"):
    old=service.latest(e,feature,kind)
    return service.publish_revision(e,feature,kind,content,expected_rev_id=old["rev_id"] if old else 0)


def approve(e):
    gate=latest_gate_record(e.db,"srs_approval","project","project")
    return decide_gate(e.db,gate["record_id"],"pass",actor="owner",expected_sha=gate["target_sha"])


def draft(key="T001",**changes):
    return SpecTaskDraft(task_key=key,package_key="save",module="storage",title="保存回答",description="实现回答持久化。",acceptance_criteria=["重启后仍可读取"],requirement_ids=["REQ-001"],allowed_paths=["src/save.py"],verify_cmds=["python -m pytest"],**changes)


def prepared(e):
    save(e,"constitution","# 项目宪章\n交付必须有验收依据。\n")
    save(e,"spec",SPEC)
    approve(e)
    save(e,"plan",PLAN)
    one=draft();two=one.model_copy(update={"task_key":"T002","title":"验证回答保存","description":"补充回归测试。","allowed_paths":["tests/test_save.py"],"depends_on":["T001"]})
    save(e,"tasks",render_tasks([one,two]))
    analysis=save(e,"analyze","# 一致性检查\n全部需求已有对应任务，无严重问题。\n")
    e.db.meta_set(f"speckit.analysis:{analysis['rev_id']}","[]")


def test_offline_assets_pinned_and_tampering_blocked(eng,tmp_path,monkeypatch):
    lock,root=assets.load_assets(eng)
    assert lock["speckit_version"]=="1.4.4" and len(lock["upstream_commit"])==40
    monkeypatch.setattr(assets,"ASSET_ROOT",tmp_path/"missing")
    assert assets.pin_assets(eng)==lock
    file=root/"presets/vernal-core.json"
    file.write_text("{}")
    with pytest.raises(ValueError,match="校验失败"):
        assets.load_assets(eng)


def test_source_projection_and_approval_invalidation(eng):
    from autodev.docs_center.service import get_doc,save_doc_version,load_product_spec
    prepared(eng)
    assert load_product_spec(eng).requirements[0].identity=="001-save:REQ-001"
    doc=get_doc(eng,"SRS.md")
    assert doc["readonly"] and "保存回答" in doc["content"]
    with pytest.raises(ValueError,match="只读"):
        save_doc_version(eng,"SRS.md","# bad")
    assert not eng.ws.prd_json_path.exists()
    assert is_gate_passed(eng.db,"srs_approval","project","project")
    save(eng,"spec",SPEC.replace("可靠保存","可靠地保存"))
    assert not is_gate_passed(eng.db,"srs_approval","project","project")
    with pytest.raises(ValueError): compiler.compile_tasks(eng,"001-save")


def test_clarification_cannot_be_bypassed_and_default_can_be_replaced(eng):
    prepared(eng)
    rev=service.latest(eng,"001-save")
    q=ClarificationItem(question_key="Q-01",category="non_goal",severity="BLOCKING",question="是否支持多人编辑？",options=["支持","不支持"])
    service.add_clarifications(eng,"001-save",rev["rev_id"],[q],actor="owner")
    assert not is_gate_passed(eng.db,"srs_approval","project","project")
    save(eng,"spec",SPEC+"\n附加说明。\n")
    with pytest.raises(GateConflictError): approve(eng)
    question=service.list_clarifications(eng,"001-save")[0]
    rev=service.answer_clarification(eng,question["clarify_id"],"不支持",expected_rev_id=service.latest(eng,"001-save")["rev_id"],actor="owner")
    approve(eng)
    default=ClarificationItem(question_key="Q-02",category="interface",severity="DEFAULTED",question="保存前是否去除空格？",options=["是","否"],default_option="是",rationale="保持输入整洁，可后续调整。")
    service.add_clarifications(eng,"001-save",rev["rev_id"],[default],actor="owner")
    question=service.list_clarifications(eng,"001-save")[-1]
    assert question["status"]=="AUTO_DEFAULTED"
    rev=service.answer_clarification(eng,question["clarify_id"],"否",expected_rev_id=service.latest(eng,"001-save")["rev_id"],actor="owner")
    assert rev["content_text"].count("保存前是否去除空格")==1
    assert "确认：否" in rev["content_text"] and "确认：是" not in rev["content_text"]


def test_compiler_grouping_idempotence_and_frozen_execution(eng):
    prepared(eng)
    packages=compiler.compile_tasks(eng,"001-save")
    assert len(packages)==1 and packages[0].source_task_keys==["T001","T002"]
    ids=compiler.publish_tasks(eng,"001-save",expected_hashes=[p.compiled_task_hash for p in packages],actor="owner")
    assert compiler.publish_tasks(eng,"001-save",expected_hashes=[p.compiled_task_hash for p in packages],actor="owner")==ids
    assert len(eng.db.list_tasks())==1 and eng.status=="PLANNED"
    task=eng.db.get_task(ids[0])
    assert compiler.assert_package_current(eng.db,task).spec_rev_id==task.spec_rev_id
    from autodev.context_bundle import build_task_input_bundle
    assert "交付必须有验收依据" in build_task_input_bundle(eng.ws,eng.db,task)
    eng.approve_plan()
    assert eng.status=="APPROVED"
    eng.db.update_task(task.task_id,description="试图修改冻结任务")
    with pytest.raises(ValueError,match="被冻结|工作包内容被修改"): compiler.assert_package_current(eng.db,eng.db.get_task(task.task_id))


@pytest.mark.parametrize("mutation,match",[
    ({"requirement_ids":["REQ-999"]},"需求不存在"),
    ({"allowed_paths":["../secret"]},"越出"),
    ({"allowed_paths":["specs/001-save/spec.md"]},"版本源"),
    ({"depends_on":["T001"]},"循环"),
    ({"verify_cmds":[]},"验证命令"),
])
def test_compiler_rejects_invalid_drafts(eng,mutation,match):
    prepared(eng)
    save(eng,"tasks",render_tasks([draft().model_copy(update=mutation)]))
    with pytest.raises(ValueError,match=match): compiler.compile_tasks(eng,"001-save",require_analysis=False)


def test_critical_analysis_and_source_drift_block_publication(eng):
    prepared(eng)
    rev=service.latest(eng,"001-save","analyze")
    eng.db.meta_set(f"speckit.analysis:{rev['rev_id']}",json.dumps([{"severity":"CRITICAL"}]))
    with pytest.raises(ValueError,match="检查尚未通过"): compiler.compile_tasks(eng,"001-save")
    (eng.ws.root/"specs/001-save/spec.md").write_text("changed")
    with pytest.raises(ValueError): compiler.compile_tasks(eng,"001-save",require_analysis=False)


def test_existing_unmanaged_file_is_never_overwritten(eng):
    file=eng.ws.root/"specs/001-save/spec.md";file.parent.mkdir(parents=True);file.write_text("original")
    with pytest.raises(ValueError,match="未纳管"):
        save(eng,"spec",SPEC)
    assert file.read_text()=="original"


@pytest.mark.parametrize("runtime",["python","node"])
def test_validator_checks_nested_schema_and_writes_source(tmp_path,runtime):
    import shutil
    if runtime=="node" and not shutil.which("node"): pytest.skip("Node unavailable")
    source=Path(__file__).parents[1]/"autodev/speckit"/("validate_output.py" if runtime=="python" else "validate_output.cjs")
    command=[sys.executable,str(source)] if runtime=="python" else ["node",str(source)]
    schema=strict_json_schema(SpecStepResult);schema["x-vernal-artifact-path"]="specs/001-save/spec.md"
    (tmp_path/".vernal-schema.json").write_text(json.dumps(schema))
    value=dict(step="specify",content=SPEC,clarifications=[],findings=[],handoff_brief=dict(BRIEF))
    output=tmp_path/"vernal-result.json";output.write_text(json.dumps(value))
    result=subprocess.run(command,cwd=tmp_path,capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert (tmp_path/"specs/001-save/spec.md").read_text()==SPEC
    value["handoff_brief"]["delivered"]=123
    output.write_text(json.dumps(value))
    assert subprocess.run(command,cwd=tmp_path,capture_output=True).returncode!=0


def test_adapter_dsh_requires_explicit_model_and_audits_transport_error(eng,monkeypatch):
    eng.settings.harness="dsh"
    monkeypatch.setattr(Settings,"model_for",lambda self,role:"")
    with pytest.raises(ValueError,match="明确配置模型"):
        asyncio.run(adapter.run_step(eng,"001-save","constitution"))
    eng.settings.harness="codex"
    async def fail(*args,**kwargs): raise RuntimeError("network unavailable")
    monkeypatch.setattr(eng.harness,"run_structured_readonly",fail)
    with pytest.raises(RuntimeError): asyncio.run(adapter.run_step(eng,"001-save","constitution"))
    rows=eng.db.list_runs("_speckit_001-save_constitution")
    assert rows[-1]["finished_at"] and rows[-1]["verdict"]=="ERROR"


def test_adapter_rejects_old_result_after_revision_change(eng,monkeypatch):
    prepared(eng)
    async def response(*args,**kwargs):
        save(eng,"spec",SPEC.replace("可靠保存","可靠地保存"))
        return SimpleNamespace(ok=True,structured=dict(step="plan",content=PLAN,clarifications=[],findings=[],handoff_brief=dict(BRIEF)),
                               session_id="run1",usage=SimpleNamespace(input_tokens=0,cached_tokens=0,output_tokens=0,reasoning_tokens=0))
    monkeypatch.setattr(eng.harness,"run_structured_readonly",response)
    old=service.latest(eng,"001-save","plan")["rev_id"]
    with pytest.raises(ValueError,match="旧结果已拒收"): asyncio.run(adapter.run_step(eng,"001-save","plan"))
    assert service.latest(eng,"001-save","plan")["rev_id"]==old


@pytest.mark.parametrize("missing_brief",[False,True])
def test_complete_adapter_uses_real_planning_dispatch_with_mock_transport(eng,monkeypatch,missing_brief):
    """Mock the remote service, keeping native SOP registration/dispatch/poll/parsing intact."""
    import httpx
    from autodev.harnesses.loopforge_harness import LoopForgeHarness
    from autodev.loopforge.client import LoopForgeClient
    calls=[]; rules=[]; current={}; issues=[]
    outputs={"constitution":"# 项目宪章\n交付必须有验收依据。\n","specify":SPEC,"clarify":"","plan":PLAN,
             "tasks":render_tasks([draft()]),"analyze":"# 一致性检查\n没有发现严重问题。\n"}
    def handler(req):
        p=req.url.path;calls.append((req.method,p))
        if p=="/api/runners": return httpx.Response(200,json=[{"id":"r1","status":"online","isDefault":True,"runtimes":[{"provider":"codex"}]}])
        if p=="/api/sandboxes": return httpx.Response(200,json=[{"id":"local"}])
        if p in ("/api/rules","/api/agents","/api/skills","/api/mcp-servers","/api/workflows"):
            if req.method=="GET":return httpx.Response(200,json=[])
            value=json.loads(req.content)
            if p=="/api/rules":rules.append(value)
            return httpx.Response(200,json={**value,"status":"draft"})
        if p.endswith("/publish"):return httpx.Response(200,json={"status":"published"})
        if p=="/api/issues" and req.method=="POST":
            body=json.loads(req.content);issues.append(body)
            assert "speckit" in body["description"] or "功能规格" in body["description"] or "项目约束" in body["description"]
            return httpx.Response(201,json={"id":f"i{len(issues)}",**body})
        if p.endswith("/dispatch"):return httpx.Response(200,json={"latestRunId":"run-"+current["step"]})
        if p.startswith("/api/runs/"):
            if current["step"]=="execute":
                nodes=["implement"] if missing_brief else ["implement","review"]
                return httpx.Response(200,json={"id":"run-execute","status":"succeeded","artifacts":[
                    {"name":"handoff-brief.json","nodeId":node,"content":json.dumps(BRIEF,ensure_ascii=False)} for node in nodes]})
            value=dict(step=current["step"],content=outputs[current["step"]],clarifications=[],findings=[],handoff_brief=dict(BRIEF))
            return httpx.Response(200,json={"id":"run-"+current["step"],"status":"succeeded","artifacts":[{"name":"vernal-result.json","content":json.dumps(value,ensure_ascii=False)}]})
        return httpx.Response(200,json={})
    client=LoopForgeClient(eng.settings.loopforge_url,eng.settings.loopforge_token,transport=httpx.MockTransport(handler))
    eng._harness=LoopForgeHarness(eng.settings,eng.db,eng.ws,poll_interval=.001,client=client)
    for step in outputs:
        current["step"]=step
        result=asyncio.run(adapter.run_step(eng,"001-save",step))
        assert result["handoff_brief"]["delivered"]==BRIEF["delivered"]
        if step=="clarify":approve(eng)
    packages=compiler.compile_tasks(eng,"001-save")
    assert len(packages)==1
    records=[dict(r) for r in eng.db._query("SELECT * FROM dispatch_records")]
    assert len(records)==6 and all(r["asset_versions_json"]!="{}" for r in records)
    assert all(r["lf_run_id"] for r in records)
    assert records[-1]["spec_sha"]==packages[0].spec_sha
    assert any("交付必须有验收依据" in r.get("content","") for r in rules)
    assert len(issues)==6
    from autodev.loopforge.bridge import LoopForgeBridge
    task_ids=compiler.publish_tasks(eng,"001-save",expected_hashes=[p.compiled_task_hash for p in packages],actor="owner")
    eng.approve_plan()
    bridge=LoopForgeBridge(eng.settings,eng.db);bridge.client=client;bridge.selected_execution=("host_link",client)
    current["step"]="execute"
    ok,message=asyncio.run(bridge.execute_task(eng.db.get_task(task_ids[0]),"core","slot-1",asyncio.Event(),inputs={}))
    assert ok is (not missing_brief),message
    if missing_brief:assert "交接便签" in message
    else:assert set(json.loads(eng.db.get_task(task_ids[0]).handoff_brief_json))=={"implement","review"}


def test_api_source_cas_readonly_and_publish(eng):
    import httpx
    from fastapi.testclient import TestClient
    from autodev.server.app import create_app
    prepared(eng)
    with TestClient(create_app(eng,proxy_transport=httpx.MockTransport(lambda r:httpx.Response(200,json={})))) as client:
        state=client.get("/vernal-api/specifications");assert state.status_code==200
        assert state.json()["features"][0]["feature_id"]=="001-save"
        process=client.get("/vernal-api/process").json()
        requirements=next(s for s in process["stages"] if s["id"]=="requirements")
        assert any(g["id"]=="srs_approval" for g in requirements["gates"])
        assert client.get("/vernal-api/plan").json()["prd"]["requirements"][0]["feature_id"]=="001-save"
        bad=client.put("/vernal-api/specifications/001-save/spec",json={"content":SPEC,"expected_rev_id":0})
        assert bad.status_code==409
        preview=client.get("/vernal-api/specifications/001-save/compile");assert preview.status_code==200,preview.text
        wrong=client.post("/vernal-api/specifications/001-save/publish",json={"expected_hashes":["old"]})
        assert wrong.status_code==409
        good=client.post("/vernal-api/specifications/001-save/publish",json={"expected_hashes":[p["compiled_task_hash"] for p in preview.json()["packages"]]})
        assert good.status_code==200,good.text
        assert len(good.json()["task_ids"])==1


def test_migration_preserves_legacy_and_refuses_unstructured_srs(tmp_path):
    from autodev.harnesses.mock import MockHarness
    legacy=parse_spec(SPEC,"legacy")
    for requirement in legacy.requirements:requirement.feature_id=""
    legacy.error_conventions=["保存失败必须提示重试。"]
    legacy.global_acceptance_redlines=["不丢失用户回答。"]
    e=Engine(init_workspace(tmp_path/"old",collaboration_mode="files"),harness=MockHarness())
    try:
        raw=legacy.model_dump_json();e.ws.prd_json_path.write_text(raw)
        with pytest.raises(ValueError,match="迁移"):service.enable(e,"vernal-lite")
        service.enable(e,"vernal-lite",migrate=True)
        assert e.ws.prd_json_path.read_text()==raw
        spec=service.product_spec(e)
        assert spec.error_conventions==legacy.error_conventions
        assert spec.global_acceptance_redlines==legacy.global_acceptance_redlines
        assert spec.requirements[0].feature_id=="001-migrated"
        with pytest.raises(ValueError,match="澄清"):service.assert_no_blocking_questions(e)
    finally:e.db.close()
    other=Engine(init_workspace(tmp_path/"document-only",collaboration_mode="files"),harness=MockHarness())
    try:
        (other.ws.root/"docs").mkdir(exist_ok=True)
        path=other.ws.root/"docs/SRS.md";path.write_text("需要保留的需求")
        with pytest.raises(ValueError,match="原文档不会覆盖"):service.enable(other,"vernal-core",migrate=True)
        assert path.read_text()=="需要保留的需求"
    finally:other.db.close()


def test_restore_preserves_external_change_and_contract_drift_blocks_execution(eng):
    prepared(eng)
    packages=compiler.compile_tasks(eng,"001-save")
    ids=compiler.publish_tasks(eng,"001-save",expected_hashes=[p.compiled_task_hash for p in packages],actor="owner")
    rev=service.latest(eng,"001-save")
    path=eng.ws.root/rev["rel_path"];path.write_text("外部编辑尚未确认")
    with pytest.raises(ValueError):compiler.assert_package_current(eng.db,eng.db.get_task(ids[0]))
    restored=service.restore_source(eng,"001-save","spec",expected_rev_id=rev["rev_id"],actor="owner")
    assert (eng.ws.root/restored["backup_path"]).read_text()=="外部编辑尚未确认"
    compiler.assert_package_current(eng.db,eng.db.get_task(ids[0]))
    contracts=eng.ws.root/"contracts";contracts.mkdir(exist_ok=True);(contracts/"api.json").write_text("{}")
    with pytest.raises(ValueError,match="契约基线"):compiler.assert_package_current(eng.db,eng.db.get_task(ids[0]))


def test_feature_namespaces_and_source_revision_guard(eng):
    from autodev.trace.scan import extract_requirements_from_sources
    prepared(eng)
    save(eng,"spec",SPEC,"002-save")
    rows=extract_requirements_from_sources(eng)
    assert {r["req_id"] for r in rows}=={"001-save:REQ-001","002-save:REQ-001"}
    approve(eng)
    packages=compiler.compile_tasks(eng,"001-save")
    ids=compiler.publish_tasks(eng,"001-save",expected_hashes=[p.compiled_task_hash for p in packages],actor="owner")
    eng.db.update_task(ids[0],status="IN_PROGRESS")
    with pytest.raises(ValueError,match="仍有任务运行"):save(eng,"spec",SPEC+"\n新说明。\n")
    with pytest.raises(ValueError,match="仍有任务运行"):save(eng,"constitution","# 宪章\n新约束")


@pytest.mark.parametrize("case,match",[("coverage","未覆盖 P0"),("overlap","范围重叠"),("unsafe_id","安全的非空"),("projection","只读规格")])
def test_compiler_additional_safety_boundaries(eng,case,match):
    prepared(eng)
    if case=="coverage":
        save(eng,"spec",SPEC.replace("## 边界情况","### REQ-002 [P0] 删除回答\n要求：用户可删除回答。\n验收：\n- 删除后不可读取。\n## 边界情况"));approve(eng);save(eng,"plan",PLAN)
        values=[draft()]
    elif case=="overlap":values=[draft(),draft().model_copy(update={"task_key":"T002","package_key":"other"})]
    elif case=="unsafe_id":values=[draft().model_copy(update={"milestone_id":"../escape"})]
    else:values=[draft().model_copy(update={"allowed_paths":["docs"]})]
    save(eng,"tasks",render_tasks(values))
    with pytest.raises(ValueError,match=match):compiler.compile_tasks(eng,"001-save",require_analysis=False)


def test_specification_write_respects_project_role(eng):
    import httpx
    from fastapi.testclient import TestClient
    from autodev.server.app import create_app
    from autodev.governance.rbac import upsert_member
    upsert_member(eng.db,"owner","viewer",project="spec-test")
    with TestClient(create_app(eng,proxy_transport=httpx.MockTransport(lambda r:httpx.Response(200,json={})))) as client:
        assert client.get("/vernal-api/specifications").status_code==200
        response=client.put("/vernal-api/specifications/001-save/constitution",json={"content":"# 项目宪章","expected_rev_id":0})
        assert response.status_code==403,response.text


def test_managed_project_cannot_dispatch_legacy_unfrozen_task(eng):
    from autodev.models import PlanTask
    prepared(eng)
    packages=compiler.compile_tasks(eng,"001-save")
    compiler.publish_tasks(eng,"001-save",expected_hashes=[p.compiled_task_hash for p in packages],actor="owner")
    legacy=PlanTask(task_id="legacy",milestone_id="M1",squad="core",module="storage",role="coder",title="绕过编译器的任务",description="unfrozen",
                    acceptance_criteria=["done"],depends_on=[],doc_slice_ids=[],allowed_paths=["src"],verify_cmds=["pytest"],priority=1)
    eng.db.insert_single_task(legacy,max_retries=1)
    from autodev.context_bundle import build_task_input_bundle
    with pytest.raises(ValueError,match="必须由工作包编译器"):
        build_task_input_bundle(eng.ws,eng.db,eng.db.get_task("legacy"))
