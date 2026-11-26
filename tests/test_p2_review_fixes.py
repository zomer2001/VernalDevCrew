"""Regressions for P2 review: frozen payloads, recovery claims and governance."""
import asyncio
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from autodev.db import Database
from autodev.governance import upsert_member
from autodev.loopforge.bridge import poll_loopforge_run
from autodev.loopforge.client import LoopForgeClient, LoopForgeError
from autodev.loopforge.completion import CompletionLedger, evaluate_waiting_node
from autodev.process.model import GateDef
from autodev.process.runner import load_builtin_template, load_template, save_custom_template
from autodev.server.app import create_app
from autodev.speckit import adapter, assets, compiler, missions, service, triage
from test_speckit import SPEC, eng, prepared, save


def waiting(*, protocol="controlled_recovery", **node_changes):
    node = {"status": "waiting_approval", "attempt": 1, "reworkCount": 0,
            "exitCode": 0, "gateCallbackMissing": True, "output": "完成本轮工作", **node_changes}
    spec = {"id": "draft", "type": "agent", "completionProtocol": protocol}
    return {"id": "run-fix", "status": "waiting", "workflowSnapshot": {"nodes": [spec]},
            "nodes": {"draft": node}}


def evaluate(run):
    return asyncio.run(evaluate_waiting_node(LoopForgeClient("http://lf.test"), run, "draft",
                       run["nodes"]["draft"], run["workflowSnapshot"]["nodes"][0]))


def test_old_frozen_payload_remains_valid_and_tampering_is_rejected(eng):
    prepared(eng)
    packages = compiler.compile_tasks(eng, "001-save")
    tid = compiler.publish_tasks(eng, "001-save", expected_hashes=[p.compiled_task_hash for p in packages], actor="owner")[0]
    task = eng.db.get_task(tid)
    old = json.loads(task.compiled_package_json)
    for name in ("checklist_rev_id", "checklist_sha", "checklist_findings_sha"):
        del old[name]
    old["compiled_task_hash"] = assets.digest_json({k: v for k, v in old.items() if k != "compiled_task_hash"})
    eng.db.update_task(tid, compiled_package_json=json.dumps(old), compiled_task_hash=old["compiled_task_hash"])
    # Simulate reopening the project with a newer model, not just an in-memory object.
    reopened = Database(eng.db.path)
    try:
        assert compiler.assert_package_current(reopened, reopened.get_task(tid)).checklist_sha == ""
        old["task"]["description"] = "篡改已发布任务"
        eng.db.update_task(tid, compiled_package_json=json.dumps(old))
        with pytest.raises(service.SpecConflictError, match="指纹"):
            compiler.assert_package_current(reopened, reopened.get_task(tid))
    finally:
        reopened.close()


@pytest.mark.parametrize("changes", [
    {"protocol": ""}, {"protocol": "new_unknown_protocol"}, {"exitCode": None},
    {"output": ""}, {"attempt": None}, {"reworkCount": None}, {"exitCode": 2},
    {"attempt": "invalid"}, {"reworkCount": -1}, {"exitCode": False},
])
def test_recovery_requires_explicit_capability_and_positive_turn_evidence(changes):
    assert evaluate(waiting(**changes)).action == "wait"


@pytest.mark.parametrize("artifact", [
    {"name": "vernal-result.json", "content": "{}"},
    {"name": "vernal-result.json", "attempt": 0, "content": "{}"},
    {"name": "vernal-result.json", "attempt": 1, "reworkCount": 9, "content": "{}"},
    {"name": "vernal-result.json", "attempt": 1},
])
def test_recovery_rejects_unattributed_stale_or_unreadable_artifacts(artifact):
    run = waiting()
    run["workflowSnapshot"]["nodes"][0]["artifactOutputs"] = [{"name": "vernal-result.json", "required": True}]
    run["nodes"]["draft"]["artifacts"] = [artifact]
    assert evaluate(run).action == "wait"


def test_recovery_reads_node_local_artifacts_with_explicit_rework_identity():
    run = waiting(reworkCount=1)
    run["workflowSnapshot"]["nodes"][0]["artifactOutputs"] = [{"name": "vernal-result.json", "required": True}]
    run["nodes"]["draft"]["artifacts"] = [{"name": "vernal-result.json", "attempt": 1,
                                             "reworkCount": 1, "content": "{}"}]
    assert evaluate(run).action == "approve_recovery"


