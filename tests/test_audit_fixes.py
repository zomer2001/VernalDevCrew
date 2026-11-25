"""Phase 0 回归测试：覆盖审计缺陷修复、安全加固、并发隔离、配置保留与前端契约一致性。"""

from __future__ import annotations

import asyncio
import json
import stat
import tomllib
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from autodev.council import interject_council
from autodev.db import Task
from autodev.engine import Engine, init_workspace
from autodev.harnesses.mock import MockCall, MockHarness
from autodev.loopforge.bootstrap import classify_lane
from autodev.loopforge.bridge import LoopForgeBridge
from autodev.loopforge.client import LoopForgeClient, use_environment_proxy
from autodev.models import (
    MilestoneStatus,
    PlanMilestone,
    PlanModule,
    PlanTask,
    ProjectPlan,
    ProjectStatus,
    TaskStatus,
)
from autodev.scheduler import Scheduler
from autodev.security import persist_workspace_settings, validate_external_git_url
from autodev.server.app import create_app
from autodev.steering import approve_steering_checkpoint


def _offline_transport() -> httpx.MockTransport:
    return httpx.MockTransport(lambda req: httpx.Response(200, json={"ok": True, "path": req.url.path}))


def _seed_plan(tasks: list[PlanTask], integration_cmd: str = "true") -> ProjectPlan:
    return ProjectPlan(
        summary="回归测试蓝图",
        tech_stack="python",
        agents_md="遵循架构规矩",
        modules=[PlanModule(name="core", path="core/", description="核心模块")],
        milestones=[PlanMilestone(milestone_id="M1", title="M1 基线", goal="交付核心模块", integration_cmd=integration_cmd)],
        tasks=tasks,
    )


def test_frontend_static_contracts_and_dark_theme_scoping() -> None:
    static_dir = Path("autodev/server/static")
    steering_js = (static_dir / "js" / "steering.js").read_text("utf-8")
    assert "waveMs.id" not in steering_js
    assert "failedMs[0]?.id" not in steering_js
    assert ".milestone_id" in steering_js

    main_js = (static_dir / "js" / "main.js").read_text("utf-8")
    interval_idx = main_js.index("setInterval(")
    interval_block = main_js[interval_idx : interval_idx + 220]
    assert "renderDAG(" not in interval_block

    index_html = (static_dir / "index.html").read_text("utf-8")
    assert "gpt-6.2-sol" not in index_html
    assert "碎星航线" not in index_html
    assert "onclick=" not in index_html
    assert "onchange=" not in index_html

    stage_css = (static_dir / "vernal-stage.css").read_text("utf-8")
    assert 'html[data-theme="dark"] #vernalStage' in stage_css


def test_snapshot_exposes_both_slices_and_slices_count(tmp_path: Path) -> None:
    ws = init_workspace(tmp_path / "proj", name="Demo", collaboration_mode="files")
    (ws.inputs_dir / "req.md").write_text("# 需求\n\n支持导出审计日志。\n", "utf-8")
    eng = Engine(ws, harness=MockHarness())
    try:
        eng.ingest()
        snap = eng.snapshot()
        assert snap["slices"] >= 1
        assert snap["slices_count"] == snap["slices"]
    finally:
        eng.db.close()


