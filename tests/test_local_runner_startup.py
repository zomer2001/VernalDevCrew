import os
import shutil
import subprocess
import time
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from autodev.config import Settings
from autodev.engine import Engine, init_workspace
from autodev.loopforge.client import LoopForgeClient
from autodev.server.app import create_app
from autodev.server.project_launcher import merge_runner_runtimes


def wait_until(predicate):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition did not become true")


def test_runner_waits_for_login_and_uses_discovered_cli(tmp_path):
    source = Path(__file__).resolve().parents[1]
    deploy = tmp_path / "deploy/loopforge"
    deploy.mkdir(parents=True)
    (deploy / ".env").touch()
    (tmp_path / "autodev").mkdir()
    (tmp_path / "Dockerfile").touch()
    (tmp_path / "pyproject.toml").touch()
    projects = tmp_path / "my projects"
    projects.mkdir()
    script = tmp_path / "vernal.sh"
    shutil.copy2(source / "vernal.sh", script)
    for name in ("start-runner.sh", "wait-and-bind-runner.sh"):
        shutil.copy2(source / "deploy/loopforge" / name, deploy / name)
    tools = deploy / "bin"
    tools.mkdir()
    home = tmp_path / "home"
    nvm = home / ".nvm/versions/node/v24/bin"
    nvm.mkdir(parents=True)
    marker = tmp_path / "login"
    started = tmp_path / "started"

    def stub(path, body):
        path.write_text("#!/bin/sh\nset -eu\n" + body + "\n")
        path.chmod(0o700)

    stub(deploy / "ensure-gitea.sh", "exit 0")
    stub(deploy / "build-sandbox.sh", "exit 0")
    stub(nvm / "opencode", "exit 0")
    stub(tools / "pgrep", "exit 1")
    stub(tools / "docker", 'if [ "$1" = compose ]; then echo "$VERNAL_HOST_PROJECTS_DIR" >> "$PROBE/mounts"; fi')
    stub(tools / "curl", """case "$*" in
      *runner/bind-code*) echo attempt >> "$PROBE/attempts"; test -f "$PROBE/login"; echo one-time-code ;;
      *runner/health*) echo unbound:0: ;;
      *) exit 0 ;;
    esac""")
    stub(tools / "sleep", '/bin/sleep 0.1')
    stub(tools / "agentflow-runner",
         'test "$4" = one-time-code; command -v opencode >> "$PROBE/started"')
    os_name = os.uname().sysname.lower()
    arch = {"aarch64": "arm64", "x86_64": "amd64"}.get(os.uname().machine, os.uname().machine)
    stub(tools / f"agentflow-gate-mcp-{os_name}-{arch}", "exit 0")
    env = {"PATH": f"{tools}:/usr/bin:/bin", "HOME": str(home), "PROBE": str(tmp_path)}
    subprocess.run([str(script), "--mount", str(projects)], env=env, check=True, capture_output=True)
    try:
        wait_until((tmp_path / "attempts").exists)
        assert (deploy / ".runner/binder.pid").is_file()
        assert not started.exists()
        subprocess.run([str(script)], env=env, check=True, capture_output=True)
        assert (tmp_path / "mounts").read_text().splitlines() == [str(projects), str(projects)]
        marker.touch()
        wait_until(started.exists)
        assert started.read_text().splitlines() == [str(nvm / "opencode")]
        assert not (deploy / ".runner/binder.pid").exists()
        log = (deploy / ".runner/binder.log").read_text()
        assert "正在启动" in log and "one-time-code" not in log
    finally:
        marker.touch()


def test_codex_gate_permission_is_scoped_to_trusted_runner_copy(tmp_path):
    deploy = tmp_path.resolve() / "test deployment"
    bin_dir = deploy / "bin"
    bin_dir.mkdir(parents=True)
    wrapper = bin_dir / "codex"
    wrapper.symlink_to(Path(__file__).resolve().parents[1] / "deploy/loopforge/runner-codex.sh")
    arch = {"x86_64": "amd64"}.get(os.uname().machine, os.uname().machine)
    trusted = bin_dir / f"agentflow-gate-mcp-darwin-{arch}"
    trusted.write_bytes(b"test helper")
    trusted.chmod(0o700)
    workspace = deploy / ".runner/workspaces/runs/run-1/workspace"
    staged = workspace / ".agentflow/config/pm/gate-mcp"
    staged.parent.mkdir(parents=True)
    shutil.copy2(trusted, staged)
    # Capture argv at exec without loading real personal credentials or invoking a model.
    capture = tmp_path / "record.sh"
    capture.write_text('function exec() { printf "%s\\n" "$@"; exit 0; }\n')
    args = ["exec", "--json", "-c", 'mcp_servers.agentflow-gate.command=".agentflow/config/pm/gate-mcp"', "-"]

    def recorded(cwd):
        result = subprocess.run([wrapper, *args], cwd=cwd, env={**os.environ, "BASH_ENV": str(capture)},
                                check=True, capture_output=True, text=True)
        return result.stdout.splitlines()[1:]

    assert recorded(workspace) == [
        "-c", 'mcp_servers.agentflow-gate.tools.submit_gate_result.approval_mode="approve"',
        *args[:3], f'mcp_servers.agentflow-gate.command="{trusted}"', "-",
    ]
    assert recorded(deploy) == args
    original = list(args)
    args[4:4] = ["-c", args[3]]
    assert recorded(workspace) == args
    args[:] = original
    staged.unlink()
    staged.symlink_to(trusted)
    assert recorded(workspace) == args
    staged.unlink()
    staged.write_bytes(b"modified")
    assert recorded(workspace) == args


