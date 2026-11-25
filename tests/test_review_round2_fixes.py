"""第二轮工程评审全量回归测试：
1. 内外网代理合约 + 有/无 socksio 依赖离线校验；
2. 里程碑人工审查 + Gitea CI 联合开启完整放行闭环；远端工单等产物期间重置 READY + 替代 run ID 原子 CAS 保护；
3. Gitea CI 真实发布隔离分支/40位提交/非破坏性 workflow（git 与 files 双模式）+ 失败->修复->成功真跑测试与质量门 CLI + GitLab external_id 重启恢复；
4. 旧轮回调与轮询并发隔离、里程碑审查绑定当前候选、受控配置部署与版本/操作者校验、移除硬编码回调密钥并拒绝未知项目。
"""

from __future__ import annotations

import asyncio
import builtins
import json
from pathlib import Path
import re
import subprocess
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

from autodev import gitops
from autodev.ci.service import handle_ci_callback, make_callback_url, poll_ci_run, trigger_milestone_ci
from autodev.db import now_iso
from autodev.engine import Engine, OpsError, init_workspace
from autodev.harnesses.mock import MockHarness
from autodev.loopforge.bridge import sign_context_sig
from autodev.loopforge.client import LoopForgeClient, LoopForgeError, create_async_http_client, use_environment_proxy
from autodev.models import MilestoneStatus, PlanMilestone, PlanModule, PlanTask, ProjectPlan, ProjectStatus, TaskStatus
from autodev.process.runner import load_builtin_template, save_custom_template
from autodev.review.service import current_milestone_candidate_ref, open_review
from autodev.scheduler import Scheduler
from autodev.server.app import create_app


def _offline_proxy() -> httpx.MockTransport:
    return httpx.MockTransport(lambda req: httpx.Response(200, json={"ok": True}))


# ---------- 评审项 1：内外网代理合约与有/无 socksio 依赖离线测试 ----------


def test_proxy_contract_with_and_without_socksio(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:1080")
    monkeypatch.setenv("HTTPS_PROXY", "socks5://127.0.0.1:1080")

    # 1. 内网/回环与公网代理合约始终保持不变，不因缺少 socksio 退化为公网直连
    assert use_environment_proxy("http://127.0.0.1:8090") is False
    assert use_environment_proxy("http://loopforge:8080") is False
    assert use_environment_proxy("http://192.168.1.20:3000") is False
    assert use_environment_proxy("https://loopforge.example.com") is True

    # 2. 无 socksio 依赖分支：模拟外部请求需要 SOCKS 代理时给出清晰可操作错误，而内网直连或显式注入 transport 正常工作
    real_import = builtins.__import__

    def _block_socksio(name: str, *args: object, **kwargs: object):
        if name == "socksio" or name.startswith("socksio."):
            raise ImportError("Using SOCKS proxy, but the 'socksio' package is not installed.")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _block_socksio)

    with pytest.raises(RuntimeError, match="socksio"):
        create_async_http_client("https://loopforge.example.com/api/health")

    lf = LoopForgeClient("https://loopforge.example.com", "tok")
    ok_h, info_h = asyncio.run(lf.health())
    assert ok_h is False and "socksio" in str(info_h.get("error", ""))
    with pytest.raises(LoopForgeError, match="socksio"):
        asyncio.run(lf.list_projects())

    # 本机地址绕过宿主机 SOCKS 代理，无 socksio 也能正常初始化客户端
    local_client = create_async_http_client("http://127.0.0.1:8090/api/health")
    asyncio.run(local_client.aclose())

    # 离线测试显式注入 transport 时，外部地址不触发宿主机 SOCKS 代理报错
    injected = LoopForgeClient(
        "https://loopforge.example.com",
        "tok",
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"status": "ok"})),
    )
    ok, data = asyncio.run(injected.health())
    assert ok is True and data["status"] == "ok"

    # 3. 具备代理依赖分支：模拟具备代理传输能力时 trust_env=True 保留外网代理挂载
    captured_kwargs: dict[str, object] = {}

    class _FakeSocksCapableClient:
        def __init__(self, **kw: object) -> None:
            captured_kwargs.update(kw)

    monkeypatch.setattr(httpx, "AsyncClient", _FakeSocksCapableClient)
    create_async_http_client("https://loopforge.example.com/api/health", timeout=9.0)
    assert captured_kwargs["trust_env"] is True