def test_loopforge_remote_execution_records_all_stages_and_usage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 即使宿主机配置了 SOCKS 代理环境变量，离线显式注入 transport 与内网地址均不应触发 socksio 异常
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:1080")
    assert use_environment_proxy("http://loopforge.local") is False
    assert use_environment_proxy("http://loopforge:3100") is False

    ws = init_workspace(tmp_path / "lf-proj", name="LF Demo", collaboration_mode="files")
    eng = Engine(ws, harness=MockHarness())
    try:
        eng.settings.runtime_backend = "loopforge"
        eng.settings.loopforge_url = "http://loopforge.local"
        eng.settings.loopforge_token = "tok-123"
        eng.settings.loopforge_project_id = "proj-1"
        eng.settings.loopforge_workflow_id = "proj-1-vernal-file-sop"

        plan = _seed_plan(
            [
                PlanTask(
                    task_id="T-001",
                    milestone_id="M1",
                    module="core",
                    role="coder",
                    title="编写问候模块",
                    description="生成 hello.py",
                    acceptance_criteria=["hello.py 存在且可执行"],
                    depends_on=[],
                    doc_slice_ids=[],
                    allowed_paths=["hello.py"],
                    verify_cmds=["python3 -c 'import hello; assert hello.X == 42'"],
                    priority=10,
                )
            ],
            integration_cmd="test -f hello.py",
        )
        eng.db.save_plan(plan, max_retries=2)
        eng.set_status(ProjectStatus.APPROVED)

        async def fake_get_run(_self: Any, run_id: str) -> dict[str, Any]:
            return {
                "id": run_id,
                "status": "completed",
                "tokenUsage": {"inputTokens": 1200, "cachedTokens": 300, "outputTokens": 450},
            }

        async def fake_named_artifact(_self: Any, run: dict[str, Any], name: str) -> str:
            if name == "vernal-files.json":
                return json.dumps({"files": {"hello.py": "X = 42\n"}, "deleted": []})
            if name == "review-report.md":
                return "VERDICT: PASS\n代码结构清晰，验收命令全部通过。"
            return ""

        async def fake_execute_task(self_bridge: Any, task_obj: Task, *args: Any, **kwargs: Any) -> tuple[bool, str]:
            eng.db.update_task(task_obj.task_id, loopforge_issue_id="iss-1", loopforge_run_id="run-xyz")
            return True, "LoopForge 工作流执行完成"

        monkeypatch.setattr(LoopForgeBridge, "execute_task", fake_execute_task)
        monkeypatch.setattr(LoopForgeClient, "get_run", fake_get_run)
        monkeypatch.setattr(LoopForgeClient, "named_artifact", fake_named_artifact)

        sched = Scheduler(eng)
        asyncio.run(sched.run())

        t = eng.db.get_task("T-001")
        assert t is not None and t.status in (TaskStatus.APPROVED, TaskStatus.MERGED)

        runs = eng.db.list_runs("T-001")
        stages = [r["stage"] for r in runs]
        assert stages == ["CODE", "VERIFY", "REVIEW"]
        code_run = next(r for r in runs if r["stage"] == "CODE")
        assert code_run["input_tokens"] == 1200
        assert code_run["output_tokens"] == 450

        client = TestClient(create_app(eng, proxy_transport=_offline_transport()))
        detail = client.get("/vernal-api/tasks/T-001").json()
        assert len(detail["runs"]) == 3
        rev_run = next(r for r in detail["runs"] if r["stage"] == "REVIEW")
        assert rev_run["review_verdict"]["verdict"] == "PASS"
        assert "代码结构清晰" in rev_run["review_verdict"]["summary"]
    finally:
        eng.db.close()


def test_steering_reopen_respects_dependencies_and_lane_routing(tmp_path: Path) -> None:
    ws = init_workspace(tmp_path / "steer-proj", name="Steer", collaboration_mode="files")
    eng = Engine(ws, harness=MockHarness())
    try:
        plan = _seed_plan(
            [
                PlanTask(
                    task_id="T-001",
                    milestone_id="M1",
                    module="core",
                    role="coder",
                    title="后端契约",
                    description="实现鉴权接口",
                    acceptance_criteria=["接口可用"],
                    depends_on=[],
                    doc_slice_ids=[],
                    allowed_paths=["auth/api.py"],
                    verify_cmds=["true"],
                    priority=10,
                ),
                PlanTask(
                    task_id="T-002",
                    milestone_id="M1",
                    module="core",
                    role="coder",
                    title="前端调用",
                    description="对接鉴权接口",
                    acceptance_criteria=["页面可用"],
                    depends_on=["T-001"],
                    doc_slice_ids=[],
                    allowed_paths=["web/login.js"],
                    verify_cmds=["true"],
                    priority=20,
                ),
            ]
        )
        eng.db.save_plan(plan, max_retries=2)
        eng.db.update_task("T-001", status=TaskStatus.APPROVED)
        eng.db.update_task("T-002", status=TaskStatus.APPROVED)
        eng.db.update_milestone("M1", status=MilestoneStatus.AWAITING_MERGE)
        eng.set_status(ProjectStatus.APPROVED)

        draft_plan = {
            "summary": "调整错误码契约并补前端页面",
            "decisions_upsert": [{"decision_id": "DEC-001", "title": "统一错误码", "rule_text": "返回 {code, message}"}],
            "reopen_tasks": [
                {"task_id": "T-001", "reason": "更新响应格式"},
                {"task_id": "T-002", "reason": "同步适配新响应格式"},
            ],
            "new_tasks": [
                {
                    "milestone_id": "M1",
                    "squad": "core",
                    "module": "core",
                    "role": "coder",
                    "title": "新增登录页状态提示",
                    "description": "展示错误消息",
                    "allowed_paths": ["web/index.html"],
                    "depends_on": ["T-001"],
                    "acceptance_criteria": ["展示错误提示"],
                    "verify_cmds": ["test -f web/index.html"],
                }
            ],
            "cancel_tasks": [],
        }
        cp = eng.db.create_checkpoint(kind="PIVOT", trigger_topic="统一错误码", status="AWAITING_APPROVAL")
        cp_id = int(cp["checkpoint_id"])
        eng.db.update_checkpoint(cp_id, draft_plan=draft_plan)

        asyncio.run(approve_steering_checkpoint(eng, cp_id, actor="owner"))

        t1 = eng.db.get_task("T-001")
        t2 = eng.db.get_task("T-002")
        t3 = eng.db.get_task("T-003")
        assert t1 is not None and t1.status == TaskStatus.READY
        assert t2 is not None and t2.status == TaskStatus.BLOCKED
        assert t3 is not None and t3.status == TaskStatus.BLOCKED
        assert classify_lane(t3.role, t3.allowed_paths, t3.title) == "frontend"

        sched = Scheduler(eng)
        assert sched._task_still_owned("T-001", attempt=1) is None

        client = TestClient(create_app(eng, proxy_transport=_offline_transport()))
        stale = client.post(f"/vernal-api/steering/{cp_id}/approve", json={"actor": "owner", "expected_revision": 999})
        assert stale.status_code == 409
    finally:
        eng.db.close()