def test_recovery_claim_is_atomic_across_connections_and_restarts(eng):
    decision = evaluate(waiting())
    barrier = Barrier(2)
    def claim():
        db = Database(eng.db.path)
        try:
            ledger = CompletionLedger(db, decision.run_id)
            barrier.wait()
            return ledger.claim(decision)[0]
        finally:
            db.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim) for _ in range(2)]
        assert sorted(f.result() for f in futures) == [False, True]
    restarted = CompletionLedger(eng.db, decision.run_id)
    # Evidence updates in the same turn cannot trigger a second approval.
    assert restarted.claim(replace(decision, artifact_digest="changed", idempotency_key="changed")) == (False, "pending")
    assert restarted.claim(replace(decision, attempt=2, idempotency_key="attempt2"))[0]
    # Rework resets attempt. Rework count is the primary ordering coordinate.
    assert restarted.claim(replace(decision, rework_count=1, idempotency_key="rework1"))[0]
    assert not restarted.claim(replace(decision, attempt=9, idempotency_key="old-turn"))[0]


def test_uncertain_approval_is_not_replayed_after_restart(eng):
    run = waiting()
    done = {"id": run["id"], "status": "succeeded", "nodes": {}}
    client = LoopForgeClient("http://lf.test")
    client.get_run = AsyncMock(side_effect=[run, run, done])
    client.approve_run = AsyncMock(side_effect=LoopForgeError("reply lost after acceptance"))
    asyncio.run(poll_loopforge_run(client, run["id"], poll_interval=0, db=eng.db))
    assert client.approve_run.await_count == 1
    run["nodes"]["draft"]["output"] += "更新摘要"
    client.get_run = AsyncMock(side_effect=[run, done])
    asyncio.run(poll_loopforge_run(client, run["id"], poll_interval=0, db=eng.db))
    assert client.approve_run.await_count == 1


def mission_with_checklist(eng, feature="001-save"):
    if not service.latest(eng, "project", "constitution"):
        save(eng, "constitution", "# 项目宪章\n交付可验证。\n")
    mission = missions.save(eng, feature, "可靠保存回答", "feature", 0, actor="owner")
    save(eng, "spec", SPEC, feature=feature)
    chk = service.publish_revision(eng, feature, "checklist", "# 规格检查\n无问题。\n", expected_rev_id=0, findings=[])
    return mission, chk


def test_profile_conversion_and_asset_upgrade_enforce_governance_roles(eng):
    mission, _ = mission_with_checklist(eng)
    body = {"target_preset": "vernal-lite", "target_process": "quick-prototype", "expected_revision": mission["revision"]}
    with TestClient(create_app(eng, proxy_transport=httpx.MockTransport(lambda req: httpx.Response(200, json={})))) as client:
        upsert_member(eng.db, "owner", "developer", project="spec-test")
        assert client.post("/vernal-api/missions/001-save/convert-profile", json=body).status_code == 403
        assert client.post("/vernal-api/specifications/upgrade-assets", json={"preset_id": "vernal-lite"}).status_code == 403
        assert assets.load_assets(eng)[0]["preset_id"] == "vernal-core"
        upsert_member(eng.db, "owner", "architect", project="spec-test")
        result = client.post("/vernal-api/missions/001-save/convert-profile", json=body)
        assert result.status_code == 200, result.text


def test_custom_gates_under_builtin_id_are_preserved(eng):
    mission, _ = mission_with_checklist(eng)
    template = load_builtin_template("standard")
    template.stages[0].gates.append(GateDef(id="security_owner", name="安全负责人确认", kind="human_approval", scope="project", blocking=True))
    save_custom_template(eng.ws, template.model_dump_json())
    before = (eng.ws.state_dir / "process.yaml").read_bytes()
    mission["route"] = {"available_preset": "vernal-lite", "process_template": "quick-prototype"}
    assert not missions.config_alignment(eng, mission)["can_convert_to_recommended"]
    with pytest.raises(service.SpecConflictError, match="自定义流程模板"):
        missions.convert_profile(eng, "001-save", target_preset="vernal-lite", target_process="quick-prototype",
                                 expected_revision=mission["revision"], actor="owner")
    assert (eng.ws.state_dir / "process.yaml").read_bytes() == before
    assert assets.load_assets(eng)[0]["preset_id"] == "vernal-core"


