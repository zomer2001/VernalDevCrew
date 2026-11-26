"""WP-2 测试：人工代码审查策略分支、阻塞下游与集成、打回返工 round+1、expected_head 409、blocker 校验与里程碑审查。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from autodev.engine import Engine, OpsError, init_workspace
from autodev.harnesses.mock import MockHarness
from autodev.models import MilestoneStatus, PlanMilestone, PlanModule, PlanTask, ProjectPlan, ProjectStatus, TaskStatus
from autodev.review.diffs import parse_unified_diff
from autodev.review.policy import evaluate_review_policy
from autodev.scheduler import Scheduler
from autodev.server.app import create_app


def _offline_transport() -> httpx.MockTransport:
    return httpx.MockTransport(lambda req: httpx.Response(200, json={"ok": True}))


def _dummy_task(**overrides: object):
    from autodev.db import Task

    base = dict(
        task_id="T-001",
        milestone_id="M1",
        squad="core",
        module="core",
        role="coder",
        title="t",
        description="d",
        acceptance_criteria=[],
        depends_on=[],
        doc_slice_ids=[],
        allowed_paths=["src/"],
        verify_cmds=[],
        priority=10,
        status=TaskStatus.REVIEWING,
        retry_count=0,
        max_retries=3,
        arbitration_count=0,
        assigned_slot=None,
        active_agent_label=None,
        loopforge_issue_id=None,
        loopforge_run_id=None,
        worktree_path=None,
        branch_name=None,
        base_sha=None,
        last_session_id=None,
        artifact_digest=None,
        candidate_commit_sha=None,
        bound_contract_hash=None,
        last_feedback=None,
        failure_reason=None,
        steer_note=None,
        pending_action=None,
        owner_member=None,
        updated_at=None,
    )
    base.update(overrides)
    return Task(**base)  # type: ignore[arg-type]


def test_review_policy_branches() -> None:
    t_normal = _dummy_task()
    small_diff = parse_unified_diff(
        "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1,2 @@\n x = 1\n+y = 2\n"
    )
    assert evaluate_review_policy("off", t_normal, small_diff)[0] is False
    assert evaluate_review_policy("milestone", t_normal, small_diff)[0] is False
    assert evaluate_review_policy("all", t_normal, small_diff)[0] is True
    assert evaluate_review_policy("risk", t_normal, small_diff)[0] is False

    # risk 触发：修改 contracts/
    contract_diff = parse_unified_diff(
        "diff --git a/contracts/api.json b/contracts/api.json\n--- /dev/null\n+++ b/contracts/api.json\n@@ -0,0 +1 @@\n+{}\n"
    )
    need, reasons = evaluate_review_policy("risk", t_normal, contract_diff)
    assert need is True and any("契约" in r for r in reasons)

    # risk 触发：敏感模式 subprocess
    subproc_diff = parse_unified_diff(
        "diff --git a/src/run.py b/src/run.py\n--- /dev/null\n+++ b/src/run.py\n@@ -0,0 +1 @@\n+subprocess.Popen(cmd)\n"
    )
    need2, reasons2 = evaluate_review_policy("risk", t_normal, subproc_diff)
    assert need2 is True and any("子进程" in r for r in reasons2)


def test_human_review_blocks_downstream_rework_round_plus_one_and_409_blocker_guards(tmp_path: Path) -> None:
    coder_runs = 0
    seen_prompts: list[str] = []

    def coder_fn(prompt: str, cwd: Path, _call: object) -> dict[str, object]:
        nonlocal coder_runs
        coder_runs += 1
        seen_prompts.append(prompt)
        if "T-001" in prompt:
            (cwd / "contracts").mkdir(parents=True, exist_ok=True)
            ver = 2 if "人工审查意见" in prompt else 1
            (cwd / "contracts" / "order.json").write_text(f'{{"version": {ver}}}\n', "utf-8")
            return {"status": "DONE", "summary": f"v{ver}", "files_changed": ["contracts/order.json"], "tests_added": [], "notes": ""}
        (cwd / "svc").mkdir(parents=True, exist_ok=True)
        (cwd / "svc" / "handler.py").write_text("OK = True\n", "utf-8")
        return {"status": "DONE", "summary": "svc", "files_changed": ["svc/handler.py"], "tests_added": [], "notes": ""}

    ws = init_workspace(tmp_path / "hr-flow", name="HRFlow", collaboration_mode="files")
    eng = Engine(ws, harness=MockHarness(coder=coder_fn))
    try:
        eng.settings.runtime_backend = "local"
        eng.settings.review_policy = "risk"
        eng.settings.require_merge_approval = True

        plan = ProjectPlan(
            summary="人工审查全链路测试",
            tech_stack="python",
            agents_md="rule",
            modules=[PlanModule(name="core", path="contracts/", description="contracts")],
            milestones=[PlanMilestone(milestone_id="M1", title="M1", goal="契约与实现", integration_cmd="test -f svc/handler.py")],
            tasks=[
                PlanTask(
                    task_id="T-001",
                    milestone_id="M1",
                    module="core",
                    role="contract",
                    title="定义订单契约",
                    description="编写 contracts/order.json",
                    acceptance_criteria=["包含版本号"],
                    depends_on=[],
                    doc_slice_ids=[],
                    allowed_paths=["contracts/"],
                    verify_cmds=["test -f contracts/order.json"],
                    priority=10,
                ),
                PlanTask(
                    task_id="T-002",
                    milestone_id="M1",
                    module="core",
                    role="coder",
                    title="实现订单服务",
                    description="编写 svc/handler.py",
                    acceptance_criteria=["存在 handler.py"],
                    depends_on=["T-001"],
                    doc_slice_ids=[],
                    allowed_paths=["svc/"],
                    verify_cmds=["test -f svc/handler.py"],
                    priority=20,
                ),
            ],
        )
        eng.db.save_plan(plan, max_retries=3)
        eng.set_status(ProjectStatus.APPROVED)

        sched = Scheduler(eng)
        asyncio.run(sched.run())

        # 1. T-001 触及 contracts/ 进入 AWAITING_HUMAN_REVIEW，下游 T-002 保持 BLOCKED，且重启恢复不会把 T-001 重置回 READY
        t1 = eng.db.get_task("T-001")
        t2 = eng.db.get_task("T-002")
        assert t1 is not None and t1.status == TaskStatus.AWAITING_HUMAN_REVIEW
        assert t2 is not None and t2.status == TaskStatus.BLOCKED
        sched._recover_stale()
        assert eng.db.get_task("T-001").status == TaskStatus.AWAITING_HUMAN_REVIEW

        client = TestClient(create_app(eng, proxy_transport=_offline_transport()))
        open_revs = client.get("/vernal-api/reviews?status=OPEN").json()
        assert len(open_revs) == 1
        rev_id = open_revs[0]["review_id"]
        detail1 = client.get(f"/vernal-api/reviews/{rev_id}").json()
        assert detail1["review"]["round"] == 1
        assert detail1["diff"]["stat"]["files_changed"] == 1

        # 2. 添加 blocker 行内评论后直接 approve 必须 422 拒绝；expected_head 过期返回 409
        cmt = client.post(
            f"/vernal-api/reviews/{rev_id}/comments",
            json={"file": "contracts/order.json", "line": 1, "severity": "blocker", "body": "契约版本号必须升级到 2"},
        )
        assert cmt.status_code == 200

        stale_head = client.post(
            f"/vernal-api/reviews/{rev_id}/decision",
            json={"decision": "approve", "expected_head": "stale-sha-000"},
        )
        assert stale_head.status_code == 409

        blocked_approve = client.post(
            f"/vernal-api/reviews/{rev_id}/decision",
            json={"decision": "approve", "expected_head": detail1["review"]["head_ref"]},
        )
        assert blocked_approve.status_code == 422

        # 3. request_changes 打回返工：T-001 回到 READY 且 steer_note 包含行内评论
        req_chg = client.post(
            f"/vernal-api/reviews/{rev_id}/decision",
            json={
                "decision": "request_changes",
                "note": "请按行内评论升级版本号",
                "expected_head": detail1["review"]["head_ref"],
            },
        )
        assert req_chg.status_code == 200
        t1_reopened = eng.db.get_task("T-001")
        assert t1_reopened.status == TaskStatus.READY
        assert "契约版本号必须升级到 2" in (t1_reopened.steer_note or "")

        # 4. 调度器重跑后产生 round=2 的新审查单，并保留上一轮评论可对比
        asyncio.run(sched.run())
        open_revs2 = client.get("/vernal-api/reviews?status=OPEN").json()
        assert len(open_revs2) == 1
        rev2_id = open_revs2[0]["review_id"]
        detail2 = client.get(f"/vernal-api/reviews/{rev2_id}").json()
        assert detail2["review"]["round"] == 2
        assert len(detail2["previous_round_comments"]) == 1

        # 5. 添加 blocker 但附带 override_reason 放行，T-001 -> APPROVED 且解锁 T-002 完成里程碑集成
        client.post(
            f"/vernal-api/reviews/{rev2_id}/comments",
            json={"file": "contracts/order.json", "line": 1, "severity": "blocker", "body": "后续补充 schema 字段"},
        )
        ok_override = client.post(
            f"/vernal-api/reviews/{rev2_id}/decision",
            json={
                "decision": "approve",
                "expected_head": detail2["review"]["head_ref"],
                "override_reason": "经架构组确认放到 M2 演进",
            },
        )
        assert ok_override.status_code == 200
        assert eng.db.get_task("T-001").status == TaskStatus.APPROVED
        assert eng.db.get_task("T-002").status == TaskStatus.READY

        asyncio.run(sched.run())
        assert eng.db.get_task("T-002").status == TaskStatus.APPROVED
        assert eng.db.get_milestone("M1").status == MilestoneStatus.AWAITING_MERGE
    finally:
        eng.db.close()


def test_milestone_review_policy_blocks_merge_until_approved(tmp_path: Path) -> None:
    def coder_fn(_p: str, cwd: Path, _c: object) -> dict[str, object]:
        (cwd / "app.py").write_text("X = 1\n", "utf-8")
        return {"status": "DONE", "summary": "ok", "files_changed": ["app.py"], "tests_added": [], "notes": ""}

    ws = init_workspace(tmp_path / "ms-rev", name="MsRev", collaboration_mode="files")
    eng = Engine(ws, harness=MockHarness(coder=coder_fn))
    try:
        eng.settings.runtime_backend = "local"
        eng.settings.review_policy = "milestone"
        eng.settings.require_merge_approval = False
        plan = ProjectPlan(
            summary="里程碑审查测试",
            tech_stack="python",
            agents_md="rule",
            modules=[PlanModule(name="app", path="app.py", description="app")],
            milestones=[PlanMilestone(milestone_id="M1", title="M1", goal="交付", integration_cmd="test -f app.py")],
            tasks=[
                PlanTask(
                    task_id="T-001",
                    milestone_id="M1",
                    module="app",
                    role="coder",
                    title="实现 app",
                    description="写 app.py",
                    acceptance_criteria=["存在"],
                    depends_on=[],
                    doc_slice_ids=[],
                    allowed_paths=["app.py"],
                    verify_cmds=["test -f app.py"],
                    priority=10,
                )
            ],
        )
        eng.db.save_plan(plan, max_retries=2)
        eng.set_status(ProjectStatus.APPROVED)

        asyncio.run(Scheduler(eng).run())
        assert eng.db.get_milestone("M1").status == MilestoneStatus.AWAITING_MERGE

        # 里程碑审查未通过前调用 merge_milestone 必须拒绝
        with pytest.raises(OpsError, match="里程碑人工代码审查"):
            eng.merge_milestone("M1", actor="lead")

        client = TestClient(create_app(eng, proxy_transport=_offline_transport()))
        revs = client.get("/vernal-api/reviews?status=OPEN").json()
        assert len(revs) == 1 and revs[0]["scope_type"] == "milestone"
        client.post(f"/vernal-api/reviews/{revs[0]['review_id']}/decision", json={"decision": "approve", "actor": "lead"})

        eng.merge_milestone("M1", actor="lead")
        assert eng.db.get_milestone("M1").status == MilestoneStatus.MERGED
    finally:
        eng.db.close()