# ---------- 评审项 2A：里程碑人工审查 + Gitea CI 同时开启完整闭环 ----------


def test_milestone_review_plus_gitea_ci_opens_review_and_merges_cleanly(tmp_path: Path) -> None:
    bare_remote = tmp_path / "gitea-ms-rev.git"
    gitops.git(["init", "--bare", "-q"], tmp_path, check=False)
    subprocess.run(["git", "init", "--bare", "-q", str(bare_remote)], check=True)

    def gitea_ok_handler(req: httpx.Request) -> httpx.Response:
        if "/commits/" in req.url.path and req.url.path.endswith("/status"):
            return httpx.Response(
                200,
                json={
                    "state": "success",
                    "statuses": [{"context": "vernal-ci / verify", "status": "success", "target_url": "http://gitea.local/r/1"}],
                },
            )
        return httpx.Response(201, json={"ok": True})

    def coder_fn(_p: str, cwd: Path, _c: object) -> dict[str, object]:
        (cwd / "app.py").write_text("VALUE = 42\n", "utf-8")
        return {"status": "DONE", "summary": "ok", "files_changed": ["app.py"], "tests_added": [], "notes": ""}

    ws = init_workspace(tmp_path / "ms-rev-ci", name="MsRevCI", collaboration_mode="files")
    eng = Engine(ws, harness=MockHarness(coder=coder_fn))
    try:
        eng.settings.runtime_backend = "local"
        eng.settings.review_policy = "milestone"
        eng.settings.ci_provider = "gitea"
        eng.settings.ci_url = "http://gitea.local"
        eng.settings.ci_git_remote = str(bare_remote)
        eng.settings.require_merge_approval = False

        tpl = load_builtin_template("standard")
        tpl.review_policy = "milestone"
        for s in tpl.stages:
            if s.id == "detailed_design":
                s.enabled = False
            if s.id == "testing":
                s.gates = [g for g in s.gates if g.kind != "quality"]
        save_custom_template(ws, json.dumps(tpl.model_dump(), ensure_ascii=False))

        plan = ProjectPlan(
            summary="里程碑审查与 CI 联调",
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

        asyncio.run(Scheduler(eng, ci_transport=httpx.MockTransport(gitea_ok_handler)).run())
        assert eng.db.get_milestone("M1").status == MilestoneStatus.AWAITING_MERGE

        # CI 通过后必须已为当前候选自动打开 1 张 OPEN 里程碑审查单，且未批准前禁止合入
        client = TestClient(create_app(eng, proxy_transport=_offline_proxy()))
        revs = client.get("/vernal-api/reviews?status=OPEN").json()
        assert len(revs) == 1
        assert revs[0]["scope_type"] == "milestone"
        assert revs[0]["head_ref"] == current_milestone_candidate_ref(eng, "M1")

        with pytest.raises(OpsError, match="里程碑人工代码审查"):
            eng.merge_milestone("M1", actor="lead")

        dec = client.post(
            f"/vernal-api/reviews/{revs[0]['review_id']}/decision",
            json={"decision": "approve", "actor": "lead", "expected_head": revs[0]["head_ref"]},
        )
        assert dec.status_code == 200

        eng.merge_milestone("M1", actor="lead")
        assert eng.db.get_milestone("M1").status == MilestoneStatus.MERGED
    finally:
        eng.db.close()


# ---------- 评审项 2B：远端工单等产物期间重置 READY 并绑定替代 run ID 的原子 CAS 保护 ----------


def test_remote_artifact_failure_does_not_clobber_replacement_run_or_retry_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    orig_init = LoopForgeClient.__init__

    def _run_scenario(mode: str) -> None:
        ws = init_workspace(tmp_path / f"cas-{mode}", name=f"CAS-{mode}", collaboration_mode="files")
        eng = Engine(ws, harness=MockHarness())
        try:
            eng.settings.runtime_backend = "loopforge"
            eng.settings.loopforge_url = "http://127.0.0.1:8090"
            eng.settings.loopforge_token = "tok-cas"
            eng.settings.loopforge_project_id = "proj-cas"
            eng.settings.loopforge_workflow_id = "wf-cas"

            plan = ProjectPlan(
                summary="远端 CAS 并发回归",
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
                        title="远端任务",
                        description="d",
                        acceptance_criteria=["ok"],
                        depends_on=[],
                        doc_slice_ids=[],
                        allowed_paths=["a.py"],
                        verify_cmds=["true"],
                        priority=10,
                    )
                ],
            )
            eng.db.save_plan(plan, max_retries=3)
            eng.db.update_task("T-001", status=TaskStatus.IN_PROGRESS, loopforge_issue_id="iss-1", loopforge_run_id="run-old")

            def lf_handler(req: httpx.Request) -> httpx.Response:
                p = req.url.path
                if p == "/api/runs/run-old":
                    return httpx.Response(
                        200,
                        json={
                            "id": "run-old",
                            "status": "succeeded",
                            "nodeRuns": {},
                            "artifacts": [{"id": "art-1", "name": "vernal-files.json"}],
                        },
                    )
                if p == "/api/workflows/wf-cas":
                    return httpx.Response(200, json={"id": "wf-cas", "nodes": []})
                if "vernal-files.json" in p or "/artifacts" in p:
                    # 模拟正在等产物时外部将工单重置为 READY 并绑定替代 run-new（或永久取消对照）
                    if mode == "cancelled":
                        eng.db.update_task("T-001", status=TaskStatus.CANCELLED)
                    elif mode == "ready_rebound":
                        eng.db.update_task("T-001", status=TaskStatus.READY, loopforge_run_id="run-replacement")
                    elif mode == "in_progress_rebound":
                        eng.db.update_task("T-001", status=TaskStatus.READY, loopforge_run_id="run-replacement")
                        eng.db.claim_task_execution("T-001", allow_from_ready=True)
                    # 返回非法产物触发旧协程校验异常
                    return httpx.Response(200, text="not-valid-json{{{")
                return httpx.Response(200, json={})

            transport = httpx.MockTransport(lf_handler)

            def _patched_init(self_c: LoopForgeClient, base_url: str, token: str = "", timeout: float = 15.0, **kw: object) -> None:
                orig_init(self_c, base_url, token, timeout, transport=transport)

            monkeypatch.setattr(LoopForgeClient, "__init__", _patched_init)

            sched = Scheduler(eng)
            asyncio.run(sched._execute_task("T-001", "slot-1"))

            after_task = eng.db.get_task("T-001")
            assert after_task is not None
            if mode == "cancelled":
                assert after_task.status == TaskStatus.CANCELLED
                assert after_task.retry_count == 0
            elif mode == "ready_rebound":
                assert after_task.status == TaskStatus.READY
                assert after_task.loopforge_run_id == "run-replacement"
                assert after_task.retry_count == 0
            elif mode == "in_progress_rebound":
                assert after_task.status == TaskStatus.IN_PROGRESS
                assert after_task.loopforge_run_id == "run-replacement"
                assert after_task.retry_count == 0
        finally:
            eng.db.close()

    _run_scenario("cancelled")
    _run_scenario("ready_rebound")
    _run_scenario("in_progress_rebound")


