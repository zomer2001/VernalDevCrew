"""第三轮工程评审专项回归与标准七阶段全链路验收测试：
1. 逐字执行 Python / Node / generic 三套渲染后的工作流步骤：
   - 仅含 `assert True` 空壳测试时，`quality` 步骤原样退出 1（不吞退出码），同时保留 `vernal-ci-result.json` 上传能力；
   - 干净环境缺少质量 CLI 时，`quality` 步骤写出 `quality_cli_missing` 结果并原样以 127 失败退出；
   - 真实断言测试时，`quality` 步骤在无宿主 PYTHONPATH 的干净环境中退出 0。
2. 一键与离线 CI 部署闭环（干净隔离目录验证）：
   - 默认普通启动不生成 `act-runner-token`、不拉起 `gitea-runner`（100% 兼容）；
   - 显式启用 CI (`--enable-ci` / `VERNAL_ENABLE_CI=1`) 时幂等生成 `0600 act-runner-token`、拉起 `gitea-runner`；
   - `build-sandbox.sh` 流式传入无凭据构建上下文（含 `autodev/quality` 与 `vernal-quality`）；
   - `VERNAL_ENABLE_CI=1 ./vernal.sh --pack` 导出 6 镜像离线包并在断网目录一键恢复运行与体检通过。
3. `standard` 标准模板全七阶段（S1 需求 -> S2 架构 -> S3 详设 -> S4 编码 -> S5 审查 -> S6 测试质量 -> S7 CI/CD 合入）端到端可重复验收。
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile

import httpx
import pytest
from fastapi.testclient import TestClient

from autodev import gitops
from autodev.ci.detect import detect_ci_template_kind, extract_workflow_steps, render_vernal_ci_workflow
from autodev.ci.service import make_callback_url
from autodev.db import Milestone, Task
from autodev.docs_center.service import save_doc_version
from autodev.engine import Engine, OpsError, init_workspace
from autodev.harnesses.mock import MockHarness
from autodev.models import (
    MilestoneStatus,
    PlanMilestone,
    PlanModule,
    PlanTask,
    ProjectPlan,
    ProjectStatus,
    TaskStatus,
)
from autodev.quality.cli import install_standalone_quality_cli
from autodev.scheduler import Scheduler
from autodev.server.app import create_app


def _offline_proxy() -> httpx.MockTransport:
    return httpx.MockTransport(lambda req: httpx.Response(200, json={"ok": True}))


def _clean_env_without_pythonpath(*extra_path_dirs: Path) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")
    }
    prefix = ":".join(str(p) for p in extra_path_dirs)
    env["PATH"] = f"{prefix}:/usr/bin:/bin" if prefix else "/usr/bin:/bin"
    return env


# ---------- 1. Python / Node / generic 三模板逐字运行步骤验证 ----------


@pytest.mark.parametrize("kind", ["python", "node", "generic"])
def test_literal_workflow_steps_quality_failure_upload_and_missing_cli(tmp_path: Path, kind: str) -> None:
    proj_marker_dir = tmp_path / f"tpl-{kind}"
    proj_marker_dir.mkdir(parents=True)
    if kind == "python":
        (proj_marker_dir / "pyproject.toml").write_text('[project]\nname = "demo"\n', "utf-8")
    elif kind == "node":
        (proj_marker_dir / "package.json").write_text('{"name": "demo"}\n', "utf-8")
    assert detect_ci_template_kind(proj_marker_dir) == kind

    ms = Milestone(
        milestone_id="M1",
        seq_order=0,
        title="M1",
        goal="goal",
        integration_cmd="true",
        status=MilestoneStatus.CI_RUNNING,
        planned=True,
        target_squads=[],
        key_deliverables=[],
        wave_notes=None,
        branch_name=None,
        commit_matrix_json=None,
        report_path=None,
        failure_reason=None,
    )
    dummy_task = Task(
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
        allowed_paths=["."],
        verify_cmds=["true"],
        priority=10,
        status=TaskStatus.APPROVED,
        retry_count=0,
        max_retries=2,
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
    )

    cb_url = "http://vernal.invalid/vernal-api/ci/callback/demo/M1/1/sig123"
    rendered = render_vernal_ci_workflow(proj_marker_dir, ms, [dummy_task], callback_url=cb_url)
    steps = {s["name"]: s for s in extract_workflow_steps(rendered)}
    assert "quality" in steps and "callback" in steps
    assert steps["callback"]["if"] == "always()"

    # 安装与 Dockerfile.sandbox 完全一致的独立零依赖 vernal-quality（优先使用宿主机裸 /usr/bin/python3 验证无第三方包可运行）
    sys_py = "/usr/bin/python3" if Path("/usr/bin/python3").is_file() else sys.executable
    standalone_root = tmp_path / f"standalone-{kind}"
    install_standalone_quality_cli(standalone_root, python_bin=sys_py)

    # 伪造 curl 以离线捕获 callback 步骤实际上传的 JSON 正文与 URL
    curl_bin_dir = tmp_path / f"curl-bin-{kind}"
    curl_bin_dir.mkdir(parents=True)
    uploaded_payload_path = tmp_path / f"uploaded-{kind}.json"
    uploaded_url_path = tmp_path / f"uploaded-{kind}.url"
    fake_curl = curl_bin_dir / "curl"
    fake_curl.write_text(
        "#!/bin/sh\n"
        "for arg in \"$@\"; do\n"
        "  case \"$arg\" in\n"
        f"    @*) cp \"${{arg#@}}\" \"{uploaded_payload_path}\" ;;\n"
        f"    http*) printf '%s' \"$arg\" > \"{uploaded_url_path}\" ;;\n"
        "  esac\n"
        "done\n"
        "exit 0\n",
        "utf-8",
    )
    fake_curl.chmod(0o755)

    # 场景 A：只含 `assert True` 的假测试 -> 逐字执行 quality 步骤必须返回非 0（退出码 1），且写出 passed=false 供 callback 上传
    work_fail = tmp_path / f"job-fail-{kind}"
    (work_fail / "src" / "tests").mkdir(parents=True)
    (work_fail / "src" / "tests" / "test_hollow.py").write_text("def test_hollow():\n    assert True\n", "utf-8")

    env_with_cli = _clean_env_without_pythonpath(standalone_root / "bin", curl_bin_dir)
    assert "PYTHONPATH" not in env_with_cli

    q_fail = subprocess.run(["sh", "-c", steps["quality"]["run"]], cwd=str(work_fail), env=env_with_cli, capture_output=True, text=True)
    assert q_fail.returncode == 1, f"{kind} 模板在质量门失败时不得吞退出码: stdout={q_fail.stdout}, stderr={q_fail.stderr}"

    result_file = work_fail / "vernal-ci-result.json"
    assert result_file.is_file()
    parsed_fail = json.loads(result_file.read_text("utf-8"))
    assert parsed_fail["passed"] is False
    assert any(g["gate"] == "test_integrity" and g["status"] == "FAILED" for g in parsed_fail["results"])

    # 执行紧随其后的 callback (if: always()) 步骤，确认失败结果仍完整上传
    cb_proc = subprocess.run(["sh", "-c", steps["callback"]["run"]], cwd=str(work_fail), env=env_with_cli, capture_output=True, text=True)
    assert cb_proc.returncode == 0
    assert uploaded_url_path.read_text("utf-8") == cb_url
    uploaded_json = json.loads(uploaded_payload_path.read_text("utf-8"))
    assert uploaded_json["passed"] is False

    # 场景 B：Runner 环境缺失 vernal-quality 与 autodev.quality.cli -> 逐字执行 quality 步骤必须以 127 失败退出并写出诊断 JSON
    work_missing = tmp_path / f"job-missing-{kind}"
    (work_missing / "src" / "tests").mkdir(parents=True)
    (work_missing / "src" / "tests" / "test_any.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n", "utf-8")

    isolated_py_dir = tmp_path / f"isolated-py-{kind}"
    isolated_py_dir.mkdir(parents=True)
    # 构造带 `-S` 的隔离 python3 替身，确保即使系统 site-packages 装过包也完全找不到 autodev
    py_shim = isolated_py_dir / "python3"
    py_shim.write_text(f"#!/bin/sh\nexec \"{sys_py}\" -I -S \"$@\"\n", "utf-8")
    py_shim.chmod(0o755)
    env_missing_cli = _clean_env_without_pythonpath(isolated_py_dir, curl_bin_dir)

    q_missing = subprocess.run(["sh", "-c", steps["quality"]["run"]], cwd=str(work_missing), env=env_missing_cli, capture_output=True, text=True)
    assert q_missing.returncode == 127
    missing_json = json.loads((work_missing / "vernal-ci-result.json").read_text("utf-8"))
    assert missing_json["passed"] is False
    assert missing_json["failed_step"] == "quality_cli_missing"

    # 场景 C：真实有效测试 -> 逐字执行 quality 步骤退出 0 且 passed=true
    work_pass = tmp_path / f"job-pass-{kind}"
    (work_pass / "src" / "tests").mkdir(parents=True)
    (work_pass / "src" / "app.py").write_text("def add(a: int, b: int) -> int:\n    return a + b\n", "utf-8")
    (work_pass / "src" / "tests" / "test_app.py").write_text(
        "from app import add\n\n\ndef test_add() -> None:\n    assert add(2, 3) == 5\n",
        "utf-8",
    )
    q_pass = subprocess.run(["sh", "-c", steps["quality"]["run"]], cwd=str(work_pass), env=env_with_cli, capture_output=True, text=True)
    assert q_pass.returncode == 0
    assert json.loads((work_pass / "vernal-ci-result.json").read_text("utf-8"))["passed"] is True


# ---------- 2. 干净环境一键与离线 CI 部署闭环验证 ----------


def test_clean_env_ci_enable_idempotent_token_sandbox_cli_and_offline_bundle(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    checkout = tmp_path / "checkout"
    deploy = checkout / "deploy" / "loopforge"
    deploy.mkdir(parents=True)

    for name in ("vernal.sh", "pyproject.toml", "Dockerfile"):
        shutil.copy2(repo_root / name, checkout / name)
    # 拷入真实 autodev/__init__.py、autodev/quality 与 semgrep 规则，验证 build-sandbox.sh 无凭据上下文打包
    (checkout / "autodev" / "assets").mkdir(parents=True)
    shutil.copy2(repo_root / "autodev" / "__init__.py", checkout / "autodev" / "__init__.py")
    shutil.copytree(
        repo_root / "autodev" / "quality",
        checkout / "autodev" / "quality",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copytree(repo_root / "autodev" / "assets" / "semgrep", checkout / "autodev" / "assets" / "semgrep")

    for file in (repo_root / "deploy" / "loopforge").iterdir():
        if file.suffix == ".sh" or file.name in ("Dockerfile.sandbox", "compose.yaml"):
            shutil.copy2(file, deploy / file.name)
    for sh_file in [checkout / "vernal.sh", *deploy.glob("*.sh")]:
        sh_file.chmod(0o755)

    # 预置敏感 .env，确保绝不会被带入 sandbox docker build 上下文
    (deploy / ".env").write_text(
        "LOOPFORGE_DB_PASSWORD='secret-db-pw'\n"
        "LOOPFORGE_DB_ROOT_PASSWORD='secret-root-pw'\n"
        "AGENTFLOW_SESSION_SECRET='secret-sess'\n"
        "LOOPFORGE_DATA_DIR='/tmp/lf-data'\n",
        "utf-8",
    )

    tools = tmp_path / "bin"
    tools.mkdir()
    probe = tmp_path / "probe"
    probe.mkdir()
    (probe / "loaded").touch()

    mock_docker = tools / "docker"
    mock_docker.write_text(
        f"#!{sys.executable}\n"
        + r'''
import hashlib, json, os, sys, tarfile
from pathlib import Path

a = sys.argv[1:]
probe = Path(os.environ["PROBE"])
with (probe / "docker.calls").open("a", encoding="utf-8") as fp:
    fp.write(" ".join(a) + "\n")

def img_id(name: str) -> str:
    return "sha256:" + hashlib.sha256(name.encode()).hexdigest()

if a[0] == "info":
    sys.exit(0)
elif a[0] == "version":
    print("linux/arm64")
elif a[:2] == ["image", "inspect"]:
    if not (probe / "loaded").exists():
        sys.exit(1)
    joined = " ".join(a)
    if "{{.Os}}" in joined:
        print("linux/arm64", img_id(a[-1]))
    elif "{{.Id}}" in a:
        print(img_id(a[-1]))
    elif "vernal.sandbox.source" in joined and (probe / "sbx.label").exists():
        print((probe / "sbx.label").read_text())
    else:
        print("")
elif a[0] == "build":
    if "--label" in a:
        lbl = a[a.index("--label") + 1].split("=", 1)[1]
        (probe / "sbx.label").write_text(lbl)
    # 校验 build-sandbox.sh 传入的 tar 流包含 quality CLI 且不含任何敏感凭据
    raw = sys.stdin.buffer.read()
    with tarfile.open(fileobj=__import__("io").BytesIO(raw), mode="r:*") as tf:
        names = [n.lstrip("./") for n in tf.getnames()]
    (probe / "sbx.tar.entries").write_text(json.dumps(names))
    assert "Dockerfile" in names
    assert "autodev/quality/cli.py" in names
    assert "autodev/assets/semgrep/vernal-security.yaml" in names
    assert not any(".env" in n or ".runner" in n for n in names)
elif a[0] == "run":
    (probe / "last.run").write_text(" ".join(a))
elif a[0] == "inspect":
    print("running")
elif a[0] == "compose":
    joined = " ".join(a)
    if "config" in a and "--images" in a:
        imgs = [
            "mysql:8.4",
            "ghcr.io/tencent/loopforge:0.4.0",
            "gitea/gitea:1.22",
            "autodev-runtime-vernal",
        ]
        if "ci" in a:
            imgs.append("gitea/act_runner:0.2.11")
        print("\n".join(imgs))
    elif "ps" in a and "-q" in a:
        print("cid-" + a[-1])
    elif "generate-runner-token" in joined:
        cnt = int((probe / "tok.gen.count").read_text()) if (probe / "tok.gen.count").exists() else 0
        (probe / "tok.gen.count").write_text(str(cnt + 1))
        print("RUNT0K3N_GITEA_ACT_001")
    elif "list" in a:
        print("1 vernal-local")
elif a[:2] == ["image", "save"]:
    out_p = Path(a[a.index("-o") + 1])
    out_p.write_text("saved-tar-6-images", encoding="utf-8")
elif a[:2] == ["image", "load"]:
    in_p = Path(a[a.index("-i") + 1])
    assert in_p.read_text(encoding="utf-8") == "saved-tar-6-images"
    (probe / "loaded").touch()
''',
        "utf-8",
    )
    mock_docker.chmod(0o755)

    mock_curl = tools / "curl"
    mock_curl.write_text("#!/bin/sh\nprintf 'online:1:local'\nexit 0\n", "utf-8")
    mock_curl.chmod(0o755)

    base_env = {
        **_clean_env_without_pythonpath(tools),
        "PROBE": str(probe),
        "VERNAL_BIND": "0.0.0.0",
        "HOME": str(tmp_path / "home"),
    }
    (tmp_path / "home").mkdir()

    def _run_vernal(cwd: Path, *args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(cwd / "vernal.sh"), *args],
            cwd=str(cwd),
            env={**base_env, **(extra_env or {})},
            check=True,
            capture_output=True,
            text=True,
        )

    # 1. 普通默认启动（未开启 CI）：不生成 act-runner-token，也不启动 gitea-runner，但沙箱上下文已打入 quality CLI
    (probe / "docker.calls").write_text("", "utf-8")
    _run_vernal(checkout)
    default_calls = (probe / "docker.calls").read_text("utf-8").splitlines()
    assert not (deploy / ".runner" / "pool" / "act-runner-token").exists()
    assert not any("gitea-runner" in c for c in default_calls)
    assert "vernal-quality --help" in (probe / "last.run").read_text("utf-8")
    tar_entries = json.loads((probe / "sbx.tar.entries").read_text("utf-8"))
    assert "autodev/quality/cli.py" in tar_entries

    # 2. 显式启用 CI (`--enable-ci`) 并重复执行两次验证幂等注册 + 启动 gitea-runner + doctor 体检通过
    (probe / "docker.calls").write_text("", "utf-8")
    _run_vernal(checkout, "--enable-ci")
    _run_vernal(checkout)
    tok_file = deploy / ".runner" / "pool" / "act-runner-token"
    assert tok_file.is_file()
    assert tok_file.read_text("utf-8").strip() == "RUNT0K3N_GITEA_ACT_001"
    assert stat.S_IMODE(tok_file.stat().st_mode) == 0o600
    assert int((probe / "tok.gen.count").read_text("utf-8")) == 1, "重复启动必须复用已有 act-runner-token，保持幂等"
    ci_calls = (probe / "docker.calls").read_text("utf-8").splitlines()
    assert any("--profile ci up -d gitea-runner" in c for c in ci_calls)

    doc_res = _run_vernal(checkout, "--doctor")
    assert "act-runner-token 已就绪" in doc_res.stdout
    assert "gitea-runner running" in doc_res.stdout

    # 3. 打包开启 CI 的离线发行包并在全新断网目录冷启动
    _run_vernal(checkout, "--pack")
    offline_extract = tmp_path / "airgapped"
    with tarfile.open(checkout / "dist" / "vernal-offline-arm64.tar.gz") as bundle:
        bundle.extractall(offline_extract, filter="data")
    offline_root = offline_extract / "vernal-offline"
    lock_lines = (offline_root / "deploy" / "loopforge" / "images.lock").read_text("utf-8").splitlines()
    assert len(lock_lines) == 7  # 1 行平台头 + 6 个镜像（含 ci_runner）
    assert any(ln.startswith("ci_runner gitea/act_runner:0.2.11 ") for ln in lock_lines)

    (probe / "loaded").unlink()
    (probe / "docker.calls").write_text("", "utf-8")
    _run_vernal(offline_root)
    airgap_calls = (probe / "docker.calls").read_text("utf-8").splitlines()
    assert sum(c.startswith("image load ") for c in airgap_calls) == 1
    assert (offline_root / "deploy" / "loopforge" / ".runner" / "pool" / "act-runner-token").is_file()
    assert any("--profile ci up -d --no-build --pull never gitea-runner" in c for c in airgap_calls)


# ---------- 3. 标准七阶段模板 (standard) 端到端全流程可重复验收 ----------


def test_standard_template_full_seven_stage_repeatable_acceptance(tmp_path: Path) -> None:
    bare_remote = tmp_path / "order-hub-gitea.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare_remote)], check=True)

    ws = init_workspace(tmp_path / "order-hub", name="OrderHub", collaboration_mode="git")
    gitops.ensure_repo(ws.root, "main")
    gitops.git(["remote", "add", "origin", str(bare_remote)], ws.root)
    gitops.git(["push", "-u", "origin", "main"], ws.root)

    # 安装独立 vernal-quality 用于干净环境逐字执行 CI 工作流步骤
    sys_py = "/usr/bin/python3" if Path("/usr/bin/python3").is_file() else sys.executable
    sbx_tools = tmp_path / "sbx-standard"
    install_standalone_quality_cli(sbx_tools, python_bin=sys_py)

    def coder_fn(prompt: str, cwd: Path, _call: object) -> dict[str, object]:
        if "T-001" in prompt:
            (cwd / "contracts").mkdir(parents=True, exist_ok=True)
            (cwd / "contracts" / "__init__.py").write_text("", "utf-8")
            (cwd / "contracts" / "order.py").write_text(
                "from dataclasses import dataclass\n\n\n@dataclass\nclass OrderDTO:\n    sku: str\n    qty: int\n",
                "utf-8",
            )
            return {
                "status": "DONE",
                "summary": "defined OrderDTO contract",
                "files_changed": ["contracts/__init__.py", "contracts/order.py"],
                "tests_added": [],
                "notes": "",
            }
        # T-002 业务实现 + 带 @pytest.mark.req("REQ-001") 的真实单测 + Cobertura coverage.xml (>= 70%)
        (cwd / "order").mkdir(parents=True, exist_ok=True)
        (cwd / "tests").mkdir(parents=True, exist_ok=True)
        (cwd / "order" / "__init__.py").write_text("", "utf-8")
        (cwd / "order" / "service.py").write_text(
            "from contracts.order import OrderDTO\n\n\n"
            "def place_order(dto: OrderDTO) -> str:\n"
            "    if dto.qty <= 0:\n"
            "        raise ValueError('qty must be positive')\n"
            "    return f'ORD-{dto.sku}-{dto.qty}'\n",
            "utf-8",
        )
        (cwd / "tests" / "test_order.py").write_text(
            "import pytest\n"
            "from contracts.order import OrderDTO\n"
            "from order.service import place_order\n\n\n"
            '@pytest.mark.req("REQ-001")\n'
            "def test_place_order_ok() -> None:\n"
            "    assert place_order(OrderDTO(sku='BOOK', qty=2)) == 'ORD-BOOK-2'\n",
            "utf-8",
        )
        (cwd / "coverage.xml").write_text(
            '<?xml version="1.0" ?>\n'
            '<coverage line-rate="1.0">\n'
            '  <packages><package name="order"><classes>\n'
            '    <class name="service.py" filename="order/service.py"><lines>\n'
            '      <line number="1" hits="1"/><line number="4" hits="1"/>\n'
            '      <line number="5" hits="1"/><line number="6" hits="1"/><line number="7" hits="1"/>\n'
            '    </lines></class>\n'
            '    <class name="test_order.py" filename="tests/test_order.py"><lines>\n'
            '      <line number="1" hits="1"/><line number="2" hits="1"/><line number="3" hits="1"/>\n'
            '      <line number="6" hits="1"/><line number="7" hits="1"/><line number="8" hits="1"/>\n'
            '    </lines></class>\n'
            "  </classes></package></packages>\n"
            "</coverage>\n",
            "utf-8",
        )
        return {
            "status": "DONE",
            "summary": "implemented order service & tests",
            "files_changed": ["order/__init__.py", "order/service.py", "tests/test_order.py", "coverage.xml"],
            "tests_added": ["tests/test_order.py"],
            "notes": "",
        }

    eng = Engine(ws, harness=MockHarness(coder=coder_fn))
    try:
        eng.settings.runtime_backend = "local"
        eng.settings.process_template = "standard"
        eng.settings.review_policy = "risk"
        eng.settings.ci_provider = "gitea"
        eng.settings.ci_url = "http://gitea.local"
        eng.settings.loopforge_project_id = "order-hub"
        eng.settings.require_plan_approval = True
        eng.settings.require_merge_approval = True
        eng.settings.deploy_cmd = "echo DEPLOYED:$VERNAL_RELEASE_TAG:$VERNAL_DEPLOY_TARGET"

        client = TestClient(create_app(eng, proxy_transport=_offline_proxy()))

        def _stage_map() -> dict[str, str]:
            proc = client.get("/vernal-api/process").json()
            return {s["id"]: s["status"] for s in proc["stages"]}

        # ---- S1 需求分析 & S2 架构设计 ----
        (ws.inputs_dir / "order_spec.md").write_text("# 订单中心需求\n\n支持下单校验库存并返回订单号（REQ-001）。\n", "utf-8")
        eng.ingest()
        save_doc_version(
            eng,
            "docs/SRS.md",
            "# 软件需求规格说明书\n\n| 编号 | 级别 | 标题 | 验收 |\n|---|---|---|---|\n| REQ-001 | P0 | 创建订单 | 数量大于0返回订单号 |\n",
            author="pm",
            note="init SRS",
        )
        save_doc_version(
            eng,
            "docs/ARCHITECTURE.md",
            "# 架构设计说明书\n\n分层：contracts -> order -> tests。\n",
            author="architect",
            note="init ADD",
        )

        plan = ProjectPlan(
            summary="标准七阶段订单中心验收",
            tech_stack="python",
            agents_md="遵守契约先行与测试驱动",
            modules=[
                PlanModule(name="contracts", path="contracts/", description="DTO 契约"),
                PlanModule(name="order", path="order/", description="订单服务"),
            ],
            milestones=[
                PlanMilestone(
                    milestone_id="M1",
                    title="M1 订单核心闭环",
                    goal="完成契约与下单核心服务",
                    integration_cmd="python3 -m compileall -q contracts order tests",
                )
            ],
            tasks=[
                PlanTask(
                    task_id="T-001",
                    milestone_id="M1",
                    module="contracts",
                    role="contract",
                    title="定义订单 DTO 契约",
                    description="编写 contracts/order.py",
                    acceptance_criteria=["可导入 OrderDTO"],
                    depends_on=[],
                    doc_slice_ids=[],
                    allowed_paths=["contracts/"],
                    verify_cmds=["python3 -c 'from contracts.order import OrderDTO'"],
                    requirement_ids=["REQ-001"],
                    priority=10,
                ),
                PlanTask(
                    task_id="T-002",
                    milestone_id="M1",
                    module="order",
                    role="coder",
                    title="实现下单服务与单测",
                    description="实现 order/service.py 与 tests/test_order.py",
                    acceptance_criteria=["REQ-001 单测通过"],
                    depends_on=["T-001"],
                    doc_slice_ids=[],
                    allowed_paths=["order/", "tests/", "coverage.xml"],
                    verify_cmds=["python3 -c 'from contracts.order import OrderDTO; from order.service import place_order; assert place_order(OrderDTO(\"A\", 2)) == \"ORD-A-2\"'"],
                    requirement_ids=["REQ-001"],
                    design_refs=["docs/design/order.md#place_order"],
                    priority=20,
                ),
            ],
        )
        eng._apply_plan(plan, eng.agent_identity("director", "指挥组"))

        # S1 未过 srs_approval 时，approve_plan 必须被阻断，且 S1 / S2 处于 WAITING_GATE
        with pytest.raises(OpsError, match="SRS"):
            eng.approve_plan(actor="lead")
        s_after_plan = _stage_map()
        assert s_after_plan["requirements"] == "WAITING_GATE"
        assert s_after_plan["architecture"] == "WAITING_GATE"

        srs_gate = next(g for g in client.get("/vernal-api/gates?status=PENDING").json() if g["gate_id"] == "srs_approval")
        assert client.post(
            f"/vernal-api/gates/{srs_gate['record_id']}/decide",
            json={"decision": "pass", "comment": "SRS 评审通过", "actor": "lead"},
        ).status_code == 200
        assert _stage_map()["requirements"] == "PASSED"

        eng.approve_plan(actor="lead")
        assert _stage_map()["architecture"] == "PASSED"

        # ---- S3 详细设计（H3 门禁放行前仅允许 contract 先行，阻断业务 coder）+ S5 契约变更风险人工审查 ----
        save_doc_version(
            eng,
            "docs/design/order.md",
            "# 订单模块详细设计\n\n## place_order\n输入 OrderDTO，输出订单号字符串。\n",
            author="architect",
            note="init detailed design",
        )
        if eng.settings.collaboration_mode == "git":
            eng.record_documents(["docs/SRS.md", "docs/ARCHITECTURE.md", "docs/design/order.md"], "docs: baseline specs")

        ci_runs_executed: list[int] = []

        def gitea_ci_handler(req: httpx.Request) -> httpx.Response:
            if req.url.path.endswith("/status"):
                # 从远端裸仓拉取真实发布的 vernal/ci/M1-r1 分支并逐字运行渲染步骤（无宿主 PYTHONPATH）
                ls_heads = subprocess.run(
                    ["git", "ls-remote", "--heads", str(bare_remote), "refs/heads/vernal/ci/M1-r1"],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.split()
                assert ls_heads, "隔离 CI 分支未推送至远端裸仓"
                ci_job_dir = tmp_path / f"ci-job-{len(ci_runs_executed) + 1}"
                ci_job_dir.mkdir(parents=True, exist_ok=True)
                subprocess.run(["git", "clone", "--quiet", "--branch", "vernal/ci/M1-r1", str(bare_remote), str(ci_job_dir / "src")], check=True)
                wf_steps = {
                    st["name"]: st["run"]
                    for st in extract_workflow_steps((ci_job_dir / "src" / ".gitea" / "workflows" / "vernal-ci.yml").read_text("utf-8"))
                }
                c_env = _clean_env_without_pythonpath(sbx_tools / "bin", Path(sys.executable).parent)
                t_rc = subprocess.run(["sh", "-c", wf_steps["test"]], cwd=str(ci_job_dir), env=c_env).returncode
                q_rc = subprocess.run(["sh", "-c", wf_steps["quality"]], cwd=str(ci_job_dir), env=c_env).returncode
                ci_runs_executed.append(q_rc)
                ok = t_rc == 0 and q_rc == 0
                return httpx.Response(
                    200,
                    json={
                        "state": "success" if ok else "failure",
                        "statuses": [{"context": "vernal-ci / verify", "status": "success" if ok else "failure"}],
                    },
                )
            return httpx.Response(201, json={"ok": True})

        sched = Scheduler(eng, ci_transport=httpx.MockTransport(gitea_ci_handler))
        asyncio.run(sched.run())

        # 此时：S3 详细设计门 PENDING (WAITING_GATE)，T-001 (contract) 触发了风险人工审查 -> S5 处于 WAITING_GATE
        s_mid = _stage_map()
        assert s_mid["detailed_design"] == "WAITING_GATE"
        assert s_mid["code_review"] == "WAITING_GATE"
        assert eng.db.get_task("T-001").status == TaskStatus.AWAITING_HUMAN_REVIEW
        assert eng.db.get_task("T-002").status == TaskStatus.BLOCKED

        # 人工在 UI/API 批准 S3 详细设计门 + S5 契约代码审查单
        dd_gate = next(g for g in client.get("/vernal-api/gates?status=PENDING").json() if g["gate_id"] == "detailed_design_approval")
        assert client.post(
            f"/vernal-api/gates/{dd_gate['record_id']}/decide",
            json={"decision": "pass", "comment": "详设通过", "actor": "architect"},
        ).status_code == 200
        open_rev = client.get("/vernal-api/reviews?status=OPEN").json()[0]
        assert client.post(
            f"/vernal-api/reviews/{open_rev['review_id']}/decision",
            json={"decision": "approve", "note": "DTO 契约清晰", "actor": "reviewer"},
        ).status_code == 200

        # ---- S4 编码实现 -> S6 测试与质量门 -> S7 隔离分支 CI 验证 ----
        asyncio.run(sched.run())
        assert ci_runs_executed == [0]
        assert eng.db.get_milestone("M1").status == MilestoneStatus.AWAITING_MERGE

        s_pre_merge = _stage_map()
        assert s_pre_merge["detailed_design"] == "PASSED"
        assert s_pre_merge["implementation"] == "PASSED"
        assert s_pre_merge["code_review"] == "PASSED"
        assert s_pre_merge["testing"] == "PASSED"
        assert s_pre_merge["integration_delivery"] == "WAITING_GATE"

        # ---- S7 合入主干 + 生成测试报告与 Release Notes + 受控部署 ----
        eng.merge_milestone("M1", actor="release-lead")
        dep = client.post("/vernal-api/deployments", json={"milestone_id": "M1", "target": "staging", "actor": "release-lead"})
        assert dep.status_code == 200
        assert "DEPLOYED:vernal-M1-v1:staging" in (dep.json().get("log_tail") or "")
        assert (ws.root / "docs" / "test" / "TEST_REPORT-M1.md").is_file()
        assert (ws.reports_dir / "releases" / "vernal-M1-v1.md").is_file()

        # 验证最终七个阶段全部绿灯 PASSED，且 P0 需求追溯覆盖率 100%！
        final_stages = client.get("/vernal-api/process").json()["stages"]
        assert [s["id"] for s in final_stages] == [
            "requirements",
            "architecture",
            "detailed_design",
            "implementation",
            "code_review",
            "testing",
            "integration_delivery",
        ]
        assert [s["status"] for s in final_stages] == ["PASSED"] * 7
        assert client.get("/vernal-api/trace/matrix").json()["summary"]["p0_coverage_rate"] == 1.0
    finally:
        eng.db.close()
