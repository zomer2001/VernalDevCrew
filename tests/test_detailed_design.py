"""WP-1 测试：规范文档模板渲染、旧版 PRD 兼容、详细设计确定性校验器、design_refs 注入、版本 Diff 与 .docx 导出。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

from autodev.assets import load_builtin_skills
from autodev.docs_center.exporter import inspect_docx_structure, markdown_to_docx_bytes
from autodev.docs_center.service import generate_detailed_design, save_doc_version
from autodev.docs_center.templates import render_detailed_design_markdown, render_srs_markdown
from autodev.docs_center.validator import validate_detailed_design
from autodev.engine import Engine, init_workspace
from autodev.harnesses.mock import MockCall, MockHarness
from autodev.models import (
    DesignInterface,
    DesignTestCase,
    DetailedDesign,
    PlanMilestone,
    PlanModule,
    PlanTask,
    ProductSpec,
    ProjectPlan,
    ProjectStatus,
    RequirementItem,
    TaskStatus,
)
from autodev.process.runner import decide_gate, ensure_gate_record, load_builtin_template, save_custom_template
from autodev.prompts import build_coder_prompt, build_review_prompt
from autodev.scheduler import Scheduler
from autodev.server.app import create_app


def _offline_proxy() -> httpx.MockTransport:
    return httpx.MockTransport(lambda req: httpx.Response(200, json={"ok": True}))


def test_builtin_spec_writing_skill_and_legacy_prd_compatibility() -> None:
    slugs = {s["slug"] for s in load_builtin_skills()}
    assert "spec-writing" in slugs

    # 旧版 6 字段 prd_spec.json 仍可无损反序列化
    legacy_payload = {
        "product_vision": "轻量订单系统",
        "core_workflows": ["下单 -> 扣库存"],
        "edge_cases": ["并发超卖"],
        "error_conventions": ["返回 409"],
        "global_acceptance_redlines": ["单测必过"],
        "spec_markdown": "# Legacy PRD\n",
    }
    spec = ProductSpec.model_validate(legacy_payload)
    assert spec.requirements == []
    md_gbt = render_srs_markdown(spec, profile="gbt8567", project_name="订单中心")
    assert "GB/T 8567" in md_gbt
    assert "订单中心" in md_gbt


def test_detailed_design_validator_rules() -> None:
    known_reqs = [
        RequirementItem(req_id="REQ-001", title="创建订单", statement="当提交合法订单时，系统应持久化订单。", priority="P0", acceptance=["201"]),
        RequirementItem(req_id="REQ-002", title="取消订单", statement="当取消未支付订单时，系统应释放库存。", priority="P0", acceptance=["200"]),
    ]

    # 违规：悬空 REQ-999、遗漏 P0 REQ-002、接口无 TC、Mermaid 非法头
    bad_dd = DetailedDesign(
        squad_id="sq-order",
        milestone_id="M1",
        responsibilities=["订单管理"],
        interfaces=[
            DesignInterface(
                name="createOrder",
                signature="POST /orders",
                inputs="OrderReq",
                outputs="OrderResp",
                errors=["409_OUT_OF_STOCK"],
                req_ids=["REQ-999"],
            )
        ],
        data_models=["Order(id, status)"],
        sequences_mermaid=["invalidDiagram\nA -> B"],
        error_handling=["事务回滚"],
        test_cases=[
            DesignTestCase(tc_id="TC-001", req_ids=["REQ-001"], kind="unit", description="测试库存校验")
        ],
        design_markdown="",
    )
    errs = validate_detailed_design(bad_dd, known_reqs)
    joined = "\n".join(errs)
    assert "REQ-999" in joined
    assert "REQ-002" in joined
    assert "createOrder" in joined
    assert "Mermaid" in joined

    # 合规详设：覆盖全部 P0 REQ，每个接口均有 TC，Mermaid 合法
    good_dd = DetailedDesign(
        squad_id="sq-order",
        milestone_id="M1",
        responsibilities=["订单与库存事务"],
        interfaces=[
            DesignInterface(
                name="createOrder",
                signature="POST /orders",
                inputs="OrderReq",
                outputs="OrderResp",
                errors=["409"],
                req_ids=["REQ-001"],
            ),
            DesignInterface(
                name="cancelOrder",
                signature="POST /orders/{id}/cancel",
                inputs="order_id",
                outputs="OrderResp",
                errors=["404"],
                req_ids=["REQ-002"],
            ),
        ],
        data_models=["Order(id, status)"],
        sequences_mermaid=['sequenceDiagram\n  Client->>OrderSvc: "createOrder()"'],
        error_handling=["幂等键去重"],
        test_cases=[
            DesignTestCase(tc_id="TC-001", req_ids=["REQ-001"], kind="unit", description="验证 createOrder 正常下单"),
            DesignTestCase(tc_id="TC-002", req_ids=["REQ-002"], kind="integration", description="验证 cancelOrder 释放库存"),
        ],
        design_markdown="补充说明",
    )
    assert validate_detailed_design(good_dd, known_reqs) == []

    # 验证 .docx 导出标题层级与表格可被标准 OOXML 解析器重新读取
    rendered = render_detailed_design_markdown(good_dd, profile="default")
    docx_bytes = markdown_to_docx_bytes(rendered, lab_name="智能软件实验室", classification="内部", version_label="v1.2")
    info = inspect_docx_structure(docx_bytes)
    heading_levels = [lvl for lvl, _ in info["headings"]]  # type: ignore[index]
    assert "Heading1" in heading_levels and "Heading2" in heading_levels and "Heading3" in heading_levels
    assert int(info["table_count"]) >= 1  # type: ignore[arg-type]


def test_docs_center_versions_diff_design_refs_prompt_injection_and_test_report(tmp_path: Path) -> None:
    arch_calls = 0

    def architect_fn(_p: str, _cwd: Path, _call: MockCall) -> dict[str, Any]:
        nonlocal arch_calls
        arch_calls += 1
        # 第 1 次故意漏掉 TC 触发校验返工，第 2 次返回合规详设
        if arch_calls == 1:
            return {
                "squad_id": "order",
                "milestone_id": "M1",
                "responsibilities": ["订单模块"],
                "interfaces": [
                    {"name": "createOrder", "signature": "def create_order()", "inputs": "dto", "outputs": "Order", "errors": [], "req_ids": ["REQ-001"]}
                ],
                "data_models": ["Order"],
                "sequences_mermaid": ["sequenceDiagram\n  A->>B: ok"],
                "error_handling": ["retry"],
                "test_cases": [],
                "design_markdown": "",
            }
        return {
            "squad_id": "order",
            "milestone_id": "M1",
            "responsibilities": ["订单核心服务"],
            "interfaces": [
                {
                    "name": "createOrder",
                    "signature": "def create_order(sku: str, qty: int) -> dict",
                    "inputs": "sku, qty",
                    "outputs": "{order_id, status}",
                    "errors": ["OUT_OF_STOCK"],
                    "req_ids": ["REQ-001"],
                }
            ],
            "data_models": ["Order"],
            "sequences_mermaid": ["sequenceDiagram\n  User->>Order: create_order"],
            "error_handling": ["库存不足抛异常"],
            "test_cases": [
                {"tc_id": "TC-001", "req_ids": ["REQ-001"], "kind": "unit", "description": "createOrder 下单成功"}
            ],
            "design_markdown": "",
        }

    seen_coder_prompts: list[str] = []

    def coder_fn(prompt: str, cwd: Path, _call: MockCall) -> dict[str, Any]:
        seen_coder_prompts.append(prompt)
        (cwd / "order").mkdir(parents=True, exist_ok=True)
        (cwd / "order" / "svc.py").write_text("def create_order(sku, qty):\n    return {'order_id': '1', 'status': 'OK'}\n", "utf-8")
        return {"status": "DONE", "summary": "ok", "files_changed": ["order/svc.py"], "tests_added": [], "notes": ""}

    ws = init_workspace(tmp_path / "wp1-e2e", name="OrderPlatform", collaboration_mode="files")
    harness = MockHarness(coder=coder_fn, role_handlers={"architect": architect_fn})
    eng = Engine(ws, harness=harness)
    try:
        eng.settings.runtime_backend = "local"
        eng.settings.require_merge_approval = True
        tpl = load_builtin_template("standard")
        tpl.review_policy = "off"
        for s in tpl.stages:
            if s.id in ("testing", "integration_delivery"):
                s.gates = [g for g in s.gates if g.kind not in ("quality", "ci")]
        save_custom_template(ws, json.dumps(tpl.model_dump(), ensure_ascii=False))

        spec = ProductSpec(
            product_vision="高可靠订单服务",
            core_workflows=["下单"],
            edge_cases=["库存为0"],
            error_conventions=["OUT_OF_STOCK"],
            global_acceptance_redlines=["测试覆盖"],
            spec_markdown="",
            requirements=[
                RequirementItem(req_id="REQ-001", title="创建订单", statement="当请求合法时创建订单。", priority="P0", acceptance=["返回 order_id"])
            ],
        )
        ws.prd_json_path.write_text(json.dumps(spec.model_dump(), ensure_ascii=False), "utf-8")

        plan = ProjectPlan(
            summary="订单蓝图",
            tech_stack="python",
            agents_md="rule",
            modules=[PlanModule(name="order", path="order/", description="订单模块")],
            milestones=[PlanMilestone(milestone_id="M1", title="M1", goal="实现下单", integration_cmd="test -f order/svc.py")],
            tasks=[
                PlanTask(
                    task_id="T-001",
                    milestone_id="M1",
                    squad="order",
                    module="order",
                    role="coder",
                    title="实现下单接口",
                    description="按详设实现 create_order",
                    acceptance_criteria=["可调用 create_order"],
                    depends_on=[],
                    doc_slice_ids=[],
                    allowed_paths=["order/"],
                    verify_cmds=["test -f order/svc.py"],
                    priority=10,
                    requirement_ids=["REQ-001"],
                    design_refs=["design/order.md#接口-createOrder"],
                )
            ],
        )
        eng.db.save_plan(plan, max_retries=2)
        eng.set_status(ProjectStatus.APPROVED)

        # 1. 生成详细设计（第 1 轮校验拦截 -> 第 2 轮通过落盘）
        dd = asyncio.run(generate_detailed_design(eng, "M1", "order"))
        assert arch_calls == 2
        assert dd.interfaces[0].name == "createOrder"
        assert (ws.root / "docs" / "design" / "order.md").is_file()

        # 2. H3 门禁未通过前阻止 coder 派单；批准后放行并在 coder / reviewer 提示词注入 design_refs 片段
        sched = Scheduler(eng)
        sched._dispatch_ready("M1")
        assert eng.db.get_task("T-001").status == TaskStatus.READY

        task_obj = eng.db.get_task("T-001")
        assert task_obj is not None
        rev_prompt = build_review_prompt(task_obj, [], "exit 0", "+code", ws_root=ws.root)
        assert "实现符合详细设计 design/order.md#接口-createOrder" in rev_prompt
        assert "REQ-001" in rev_prompt

        dd_gate = ensure_gate_record(
            eng.db,
            stage_id="detailed_design",
            gate_id="detailed_design_approval",
            kind="human_approval",
            scope_type="milestone",
            scope_id="M1",
        )
        decide_gate(eng.db, int(dd_gate["record_id"]), "pass", actor="architect", comment="详设评审通过")

        asyncio.run(sched.run())
        assert seen_coder_prompts and "def create_order(sku: str, qty: int) -> dict" in seen_coder_prompts[0]

        # 3. 里程碑合入自动生成 TEST_REPORT-M1.md，并在文档中心完成在线编辑、版本对比与 .docx 导出
        eng.merge_milestone("M1", actor="lead")
        assert (ws.root / "docs" / "test" / "TEST_REPORT-M1.md").is_file()

        client = TestClient(create_app(eng, proxy_transport=_offline_proxy()))
        docs_list = client.get("/vernal-api/docs").json()
        kinds = {d["kind"] for d in docs_list}
        assert {"SRS", "DETAILED_DESIGN", "TEST_REPORT"} <= kinds

        put_res = client.put(
            "/vernal-api/docs/docs/SRS.md",
            json={"content": "# 软件需求规格说明书\n\n## 新增章节：幂等性要求\n", "note": "补充幂等要求", "actor": "pm"},
        )
        assert put_res.status_code == 200
        assert put_res.json()["version"] == 2

        diff_res = client.get("/vernal-api/docs/docs/SRS.md/diff?from_ver=1&to_ver=2").json()
        assert "+## 新增章节：幂等性要求" in diff_res["diff"]

        exp_docx = client.get("/vernal-api/docs/export?kinds=SRS&format=docx")
        assert exp_docx.status_code == 200
        parsed_docx = inspect_docx_structure(exp_docx.content)
        assert any("软件需求规格说明书" in h[1] for h in parsed_docx["headings"])  # type: ignore[index]
    finally:
        eng.db.close()