# ---------- 评审项 3：真实发布隔离分支、40位提交与工作流，实跑测试与质量门，及 GitLab external_id 恢复 ----------


@pytest.mark.parametrize("collab_mode", ["git", "files"])
def test_gitea_ci_publishes_real_isolated_branch_commit_and_workflow_and_executes_gates(
    tmp_path: Path, collab_mode: str
) -> None:
    bare_remote = tmp_path / f"gitea-origin-{collab_mode}.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare_remote)], check=True)

    ws = init_workspace(tmp_path / f"real-ci-{collab_mode}", name=f"RealCI-{collab_mode}", collaboration_mode=collab_mode)
    # 预置用户自定义 workflow，验证非破坏性写入与合入分支隔离
    user_wf_rel = Path(".gitea/workflows/user-existing.yml")
    if collab_mode == "git":
        gitops.ensure_repo(ws.root, "main")
        (ws.root / user_wf_rel).parent.mkdir(parents=True, exist_ok=True)
        (ws.root / user_wf_rel).write_text("name: keep-user-workflow\n", "utf-8")
        gitops.commit_all(ws.root, "chore: add existing user workflow")
        gitops.git(["remote", "add", "origin", str(bare_remote)], ws.root)
        gitops.git(["push", "-u", "origin", "main"], ws.root)

    ci_exec_records: list[dict[str, object]] = []
    commit_results: dict[str, tuple[str, str, dict[str, object]]] = {}
    from autodev.ci.detect import extract_workflow_steps
    from autodev.quality.cli import install_standalone_quality_cli

    # 安装与 Dockerfile.sandbox 同构的独立 vernal-quality，测试全程剥离宿主机 PYTHONPATH
    sys_py = "/usr/bin/python3" if Path("/usr/bin/python3").is_file() else sys.executable
    sandbox_tools = tmp_path / f"sandbox-tools-{collab_mode}"
    install_standalone_quality_cli(sandbox_tools, python_bin=sys_py)

    def _run_published_ci_branch(sha: str) -> tuple[str, str, dict[str, object]]:
        if sha in commit_results:
            return commit_results[sha]
        # 从远端裸仓库找出指向该 40 位 SHA 的隔离分支 vernal/ci/*
        ls_out = subprocess.run(
            ["git", "ls-remote", "--heads", str(bare_remote), "refs/heads/vernal/ci/*"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        matched_ref = ""
        for line in ls_out.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == sha:
                matched_ref = parts[1].removeprefix("refs/heads/")
                break
        assert matched_ref.startswith("vernal/ci/M1-r"), f"远端未找到提交 {sha} 对应的隔离 CI 分支：{ls_out}"
        assert re.fullmatch(r"[0-9a-f]{40}", sha), f"CI commit_sha 必须是真实 40 位 Git SHA：{sha}"

        runner_dir = tmp_path / f"runner-{collab_mode}-{len(ci_exec_records) + 1}"
        runner_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "--quiet", "--branch", matched_ref, str(bare_remote), str(runner_dir / "src")],
            check=True,
        )
        cloned_head = gitops.head_sha(runner_dir / "src")
        assert cloned_head == sha

        wf_file = runner_dir / "src" / ".gitea" / "workflows" / "vernal-ci.yml"
        assert wf_file.is_file(), "隔离 CI 分支必须包含渲染后的 vernal-ci.yml"
        wf_text = wf_file.read_text("utf-8")
        assert "vernal/ci/**" in wf_text
        if collab_mode == "git":
            assert (runner_dir / "src" / user_wf_rel).read_text("utf-8") == "name: keep-user-workflow\n"

        # 剥离宿主机 PYTHONPATH，逐字执行渲染出的 test 与 quality 工作流步骤！
        steps_by_name = {s["name"]: s["run"] for s in extract_workflow_steps(wf_text)}
        venv_bin = str(Path(sys.executable).parent)
        clean_env = {
            k: v
            for k, v in __import__("os").environ.items()
            if k not in ("PYTHONPATH", "PYTHONHOME")
        }
        clean_env["PATH"] = f"{sandbox_tools / 'bin'}:{venv_bin}:/usr/bin:/bin"
        clean_env["CI"] = "true"

        test_proc = subprocess.run(
            ["sh", "-c", steps_by_name["test"]],
            cwd=str(runner_dir),
            env=clean_env,
            capture_output=True,
            text=True,
        )
        qual_proc = subprocess.run(
            ["sh", "-c", steps_by_name["quality"]],
            cwd=str(runner_dir),
            env=clean_env,
            capture_output=True,
            text=True,
        )
        qual_out_file = runner_dir / "vernal-ci-result.json"
        qual_json = json.loads(qual_out_file.read_text("utf-8"))
        passed = test_proc.returncode == 0 and qual_proc.returncode == 0 and bool(qual_json.get("passed"))
        combined_log = (test_proc.stdout + "\n" + test_proc.stderr + "\n" + qual_proc.stdout + "\n" + qual_proc.stderr).strip()
        state = "success" if passed else "failure"
        ci_exec_records.append(
            {
                "ref": matched_ref,
                "sha": sha,
                "test_rc": test_proc.returncode,
                "quality_rc": qual_proc.returncode,
                "quality": qual_json,
            }
        )
        commit_results[sha] = (state, combined_log, qual_json)
        return commit_results[sha]

    def gitea_real_runner_handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        m_status = re.search(r"/commits/([0-9a-fA-F]{40})/status$", path)
        if m_status:
            sha = m_status.group(1)
            st, _log, qj = _run_published_ci_branch(sha)
            return httpx.Response(
                200,
                json={
                    "state": st,
                    "statuses": [
                        {
                            "context": "vernal-ci / verify",
                            "status": st,
                            "description": "pytest + autodev.quality.cli",
                            "target_url": f"http://gitea.local/actions/{sha[:8]}",
                        }
                    ],
                    "quality": qj,
                },
            )
        m_logs = re.search(r"/commits/([0-9a-fA-F]{40})/logs$", path)
        if m_logs:
            sha = m_logs.group(1)
            _st, log_txt, _qj = _run_published_ci_branch(sha)
            return httpx.Response(200, text=log_txt)
        return httpx.Response(201, json={"ok": True})

    def coder_fn(prompt: str, cwd: Path, _call: object) -> dict[str, object]:
        (cwd / "calc").mkdir(parents=True, exist_ok=True)
        (cwd / "tests").mkdir(parents=True, exist_ok=True)
        (cwd / "calc" / "__init__.py").write_text("", "utf-8")
        if "修复 CI 失败" in prompt:
            # 第二轮修复计算逻辑并补齐有效断言单测（通过 pytest + test_integrity 质量门）
            (cwd / "calc" / "core.py").write_text("def double(x: int) -> int:\n    return x * 2\n", "utf-8")
            (cwd / "tests" / "test_core.py").write_text(
                "from calc.core import double\n\n\ndef test_double() -> None:\n    assert double(3) == 6\n",
                "utf-8",
            )
        else:
            # 第一轮：语法编译在任务阶段通过，CI 全量 pytest 暴露断言错误（double(3) 返回 5 != 6）
            (cwd / "calc" / "core.py").write_text("def double(x: int) -> int:\n    return x + 2\n", "utf-8")
            (cwd / "tests" / "test_core.py").write_text(
                "from calc.core import double\n\n\ndef test_double() -> None:\n    assert double(2) == 4\n    assert double(3) == 6\n",
                "utf-8",
            )
        return {
            "status": "DONE",
            "summary": "implemented calc",
            "files_changed": ["calc/__init__.py", "calc/core.py", "tests/test_core.py"],
            "tests_added": ["tests/test_core.py"],
            "notes": "",
        }

    eng = Engine(ws, harness=MockHarness(coder=coder_fn))
    try:
        eng.settings.runtime_backend = "local"
        eng.settings.ci_provider = "gitea"
        eng.settings.ci_url = "http://gitea.local"
        eng.settings.ci_git_remote = str(bare_remote)
        eng.settings.require_merge_approval = False

        tpl = load_builtin_template("standard")
        tpl.review_policy = "off"
        for s in tpl.stages:
            if s.id == "detailed_design":
                s.enabled = False
            if s.id == "testing":
                s.gates = [g for g in s.gates if g.kind != "quality"]
        save_custom_template(ws, json.dumps(tpl.model_dump(), ensure_ascii=False))

        # 任务级只校验语法编译（让第 1 轮能到达里程碑 CI，由真实隔离分支 CI 捕获 pytest 断言失败并触发自动修复）
        plan = ProjectPlan(
            summary="真实隔离分支 CI 闭环验证",
            tech_stack="python",
            agents_md="rule",
            modules=[PlanModule(name="calc", path="calc/", description="calc")],
            milestones=[PlanMilestone(milestone_id="M1", title="M1", goal="交付计算模块", integration_cmd="python3 -m compileall -q calc")],
            tasks=[
                PlanTask(
                    task_id="T-001",
                    milestone_id="M1",
                    module="calc",
                    role="coder",
                    title="实现倍增计算",
                    description="实现 calc/core.py 与 tests/test_core.py",
                    acceptance_criteria=["测试通过"],
                    depends_on=[],
                    doc_slice_ids=[],
                    allowed_paths=["calc/", "tests/"],
                    verify_cmds=["python3 -m compileall -q calc tests", '[ "${CI:-}" != "true" ] || PYTHONPATH=. python3 -m pytest -q'],
                    priority=10,
                )
            ],
        )
        eng.db.save_plan(plan, max_retries=2)
        eng.set_status(ProjectStatus.APPROVED)

        sched = Scheduler(eng, ci_transport=httpx.MockTransport(gitea_real_runner_handler))
        final_st = asyncio.run(sched.run())
        assert final_st == ProjectStatus.COMPLETED
        assert eng.db.get_milestone("M1").status == MilestoneStatus.MERGED

        # 核验两轮真实 CI：第 1 轮测试失败，自动创建 T-002 修复后第 2 轮测试与质量门均通过
        assert len(ci_exec_records) == 2
        assert ci_exec_records[0]["ref"] == "vernal/ci/M1-r1"
        assert ci_exec_records[0]["test_rc"] != 0
        assert ci_exec_records[1]["ref"] == "vernal/ci/M1-r2"
        assert ci_exec_records[1]["test_rc"] == 0
        assert ci_exec_records[1]["quality_rc"] == 0
        q_res = ci_exec_records[1]["quality"]
        assert isinstance(q_res, dict) and q_res["passed"] is True
        gate_names = {g["gate"]: g["status"] for g in q_res["results"]}
        assert gate_names["test_integrity"] == "PASSED"
        assert gate_names["secrets"] == "PASSED"

        if collab_mode == "git":
            # 主分支已合入业务代码，保留用户原有 workflow，且未被临时 vernal-ci.yml 污染
            assert not (ws.root / ".gitea" / "workflows" / "vernal-ci.yml").exists()
            assert (ws.root / user_wf_rel).read_text("utf-8") == "name: keep-user-workflow\n"
    finally:
        eng.db.close()