def test_security_csrf_ssrf_token_permissions_and_multiuser_slot_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for bad_url in (
        "http://169.254.169.254/latest/meta-data",
        "http://127.0.0.1:8080/internal.git",
        "http://loopforge-server:3100/repo.git",
        "file:///etc/passwd",
    ):
        with pytest.raises(ValueError):
            validate_external_git_url(bad_url)

    workspaces_dir = tmp_path / "workspaces"
    ws = init_workspace(workspaces_dir / "proj-alpha", name="Alpha", collaboration_mode="files")
    eng = Engine(ws, harness=MockHarness())
    try:
        eng.settings.runtime_backend = "loopforge"
        eng.settings.loopforge_url = "http://127.0.0.1:3100"
        eng.settings.loopforge_project_id = "proj-alpha"
        eng.settings.loopforge_token = "secret-bearer-token"
        persist_workspace_settings(ws, eng.settings)

        cfg_text = ws.config_path.read_text("utf-8")
        assert "secret-bearer-token" not in cfg_text
        cred_file = ws.state_dir / "credentials.json"
        assert cred_file.exists()
        assert (cred_file.stat().st_mode & 0o777) == stat.S_IRUSR | stat.S_IWUSR
        assert ws.load_settings().effective_loopforge_token() == "secret-bearer-token"

        async def fake_auth_cookie(_self: Any, cookie: str) -> dict[str, Any]:
            if "sessA" in cookie:
                return {"user": {"loginName": "alice"}}
            if "sessB" in cookie:
                return {"user": {"loginName": "bob"}}
            raise RuntimeError("unknown session")

        async def fake_projects_cookie(_self: Any, cookie: str) -> list[dict[str, Any]]:
            if "sessA" in cookie or "sessB" in cookie:
                return [{"id": "proj-alpha", "name": "Alpha"}, {"id": "proj-beta", "name": "Beta"}]
            return []

        async def fake_create_token_cookie(_self: Any, cookie: str, **kwargs: Any) -> dict[str, Any]:
            return {"token": "tok-alice" if "sessA" in cookie else "tok-bob"}

        async def fake_ensure_assets(*args: Any, **kwargs: Any) -> dict[str, str]:
            return {"files": "wf-files"}

        monkeypatch.setattr(LoopForgeClient, "auth_current_with_cookie", fake_auth_cookie)
        monkeypatch.setattr(LoopForgeClient, "list_projects_with_cookie", fake_projects_cookie)
        monkeypatch.setattr(LoopForgeClient, "create_token_with_cookie", fake_create_token_cookie)
        import autodev.server.app as app_mod

        monkeypatch.setattr(app_mod, "ensure_vernal_loopforge_assets", fake_ensure_assets)

        app = create_app(eng, proxy_transport=_offline_transport())
        beta = init_workspace(workspaces_dir / "proj-beta", name="Beta", collaboration_mode="files")
        beta_config = beta.load_settings()
        beta_config.loopforge_project_id = "proj-beta"
        persist_workspace_settings(beta, beta_config)
        ctx = app.state.ctx
        client_a = TestClient(app)
        client_b = TestClient(app)
        client_cli = TestClient(app)

        # 1. 跨源 Cookie 写请求被 CSRF 中间件 403 拦截
        csrf_res = client_a.post(
            "/vernal-api/agent/config",
            headers={"Cookie": "af_session=sessA", "Origin": "https://attacker.example"},
            json={"max_concurrency": 4},
        )
        assert csrf_res.status_code == 403

        # 2. SSRF 内网地址在项目创建入口被 422 拦截
        ssrf_api = client_cli.post(
            "/vernal-api/project/create",
            json={"mode": "git_repo", "name": "bad-ssrf-proj", "git_url": "http://169.254.169.254/repo.git"},
        )
        assert ssrf_api.status_code == 422

        # 3. 两个真实浏览器会话分别通过 /vernal-api/project/select 注册并切换至不同项目，互不串台且不污染 CLI 默认槽位
        sel_a = client_a.post(
            "/vernal-api/project/select",
            headers={"Cookie": "af_session=sessA", "Origin": "http://testserver"},
            json={"project_id": "proj-alpha", "project_name": "Alpha"},
        )
        assert sel_a.status_code == 200

        sel_b = client_b.post(
            "/vernal-api/project/select",
            headers={"Cookie": "af_session=sessB", "Origin": "http://testserver"},
            json={"project_id": "proj-beta", "project_name": "Beta"},
        )
        assert sel_b.status_code == 200
        assert sel_b.json()["switched"] is True
        assert "proj-beta" in ctx.slots

        # CLI 默认槽位与初始槽位保持 Alpha 不被会话 B 篡改
        assert ctx.cli_default_slot.engine is eng
        assert ctx.initial_slot.engine is eng

        state_a = client_a.get(
            "/vernal-api/state",
            headers={"Cookie": "af_session=sessA", "X-Vernal-Project": "proj-alpha"},
        ).json()
        state_b = client_b.get(
            "/vernal-api/state",
            headers={"Cookie": "af_session=sessB", "X-Vernal-Project": "proj-beta"},
        ).json()
        state_cli = client_cli.get("/vernal-api/state").json()

        assert state_a["name"] == "Alpha" and state_a["current_user"] == "alice"
        assert state_b["name"] == "Beta" and state_b["current_user"] == "bob"
        assert state_cli["name"] == "Alpha"
    finally:
        for s in list(getattr(app.state.ctx, "all_slots", [])) if "app" in locals() else []:
            s.engine.db.close()
        eng.db.close()