def test_only_launcher_can_bind_through_docker_gateway(tmp_path, monkeypatch):
    ws = init_workspace(tmp_path / "workspace", collaboration_mode="files")
    eng = Engine(ws, settings=Settings(loopforge_token="test-token", collaboration_mode="files"))
    monkeypatch.setenv("VERNAL_RUNNER_BOOTSTRAP_SECRET", "host-secret")
    mint = AsyncMock(return_value={"code": "one-time"})
    monkeypatch.setattr(LoopForgeClient, "create_runner_bind_code", mint)
    with TestClient(create_app(eng), client=("172.17.0.1", 5000)) as client:
        path = "/vernal-api/runner/bind-code?format=raw"
        assert client.post(path).status_code == 403
        assert client.post(path, headers={"X-Vernal-Runner-Secret": "wrong"}).status_code == 403
        assert client.post(path, headers={"X-Vernal-Runner-Secret": "host-secret"}).text == "one-time"
        mint.assert_awaited_once()
    eng.db.close()


def test_opencode_host_detection_and_credential_status():
    row = {"status": "online", "name": "workstation", "runtimes": [
        {"provider": "opencode-cli", "status": "online", "binaryPath": "/host/opencode"}
    ]}
    for user, ready, key in [({}, False, None),
                             ({"agentCliCredentials": {"opencode": {"apiKeyConfigured": True}}}, True, True)]:
        runtimes, _ = merge_runner_runtimes({}, Settings(runtime_backend="loopforge"), runners=[row], user_info=user)
        assert runtimes["opencode"]["bin"] == "/host/opencode"
        assert runtimes["opencode"]["installed"]
        assert runtimes["opencode"]["ready"] is ready
        assert runtimes["opencode"]["api_key_configured"] is key


def test_registered_roles_follow_runner_with_only_opencode():
    import asyncio
    import json
    import httpx
    from unittest.mock import patch
    from autodev.loopforge.bootstrap import ensure_vernal_loopforge_assets

    roles = []
    def loopforge(request):
        endpoint = request.url.path
        if endpoint == "/api/runners":
            return httpx.Response(200, json=[{"id": "personal", "status": "online", "runtimes": [
                {"provider": "opencode", "status": "online", "binaryPath": "/host/opencode"}]}])
        if endpoint == "/api/auth/me":
            return httpx.Response(200, json={"user": {"agentCliCredentials": {"opencode": {"apiKeyConfigured": True}}}})
        if endpoint == "/api/agents" and request.method == "PUT":
            roles.append(json.loads(request.content))
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"status": "published"})

    settings = Settings(harness="auto", runtime_backend="loopforge")
    real = httpx.AsyncClient
    with patch("autodev.loopforge.client.httpx.AsyncClient", lambda **kw: real(transport=httpx.MockTransport(loopforge))):
        asyncio.run(ensure_vernal_loopforge_assets(LoopForgeClient("http://lf.test", "fixture"), "demo", settings))
    assert roles and {role["executorId"] for role in roles} == {"opencode"}
    assert settings.harness_for("coder") == "opencode"


def test_first_project_rules_reference_existing_workflows(monkeypatch):
    import asyncio
    import json

    import httpx

    from autodev.loopforge.bootstrap import decisions_rule_id, ensure_vernal_loopforge_assets

    rules, workflows = {}, {}
    bindings = []
    def handle(req):
        path = req.url.path
        if path in ("/api/rules", "/api/workflows"):
            assets = rules if path == "/api/rules" else workflows
            if req.method == "GET":
                return httpx.Response(200, json=list(assets.values()))
            item = json.loads(req.content)
            assert all(wid in workflows for wid in item.get("workflowIds", []))
            assets[item["id"]] = item
            if item["id"] == decisions_rule_id("fresh"):
                bindings.append(list(item["workflowIds"]))
            return httpx.Response(200, json=item)
        if path.endswith("/publish"):
            workflows[path.split("/")[-2]]["status"] = "published"
        if path == "/api/agents" and req.method == "PUT":
            assert set(json.loads(req.content)["rules"]) <= rules.keys()
        return httpx.Response(200, json=[] if req.method == "GET" else {})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handle)))
    client = LoopForgeClient("http://lf.test", "fixture")
    settings = Settings()
    asyncio.run(ensure_vernal_loopforge_assets(client, "fresh", settings))
    assert len(bindings) == 2 and bindings[0] == [] and bindings[1]
    rules[decisions_rule_id("fresh")]["content"] = "Existing decisions"
    asyncio.run(ensure_vernal_loopforge_assets(client, "fresh", settings))
    assert len(bindings) == 2
    assert rules[decisions_rule_id("fresh")]["content"] == "Existing decisions"
