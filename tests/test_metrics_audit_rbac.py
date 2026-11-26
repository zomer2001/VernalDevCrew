"""WP-7 测试：效能度量计算、防篡改审计哈希链篡改检测、RBAC 越权与职责分离（SoD）、internal 密级阻断外网模型及预算超限暂停调度。"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from autodev.engine import Engine, init_workspace
from autodev.governance.audit import platform_db_path_for
from autodev.governance.gateway import scrub_sensitive_text
from autodev.harnesses.mock import MockHarness
from autodev.models import PlanMilestone, PlanModule, PlanTask, ProjectPlan, ProjectStatus, TaskStatus
from autodev.process.runner import ensure_gate_record
from autodev.review.service import open_review
from autodev.scheduler import Scheduler
from autodev.server.app import create_app


def _offline_tp() -> httpx.MockTransport:
    return httpx.MockTransport(lambda req: httpx.Response(200, json={"ok": True}))


def test_metrics_audit_hash_chain_tamper_detection_rbac_sod_and_model_governance(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("VERNAL_POOL_DIR", str(tmp_path / "pool"))
    monkeypatch.setenv("VERNAL_POOL_TOKEN", "pool-svc-token")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-only")

    workspaces_dir = tmp_path / "workspaces"
    ws = init_workspace(workspaces_dir / "gov-proj", name="GovProj", collaboration_mode="files")
    eng = Engine(ws, harness=MockHarness())
    try:
        eng.settings.loopforge_project_id = "gov-proj"
        plan = ProjectPlan(
            summary="治理全链路测试",
            tech_stack="python",
            agents_md="rule",
            modules=[PlanModule(name="m", path="a.py", description="a")],
            milestones=[PlanMilestone(milestone_id="M1", title="M1", goal="g", integration_cmd="true")],
            tasks=[
                PlanTask(
                    task_id="T-001",
                    milestone_id="M1",
                    module="m",
                    role="coder",
                    title="t1",
                    description="d",
                    acceptance_criteria=["ok"],
                    depends_on=[],
                    doc_slice_ids=[],
                    allowed_paths=["a.py"],
                    verify_cmds=["true"],
                    priority=10,
                ),
                PlanTask(
                    task_id="T-002",
                    milestone_id="M1",
                    module="m",
                    role="coder",
                    title="t2",
                    description="d2",
                    acceptance_criteria=["ok"],
                    depends_on=[],
                    doc_slice_ids=[],
                    allowed_paths=["b.py"],
                    verify_cmds=["true"],
                    priority=20,
                ),
            ],
        )
        eng.db.save_plan(plan, max_retries=3)

        # 造一组任务运行记录验证效能指标计算
        r1 = eng.db.start_run("T-001", 1, "CODE", "codex", "Coder")
        eng.db.finish_run(r1, exit_code=0, input_tokens=5000, output_tokens=2000, verdict="OK")
        eng.db.update_task("T-001", status=TaskStatus.APPROVED, owner_member="dev-alice")

        client = TestClient(create_app(eng, proxy_transport=_offline_tp()))

        # 1. 度量看板与跨项目驾驶舱
        m = client.get("/vernal-api/metrics").json()
        assert m["tasks"]["total"] == 2
        assert m["tasks"]["completed"] == 1
        assert m["tasks"]["first_pass_ai_review_rate"] == 1.0
        assert m["tokens"]["total"] == 7000

        pf = client.get("/vernal-api/metrics/portfolio").json()
        assert pf["totals"]["project_count"] >= 1
        assert pf["totals"]["tokens_total"] == 7000

        # 2. RBAC：注册 admin / reviewer / viewer，校验越权 403 与职责分离（SoD）
        assert client.post("/vernal-api/rbac/members", json={"user_id": "boss-admin", "role": "admin", "actor": "boss-admin"}).status_code == 200
        assert client.post("/vernal-api/rbac/members", json={"user_id": "qa-bob", "role": "reviewer", "actor": "boss-admin"}).status_code == 200
        assert client.post("/vernal-api/rbac/members", json={"user_id": "dev-alice", "role": "reviewer", "actor": "boss-admin"}).status_code == 200
        assert client.post("/vernal-api/rbac/members", json={"user_id": "guest-view", "role": "viewer", "actor": "boss-admin"}).status_code == 200

        # viewer 尝试改成员或豁免门禁必须 403
        assert client.post("/vernal-api/rbac/members", json={"user_id": "x", "role": "admin", "actor": "guest-view"}).status_code == 403

        gate_rec = ensure_gate_record(
            eng.db,
            stage_id="architecture",
            gate_id="arch_signoff",
            kind="human_approval",
            scope_type="project",
            scope_id="project",
        )
        # qa-bob (reviewer) 尝试 waive 门禁（要求 admin）-> 403；boss-admin 批准 -> 200
        assert client.post(
            f"/vernal-api/gates/{gate_rec['record_id']}/decide",
            json={"decision": "waive", "comment": "紧急放行", "actor": "qa-bob"},
        ).status_code == 403
        assert client.post(
            f"/vernal-api/gates/{gate_rec['record_id']}/decide",
            json={"decision": "pass", "comment": "架构通过", "actor": "boss-admin"},
        ).status_code == 200

        # SoD 职责分离：T-001 的 owner_member 是 dev-alice，dev-alice 即使拥有 reviewer 角色也不能自审通过 T-001！
        from autodev.artifacts import FileExchange

        ex = FileExchange(ws, eng.db)
        k1 = ex.put({"a.py": "A = 1\n"})
        eng.db.update_task("T-001", status=TaskStatus.AWAITING_HUMAN_REVIEW, artifact_digest=k1, owner_member="dev-alice")
        rev = open_review(eng, scope_type="task", scope_id="T-001", milestone_id="M1", head_ref=k1)
        sod_res = client.post(
            f"/vernal-api/reviews/{rev['review']['review_id']}/decision",
            json={"decision": "approve", "actor": "dev-alice"},
        )
        assert sod_res.status_code == 403
        # 由独立审查人 qa-bob 审批则正常 200 通过
        ok_rev = client.post(
            f"/vernal-api/reviews/{rev['review']['review_id']}/decision",
            json={"decision": "approve", "actor": "qa-bob"},
        )
        assert ok_rev.status_code == 200

        # 3. 审计日志哈希链校验与防篡改检测
        ver_ok = client.get("/vernal-api/audit/verify").json()
        assert ver_ok["valid"] is True and ver_ok["checked_count"] >= 5

        audit_csv = client.get("/vernal-api/audit?format=csv")
        assert audit_csv.status_code == 200 and "rbac.member_upserted" in audit_csv.text

        # 直接在底层 SQLite 篡改第 2 条审计记录的 actor 字段，验证 verify 精准捕获篡改行 ID！
        pdb = platform_db_path_for(workspaces_dir)
        conn = sqlite3.connect(str(pdb))
        try:
            conn.execute("UPDATE audit_log SET actor='attacker' WHERE id=2")
            conn.commit()
        finally:
            conn.close()

        ver_bad = client.get("/vernal-api/audit/verify").json()
        assert ver_bad["valid"] is False
        assert ver_bad["tampered_id"] == 2

        # 4. 模型安全网关：设置 data_classification='internal' 后调用公网 DeepSeek 端点被 403 阻断
        assert (
            client.post(
                "/vernal-api/governance/settings",
                json={"data_classification": "internal", "token_budget_limit": 5000, "actor": "boss-admin"},
            ).status_code
            == 200
        )
        from autodev.public_pool import model_ticket
        import time
        ticket = model_ticket("gov-proj", "deepseek", int(time.time()) + 60)
        blocked_gw = client.get("/vernal-api/pool-model/gov-proj/deepseek/v1/models",
                                headers={"Authorization": "Bearer " + ticket})
        assert blocked_gw.status_code == 403
        assert "internal" in blocked_gw.json()["detail"]

        # 敏感密钥脱敏函数校验
        assert "[REDACTED_SECRET]" in scrub_sensitive_text("token is sk-1234567890abcdefghijklmnop")

        # 5. Token 预算超限（已消耗 7000 >= 上限 5000）自动暂停调度器派发 T-002
        eng.settings.runtime_backend = "local"
        eng.set_status(ProjectStatus.APPROVED)
        sched = Scheduler(eng)
        asyncio.run(sched.run())
        assert eng.db.get_task("T-002").status == TaskStatus.READY
        events = eng.db.events_after(0)
        assert any(e["event_type"] == "governance.budget_exceeded" for e in events)
    finally:
        eng.db.close()