def test_gitlab_ci_external_id_persists_and_recovers_after_restart(tmp_path: Path) -> None:
    ws = init_workspace(tmp_path / "gitlab-rec", name="GitLabRec", collaboration_mode="files")
    eng1 = Engine(ws, harness=MockHarness())
    seen_poll_paths: list[str] = []

    def gitlab_handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path.endswith("/pipeline"):
            return httpx.Response(201, json={"id": 987654, "web_url": "https://gitlab.local/p/-/pipelines/987654"})
        if req.method == "GET" and "/pipelines/" in req.url.path:
            seen_poll_paths.append(req.url.path)
            return httpx.Response(200, json={"id": 987654, "status": "success", "web_url": "https://gitlab.local/p/-/pipelines/987654"})
        return httpx.Response(404)

    transport = httpx.MockTransport(gitlab_handler)
    try:
        eng1.settings.ci_provider = "gitlab"
        eng1.settings.ci_url = "http://gitlab.local"
        eng1.settings.loopforge_project_id = "group/demo"
        eng1.settings.require_merge_approval = True
        from autodev.security import persist_workspace_settings

        persist_workspace_settings(ws, eng1.settings)

        plan = ProjectPlan(
            summary="GitLab 重启恢复",
            tech_stack="python",
            agents_md="r",
            modules=[PlanModule(name="m", path="a.py", description="a")],
            milestones=[PlanMilestone(milestone_id="M1", title="M1", goal="g", integration_cmd="true")],
            tasks=[],
        )
        eng1.db.save_plan(plan, max_retries=2)
        ms = eng1.db.get_milestone("M1")
        assert ms is not None
        triggered = asyncio.run(trigger_milestone_ci(eng1, ms, "", transport=transport))
        ci_run_id = int(triggered["ci_run_id"])
        assert triggered["external_id"] == "987654"
    finally:
        eng1.db.close()

    # 模拟进程重启：重新加载工作区与 SQLite，验证轮询仍能恢复 external_id=987654
    eng2 = Engine(ws, harness=MockHarness())
    try:
        polled = asyncio.run(poll_ci_run(eng2, ci_run_id, transport=transport))
        assert polled["status"] == "SUCCESS"
        assert any(p.endswith("/pipelines/987654") for p in seen_poll_paths)
        assert eng2.db.get_milestone("M1").status == MilestoneStatus.AWAITING_MERGE
    finally:
        eng2.db.close()


