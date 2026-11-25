"""测试多智能体架构评审圆桌（2 轮收敛 + 人类插话）与 LoopForge BFF/桥接接口。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from autodev.config import Settings
from autodev.council import interject_council, run_council
from autodev.engine import Engine, init_workspace
from autodev.harnesses.mock import MockCall, MockHarness
from autodev.loopforge.bridge import format_issue_description
from autodev.server.app import create_app


def _make_council_harness() -> MockHarness:
    def opinion(role_name: str):
        def _fn(_prompt: str, _cwd: Path, _call: MockCall) -> dict[str, Any]:
            return {
                "headline": f"{role_name} 提出核心架构建议",
                "proposals": [f"{role_name} 提案 1", f"{role_name} 提案 2"],
                "concerns": [f"{role_name} 风险点"],
                "questions": [f"{role_name} 待拍板分歧"],
                "recommended_sandboxes": ["generic-docker", "sandbox-python"],
            }

        return _fn

    def director_consensus(prompt: str, _cwd: Path, _call: MockCall) -> dict[str, Any]:
        has_human = "SDK 放到 M3" in prompt
        return {
            "executive_summary": "采纳负责人插话调整排期" if has_human else "完成首轮圆桌拍板共识",
            "resolved_debates": ["[契约版本策略] → [M1 冻结 contracts/，变更走总监仲裁]"],
            "open_risks": ["高并发库存扣减需加锁单测"],
            "contract_strategy": "所有跨模块接口冻结在 contracts/",
            "adrs": [
                {
                    "adr_id": "ADR-001",
                    "title": "按业务域划分物理目录领地",
                    "decision": "用户/库存/订单各占独立目录，由网关组收口",
                    "rationale": "消除并行分支合并冲突",
                    "affected_squads": ["squad-user", "squad-order", "squad-gateway"],
                }
            ],
            "spec_markdown": "# PRD_SPEC\n\n- ADR-001 物理领地隔离\n",
        }

    return MockHarness(
        role_handlers={
            "pm": opinion("PM"),
            "architect": opinion("契约架构师"),
            "qa_lead": opinion("QA总监"),
            "director": director_consensus,
        }
    )


def test_architecture_council_two_rounds_and_human_interjection(tmp_path: Path) -> None:
    ws = init_workspace(tmp_path / "council-demo", name="council-demo")
    (ws.inputs_dir / "arch.md").write_text("# Demo Backend\n\n多模块后端与多语言 SDK。\n", "utf-8")

    eng = Engine(ws, settings=Settings(planning_mode="goal", enable_council=True), harness=_make_council_harness())
    try:
        eng.ingest()
        cons = asyncio.run(run_council(eng))
        assert len(cons.adrs) == 1
        assert (ws.root / "docs" / "PRD_SPEC.md").exists()

        msgs = eng.db.list_council_messages()
        assert len(msgs) == 4  # 3 位第 1 轮专家 + 1 次总监拍板
        assert {m["speaker_role"] for m in msgs if m["round_no"] == 1} == {"pm", "architect", "qa_lead"}
        assert any(m["kind"] == "consensus" for m in msgs)

        # 人类插话并触发总监重新裁决
        cons2 = asyncio.run(interject_council(eng, actor="zomer", note="SDK 放到 M3 再做"))
        assert "负责人插话" in cons2.executive_summary
        msgs2 = eng.db.list_council_messages()
        assert len(msgs2) == 6  # +1 human +1 新 consensus
        assert any(m["kind"] == "human" for m in msgs2)
    finally:
        eng.db.close()


def test_new_studio_ui_and_runtime_bff_endpoints(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VERNAL_LOOPFORGE_URL", "http://127.0.0.1:19999")
    ws = init_workspace(tmp_path / "studio-demo", name="studio-demo")
    (ws.inputs_dir / "arch.md").write_text("# Studio Demo\n\n架构文档。\n", "utf-8")

    eng = Engine(
        ws,
        settings=Settings(loopforge_url="http://127.0.0.1:19999", gitea_url="http://127.0.0.1:19998"),
        harness=_make_council_harness(),
    )
    app = create_app(eng)
    try:
        with TestClient(app) as client:
            tpl = client.get("/vernal-static/index.html")
            assert tpl.status_code == 200
            assert "Vernal" in tpl.text
            assert client.get("/vernal-static/vernal-overlay.js").status_code == 200

            # 静态 ESM 模块均可访问
            for mod in ("api.js", "dag.js", "council.js", "roster.js", "drawer.js", "runtime.js", "main.js"):
                r = client.get(f"/vernal-static/js/{mod}")
                assert r.status_code == 200

            # Council & Runtime & Approvals 接口
            assert client.get("/vernal-api/council").status_code == 200
            rt = client.get("/vernal-api/runtime/status").json()
            assert rt["runtime_backend"] == "local"
            assert rt["loopforge"]["reachable"] is False

            appr = client.get("/vernal-api/runtime/approvals").json()
            assert appr["fused_tasks"] == []
            assert appr["loopforge_gates"] == []

            # 保存底座配置
            save = client.post(
                "/vernal-api/runtime/config",
                json={"runtime_backend": "loopforge", "loopforge_url": "http://127.0.0.1:19999", "loopforge_project_id": "proj-1"},
            )
            assert save.status_code == 200
            assert eng.settings.runtime_backend == "loopforge"
            assert 'runtime_backend = "loopforge"' in ws.config_path.read_text("utf-8")
    finally:
        eng.db.close()


def test_loopforge_issue_formatter_includes_guard_command(tmp_path: Path) -> None:
    ws = init_workspace(tmp_path / "fmt-demo")
    eng = Engine(ws, settings=Settings())
    try:
        from autodev.models import PlanMilestone, PlanModule, PlanTask, ProjectPlan

        eng.db.save_plan(
            ProjectPlan(
                summary="s",
                tech_stack="py",
                agents_md="# a",
                modules=[PlanModule(name="m", path="m/", description="d")],
                milestones=[PlanMilestone(milestone_id="M1", title="M1", goal="g", integration_cmd="")],
                tasks=[
                    PlanTask(
                        task_id="T-001",
                        milestone_id="M1",
                        squad="squad-order",
                        module="order",
                        role="coder",
                        title="订单实现",
                        description="写订单逻辑",
                        acceptance_criteria=["单测全绿"],
                        depends_on=[],
                        doc_slice_ids=[],
                        allowed_paths=["modules/order/"],
                        verify_cmds=["python3 -m unittest"],
                        priority=10,
                    )
                ],
            ),
            max_retries=3,
        )
        t = eng.db.get_task("T-001")
        assert t is not None
        desc = format_issue_description(t, "订单域研发组")
        assert "autodev guard --task T-001" in desc
        assert "modules/order/" in desc
    finally:
        eng.db.close()


def test_recovers_completed_roles_after_partial_council_failure(tmp_path):
    from autodev.engine import PlanningError
    import pytest

    ws = init_workspace(tmp_path / "resume", collaboration_mode="files")
    (ws.inputs_dir / "spec.md").write_text("Implement add_two.")
    harness = _make_council_harness()
    eng = Engine(ws, settings=Settings(planning_mode="goal", collaboration_mode="files", max_concurrency=3), harness=harness)
    good_pm = harness.role_handlers["pm"]
    harness.role_handlers["pm"] = lambda *args: {"incomplete": True}
    try:
        eng.ingest()
        with pytest.raises(PlanningError):
            asyncio.run(run_council(eng))
        assert [m["speaker_role"] for m in eng.db.list_council_messages()] == ["architect", "qa_lead"]
        harness.calls.clear()
        harness.role_handlers["pm"] = good_pm
        asyncio.run(run_council(eng))
        assert [c.role for c in harness.calls] == ["pm", "director"]
        assert len(eng.db.list_council_messages()) == 4
    finally:
        eng.db.close()