def test_config_toml_preserves_custom_sections_and_unknown_fields(tmp_path: Path) -> None:
    ws = init_workspace(tmp_path / "custom-cfg", name="CustomCfg", collaboration_mode="files")
    custom_toml = (
        'top_custom_banner = "lab-internal"\n\n'
        '[agent]\n'
        'harness = "codex"\n'
        'model = ""\n'
        'experimental_flag = true\n'
        'custom_tags = ["sec", "offline"]\n\n'
        '[runtime]\n'
        'runtime_backend = "loopforge"\n'
        'loopforge_token = "should-be-stripped"\n'
        'custom_gateway_note = "preserve-me"\n\n'
        '[my_plugin]\n'
        'enabled = true\n'
        'endpoint = "https://plugin.example.org"\n\n'
        '[my_plugin.retry]\n'
        'max_attempts = 5\n\n'
        '[[hooks]]\n'
        'event = "post_merge"\n'
        'cmd = "echo merged"\n'
    )
    ws.config_path.write_text(custom_toml, "utf-8")

    settings = ws.load_settings()
    settings.max_concurrency = 6
    settings.process_template = "standard"
    settings.loopforge_token = "super-secret-token"
    persist_workspace_settings(ws, settings)

    saved_raw = ws.config_path.read_text("utf-8")
    assert "super-secret-token" not in saved_raw
    assert "should-be-stripped" not in saved_raw

    parsed = tomllib.loads(saved_raw)
    assert parsed["top_custom_banner"] == "lab-internal"
    assert parsed["scheduler"]["max_concurrency"] == 6
    assert parsed["scheduler"]["process_template"] == "standard"
    assert parsed["agent"]["experimental_flag"] is True
    assert parsed["agent"]["custom_tags"] == ["sec", "offline"]
    assert parsed["runtime"]["custom_gateway_note"] == "preserve-me"
    assert parsed["runtime"]["loopforge_token"] == ""
    assert parsed["my_plugin"]["enabled"] is True
    assert parsed["my_plugin"]["endpoint"] == "https://plugin.example.org"
    assert parsed["my_plugin"]["retry"]["max_attempts"] == 5
    assert parsed["hooks"] == [{"event": "post_merge", "cmd": "echo merged"}]
    assert ws.load_settings().effective_loopforge_token() == "super-secret-token"