# ---------- 评审项 4：轮次并发、里程碑候选绑定、受控部署与未知项目/默认密钥加固 ----------


def test_ci_round_concurrency_candidate_binding_controlled_cd_and_callback_security(tmp_path: Path) -> None:
    ws = init_workspace(tmp_path / "sec-ci", name="SecCI", collaboration_mode="files")
    eng = Engine(ws, harness=MockHarness())
    try:
        eng.settings.runtime_backend = "local"
        eng.settings.loopforge_project_id = "sec-ci"
        eng.settings.ci_provider = "gitea"
        eng.settings.ci_url = "http://gitea.local"
        eng.settings.review_policy = "milestone"
        eng.settings.require_merge_approval = True

        plan = ProjectPlan(
            summary="并发与边界测试",
            tech_stack="python",
            agents_md="rule",
            modules=[PlanModule(name="m", path="x.py", description="x")],
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
                    allowed_paths=["x.py"],
                    verify_cmds=["true"],
                    priority=10,
                )
            ],
        )
        eng.db.save_plan(plan, max_retries=2)
        from autodev.artifacts import FileExchange

        ex = FileExchange(ws, eng.db)
        real_key_v1 = ex.put({"x.py": "X = 1\n"})
        real_key_v2 = ex.put({"x.py": "X = 2\n"})
        real_key_v3 = ex.put({"x.py": "X = 3\n"})
        eng.db.update_task("T-001", status=TaskStatus.APPROVED, artifact_digest=real_key_v1)
        eng.db.meta_set("delivery:M1", real_key_v1)
        eng.db.meta_set("candidate_ref:M1", real_key_v1)

        ms = eng.db.get_milestone("M1")
        assert ms is not None

        # 1. 移除固定默认签名密钥 "vernal-local-ci"：伪造旧默认密钥签名必须 403 拒绝，未知项目必须 404 拒绝
        pending_tp = httpx.MockTransport(lambda req: httpx.Response(200, json={"state": "pending", "statuses": []}))
        r1 = asyncio.run(trigger_milestone_ci(eng, ms, real_key_v1, transport=pending_tp))
        _, r1_sig = make_callback_url(eng, "M1", 1)

        client = TestClient(create_app(eng, proxy_transport=_offline_proxy()))
        legacy_forged_sig = sign_context_sig("vernal-local-ci", "sec-ci", "ci:M1:1")
        assert client.post(f"/vernal-api/ci/callback/sec-ci/M1/1/{legacy_forged_sig}", json={"passed": True}).status_code == 403
        assert client.post(f"/vernal-api/ci/callback/unknown-proj/M1/1/{r1_sig}", json={"passed": True}).status_code == 404
        assert client.post("/vernal-api/ci/callback/unknown-proj/M1/1/non-hex-sig-xyz", json={"passed": True}).status_code == 404

        # 2. 模拟候选更新为 real_key_v2 并触发第 2 轮 CI；此时第 1 轮迟到回调与第 2 轮轮询并发执行
        eng.db.meta_set("delivery:M1", real_key_v2)
        eng.db.meta_set("candidate_ref:M1", real_key_v2)
        r2 = asyncio.run(trigger_milestone_ci(eng, ms, real_key_v2, transport=pending_tp))
        _, r2_sig = make_callback_url(eng, "M1", 2)

        fail_tp = httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json={"state": "failure", "statuses": [{"context": "vernal-ci / verify", "status": "failure"}]},
            )
            if req.url.path.endswith("/status")
            else httpx.Response(200, text="boom")
        )

        async def _race_stale_cb_and_active_poll_and_cb() -> None:
            # 旧轮 (round=1, passed=True) 回调 vs 当前轮 (round=2, failure) 轮询与回调并发
            async def _call_r1_pass() -> dict[str, object]:
                return handle_ci_callback(eng, "sec-ci", "M1", 1, r1_sig, {"passed": True})

            async def _call_r2_fail_cb() -> dict[str, object]:
                return handle_ci_callback(eng, "sec-ci", "M1", 2, r2_sig, {"passed": False, "failed_step": "unit-test"})

            await asyncio.gather(
                _call_r1_pass(),
                poll_ci_run(eng, int(r2["ci_run_id"]), transport=fail_tp),
                _call_r2_fail_cb(),
            )

        asyncio.run(_race_stale_cb_and_active_poll_and_cb())

        # 旧轮 round=1 的 SUCCESS 绝不能把里程碑置为 AWAITING_MERGE；且 round=2 并发轮询+回调只创建 1 个 bugfix 任务！
        assert eng.db.get_milestone("M1").status == MilestoneStatus.PENDING
        all_tasks = eng.db.list_tasks(milestone_id="M1")
        bugfix_tasks = [t for t in all_tasks if "修复 CI 失败" in t.title]
        assert len(bugfix_tasks) == 1

        # 3. 里程碑人工审查必须严格绑定当前候选（旧候选审批或合入均拒绝）
        eng.db.update_task(bugfix_tasks[0].task_id, status=TaskStatus.APPROVED, artifact_digest=real_key_v2)
        eng.db.meta_set("delivery:M1", real_key_v2)
        eng.db.meta_set("candidate_ref:M1", real_key_v2)
        eng.db.update_milestone("M1", status=MilestoneStatus.AWAITING_MERGE)

        # 为 cand-v2 开具并批准审查单，同时让 CI 对 cand-v2 通过
        rev_v2 = open_review(eng, scope_type="milestone", scope_id="M1", milestone_id="M1", head_ref=real_key_v2)
        ok_tp = httpx.MockTransport(
            lambda req: httpx.Response(200, json={"state": "success", "statuses": [{"context": "ci", "status": "success"}]})
        )
        r3 = asyncio.run(trigger_milestone_ci(eng, ms, real_key_v2, transport=ok_tp))
        asyncio.run(poll_ci_run(eng, int(r3["ci_run_id"]), transport=ok_tp))
        # poll_ci_run 成功后自动复用/确保了 real_key_v2 的 OPEN 审查单；现在模拟候选推进到 real_key_v3
        eng.db.meta_set("delivery:M1", real_key_v3)
        eng.db.meta_set("candidate_ref:M1", real_key_v3)

        # 对指向旧候选 real_key_v2 的审查单点 approve 必须返回 409 冲突
        stale_dec = client.post(
            f"/vernal-api/reviews/{rev_v2['review']['review_id']}/decision",
            json={"decision": "approve", "actor": "reviewer-a"},
        )
        assert stale_dec.status_code == 409

        # 为最新候选 real_key_v3 跑过 CI + 批准最新里程碑审查单后才允许合入
        r4 = asyncio.run(trigger_milestone_ci(eng, ms, real_key_v3, transport=ok_tp))
        asyncio.run(poll_ci_run(eng, int(r4["ci_run_id"]), transport=ok_tp))
        open_v3 = client.get("/vernal-api/reviews?status=OPEN&scope_type=milestone").json()
        assert len(open_v3) == 1 and open_v3[0]["head_ref"] == real_key_v3

        with pytest.raises(OpsError, match="里程碑人工代码审查"):
            eng.merge_milestone("M1", actor="lead")

        ok_dec = client.post(
            f"/vernal-api/reviews/{open_v3[0]['review_id']}/decision",
            json={"decision": "approve", "actor": "reviewer-a", "expected_head": real_key_v3},
        )
        assert ok_dec.status_code == 200

        # 4. 受控部署边界：未合入前禁止部署；禁止请求体传入 deploy_cmd；禁止未交付版本与空 actor
        unmerged_dep = client.post("/vernal-api/deployments", json={"milestone_id": "M1", "version_no": 1, "actor": "lead"})
        assert unmerged_dep.status_code == 422

        eng.merge_milestone("M1", actor="lead")

        injected_cmd = client.post(
            "/vernal-api/deployments",
            json={"milestone_id": "M1", "target": "staging", "deploy_cmd": "echo PWNED", "actor": "lead"},
        )
        assert injected_cmd.status_code == 422

        bad_ver = client.post("/vernal-api/deployments", json={"milestone_id": "M1", "version_no": 99, "actor": "lead"})
        assert bad_ver.status_code == 422

        missing_actor = client.post("/vernal-api/deployments", json={"milestone_id": "M1", "version_no": 1, "actor": "   "})
        assert missing_actor.status_code == 422

        # 受控模板配置 deploy_cmd + RBAC 角色校验
        eng.db._exec("INSERT INTO members(user_id, project, role, created_at) VALUES('viewer-u', '*', 'viewer', ?)", (now_iso(),))
        eng.db._exec("INSERT INTO members(user_id, project, role, created_at) VALUES('release-mgr', '*', 'admin', ?)", (now_iso(),))
        forbidden_dep = client.post("/vernal-api/deployments", json={"milestone_id": "M1", "version_no": 1, "actor": "viewer-u"})
        assert forbidden_dep.status_code == 403

        tpl = load_builtin_template("standard")
        tpl.deliver = {"targets": {"staging": {"deploy_cmd": "echo CONTROLLED:$VERNAL_RELEASE_TAG:$VERNAL_DEPLOY_TARGET"}}}
        save_custom_template(ws, json.dumps(tpl.model_dump(), ensure_ascii=False))

        good_dep = client.post("/vernal-api/deployments", json={"milestone_id": "M1", "target": "staging", "version_no": 1, "actor": "release-mgr"})
        assert good_dep.status_code == 200
        assert good_dep.json()["status"] == "SUCCESS"
        assert "CONTROLLED:vernal-M1-v1:staging" in (good_dep.json().get("log_tail") or "")
    finally:
        eng.db.close()
