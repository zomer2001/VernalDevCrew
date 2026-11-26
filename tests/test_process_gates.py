"""WP-0 测试：流程阶段门禁引擎、模板优先级、阶段状态推导表与调度阻塞钩子。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from autodev.engine import Engine, OpsError, init_workspace
from autodev.docs_center.service import save_doc_version
from autodev.harnesses.mock import MockHarness
from autodev.models import PlanMilestone, PlanModule, PlanTask, ProjectPlan, ProjectStatus, TaskStatus
from autodev.process.runner import decide_gate, ensure_gate_record, load_template, save_custom_template, stage_status
from autodev.scheduler import Scheduler
from autodev.server.app import create_app


def _offline_transport() -> httpx.MockTransport:
    return httpx.MockTransport(lambda req: httpx.Response(200, json={"ok": True}))


def test_template_loading_priority_and_legacy_default(tmp_path: Path) -> None:
    ws = init_workspace(tmp_path / "proc-prio", name="ProcPrio", collaboration_mode="files")
    eng = Engine(ws, harness=MockHarness())
    try:
        # 1. 默认 legacy
        tpl0 = load_template(ws, eng.settings)
        assert tpl0.id == "legacy"

        # 2. config.toml / settings 指定 standard
        eng.settings.process_template = "standard"
        tpl1 = load_template(ws, eng.settings)
        assert tpl1.id == "standard"
        assert tpl1.review_policy == "risk"

        # 3. .autodev/process.yaml 优先级最高
        custom_raw = json.dumps(
            {
                "id": "custom_lab",
                "name": "实验室定制流程",
                "description": "定制门禁",
                "review_policy": "all",
                "stages": [s.model_dump() for s in tpl1.stages],
            },
            ensure_ascii=False,
        )
        save_custom_template(ws, custom_raw)
        tpl2 = load_template(ws, eng.settings)
        assert tpl2.id == "custom_lab"
        assert tpl2.review_policy == "all"
    finally:
        eng.db.close()


def test_gate_decide_idempotency_round_conflict_and_waive_validation(tmp_path: Path) -> None:
    ws = init_workspace(tmp_path / "gate-api", name="GateAPI", collaboration_mode="files")
    eng = Engine(ws, harness=MockHarness())
    try:
        eng.settings.process_template = "standard"
        save_doc_version(eng, "SRS.md", "# Reviewed specification")
        rec = ensure_gate_record(
            eng.db,
            stage_id="requirements",
            gate_id="srs_approval",
            kind="human_approval",
            scope_type="project",
            scope_id="project",
            summary="SRS 待评审",
        )
        rid = int(rec["record_id"])
        client = TestClient(create_app(eng, proxy_transport=_offline_transport()))

        # 1. waive 无理由返回 422
        bad_waive = client.post(f"/vernal-api/gates/{rid}/decide", json={"decision": "waive", "comment": "   "})
        assert bad_waive.status_code == 422

        # 2. expected_round 不匹配返回 409
        bad_round = client.post(
            f"/vernal-api/gates/{rid}/decide",
            json={"decision": "pass", "comment": "ok", "expected_round": 99},
        )
        assert bad_round.status_code == 409

        # 3. 正常批准 + 幂等重放返回 200，冲突改判 fail 返回 409
        ok1 = client.post(
            f"/vernal-api/gates/{rid}/decide",
            json={"decision": "pass", "comment": "评审通过", "expected_round": 1},
        )
        assert ok1.status_code == 200
        assert ok1.json()["status"] == "PASSED"

        ok2 = client.post(
            f"/vernal-api/gates/{rid}/decide",
            json={"decision": "pass", "comment": "重复点击", "expected_round": 1},
        )
        assert ok2.status_code == 200

        conflict = client.post(
            f"/vernal-api/gates/{rid}/decide",
            json={"decision": "fail", "comment": "反悔"},
        )
        assert conflict.status_code == 409
    finally:
        eng.db.close()


def test_standard_template_srs_and_detailed_design_hooks_and_stage_derivation(tmp_path: Path) -> None:
    def coder_fn(_p: str, cwd: Path, _c: object) -> dict[str, object]:
        (cwd / "core").mkdir(parents=True, exist_ok=True)
        (cwd / "core" / "svc.py").write_text("VALUE = 1\n", "utf-8")
        return {"status": "DONE", "summary": "done", "files_changed": ["core/svc.py"], "tests_added": [], "notes": ""}

    ws = init_workspace(tmp_path / "std-flow", name="StdFlow", collaboration_mode="files")
    eng = Engine(ws, harness=MockHarness(coder=coder_fn))
    try:
        eng.settings.runtime_backend = "local"
        eng.settings.require_merge_approval = True
        eng.settings.process_template = "standard"
        # 关闭 quality 门以便纯粹测试 WP-0 H1/H2/H3 阶段流转
        tpl = load_template(ws, eng.settings)
        for s in tpl.stages:
            if s.id in ("testing", "integration_delivery"):
                s.gates = [g for g in s.gates if g.kind not in ("quality", "ci")]
        tpl.review_policy = "off"
        save_custom_template(ws, json.dumps(tpl.model_dump(), ensure_ascii=False))

        # 初始推导：NOT_STARTED / SKIPPED 检查
        views0 = {v.id: v.status for v in stage_status(eng.db, ws, tpl)}
        assert views0["requirements"] == "NOT_STARTED"
        assert views0["detailed_design"] == "NOT_STARTED"

        plan = ProjectPlan(
            summary="标准流程测试",
            tech_stack="python",
            agents_md="rule",
            modules=[PlanModule(name="core", path="core/", description="core")],
            milestones=[PlanMilestone(milestone_id="M1", title="M1", goal="交付", integration_cmd="test -f core/svc.py")],
            tasks=[
                PlanTask(
                    task_id="T-001",
                    milestone_id="M1",
                    module="core",
                    role="coder",
                    title="实现服务",
                    description="写 svc.py",
                    acceptance_criteria=["存在 svc.py"],
                    depends_on=[],
                    doc_slice_ids=[],
                    allowed_paths=["core/"],
                    verify_cmds=["test -f core/svc.py"],
                    priority=10,
                )
            ],
        )
        eng.db.save_plan(plan, max_retries=2)
        eng.set_status(ProjectStatus.PLANNED)
        save_doc_version(eng, "SRS.md", "# Reviewed requirements")
        save_doc_version(eng, "ARCHITECTURE.md", "# Reviewed architecture")
        eng._sync_planning_gates()

        # H1：SRS 门未通过时 approve_plan 拒绝
        with pytest.raises(OpsError, match="SRS"):
            eng.approve_plan(actor="lead")

        srs_rec = ensure_gate_record(
            eng.db,
            stage_id="requirements",
            gate_id="srs_approval",
            kind="human_approval",
            scope_type="project",
            scope_id="project",
        )
        decide_gate(eng.db, int(srs_rec["record_id"]), "pass", actor="lead", comment="SRS 确认")

        # H2：SRS 通过后 approve_plan 放行并自动通过架构门
        eng.approve_plan(actor="lead")
        assert eng.status == ProjectStatus.APPROVED

        # H3：详细设计门未通过时，Scheduler 暂缓派发 coder 任务
        sched = Scheduler(eng)
        sched._dispatch_ready("M1")
        assert eng.db.get_task("T-001").status == TaskStatus.READY
        assert not sched.running_tasks

        views_wait = {v.id: v.status for v in stage_status(eng.db, ws, tpl)}
        assert views_wait["detailed_design"] == "WAITING_GATE"

        # 批准 M1 详细设计门后，Scheduler 正常派单完成编码
        save_doc_version(eng, "design/M1/core.md", "# Reviewed detailed design")
        dd_rec = ensure_gate_record(
            eng.db,
            stage_id="detailed_design",
            gate_id="detailed_design_approval",
            kind="human_approval",
            scope_type="milestone",
            scope_id="M1",
        )
        decide_gate(eng.db, int(dd_rec["record_id"]), "pass", actor="architect", comment="详设通过")
        asyncio.run(sched.run())

        assert eng.db.get_task("T-001").status == TaskStatus.APPROVED
        views_after = {v.id: v.status for v in stage_status(eng.db, ws, tpl)}
        assert views_after["detailed_design"] == "PASSED"
        assert views_after["implementation"] == "PASSED"
        assert views_after["integration_delivery"] == "WAITING_GATE"
    finally:
        eng.db.close()


def test_r3_distinguishes_content_currency_from_preconditions_and_blocks_bypass(tmp_path: Path) -> None:
    """R3: checklist running, open questions, critical findings, missing metadata, prerequisites, RBAC, multi-feature."""
    from autodev.config import Settings
    from autodev.db import Database
    from autodev.governance.rbac import upsert_member
    from autodev.models import ClarificationItem
    from autodev.process.runner import latest_gate_record, list_gate_records
    from autodev.speckit import missions, service, triage
    from test_speckit import BRIEF, SPEC, save

    ws = init_workspace(tmp_path / "r3-gates", name="R3Gates", collaboration_mode="files")
    settings = Settings(
        collaboration_mode="files",
        runtime_backend="loopforge",
        loopforge_project_id="r3-proj",
        loopforge_token="tok",
        loopforge_url="http://lf.test",
    )
    eng = Engine(ws, settings=settings, harness=MockHarness())
    try:
        service.enable(eng, "vernal-core")
        save(eng, "constitution", "# 项目宪章\n交付须有验收依据。\n")
        mission = missions.save(eng, "001-alpha", "保存回答功能", "feature", 0, actor="owner")
        save(eng, "spec", SPEC, "001-alpha")
        assessment = triage.Assessment(
            goal_clear=True,
            scope_clear=True,
            acceptance_clear=True,
            local_impact=True,
            change_type="feature",
            risk="low",
            rationale="目标清晰",
            questions=[],
            prerequisites=[],
            experience_items=[],
            proposed_spec=SPEC,
            handoff_brief=BRIEF,
        )
        mission.update(
            assessment=assessment.model_dump(),
            route=triage.recommendation(assessment, "greenfield"),
            assessed_input=triage.input_key(eng, mission),
        )
        missions._store(eng, mission)

        # 1. Checklist running on the SAME spec -> status stays PENDING, content_current=True, can_approve=False, CHECKLIST_RUNNING
        token = missions.reserve(eng, "001-alpha", mission["revision"])
        missions._update_lease_step(eng, token, "checklist")
        gate_before_rounds = len(list_gate_records(eng.db, gate_id="srs_approval"))
        client = TestClient(create_app(eng, proxy_transport=_offline_transport()))
        gates_running = client.get("/vernal-api/gates").json()
        srs = [g for g in gates_running if g["gate_id"] == "srs_approval"][-1]
        assert srs["status"] == "PENDING"
        assert srs["content_current"] is True
        assert srs["ready_for_decision"] is False
        assert srs["can_approve"] is False
        assert any(b["code"] == "CHECKLIST_RUNNING" for b in srs["blockers"])
        # Read-only status query must not create extra rounds
        assert len(list_gate_records(eng.db, gate_id="srs_approval")) == gate_before_rounds
        # Direct HTTP POST while checklist running must be rejected with 409
        r_running = client.post(
            f"/vernal-api/gates/{srs['record_id']}/decide",
            json={"decision": "pass", "expected_round": srs["round"], "expected_sha": srs["target_sha"]},
        )
        assert r_running.status_code == 409

        # 2. Release lease; checklist exists with MISSING findings metadata -> cannot be treated as no issues
        missions.release(eng, token)
        check = save(eng, "checklist", "# 质量检查\n- [ ] CHK001 验收可测\n", "001-alpha")
        assert len(list_gate_records(eng.db, gate_id="srs_approval")) == gate_before_rounds
        srs_missing_meta = [g for g in client.get("/vernal-api/gates").json() if g["gate_id"] == "srs_approval"][-1]
        assert srs_missing_meta["status"] == "PENDING" and srs_missing_meta["content_current"] is True
        assert any(b["code"] == "CHECKLIST_FINDINGS_MISSING" for b in srs_missing_meta["blockers"])
        assert client.post(
            f"/vernal-api/gates/{srs_missing_meta['record_id']}/decide",
            json={"decision": "pass", "expected_round": srs_missing_meta["round"], "expected_sha": srs_missing_meta["target_sha"]},
        ).status_code == 409

        # 3. CRITICAL finding -> still PENDING + content_current=True, blocked by CHECKLIST_CRITICAL_FINDINGS, direct POST 409
        eng.db.meta_set(
            f"speckit.analysis:{check['rev_id']}",
            json.dumps([{"severity": "CRITICAL", "message": "缺少核心验收", "requirement_ids": ["REQ-001"]}], ensure_ascii=False),
        )
        srs_crit = [g for g in client.get("/vernal-api/gates").json() if g["gate_id"] == "srs_approval"][-1]
        assert srs_crit["status"] == "PENDING" and srs_crit["content_current"] is True and srs_crit["can_approve"] is False
        assert any(b["code"] == "CHECKLIST_CRITICAL_FINDINGS" for b in srs_crit["blockers"])
        assert client.post(
            f"/vernal-api/gates/{srs_crit['record_id']}/decide",
            json={"decision": "pass", "expected_round": srs_crit["round"], "expected_sha": srs_crit["target_sha"]},
        ).status_code == 409

        # 4. Warning-only findings + multi-feature: second feature with OPEN question blocks project approval and names the feature
        eng.db.meta_set(
            f"speckit.analysis:{check['rev_id']}",
            json.dumps([{"severity": "WARNING", "message": "建议补充提示", "requirement_ids": ["REQ-001"]}], ensure_ascii=False),
        )
        rev2 = save(eng, "spec", SPEC, "002-beta")
        service.add_clarifications(
            eng,
            "002-beta",
            rev2["rev_id"],
            [ClarificationItem(question_key="Q-01", category="scope", severity="BLOCKING", question="是否导出？", options=["是", "否"])],
            actor="owner",
        )
        srs_multi = [g for g in client.get("/vernal-api/gates").json() if g["gate_id"] == "srs_approval"][-1]
        assert srs_multi["status"] == "PENDING" and srs_multi["content_current"] is True and srs_multi["can_approve"] is False
        assert any(b["code"] == "OPEN_QUESTIONS" and b["feature_id"] == "002-beta" for b in srs_multi["blockers"])

        # Answer 002-beta question -> now material is ready, test RBAC viewer permission overlay
        q_beta = service.list_clarifications(eng, "002-beta")[0]
        service.answer_clarification(eng, q_beta["clarify_id"], "否", expected_rev_id=rev2["rev_id"], actor="owner")
        upsert_member(eng.db, "owner", "viewer", project="r3-proj")
        srs_viewer = [g for g in client.get("/vernal-api/gates").json() if g["gate_id"] == "srs_approval"][-1]
        assert srs_viewer["status"] == "PENDING" and srs_viewer["content_current"] is True
        assert srs_viewer["ready_for_decision"] is True
        assert srs_viewer["can_approve"] is False
        assert any(b["code"] == "PERMISSION_DENIED" for b in srs_viewer["blockers"])
        assert client.post(
            f"/vernal-api/gates/{srs_viewer['record_id']}/decide",
            json={"decision": "pass", "expected_round": srs_viewer["round"], "expected_sha": srs_viewer["target_sha"]},
        ).status_code == 403

        # Grant admin role -> same round succeeds without regenerating documents, and persists across DB reopen
        upsert_member(eng.db, "owner", "admin", project="r3-proj")
        srs_ready = [g for g in client.get("/vernal-api/gates").json() if g["gate_id"] == "srs_approval"][-1]
        assert srs_ready["can_approve"] is True and srs_ready["can_waive"] is True
        ok_resp = client.post(
            f"/vernal-api/gates/{srs_ready['record_id']}/decide",
            json={"decision": "pass", "comment": "审阅通过", "expected_round": srs_ready["round"], "expected_sha": srs_ready["target_sha"]},
        )
        assert ok_resp.status_code == 200
        reopened = Database(eng.ws.db_path)
        try:
            rec_reopen = latest_gate_record(reopened, "srs_approval", "project", "project")
            assert rec_reopen["status"] == "PASSED" and rec_reopen["approval_valid"] is True
        finally:
            reopened.close()

        # External file edit invalidates approval and rejects stale approval submission
        (eng.ws.root / "specs/001-alpha/spec.md").write_text("# tampered externally\n", "utf-8")
        srs_drift = [g for g in client.get("/vernal-api/gates").json() if g["gate_id"] == "srs_approval"][-1]
        assert srs_drift["status"] == "STALE" and srs_drift["content_current"] is False
        assert any(b["code"] == "FILE_MISSING_OR_DRIFTED" for b in srs_drift["blockers"])
    finally:
        eng.db.close()