def test_conversion_failure_rolls_back_files_lock_and_revision(eng, monkeypatch):
    mission, chk = mission_with_checklist(eng)
    path = eng.ws.state_dir / "process.yaml"
    before = path.read_bytes()
    previous_setting = eng.settings.process_template
    def fail(*args):
        raise RuntimeError("injected storage failure")
    monkeypatch.setattr(missions, "_store", fail)
    with pytest.raises(RuntimeError, match="injected"):
        missions.convert_profile(eng, "001-save", target_preset="vernal-lite", target_process="quick-prototype",
                                 expected_revision=mission["revision"], actor="owner")
    assert path.read_bytes() == before
    assert eng.settings.process_template == previous_setting
    assert assets.load_assets(eng)[0]["preset_id"] == "vernal-core"
    assert missions.get(eng, "001-save")["revision"] == mission["revision"]
    assert service.latest(eng, "001-save", "checklist")["status"] == chk["status"]


def test_upgrade_invalidates_all_features_and_same_output_can_be_revalidated(eng, monkeypatch):
    one, chk = mission_with_checklist(eng)
    two, other = mission_with_checklist(eng, "002-save")
    old_input = triage.input_key(eng, one)
    spec = service.latest(eng, "001-save")
    assets.upgrade_assets(eng, "vernal-lite")
    for feature in ("001-save", "002-save"):
        assert service.quality_review(eng, feature)["status"] == "STALE"
    assert triage.input_key(eng, one) != old_input
    assert service.latest(eng, "001-save")["rev_id"] == spec["rev_id"]
    new = service.publish_revision(eng, "001-save", "checklist", chk["content_text"], expected_rev_id=chk["rev_id"], findings=[])
    assert new["rev_id"] != chk["rev_id"] and new["status"] != "STALE"
    assert new["preset_id"] == "vernal-lite" and service.quality_review(eng, "001-save")["is_current"]
    new_input = triage.input_key(eng, one)
    monkeypatch.setattr(assets, "PROMPT_ASSEMBLY_VERSION", "next-generation")
    assert not service.quality_review(eng, "001-save")["is_current"]
    assert triage.input_key(eng, one) != new_input


def test_stale_analysis_cannot_compile_even_with_matching_content_and_inputs(eng):
    prepared(eng)
    eng.db._exec("UPDATE spec_revisions SET status='STALE' WHERE source_kind='analyze'")
    with pytest.raises(service.SpecConflictError, match="一致性检查"):
        compiler.compile_tasks(eng, "001-save")


def test_stale_same_text_plan_creates_valid_revision(eng):
    prepared(eng)
    old = service.latest(eng, "001-save", "plan")
    eng.db._exec("UPDATE spec_revisions SET status='STALE' WHERE rev_id=?", (old["rev_id"],))
    new = service.publish_revision(eng, "001-save", "plan", old["content_text"], expected_rev_id=old["rev_id"])
    assert new["rev_id"] != old["rev_id"] and new["status"] != "STALE"


