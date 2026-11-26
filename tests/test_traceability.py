"""WP-6 测试：SRS/PRD 需求解析、@pytest.mark.req 扫描、断链检测、CSV/DOCX 导出与 trace_coverage P0 合入门禁阻断。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from autodev.artifacts import FileExchange
from autodev.docs_center.exporter import inspect_docx_structure
from autodev.engine import Engine, OpsError, init_workspace
from autodev.harnesses.mock import MockHarness
from autodev.models import MilestoneStatus, PlanMilestone, PlanModule, PlanTask, ProjectPlan, TaskStatus
from autodev.process.model import GateDef
from autodev.process.runner import load_builtin_template, save_custom_template
from autodev.server.app import create_app


def _offline_tp() -> httpx.MockTransport:
    return httpx.MockTransport(lambda req: httpx.Response(200, json={"ok": True}))


def test_traceability_matrix_broken_links_export_and_p0_merge_gate(tmp_path: Path) -> None:
    ws = init_workspace(tmp_path / "trace-ws", name="TraceDemo", collaboration_mode="files")
    (ws.root / "docs").mkdir(parents=True, exist_ok=True)
    (ws.root / "docs" / "SRS.md").write_text(
        "# 软件需求规格说明书\n\n"
        "| 需求编号 | 优先级 | 需求标题 | 验收标准 |\n"
        "|---|---|---|---|\n"
        "| REQ-001 | P0 | 创建订单 | 校验库存并返回订单号 |\n"
        "| REQ-002 | P0 | 扣减库存 | 并发下不超卖 |\n"
        "| REQ-003 | P1 | 导出报表 | 支持 CSV 下载 |\n",
        "utf-8",
    )
    design_dir = ws.state_dir / "design"
    design_dir.mkdir(parents=True, exist_ok=True)
    (design_dir / "order.json").write_text(
        json.dumps(
            {
                "module": "order",
                "interfaces": [{"name": "create_order", "req_ids": ["REQ-001", "REQ-002"]}],
                "test_cases": [{"id": "TC-001", "req_id": "REQ-001"}],
            },
            ensure_ascii=False,
        ),
        "utf-8",
    )

    eng = Engine(ws, harness=MockHarness())
    try:
        # 启用阻塞式 trace_coverage 门禁
        tpl = load_builtin_template("feature")
        tpl.review_policy = "off"
        for s in tpl.stages:
            if s.id == "integration_delivery":
                s.gates = [g for g in s.gates if g.kind != "ci"]
                s.gates.append(GateDef(id="req_trace_p0", name="P0 需求测试覆盖门禁", kind="trace_coverage", scope="milestone", blocking=True))
        save_custom_template(ws, json.dumps(tpl.model_dump(), ensure_ascii=False))

        plan = ProjectPlan(
            summary="追溯矩阵测试",
            tech_stack="python",
            agents_md="r",
            modules=[PlanModule(name="order", path="order/", description="order")],
            milestones=[PlanMilestone(milestone_id="M1", title="M1", goal="交付订单", integration_cmd="true")],
            tasks=[
                PlanTask(
                    task_id="T-001",
                    milestone_id="M1",
                    module="order",
                    role="coder",
                    title="实现订单与库存",
                    description="覆盖 REQ-001 与 REQ-002",
                    acceptance_criteria=["ok"],
                    depends_on=[],
                    doc_slice_ids=[],
                    allowed_paths=["order/", "tests/"],
                    verify_cmds=["pytest -q"],
                    requirement_ids=["REQ-001", "REQ-002"],
                    priority=10,
                )
            ],
        )
        eng.db.save_plan(plan, max_retries=2)

        # 第一阶段：仅给 REQ-001 写了 @pytest.mark.req("REQ-001") 测试，REQ-002 (P0) 缺测试断链！
        ex = FileExchange(ws, eng.db)
        key1 = ex.put(
            {
                "order/service.py": "def create_order():\n    return 'ORD-1'\n",
                "tests/test_order.py": (
                    "import pytest\n\n"
                    '@pytest.mark.req("REQ-001")\n'
                    "def test_create_order():\n"
                    "    assert True\n"
                ),
            }
        )
        eng.db.update_task("T-001", status=TaskStatus.APPROVED, artifact_digest=key1)
        eng.db.meta_set("delivery:M1", key1)
        eng.db.meta_set("candidate_ref:M1", key1)
        eng.db.update_milestone("M1", status=MilestoneStatus.AWAITING_MERGE)

        client = TestClient(create_app(eng, proxy_transport=_offline_tp()))
        m1 = client.get("/vernal-api/trace/matrix").json()
        assert m1["summary"]["p0_total"] == 2
        assert m1["summary"]["p0_passed"] == 1
        assert m1["summary"]["p0_coverage_rate"] == 0.5
        by_req = {r["req_id"]: r for r in m1["rows"]}
        assert by_req["REQ-001"]["verify_status"] == "PASSED"
        assert "missing_test" in by_req["REQ-002"]["broken_links"]
        assert "missing_design" in by_req["REQ-003"]["broken_links"]

        # P0 需求 REQ-002 缺少通过测试，merge_milestone 必须被 trace_coverage 门禁阻断！
        with pytest.raises(OpsError, match="REQ-002"):
            eng.merge_milestone("M1", actor="lead")

        # 导出 CSV 与 DOCX 均可正常读取
        csv_res = client.get("/vernal-api/trace/export?format=csv")
        assert csv_res.status_code == 200 and "REQ-001" in csv_res.text
        docx_res = client.get("/vernal-api/trace/export?format=docx")
        assert docx_res.status_code == 200
        docx_info = inspect_docx_structure(docx_res.content)
        assert any("追溯" in txt for _, txt in docx_info["headings"])
        assert int(docx_info["table_count"]) >= 1

        # 第二阶段：补齐 REQ-002 测试后，P0 覆盖率达到 100%，允许合入！
        key2 = ex.put(
            {
                "order/service.py": "def create_order():\n    return 'ORD-1'\n",
                "tests/test_order.py": (
                    "import pytest\n\n"
                    '@pytest.mark.req("REQ-001")\n'
                    "def test_create_order():\n"
                    "    assert 1 == 1\n\n"
                    "def test_deduct_stock():\n"
                    "    # covers REQ-002\n"
                    "    assert 2 == 2\n"
                ),
            }
        )
        eng.db.update_task("T-001", status=TaskStatus.APPROVED, artifact_digest=key2)
        eng.db.meta_set("delivery:M1", key2)
        eng.db.meta_set("candidate_ref:M1", key2)

        m2 = client.get("/vernal-api/trace/matrix").json()
        assert m2["summary"]["p0_coverage_rate"] == 1.0
        eng.merge_milestone("M1", actor="lead")
        assert eng.db.get_milestone("M1").status == MilestoneStatus.MERGED
    finally:
        eng.db.close()