def test_council_interjection_refreshes_goal_blueprint(tmp_path: Path) -> None:
    blueprint_runs = 0

    def opinion_fn(_p: str, _c: Path, _call: MockCall) -> dict[str, Any]:
        return {
            "headline": "架构提案",
            "proposals": ["按模块解耦"],
            "concerns": ["并发一致性"],
            "questions": [],
            "recommended_sandboxes": ["generic-docker"],
        }

    def director_fn(prompt: str, _c: Path, _call: MockCall) -> dict[str, Any]:
        nonlocal blueprint_runs
        if "第 2 轮收敛拍板人" not in prompt:
            blueprint_runs += 1
            title = "JWT 鉴权中间件" if "JWT" in prompt else "订单基线"
            return {
                "summary": f"立项蓝图 v{blueprint_runs}：{title}",
                "tech_stack": "Python 3.12",
                "agents_md": "# AGENTS\n",
                "squads": [
                    {
                        "squad_id": "sq-order",
                        "name": "订单研发组",
                        "mission": "负责订单接口",
                        "owned_paths": ["order/"],
                        "roles": ["coder"],
                        "sandbox_profile": "generic-docker",
                    }
                ],
                "milestones": [
                    {
                        "milestone_id": "M1",
                        "title": "M1",
                        "goal": title,
                        "target_squads": ["sq-order"],
                        "key_deliverables": ["order/api.py"],
                        "integration_cmd": "true",
                    }
                ],
                "initial_tasks": [
                    {
                        "task_id": "T-001",
                        "milestone_id": "M1",
                        "squad": "sq-order",
                        "module": "order",
                        "role": "coder",
                        "title": title,
                        "description": "实现核心模块",
                        "acceptance_criteria": ["测试通过"],
                        "depends_on": [],
                        "doc_slice_ids": [],
                        "allowed_paths": ["order/"],
                        "verify_cmds": ["true"],
                        "priority": 10,
                    }
                ],
            }
        return {
            "executive_summary": "采纳 JWT 约束" if "JWT" in prompt else "首轮共识",
            "resolved_debates": ["冻结接口"],
            "open_risks": [],
            "contract_strategy": "contracts/",
            "adrs": [
                {
                    "adr_id": "ADR-001",
                    "title": "鉴权方案",
                    "decision": "JWT" if "JWT" in prompt else "Session",
                    "rationale": "无状态横向扩展",
                    "affected_squads": ["sq-order"],
                }
            ],
            "spec_markdown": "# PRD_SPEC\n",
        }

    harness = MockHarness(
        role_handlers={
            "pm": opinion_fn,
            "architect": opinion_fn,
            "qa_lead": opinion_fn,
            "director": director_fn,
        }
    )
    ws = init_workspace(tmp_path / "council-goal", name="CouncilGoal", collaboration_mode="files")
    (ws.inputs_dir / "prd.md").write_text("# 订单服务\n\n实现下单与库存扣减接口。\n", "utf-8")
    eng = Engine(ws, harness=harness)
    try:
        eng.settings.planning_mode = "goal"
        eng.settings.enable_council = True
        eng.ingest()
        asyncio.run(eng.plan(mode="goal"))
        assert eng.status == ProjectStatus.PLANNED
        assert blueprint_runs == 1

        asyncio.run(interject_council(eng, note="鉴权统一收敛为 JWT 中间件", actor="tech_lead"))
        assert eng.status == ProjectStatus.PLANNED
        assert blueprint_runs == 2
        assert "JWT" in eng.db.get_task("T-001").title
    finally:
        eng.db.close()
