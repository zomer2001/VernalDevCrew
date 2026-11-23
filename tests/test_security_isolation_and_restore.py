"""覆盖 Codex 审查提出的全部 6 项问题的专项回归测试：
1. 多人同时切换不同项目互不串台（按 X-Vernal-Project 引擎池隔离）
2. 敏感操作 Cookie 鉴权、项目权限校验、bind-code 仅限本机且不借相邻工作区令牌、签名上下文拉取
3. 长文档与大基线还原命令经真实 Shell 执行无 SyntaxError（含中文、空格、单引号路径）
4. 圆桌多角色（PM / QA / Director）分别使用各自配置模型与独立规划 SOP
5. vernal.sh 网关失败非零退出与 Runner 状态分报
6. /vernal-api/runtime/analytics 显式透出数据来源、查询错误与关联原生 Run
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from autodev.config import Settings
from autodev.engine import Engine, init_workspace
from autodev.harnesses.loopforge_harness import LoopForgeHarness
from autodev.loopforge.bootstrap import build_vernal_agents, plan_workflow_id
from autodev.loopforge.bridge import build_restore_command, sign_context_sig
from autodev.server.app import create_app


def test_restore_command_executes_cleanly_in_real_shell_with_special_chars(tmp_path: Path) -> None:
    # 路径故意包含中文、空格与单引号，验证不会触发 Shell 引号截断或 Python SyntaxError
    tricky_dir = tmp_path / "春天 测试" / "bob's proj"
    tricky_dir.mkdir(parents=True)
    bundle = tricky_dir / "plan-blueprint.json"
    bundle.write_text(
        json.dumps(
            {
                "files": {
                    "PLAN_PROMPT.md": "请阅读架构文档并输出 JSON\n",
                    "inputs/arch.md": "# 长架构文档\n内容包含 '单引号' 和 \"双引号\"\n",
                }
            },
            ensure_ascii=False,
        ),
        "utf-8",
    )

    work_plan = tmp_path / "runner_plan_ws"
    work_plan.mkdir()
    cmd_plan = build_restore_command(str(bundle), "http://127.0.0.1:1/unused", nested_key="files", save_baseline=False)
    res1 = subprocess.run(cmd_plan, shell=True, cwd=work_plan, capture_output=True, text=True)
    assert res1.returncode == 0, f"stderr={res1.stderr}"
    assert (work_plan / "PLAN_PROMPT.md").read_text("utf-8").startswith("请阅读")
    assert "单引号" in (work_plan / "inputs" / "arch.md").read_text("utf-8")

    # 同样验证大基线文件交接模式（save_baseline=True）
    base_json = tricky_dir / "T-005.json"
    base_json.write_text(json.dumps({"src/app.py": "print('hello')\n"}, ensure_ascii=False), "utf-8")
    work_code = tmp_path / "runner_code_ws"
    work_code.mkdir()
    cmd_code = build_restore_command(str(base_json), "http://127.0.0.1:1/unused", nested_key="", save_baseline=True)
    res2 = subprocess.run(cmd_code, shell=True, cwd=work_code, capture_output=True, text=True)
    assert res2.returncode == 0, f"stderr={res2.stderr}"
    assert (work_code / "src" / "app.py").read_text("utf-8") == "print('hello')\n"
    assert json.loads((work_code / ".vernal-baseline.json").read_text("utf-8")) == {"src/app.py": "print('hello')\n"}


def test_council_roles_use_distinct_models_and_plan_sops(tmp_path: Path) -> None:
    ws = init_workspace(tmp_path / "multi-model", name="multi-model", collaboration_mode="files")
    settings = Settings(
        collaboration_mode="files",
        runtime_backend="loopforge",
        loopforge_url="http://lf.test",
        loopforge_token="afp_tok",
        loopforge_project_id="proj-mm",
        harness="codex",
        model="gpt-6.2-sol",
        pm_harness="opencode",
        pm_model="deepseek-chat",
        qa_harness="codebuddy",
        qa_model="glm-5",
    )
    agents = {a["id"]: a for a in build_vernal_agents("proj-mm", settings)}
    assert agents["proj-mm-vernal-planner"]["executorId"] == "codex"
    assert agents["proj-mm-vernal-planner"]["model"] == "gpt-6.2-sol"
    assert agents["proj-mm-vernal-planner-pm"]["executorId"] == "opencode"
    assert agents["proj-mm-vernal-planner-pm"]["model"] == "deepseek-chat"
    assert agents["proj-mm-vernal-planner-qa"]["executorId"] == "codebuddy"
    assert agents["proj-mm-vernal-planner-qa"]["model"] == "glm-5"

    dispatched_wfs: list[str] = []

    def _lf(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p == "/api/runners":
            return httpx.Response(200, json=[{"id": "r1", "status": "online"}])
        if p in ("/api/sandboxes", "/api/agents", "/api/workflows", "/api/skills", "/api/mcp-servers") and req.method == "GET":
            return httpx.Response(200, json=[{"id": "local"}] if p == "/api/sandboxes" else [])
        if req.method == "PUT" or p.endswith("/publish") or p == "/api/runners/default":
            return httpx.Response(200, json={"status": "published", "ok": True})
        if p == "/api/issues" and req.method == "POST":
            body = json.loads(req.content)
            dispatched_wfs.append(body["workflowId"])
            return httpx.Response(201, json={"id": f"iss-{len(dispatched_wfs)}", **body})
        if p.endswith("/dispatch"):
            return httpx.Response(200, json={"latestRunId": f"run-{len(dispatched_wfs)}"})
        if p.startswith("/api/runs/run-"):
            return httpx.Response(
                200,
                json={
                    "id": p.rsplit("/", 1)[-1],
                    "status": "succeeded",
                    "artifacts": [
                        {
                            "name": "vernal-result.json",
                            "content": json.dumps(
                                {"headline": "ok", "proposals": ["p"], "concerns": [], "questions": [], "recommended_sandboxes": ["local"]}
                            ),
                        }
                    ],
                },
            )
        return httpx.Response(200, json={})

    eng = Engine(ws, settings=settings)
    harness = LoopForgeHarness(settings, eng.db, ws, poll_interval=0.01)
    real_async = httpx.AsyncClient
    transport = httpx.MockTransport(_lf)
    with patch("autodev.loopforge.client.httpx.AsyncClient", lambda **kw: real_async(transport=transport)):
        for role in ("pm", "qa_lead", "architect"):
            r = asyncio.run(
                harness.run_structured_readonly(
                    "审阅需求",
                    schema_path=eng.schemas["council_opinion"],
                    cwd=ws.root,
                    log_dir=ws.runs_dir / "_council" / f"r1-{role}",
                    role=role,
                )
            )
            assert r.ok
    assert dispatched_wfs == [
        plan_workflow_id("proj-mm", "pm"),
        plan_workflow_id("proj-mm", "qa_lead"),
        plan_workflow_id("proj-mm", "architect"),
    ]
    pm_meta = json.loads((ws.runs_dir / "_council" / "r1-pm" / "loopforge_run.json").read_text("utf-8"))
    qa_meta = json.loads((ws.runs_dir / "_council" / "r1-qa_lead" / "loopforge_run.json").read_text("utf-8"))
    assert pm_meta["executor"] == "opencode" and pm_meta["model"] == "deepseek-chat"
    assert qa_meta["executor"] == "codebuddy" and qa_meta["model"] == "glm-5"
    eng.db.close()


def test_multi_project_isolation_and_auth_guards(tmp_path: Path) -> None:
    ws_root = tmp_path / "workspaces"
    ws_a = init_workspace(ws_root / "proj-a", name="Project A", collaboration_mode="files")
    ws_b = init_workspace(ws_root / "proj-b", name="Project B", collaboration_mode="files")
    # 给 proj-b 写入独立 token，验证 proj-a 不会跨工作区借用它的令牌
    cfg_b = ws_b.config_path.read_text("utf-8").replace('loopforge_token = ""', 'loopforge_token = "afp_b_secret"')
    cfg_b = cfg_b.replace('loopforge_project_id = ""', 'loopforge_project_id = "proj-b"')
    ws_b.config_path.write_text(cfg_b, "utf-8")

    eng_a = Engine(ws_a, settings=Settings(collaboration_mode="files", runtime_backend="loopforge", loopforge_project_id="proj-a", loopforge_token="afp_a_tok"))
    # 预先写入一条规划上下文，测试局域网签名保护
    bdir = ws_a.state_dir / "baselines"
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / "plan-blueprint.json").write_text(json.dumps({"files": {"PLAN_PROMPT.md": "secret"}}), "utf-8")

    def _lf(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        ck = req.headers.get("cookie", "")
        if p == "/api/auth/current":
            if "af_session=alice" in ck:
                return httpx.Response(200, json={"user": {"loginName": "alice", "isGuest": False}})
            if "af_session=bob" in ck:
                return httpx.Response(200, json={"user": {"loginName": "bob", "isGuest": False}})
            return httpx.Response(401, json={"error": "unauthorized"})
        if p == "/api/projects":
            if "af_session=alice" in ck:
                return httpx.Response(200, json=[{"id": "proj-a", "name": "Project A"}])
            if "af_session=bob" in ck:
                return httpx.Response(200, json=[{"id": "proj-b", "name": "Project B"}])
            return httpx.Response(200, json=[{"id": "proj-a"}, {"id": "proj-b"}])
        if p == "/api/analytics":
            return httpx.Response(500, json={"error": "analytics backend down"})
        if p in ("/api/sandboxes", "/api/agents", "/api/workflows", "/api/skills", "/api/mcp-servers") and req.method == "GET":
            return httpx.Response(200, json=[{"id": "local"}] if p == "/api/sandboxes" else [])
        if req.method == "PUT" or p.endswith("/publish") or p == "/api/runners/default":
            return httpx.Response(200, json={"status": "published", "ok": True})
        if p == "/api/runners":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={})

    real_async = httpx.AsyncClient
    transport = httpx.MockTransport(_lf)
    app = create_app(eng_a, proxy_transport=transport)

    with patch("autodev.loopforge.client.httpx.AsyncClient", lambda **kw: real_async(transport=transport)):
        # 1. 两个并发浏览器（Alice 看 proj-a，Bob 切到 proj-b）互不串台
        with TestClient(app, client=("192.168.1.10", 50001)) as lan_client:
            # 未登录局域网用户尝试敏感写操作 -> 401
            assert lan_client.post("/vernal-api/wish", json={"title": "x", "content": "y"}).status_code == 401
            assert lan_client.post("/vernal-api/agent/config", json={"max_concurrency": 4}).status_code == 401
            # 局域网用户尝试拉取宿主机绑定码 -> 403
            assert lan_client.post("/vernal-api/runner/bind-code").status_code == 403
            # 局域网未带签名拉取规划上下文 -> 401；带合法签名 -> 200
            assert lan_client.get("/vernal-api/plan-context/blueprint.json?project=proj-a").status_code == 401
            sig = sign_context_sig("afp_a_tok", "proj-a", "plan:blueprint")
            ok_ctx = lan_client.get(f"/vernal-api/plan-context/blueprint.json?project=proj-a&sig={sig}")
            assert ok_ctx.status_code == 200 and ok_ctx.json()["files"]["PLAN_PROMPT.md"] == "secret"

            # Bob 登录但试图操作无权的 proj-a -> 403
            bob_headers_a = {"Cookie": "af_session=bob", "X-Vernal-Project": "proj-a"}
            assert lan_client.post("/vernal-api/wish", headers=bob_headers_a, json={"title": "hack", "content": "no"}).status_code == 403

            # Bob 切到并提交自己的 proj-b -> 200
            bob_headers_b = {"Cookie": "af_session=bob", "X-Vernal-Project": "proj-b"}
            assert lan_client.post("/vernal-api/project/select", headers=bob_headers_b, json={"project_id": "proj-b", "project_name": "Project B"}).status_code == 200
            assert lan_client.post("/vernal-api/wish", headers=bob_headers_b, json={"title": "b-wish", "content": "Bob 的需求"}).status_code == 200

            # 同时 Alice 仍在 proj-a 提交并查询 -> 完全不受 Bob 切换影响！
            alice_headers_a = {"Cookie": "af_session=alice", "X-Vernal-Project": "proj-a"}
            assert lan_client.post("/vernal-api/wish", headers=alice_headers_a, json={"title": "a-wish", "content": "Alice 的需求"}).status_code == 200

            wishes_a = lan_client.get("/vernal-api/wish", headers=alice_headers_a).json()["items"]
            wishes_b = lan_client.get("/vernal-api/wish", headers=bob_headers_b).json()["items"]
            assert [w["name"] for w in wishes_a] == ["a-wish"]
            assert [w["name"] for w in wishes_b] == ["b-wish"]

            # 统计接口不再静默返回空对象，必须明确透出 loopforge_error 与来源
            an = lan_client.get("/vernal-api/runtime/analytics", headers=alice_headers_a).json()
            assert an["loopforge_ok"] is False
            assert "analytics backend down" in an["loopforge_error"]
            assert an["project_id"] == "proj-a"
            assert "local" in an["sources"] and "loopforge" in an["sources"]

        # 2. 本机调用 bind-code 时，当前项目无 token 绝不偷借相邻工作区 proj-b 的 token
        eng_empty = Engine(init_workspace(ws_root / "proj-empty", name="Empty", collaboration_mode="files"))
        app_empty = create_app(eng_empty, proxy_transport=transport)
        with TestClient(app_empty, client=("127.0.0.1", 50002)) as local_client:
            r_no_tok = local_client.post("/vernal-api/runner/bind-code")
            assert r_no_tok.status_code == 401
        eng_empty.db.close()

    eng_a.db.close()


def test_new_project_first_open_does_not_404_and_initializes_cleanly(tmp_path: Path) -> None:
    """新项目首次在 LoopForge 顶栏选中但尚未建本地工作区时，查状态返回 200 (workspace_initialized=False) 而非 404。"""
    ws_root = tmp_path / "workspaces"
    ws_default = init_workspace(ws_root / "proj-default", name="Default", collaboration_mode="files")
    eng = Engine(
        ws_default,
        settings=Settings(
            collaboration_mode="files",
            runtime_backend="loopforge",
            loopforge_url="http://lf.test",
            loopforge_token="afp_tok",
            loopforge_project_id="proj-default",
        ),
    )

    def _lf(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p == "/api/health":
            return httpx.Response(200, json={"status": "ok"})
        if p == "/api/projects":
            return httpx.Response(200, json=[{"id": "proj-default", "name": "Default"}, {"id": "brand-new", "name": "Brand New"}])
        if p == "/api/runners":
            return httpx.Response(200, json=[{"id": "r-mac", "name": "MacRunner", "status": "online"}])
        if p in ("/api/workflows", "/api/agents", "/api/sandboxes", "/api/executors", "/api/runs", "/api/skills", "/api/mcp-servers"):
            return httpx.Response(200, json=[{"id": "local"}] if p == "/api/sandboxes" else [])
        if req.method == "PUT" or p.endswith("/publish") or p == "/api/runners/default":
            return httpx.Response(200, json={"status": "published", "ok": True})
        return httpx.Response(200, json={})

    real_async = httpx.AsyncClient
    transport = httpx.MockTransport(_lf)
    app = create_app(eng, proxy_transport=transport)

    with patch("autodev.loopforge.client.httpx.AsyncClient", lambda **kw: real_async(transport=transport)):
        with TestClient(app, client=("127.0.0.1", 50003)) as client:
            # 未初始化项目首查状态：必须 200 并标记 workspace_initialized=False（不能 404 导致前端跳过初始化）
            st1 = client.get("/vernal-api/runtime/status", headers={"X-Vernal-Project": "brand-new"})
            assert st1.status_code == 200
            body1 = st1.json()
            assert body1["workspace_initialized"] is False
            assert body1["requested_project_id"] == "brand-new"
            assert body1["runners_online_count"] == 1
            assert body1["online_runners"][0]["name"] == "MacRunner"

            # 随后 /project/select 初始化该新项目工作区
            sel = client.post(
                "/vernal-api/project/select",
                headers={"X-Vernal-Project": "brand-new"},
                json={"project_id": "brand-new", "project_name": "Brand New"},
            )
            assert sel.status_code == 200
            assert (ws_root / "brand-new" / ".autodev" / "config.toml").exists()

            # 再次查询状态：已初始化为 True
            st2 = client.get("/vernal-api/runtime/status", headers={"X-Vernal-Project": "brand-new"})
            assert st2.status_code == 200
            assert st2.json()["workspace_initialized"] is True

    eng.db.close()


def test_runner_health_reports_real_online_offline_unbound_and_blocks_lan(tmp_path: Path) -> None:
    """/vernal-api/runner/health 真实核对 LoopForge 在线 Runner 心跳，且仅限本机调用。"""
    ws = init_workspace(tmp_path / "proj-rh", name="RH", collaboration_mode="files")
    runners_state = {"items": [{"id": "r1", "name": "StudioMac", "status": "online"}]}

    def _lf(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/runners":
            return httpx.Response(200, json=runners_state["items"])
        return httpx.Response(200, json={})

    eng = Engine(
        ws,
        settings=Settings(
            collaboration_mode="files",
            runtime_backend="loopforge",
            loopforge_url="http://lf.test",
            loopforge_token="afp_tok",
            loopforge_project_id="proj-rh",
        ),
    )
    real_async = httpx.AsyncClient
    transport = httpx.MockTransport(_lf)
    app = create_app(eng, proxy_transport=transport)

    with patch("autodev.loopforge.client.httpx.AsyncClient", lambda **kw: real_async(transport=transport)):
        with TestClient(app, client=("192.168.1.99", 50004)) as lan:
            assert lan.get("/vernal-api/runner/health").status_code == 403

        with TestClient(app, client=("127.0.0.1", 50005)) as local:
            r_on = local.get("/vernal-api/runner/health?format=raw")
            assert r_on.status_code == 200
            assert r_on.text == "online:1:StudioMac"

            runners_state["items"] = [{"id": "r1", "name": "StudioMac", "status": "offline"}]
            r_off = local.get("/vernal-api/runner/health?format=raw")
            assert r_off.status_code == 200
            assert r_off.text == "offline:0:"

            r_json = local.get("/vernal-api/runner/health").json()
            assert r_json["status"] == "offline" and r_json["online_count"] == 0

    eng.db.close()

    # 未绑定 token 时返回 unbound:0:
    ws_u = init_workspace(tmp_path / "proj-unbound", name="Unbound", collaboration_mode="files")
    eng_u = Engine(ws_u, settings=Settings(collaboration_mode="files", runtime_backend="loopforge"))
    app_u = create_app(eng_u, proxy_transport=transport)
    with TestClient(app_u, client=("127.0.0.1", 50006)) as local_u:
        assert local_u.get("/vernal-api/runner/health?format=raw").text == "unbound:0:"
    eng_u.db.close()


def test_coding_task_poll_respects_agent_timeout_sec(tmp_path: Path) -> None:
    """开发任务轮询必须遵守 Settings.agent_timeout_sec，Runner 卡住时超时返回而非死等。"""
    from autodev.db import Task
    from autodev.loopforge.bridge import LoopForgeBridge
    from autodev.models import TaskStatus

    ws = init_workspace(tmp_path / "proj-timeout", name="Timeout", collaboration_mode="files")
    settings = Settings(
        collaboration_mode="files",
        runtime_backend="loopforge",
        loopforge_url="http://lf.test",
        loopforge_token="afp_tok",
        loopforge_project_id="proj-timeout",
        loopforge_workflow_id="proj-timeout-vernal-file-sop",
        agent_timeout_sec=1,
    )
    eng = Engine(ws, settings=settings)
    task = Task(
        task_id="T-001",
        milestone_id="M1",
        squad="core",
        module="core",
        role="coder",
        title="会卡住的任务",
        description="模拟 Runner 卡死",
        acceptance_criteria=[],
        depends_on=[],
        doc_slice_ids=[],
        allowed_paths=["src/"],
        verify_cmds=[],
        priority=1,
        status=TaskStatus.IN_PROGRESS,
        retry_count=0,
        max_retries=2,
        arbitration_count=0,
        assigned_slot="slot-1",
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

    def _lf(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p == "/api/runners":
            return httpx.Response(200, json=[{"id": "r1", "status": "online"}])
        if p in ("/api/sandboxes", "/api/agents", "/api/workflows", "/api/skills", "/api/mcp-servers") and req.method == "GET":
            return httpx.Response(200, json=[{"id": "local"}] if p == "/api/sandboxes" else [])
        if req.method == "PUT" or p.endswith("/publish") or p == "/api/runners/default":
            return httpx.Response(200, json={"status": "published", "ok": True})
        if p == "/api/issues" and req.method == "POST":
            return httpx.Response(201, json={"id": "iss-hang"})
        if p.endswith("/dispatch"):
            return httpx.Response(200, json={"latestRunId": "run-hang"})
        if p == "/api/runs/run-hang":
            return httpx.Response(200, json={"id": "run-hang", "status": "running", "nodes": {}})
        return httpx.Response(200, json={})

    real_async = httpx.AsyncClient
    transport = httpx.MockTransport(_lf)
    bridge = LoopForgeBridge(settings, eng.db)

    with patch("autodev.loopforge.client.httpx.AsyncClient", lambda **kw: real_async(transport=transport)):
        ok, summary = asyncio.run(
            bridge.execute_task(
                task,
                squad_name="核心研发组",
                slot_id="slot-1",
                stop_event=asyncio.Event(),
            )
        )
    assert ok is False
    assert "超时" in summary
    eng.db.close()


def test_zero_python_host_node_fallback_for_restore_and_sop_scripts(tmp_path: Path) -> None:
    """在没有 python3 的宿主机（PATH 中屏蔽 python3，仅提供 node）下，还原命令与 SOP 打包/校验脚本均自动走 Node 跑通。"""
    import os
    import shutil
    from autodev.loopforge.bootstrap import PACKAGE_FILES_SHELL_SCRIPT, VALIDATE_PLAN_JSON_SHELL_SCRIPT

    node_bin = shutil.which("node")
    assert node_bin, "测试环境需要安装 node 才能验证无 Python 宿主机兼容分支"

    # 构造一个不包含 python3 的纯净 PATH 目录（只放入 node 与必要基础命令软链）
    no_py_bin = tmp_path / "no-python-bin"
    no_py_bin.mkdir()
    os.symlink(node_bin, no_py_bin / "node")
    for cmd_name in ("sh", "bash", "cat", "rm", "mkdir"):
        found = shutil.which(cmd_name)
        if found:
            os.symlink(found, no_py_bin / cmd_name)
    env_no_py = {"PATH": str(no_py_bin), "HOME": str(tmp_path)}

    # 1. 验证 build_restore_command 的 Node 降级分支（含中文、空格与单引号路径 + save_baseline=True）
    tricky_dir = tmp_path / "无Python 宿主机" / "o'connor"
    tricky_dir.mkdir(parents=True)
    bundle = tricky_dir / "T-099.json"
    bundle.write_text(
        json.dumps({"src/main.js": "console.log('hi');\n", "README.md": "你好\n"}, ensure_ascii=False),
        "utf-8",
    )
    ws_restore = tmp_path / "ws_restore"
    ws_restore.mkdir()
    cmd = build_restore_command(str(bundle), "http://127.0.0.1:1/unused", nested_key="", save_baseline=True)
    r1 = subprocess.run(["/bin/sh", "-c", cmd], cwd=ws_restore, env=env_no_py, capture_output=True, text=True)
    assert r1.returncode == 0, f"stderr={r1.stderr}"
    assert (ws_restore / "src" / "main.js").read_text("utf-8") == "console.log('hi');\n"
    assert json.loads((ws_restore / ".vernal-baseline.json").read_text("utf-8"))["README.md"] == "你好\n"

    # 2. 验证 PACKAGE_FILES_SHELL_SCRIPT 的 Node 降级分支（新增、修改、删除识别 + vernal-files.json 产出）
    (ws_restore / "src" / "main.js").write_text("console.log('updated');\n", "utf-8")
    (ws_restore / "src" / "new.js").write_text("export const x = 1;\n", "utf-8")
    (ws_restore / "README.md").unlink()
    r2 = subprocess.run(["/bin/sh", "-c", PACKAGE_FILES_SHELL_SCRIPT], cwd=ws_restore, env=env_no_py, capture_output=True, text=True)
    assert r2.returncode == 0, f"stderr={r2.stderr}"
    pkg = json.loads((ws_restore / "vernal-files.json").read_text("utf-8"))
    assert pkg["files"] == {
        "src/main.js": "console.log('updated');\n",
        "src/new.js": "export const x = 1;\n",
        "README.md": None,
    }

    # 3. 验证 VALIDATE_PLAN_JSON_SHELL_SCRIPT 的 Node 降级分支（有效 JSON 通过，缺文件报错退出）
    ws_plan = tmp_path / "ws_plan"
    ws_plan.mkdir()
    (ws_plan / "vernal-result.json").write_text(json.dumps({"headline": "方案通过"}), "utf-8")
    r3 = subprocess.run(["/bin/sh", "-c", VALIDATE_PLAN_JSON_SHELL_SCRIPT], cwd=ws_plan, env=env_no_py, capture_output=True, text=True)
    assert r3.returncode == 0, f"stderr={r3.stderr}"
    assert "校验通过" in r3.stdout

    (ws_plan / "vernal-result.json").unlink()
    r4 = subprocess.run(["/bin/sh", "-c", VALIDATE_PLAN_JSON_SHELL_SCRIPT], cwd=ws_plan, env=env_no_py, capture_output=True, text=True)
    assert r4.returncode != 0


def test_restore_command_fails_fast_on_http_404_and_bad_bundle_in_node_and_python(tmp_path: Path) -> None:
    """无 Python 宿主机收到 HTTP 404 或非法文件包时，必须非零退出且绝不写出 detail 假文件。"""
    import os
    import shutil

    node_bin = shutil.which("node")
    assert node_bin
    no_py_bin = tmp_path / "no-py-404"
    no_py_bin.mkdir()
    os.symlink(node_bin, no_py_bin / "node")
    for cmd_name in ("sh", "bash", "cat", "rm", "mkdir"):
        found = shutil.which(cmd_name)
        if found:
            os.symlink(found, no_py_bin / cmd_name)

    # 1. 注入模拟 HTTP 404 {"detail": "not found"} 的 fetch 响应，零端口、纯沙箱实跑 Node 还原脚本
    mock_fetch_js = tmp_path / "mock_fetch_404.js"
    mock_fetch_js.write_text(
        'globalThis.fetch = async () => ({ ok: false, status: 404, '
        'text: async () => \'{"detail":"not found"}\', '
        'json: async () => ({ detail: "not found" }) });\n',
        "utf-8",
    )
    env_no_py = {"PATH": str(no_py_bin), "HOME": str(tmp_path)}
    env_no_py_404 = {**env_no_py, "NODE_OPTIONS": f"--require {mock_fetch_js}"}
    url = "http://127.0.0.1:8787/vernal-api/tasks/T-404/baseline.json"

    # 规划模式（nested_key="files"）收到 404 -> 必须非零退出且不写任何文件
    ws_plan = tmp_path / "ws_404_plan"
    ws_plan.mkdir()
    cmd_plan = build_restore_command("/nonexistent/plan.json", url, nested_key="files", save_baseline=False)
    r_plan = subprocess.run(["/bin/sh", "-c", cmd_plan], cwd=ws_plan, env=env_no_py_404, capture_output=True, text=True)
    assert r_plan.returncode != 0
    assert "HTTP 404" in r_plan.stderr
    assert list(ws_plan.iterdir()) == []

    # 代码模式（nested_key="", save_baseline=True）收到 404 -> 必须非零退出且绝不生成 detail 文件
    ws_code = tmp_path / "ws_404_code"
    ws_code.mkdir()
    cmd_code = build_restore_command("/nonexistent/base.json", url, nested_key="", save_baseline=True)
    r_code = subprocess.run(["/bin/sh", "-c", cmd_code], cwd=ws_code, env=env_no_py_404, capture_output=True, text=True)
    assert r_code.returncode != 0
    assert "HTTP 404" in r_code.stderr
    assert not (ws_code / "detail").exists()
    assert not (ws_code / ".vernal-baseline.json").exists()

    # 2. 本地非法文件包（空 files 或非字符串内容）：Python 与 Node 分支都必须非零退出
    bad_empty = tmp_path / "bad_empty.json"
    bad_empty.write_text(json.dumps({"files": {}}), "utf-8")
    ws_bad1 = tmp_path / "ws_bad1"
    ws_bad1.mkdir()
    cmd_bad1 = build_restore_command(str(bad_empty), "http://127.0.0.1:1/x", nested_key="files", save_baseline=False)
    assert subprocess.run(["/bin/sh", "-c", cmd_bad1], cwd=ws_bad1, env=env_no_py, capture_output=True, text=True).returncode != 0
    assert subprocess.run(cmd_bad1, shell=True, cwd=ws_bad1, capture_output=True, text=True).returncode != 0

    bad_type = tmp_path / "bad_type.json"
    bad_type.write_text(json.dumps({"detail": 404}), "utf-8")
    ws_bad2 = tmp_path / "ws_bad2"
    ws_bad2.mkdir()
    cmd_bad2 = build_restore_command(str(bad_type), "http://127.0.0.1:1/x", nested_key="", save_baseline=True)
    assert subprocess.run(["/bin/sh", "-c", cmd_bad2], cwd=ws_bad2, env=env_no_py, capture_output=True, text=True).returncode != 0
    assert subprocess.run(cmd_bad2, shell=True, cwd=ws_bad2, capture_output=True, text=True).returncode != 0


def test_existing_published_legacy_sop_upgrades_and_keeps_user_customizations(tmp_path: Path) -> None:
    """已有项目重新同步资产时：自动升级未改动的旧版 Python 脚本节点，同时保留用户手工改过的节点与自定义脚本。"""
    from autodev.loopforge.bootstrap import (
        PACKAGE_FILES_SCRIPT,
        PACKAGE_FILES_SHELL_SCRIPT,
        VALIDATE_PLAN_JSON_SCRIPT,
        VALIDATE_PLAN_JSON_SHELL_SCRIPT,
        build_plan_workflow,
        build_vernal_workflows,
        ensure_vernal_loopforge_assets,
    )
    from autodev.loopforge.client import LoopForgeClient

    pid = "proj-legacy"
    # 1. 旧版已发布规划 SOP：validate 节点为旧版 Python 脚本，同时用户手工把 draft.maxAttempts 改成了 5
    legacy_plan = build_plan_workflow(pid, sandbox_id="local", role="director")
    legacy_plan["revision"] = 3
    legacy_plan["nodes"][0]["maxAttempts"] = 5
    legacy_plan["nodes"][1]["scriptLanguage"] = "python"
    legacy_plan["nodes"][1]["script"] = VALIDATE_PLAN_JSON_SCRIPT

    # 2. 用户手工定制过 package 脚本的 file SOP：不应被覆盖
    custom_file = build_vernal_workflows(pid, sandbox_id="local")["files"]
    custom_file["revision"] = 4
    custom_file["nodes"][1]["scriptLanguage"] = "python"
    custom_file["nodes"][1]["script"] = "print('my custom packager')"

    # 3. 未改动的旧版 file-frontend SOP：应自动升级为 shell 双运行时
    legacy_fe = build_vernal_workflows(pid, sandbox_id="local")["files"]
    legacy_fe["id"] = f"{pid}-vernal-file-frontend-sop"
    legacy_fe["revision"] = 2
    legacy_fe["nodes"][1]["scriptLanguage"] = "python"
    legacy_fe["nodes"][1]["script"] = PACKAGE_FILES_SCRIPT

    put_wfs: dict[str, dict] = {}
    published_ids: list[str] = []

    def _lf(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p == "/api/sandboxes":
            return httpx.Response(200, json=[{"id": "local"}])
        if p == "/api/workflows" and req.method == "GET":
            return httpx.Response(200, json=[legacy_plan, custom_file, legacy_fe])
        if p == "/api/workflows" and req.method == "PUT":
            body = json.loads(req.content)
            put_wfs[body["id"]] = body
            return httpx.Response(200, json={**body, "status": "draft"})
        if p.startswith("/api/workflows/") and p.endswith("/publish"):
            published_ids.append(p.split("/")[3])
            return httpx.Response(200, json={"status": "published"})
        if p in ("/api/agents", "/api/skills", "/api/mcp-servers", "/api/runners") and req.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"ok": True})

    real_async = httpx.AsyncClient
    transport = httpx.MockTransport(_lf)
    settings = Settings(collaboration_mode="files", runtime_backend="loopforge", loopforge_url="http://lf.test", loopforge_token="tok")
    client = LoopForgeClient("http://lf.test", "tok")

    with patch("autodev.loopforge.client.httpx.AsyncClient", lambda **kw: real_async(transport=transport)):
        asyncio.run(ensure_vernal_loopforge_assets(client, pid, settings))

    # 旧版规划 SOP 被升级并重新发布，且用户对 draft.maxAttempts=5 和 revision=3 的改动原样保留
    upgraded_plan = put_wfs[legacy_plan["id"]]
    assert upgraded_plan["revision"] == 3
    assert upgraded_plan["nodes"][0]["maxAttempts"] == 5
    assert upgraded_plan["nodes"][1]["scriptLanguage"] == "shell"
    assert upgraded_plan["nodes"][1]["script"] == VALIDATE_PLAN_JSON_SHELL_SCRIPT
    assert legacy_plan["id"] in published_ids

    # 未改动的旧版 frontend SOP 同样升级为 shell 双运行时
    upgraded_fe = put_wfs[legacy_fe["id"]]
    assert upgraded_fe["nodes"][1]["scriptLanguage"] == "shell"
    assert upgraded_fe["nodes"][1]["script"] == PACKAGE_FILES_SHELL_SCRIPT
    assert legacy_fe["id"] in published_ids

    # 用户手工定制脚本的 file SOP 绝不被覆盖
    assert custom_file["id"] not in put_wfs


def test_merge_milestone_pushes_origin_and_rolls_back_on_push_failure(tmp_path: Path) -> None:
    """Git 模式合入里程碑时若配置了 origin 远端，必须真正推送远端主分支；推送失败时回滚本地合入，绝不误报完成。"""
    import pytest
    from autodev import gitops
    from autodev.engine import OpsError
    from autodev.models import MilestoneStatus, ProjectStatus

    remote_bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote_bare)], check=True, capture_output=True)

    ws = init_workspace(tmp_path / "repo", name="GitRemote", collaboration_mode="git")
    gitops.git(["remote", "add", "origin", str(remote_bare)], ws.root)
    gitops.git(["push", "-u", "origin", "main"], ws.root)
    initial_sha = gitops.head_sha(ws.root)

    eng = Engine(ws, settings=Settings(collaboration_mode="git", base_branch="main"))
    # 插入一个待合并里程碑 M1，并在分支 autodev/m/M1 上提交改动
    eng.db._conn.execute(
        "INSERT INTO milestones (milestone_id, seq_order, title, goal, integration_cmd, status, planned, branch_name) VALUES (?,?,?,?,?,?,?,?)",
        ("M1", 1, "M1", "交付功能", "", MilestoneStatus.AWAITING_MERGE.value, 1, "autodev/m/M1"),
    )
    gitops.git(["checkout", "-b", "autodev/m/M1"], ws.root)
    (ws.root / "feature.txt").write_text("v1\n", "utf-8")
    gitops.commit_all(ws.root, "feat: m1")
    m1_sha = gitops.head_sha(ws.root)
    gitops.git(["checkout", "main"], ws.root)

    # 1. 正常远端：合入后远端 origin/main 必须包含新提交，里程碑与项目标为完成
    eng.merge_milestone("M1", actor="tester")
    remote_sha = subprocess.run(
        ["git", "--git-dir", str(remote_bare), "rev-parse", "refs/heads/main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert remote_sha == m1_sha
    assert gitops.head_sha(ws.root) == m1_sha
    assert eng.db.get_milestone("M1").status == MilestoneStatus.MERGED
    assert eng.status == ProjectStatus.COMPLETED

    # 2. 模拟远端不可达/拒绝推送：必须抛 OpsError、回滚本地 main，且绝不把 M2 标为 MERGED
    eng.db._conn.execute(
        "INSERT INTO milestones (milestone_id, seq_order, title, goal, integration_cmd, status, planned, branch_name) VALUES (?,?,?,?,?,?,?,?)",
        ("M2", 2, "M2", "二期功能", "", MilestoneStatus.AWAITING_MERGE.value, 1, "autodev/m/M2"),
    )
    eng.set_status(ProjectStatus.RUNNING)
    gitops.git(["checkout", "-b", "autodev/m/M2"], ws.root)
    (ws.root / "feature2.txt").write_text("v2\n", "utf-8")
    gitops.commit_all(ws.root, "feat: m2")
    gitops.git(["checkout", "main"], ws.root)
    gitops.git(["remote", "set-url", "origin", str(tmp_path / "missing-remote.git")], ws.root)

    with pytest.raises(OpsError, match="推送 main 到远端 origin 失败"):
        eng.merge_milestone("M2", actor="tester")

    assert gitops.head_sha(ws.root) == m1_sha
    assert eng.db.get_milestone("M2").status == MilestoneStatus.AWAITING_MERGE
    assert eng.status == ProjectStatus.RUNNING
    assert initial_sha != m1_sha
    eng.db.close()


