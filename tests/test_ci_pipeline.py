"""WP-3 测试：CI 模板自动识别、非破坏性 workflow 写入、轮询/签名回调双通道、失败自动生成 bugfix 单与超限熔断。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from autodev.ci.detect import (
    detect_ci_template_kind,
    render_vernal_ci_workflow,
    synthesize_test_cmds,
    write_ci_workflow_non_destructive,
)
from autodev.ci.service import make_callback_url, poll_ci_run
from autodev.engine import Engine, OpsError, init_workspace
from autodev.harnesses.mock import MockHarness
from autodev.models import MilestoneStatus, PlanMilestone, PlanModule, PlanTask, ProjectPlan, ProjectStatus
from autodev.process.runner import load_builtin_template, save_custom_template
from autodev.scheduler import Scheduler
from autodev.server.app import create_app


def _offline_proxy() -> httpx.MockTransport:
    return httpx.MockTransport(lambda req: httpx.Response(200, json={"ok": True}))


def test_ci_template_detection_cmd_synthesis_and_preserve_existing_workflows(tmp_path: Path) -> None:
    assert detect_ci_template_kind(tmp_path) == "generic"
    (tmp_path / "package.json").write_text('{"name":"demo"}', "utf-8")
    assert detect_ci_template_kind(tmp_path) == "node"
    (tmp_path / "pyproject.toml").write_text('[project]\nname="demo"\n', "utf-8")
    assert detect_ci_template_kind(tmp_path) == "python"

    # 已有用户自定义 workflow 时绝不覆盖
    existing_wf = tmp_path / ".gitea" / "workflows" / "user-build.yml"
    existing_wf.parent.mkdir(parents=True, exist_ok=True)
    existing_wf.write_text("name: user-build\n", "utf-8")

    ws = init_workspace(tmp_path / "ci-det", name="CIDet", collaboration_mode="files")
    eng = Engine(ws, harness=MockHarness())
    try:
        plan = ProjectPlan(
            summary="ci",
            tech_stack="python",
            agents_md="r",
            modules=[PlanModule(name="m", path="m/", description="m")],
            milestones=[PlanMilestone(milestone_id="M1", title="M1", goal="g", integration_cmd="pytest -q")],
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
                    allowed_paths=["m/"],
                    verify_cmds=["pytest -q", "python3 -m compileall m"],
                    priority=10,
                )
            ],
        )
        eng.db.save_plan(plan, max_retries=2)
        ms = eng.db.get_milestone("M1")
        tasks = eng.db.list_tasks(milestone_id="M1")
        assert ms is not None
        cmds = synthesize_test_cmds(ms, tasks)
        assert cmds == ["pytest -q", "python3 -m compileall m"]

        rendered = render_vernal_ci_workflow(tmp_path, ms, tasks, callback_url="http://vernal/cb")
        assert "uses:" not in rendered
        assert "vernal/ci/**" in rendered
        out_path = write_ci_workflow_non_destructive(tmp_path, rendered)
        assert out_path.name == "vernal-ci.yml"
        assert existing_wf.read_text("utf-8") == "name: user-build\n"
    finally:
        eng.db.close()


def test_ci_failure_creates_bugfix_round_plus_one_then_success_allows_merge(tmp_path: Path) -> None:
    ci_round_states = {1: "failure", 2: "success"}

    def gitea_handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/logs"):
            return httpx.Response(200, text="FAILED tests/test_order.py::test_calc - AssertionError: 1 != 2\n")
        if "/commits/" in path and path.endswith("/status"):
            # 根据触发次数返回第 1 轮 failure，第 2 轮 success
            st = ci_round_states.get(current_round[0], "success")
            return httpx.Response(
                200,
                json={
                    "state": st,
                    "statuses": [{"context": "vernal-ci / verify", "status": st, "target_url": "http://gitea.local/run/1"}],
                },
            )
        return httpx.Response(200, json={})

    current_round = [1]
    ci_transport = httpx.MockTransport(gitea_handler)

    def coder_fn(prompt: str, cwd: Path, _call: object) -> dict[str, object]:
        (cwd / "order").mkdir(parents=True, exist_ok=True)
        if "修复 CI 失败" in prompt:
            current_round[0] = 2
            (cwd / "order" / "calc.py").write_text("VAL = 2\n", "utf-8")
        else:
            (cwd / "order" / "calc.py").write_text("VAL = 1\n", "utf-8")
        return {"status": "DONE", "summary": "done", "files_changed": ["order/calc.py"], "tests_added": [], "notes": ""}

    ws = init_workspace(tmp_path / "ci-loop", name="CILoop", collaboration_mode="files")
    eng = Engine(ws, harness=MockHarness(coder=coder_fn))
    try:
        eng.settings.runtime_backend = "local"
        eng.settings.ci_provider = "gitea"
        eng.settings.ci_url = "http://gitea.local"
        eng.settings.require_merge_approval = True
        tpl = load_builtin_template("standard")
        tpl.review_policy = "off"
        for s in tpl.stages:
            if s.id in {"requirements", "architecture", "detailed_design"}:
                s.enabled = False  # These fixtures isolate CI and candidate review.
            if s.id == "testing":
                s.gates = [g for g in s.gates if g.kind != "quality"]
        save_custom_template(ws, json.dumps(tpl.model_dump(), ensure_ascii=False))

        plan = ProjectPlan(
            summary="CI 闭环测试",
            tech_stack="python",
            agents_md="rule",
            modules=[PlanModule(name="order", path="order/", description="order")],
            milestones=[PlanMilestone(milestone_id="M1", title="M1", goal="交付", integration_cmd="test -f order/calc.py")],
            tasks=[
                PlanTask(
                    task_id="T-001",
                    milestone_id="M1",
                    module="order",
                    role="coder",
                    title="实现订单计算",
                    description="写 calc.py",
                    acceptance_criteria=["存在"],
                    depends_on=[],
                    doc_slice_ids=[],
                    allowed_paths=["order/"],
                    verify_cmds=["test -f order/calc.py"],
                    priority=10,
                )
            ],
        )
        eng.db.save_plan(plan, max_retries=2)
        eng.set_status(ProjectStatus.APPROVED)

        sched = Scheduler(eng, ci_transport=ci_transport)
        asyncio.run(sched.run())

        # 第 1 轮 CI 失败自动插入了 T-002 bugfix 单，并在同一次调度循环中自动跑完第 2 轮 CI 转绿进入 AWAITING_MERGE
        tasks = eng.db.list_tasks(milestone_id="M1")
        assert len(tasks) == 2
        assert any("修复 CI 失败" in t.title for t in tasks)
        ci_runs = client_runs = eng.db._query("SELECT * FROM ci_runs ORDER BY round ASC")
        assert [r["round"] for r in client_runs] == [1, 2]
        assert [r["status"] for r in client_runs] == ["FAILURE", "SUCCESS"]
        assert eng.db.get_milestone("M1").status == MilestoneStatus.AWAITING_MERGE

        # 合入并验证 Release Notes 生成 + 受控配置部署接口
        eng.settings.deploy_cmd = "echo deployed:$VERNAL_RELEASE_TAG"
        eng.merge_milestone("M1", actor="lead")
        assert (ws.reports_dir / "releases" / "vernal-M1-v1.md").is_file()

        client = TestClient(create_app(eng, proxy_transport=_offline_proxy()))
        dep_res = client.post("/vernal-api/deployments", json={"milestone_id": "M1", "target": "staging", "actor": "lead"})
        assert dep_res.status_code == 200
        assert dep_res.json()["status"] == "SUCCESS"
        assert "deployed:vernal-M1-v1" in (dep_res.json().get("log_tail") or "")
    finally:
        eng.db.close()


def test_ci_callback_signature_idempotency_timeout_and_merge_gate_guard(tmp_path: Path) -> None:
    def coder_fn(_p: str, cwd: Path, _c: object) -> dict[str, object]:
        (cwd / "x.py").write_text("X = 1\n", "utf-8")
        return {"status": "DONE", "summary": "ok", "files_changed": ["x.py"], "tests_added": [], "notes": ""}

    ws = init_workspace(tmp_path / "ci-cb", name="CICallback", collaboration_mode="files")
    eng = Engine(ws, harness=MockHarness(coder=coder_fn))
    try:
        eng.settings.runtime_backend = "local"
        eng.settings.loopforge_project_id = "ci-cb"
        eng.settings.ci_provider = "gitea"
        eng.settings.ci_url = "http://gitea.local"
        eng.settings.ci_max_fix_rounds = 1
        eng.settings.require_merge_approval = True

        tpl = load_builtin_template("standard")
        tpl.review_policy = "off"
        for s in tpl.stages:
            if s.id in {"requirements", "architecture", "detailed_design"}:
                s.enabled = False  # These fixtures isolate CI and candidate review.
            if s.id == "testing":
                s.gates = [g for g in s.gates if g.kind != "quality"]
        save_custom_template(ws, json.dumps(tpl.model_dump(), ensure_ascii=False))

        plan = ProjectPlan(
            summary="CI 回调与超时测试",
            tech_stack="python",
            agents_md="rule",
            modules=[PlanModule(name="m", path="x.py", description="x")],
            milestones=[PlanMilestone(milestone_id="M1", title="M1", goal="x", integration_cmd="true")],
            tasks=[
                PlanTask(
                    task_id="T-001",
                    milestone_id="M1",
                    module="m",
                    role="coder",
                    title="t1",
                    description="x",
                    acceptance_criteria=["ok"],
                    depends_on=[],
                    doc_slice_ids=[],
                    allowed_paths=["x.py"],
                    verify_cmds=["true"],
                    priority=10,
                )
            ],
        )
        eng.db.save_plan(plan, max_retries=2)
        eng.set_status(ProjectStatus.APPROVED)

        # 模拟 Gitea CI 仍在 pending，调度器启动后通过 request_stop 让里程碑停留在 CI_RUNNING
        pending_transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"state": "pending", "statuses": []}))
        sched = Scheduler(eng, ci_transport=pending_transport)

        async def _run_briefly() -> None:
            t = asyncio.create_task(sched.run())
            await asyncio.sleep(0.12)
            sched.request_stop()
            await t

        asyncio.run(_run_briefly())
        assert eng.db.get_milestone("M1").status == MilestoneStatus.CI_RUNNING
        sched._recover_stale()
        assert eng.db.get_milestone("M1").status == MilestoneStatus.CI_RUNNING

        # CI 尚未通过时调用 merge_milestone 必须被阻断
        with pytest.raises(OpsError, match="CI"):
            eng.merge_milestone("M1", actor="lead")

        client = TestClient(create_app(eng, proxy_transport=_offline_proxy()))
        _, good_sig = make_callback_url(eng, "M1", 1)

        # 错误签名返回 403
        bad_cb = client.post("/vernal-api/ci/callback/ci-cb/M1/1/forgedsig000", json={"passed": True})
        assert bad_cb.status_code == 403

        # 正确签名回调幂等放行
        ok_cb1 = client.post(f"/vernal-api/ci/callback/ci-cb/M1/1/{good_sig}", json={"passed": True, "tests": 12})
        assert ok_cb1.status_code == 200
        assert ok_cb1.json()["status"] == "SUCCESS"

        # 重复回调保持幂等不报错
        ok_cb2 = client.post(f"/vernal-api/ci/callback/ci-cb/M1/1/{good_sig}", json={"passed": False})
        assert ok_cb2.status_code == 200
        assert ok_cb2.json()["status"] == "SUCCESS"
        assert eng.db.get_milestone("M1").status == MilestoneStatus.AWAITING_MERGE

        # 验证 TIMEOUT 判定分支 + 达到 ci_max_fix_rounds=1 直接熔断里程碑
        eng.db.update_milestone("M1", status=MilestoneStatus.CI_RUNNING)
        cur_cand = eng.db.meta_get("candidate_ref:M1") or ""
        cur = eng.db._exec(
            "INSERT INTO ci_runs(milestone_id, round, provider, ref, commit_sha, candidate_ref, status, started_at, created_at)"
            " VALUES('M1', 2, 'gitea', 'vernal/ci/M1-r2', '0123456789abcdef0123456789abcdef01234567', ?, 'RUNNING', '2020-01-01T00:00:00+00:00', '2020-01-01T00:00:00+00:00')",
            (cur_cand,),
        )
        timed_out = asyncio.run(poll_ci_run(eng, int(cur.lastrowid), transport=pending_transport))
        assert timed_out["status"] == "TIMEOUT"
        assert eng.db.get_milestone("M1").status == MilestoneStatus.FAILED
    finally:
        eng.db.close()