@pytest.mark.parametrize(("role", "step", "risk", "override", "expected"), [
    ("qa_lead", "analyze", "low", "", "high"),
    ("qa_lead", "checklist", "low", "", "medium"),
    ("pm", "intake", "high", "", "high"),
    ("qa_lead", "analyze", "high", "low", "low"),
])
def test_native_dispatch_and_log_use_the_step_effort(eng, monkeypatch, role, step, risk, override, expected):
    from autodev.harnesses.loopforge_harness import LoopForgeHarness
    from autodev.loopforge.bootstrap import build_plan_workflow, build_vernal_agents
    eng.settings.harness = "codex"
    eng.settings.qa_reasoning_effort = override
    pid = eng.settings.loopforge_project_id
    source = build_plan_workflow(pid, role=role)
    source["nodes"].append({"id": "custom_signoff", "type": "approval", "name": "组织审批"})
    workflows = {source["id"]: copy.deepcopy(source)}
    agents = {a["id"]: a for a in build_vernal_agents(pid, eng.settings)}
    original_agents = copy.deepcopy(agents)
    requests = []
    def handler(req):
        path = req.url.path
        requests.append((req.method, path))
        table = workflows if path == "/api/workflows" else agents if path == "/api/agents" else None
        if table is not None:
            if req.method == "GET":
                return httpx.Response(200, json=list(table.values()))
            value = json.loads(req.content)
            table[value["id"]] = value
            return httpx.Response(200, json=value)
        if path == "/api/rules":
            return httpx.Response(200, json=[{"id": "org-policy", "workflowIds": [source["id"]]}])
        if path == "/api/runs/run-effort":
            return httpx.Response(200, json={"id": "run-effort", "status": "succeeded",
                "artifacts": [{"name": "vernal-result.json", "content": '{"ok":true}'}]})
        return httpx.Response(200, json={})
    client = LoopForgeClient("http://lf.test", transport=httpx.MockTransport(handler))
    harness = LoopForgeHarness(eng.settings, eng.db, eng.ws, client=client, poll_interval=0)
    monkeypatch.setattr("autodev.public_pool.resolve", AsyncMock(return_value=("local_runner", client)))
    monkeypatch.setattr("autodev.public_pool.check_workflow_sandbox", AsyncMock())
    monkeypatch.setattr(harness, "_ensure_ready", AsyncMock(return_value=(pid, source["id"])))
    dispatch = AsyncMock(return_value=("issue-effort", "run-effort"))
    monkeypatch.setattr(harness, "_dispatch_plan", dispatch)
    schema = eng.ws.state_dir / "effort.schema.json"
    schema.write_text('{"type":"object","properties":{"ok":{"type":"boolean"}}}')
    log_dir = eng.ws.runs_dir / "effort"
    async def execute():
        token = adapter.PLANNING_BASELINE.set({"step": step, "risk": risk})
        try:
            return await harness.run_structured_readonly("核验本轮产物", schema_path=schema, cwd=eng.ws.root, log_dir=log_dir, role=role)
        finally:
            adapter.PLANNING_BASELINE.reset(token)
    result = asyncio.run(execute())
    assert result.ok, result.error
    sent_wf = dispatch.call_args.args[5]
    selected = workflows[sent_wf]
    planner_id = next(n["agentId"] for n in selected["nodes"] if n["id"] == "draft")
    assert agents[planner_id]["effort"] == expected
    assert any(n["id"] == "custom_signoff" for n in selected["nodes"])
    assert workflows[source["id"]] == source
    assert all(agents[k] == v for k, v in original_agents.items())
    log = json.loads((log_dir / "loopforge_run.json").read_text())
    assert (log["effort"], log["workflow_id"], log["step"], log["risk"]) == (expected, sent_wf, step, risk)
    if sent_wf != source["id"]:
        assert "org-policy" in agents[planner_id]["rules"]
        writes = [r for r in requests if r[0] != "GET"]
        assert asyncio.run(harness._effort_workflow(client, pid, source["id"], role, expected)) == sent_wf
        assert [r for r in requests if r[0] != "GET"] == writes
        # Adding a gate in the source SOP after the first run must affect later runs.
        workflows[source["id"]]["nodes"].append({"id": "new_gate", "type": "approval"})
        changed_wf = asyncio.run(harness._effort_workflow(client, pid, source["id"], role, expected))
        assert changed_wf != sent_wf
        assert any(n["id"] == "new_gate" for n in workflows[changed_wf]["nodes"])
        workflows[source["id"]]["status"] = "draft"
        writes_before = [r for r in requests if r[0] != "GET"]
        with pytest.raises(LoopForgeError, match="尚未发布"):
            asyncio.run(harness._effort_workflow(client, pid, source["id"], role, expected))
        assert [r for r in requests if r[0] != "GET"] == writes_before


def test_legacy_document_fingerprints_survive_reopen_but_not_prompt_change(eng, monkeypatch):
    prepared(eng)
    for kind in ("plan", "tasks", "analyze"):
        rev = service.latest(eng, "001-save", kind)
        eng.db._exec("UPDATE spec_revisions SET input_sha=? WHERE rev_id=?", (
            service.input_fingerprint(eng, "001-save", kind, legacy=True), rev["rev_id"]))
    assert compiler.compile_tasks(eng, "001-save")
    monkeypatch.setattr(assets, "PROMPT_ASSEMBLY_VERSION", "new-methodology")
    with pytest.raises(service.SpecConflictError, match="旧规格"):
        compiler.compile_tasks(eng, "001-save")
