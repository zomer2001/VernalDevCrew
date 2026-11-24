import asyncio
import subprocess
import time
from urllib.parse import urlsplit
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from autodev import gitops, local_git, local_git_bridge
from autodev.config import Settings, Workspace
from autodev.engine import Engine, init_workspace
from autodev.server.app import create_app


@pytest.mark.parametrize("payload", ["text", "binary"])
def test_large_host_directory_uses_private_mirror_and_separate_delivery(tmp_path, monkeypatch, payload):
    source = tmp_path / "projects" / "large"
    source.mkdir(parents=True)
    if payload == "text":
        (source / "code.txt").write_text("x" * (4 * 1024 * 1024 + 1))
    else:
        (source / "logo.png").write_bytes(b"\x89PNG\x00\xff")
    (source / ".env").write_text("test credential")
    monkeypatch.setenv("VERNAL_HOST_PROJECTS_DIR", str(source.parent))
    monkeypatch.setenv("VERNAL_POOL_DIR", str(tmp_path / "pool"))
    pw = local_git_bridge.password_path()
    pw.parent.mkdir()
    pw.write_text("test-gitea-password")
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    monkeypatch.setattr(local_git_bridge, "urls", lambda project: (str(remote), str(remote)))
    real = httpx.AsyncClient
    created = []
    def gitea(req):
        if req.method == "POST":
            created.append(req.content)
            return httpx.Response(201, json={})
        return httpx.Response(200 if created else 404, json={"private": True})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(base_url=kw["base_url"], transport=httpx.MockTransport(gitea)))
    eng = Engine(init_workspace(tmp_path / "workspaces" / "default", collaboration_mode="files"),
                 settings=Settings(collaboration_mode="files"))
    with TestClient(create_app(eng), client=("127.0.0.1", 10)) as client:
        res = client.post("/vernal-api/project/create", json={"mode":"local_folder", "name":"Large", "local_path":"large",
                                                              "collaboration_mode":"files"})
        assert res.status_code == 200, res.text
        workspace = Path(res.json()["workspace_root"])
        assert created and workspace != source and not (workspace / ".env").exists()
        if payload == "binary":
            assert (workspace / "logo.png").read_bytes() == (source / "logo.png").read_bytes()
        assert gitops.head_sha(remote, "refs/heads/main") == gitops.head_sha(source, "HEAD")
        assert gitops.git(["remote", "get-url", "vernal-bridge"], source).stdout.strip() == str(remote)
        baseline = gitops.head_sha(source)
        (source / "code.txt").write_text("in progress")
        (workspace / "new.txt").write_text("delivered")
        gitops.commit_paths(workspace, ["new.txt"], "test")
        commit = gitops.head_sha(workspace)
        branch = res.json()["delivery_branch_prefix"] + "M1"
        assert branch == "vernal/milestone-M1"
        local_git_bridge.deliver("large", workspace, branch, commit)
        local_git.publish(source, workspace, "large", "M1", commit, branch=branch)
        local_git_bridge.deliver("large", workspace, branch, commit)
        assert gitops.head_sha(remote, branch) == gitops.head_sha(source, branch) == commit
        assert gitops.head_sha(source) == baseline and (source / "code.txt").read_text() == "in progress"
    eng.db.close()


def test_private_task_checkout_push_and_signed_relay(tmp_path, monkeypatch):
    monkeypatch.setenv("VERNAL_POOL_DIR", str(tmp_path / "pool"))
    pw = local_git_bridge.password_path()
    pw.parent.mkdir()
    pw.write_text("fixture")
    ws = init_workspace(tmp_path / "ws", collaboration_mode="git")
    eng = Engine(ws, settings=Settings(loopforge_project_id="demo", loopforge_token="fixture-signing"))
    repo = tmp_path / "task.git"
    gitops.git(["init", "--bare", str(repo)], tmp_path)
    async def ensure(name, marker):
        assert marker.startswith("Vernal task ") and name.startswith("vernal-task-")
    monkeypatch.setattr(local_git_bridge, "ensure_private_repo", ensure)
    monkeypatch.setattr(local_git_bridge, "urls", lambda name: (repo.as_uri(), repo.as_uri()))
    try:
        snapshot = local_git.prepare_snapshot(ws.root, "HEAD", ws.state_dir / "packs")
        remote = asyncio.run(local_git_bridge.task_remote(ws.root, eng.settings, eng.db, "T1", snapshot))
        checkout = tmp_path / "runner"
        checkout.mkdir()
        restore = local_git_bridge.restore_command(repo.as_uri(), snapshot)
        for _ in range(2):
            subprocess.run(["sh", "-ec", restore], cwd=checkout, check=True, capture_output=True)
        (checkout / "vernal-source/result.txt").write_text("result\n")
        subprocess.run(["sh", "-ec", local_git.PACKAGE_PATCH], cwd=checkout, check=True, capture_output=True)
        result = gitops.git(["for-each-ref", "--format=%(objectname)", "refs/heads/results/"], repo).stdout.strip()
        assert gitops.git(["show", f"{result}:result.txt"], repo).stdout == "result\n"
        assert gitops.head_sha(repo, f"base/{snapshot}") == snapshot

        path = urlsplit(remote).path
        task_repo = path.split("/")[-3]
        monkeypatch.setattr(local_git_bridge, "urls", lambda name: (f"http://gitea/{name}.git", ""))
        def upstream(req):
            assert req.url.path.startswith(f"/{task_repo}.git/") and req.headers["authorization"].startswith("Basic ")
            assert not req.headers.get("cookie")
            if req.method == "POST":
                assert req.content == b"git request"
            return httpx.Response(200, content=b"git response", headers={"content-type": "application/x-git-upload-pack-result"})
        original = httpx.AsyncClient
        app = create_app(eng)
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(**kw, transport=httpx.MockTransport(upstream)))
        with TestClient(app, client=("192.0.2.7", 42)) as client:
            assert client.get(path + "/info/refs?service=git-upload-pack").content == b"git response"
            assert client.post(path + "/git-receive-pack", content=b"git request").content == b"git response"
            assert client.get(path.replace(task_repo, "vernal-task-" + "0"*32) + "/info/refs?service=git-upload-pack").status_code == 403
            assert client.get(path + "/config").status_code == 404
            monkeypatch.setattr(time, "time", lambda: 2**40)
            assert client.get(path + "/info/refs?service=git-upload-pack").status_code == 403
    finally:
        eng.db.close()


def test_verified_export_is_idempotent_and_preserves_existing_branch(tmp_path, monkeypatch):
    monkeypatch.setenv("VERNAL_POOL_DIR", str(tmp_path / "pool"))
    pw = local_git_bridge.password_path()
    pw.parent.mkdir()
    pw.write_text("test")
    remote = tmp_path / "export.git"
    gitops.git(["init", "--bare", str(remote)], tmp_path)
    async def ensure(name):
        assert name == "demo"
    monkeypatch.setattr(local_git_bridge, "ensure_private_repo", ensure)
    monkeypatch.setattr(local_git_bridge, "urls", lambda name: (str(remote), str(remote)))
    for _ in range(2):
        branch = asyncio.run(local_git_bridge.export_release("demo", "M1", {"web/index.html": "ok"}))
        assert gitops.git(["show", branch + ":web/index.html"], remote).stdout == "ok"
    with pytest.raises(gitops.GitError):
        asyncio.run(local_git_bridge.export_release("demo", "M1", {"web/index.html": "new"}))
    assert gitops.git(["show", branch + ":web/index.html"], remote).stdout == "ok"


def test_blank_host_folder_prompts_for_first_requirement(tmp_path, monkeypatch):
    root = tmp_path / "host"
    (root / "empty").mkdir(parents=True)
    monkeypatch.setenv("VERNAL_HOST_PROJECTS_DIR", str(root))
    eng = Engine(init_workspace(tmp_path / "workspace" / "demo", collaboration_mode="files"),
                 settings=Settings(collaboration_mode="files"))
    with TestClient(create_app(eng), client=("127.0.0.1", 11)) as client:
        res = client.post("/vernal-api/project/create", json={"mode":"local_folder", "local_path":"empty",
                                                              "start_council":True})
        assert res.is_success and res.json()["needs_wish"] and not res.json()["council_started"]
    eng.db.close()


@pytest.mark.parametrize("private", [False, True], ids=["new-git-project", "private-https-project"])
def test_managed_project_first_delivery(tmp_path, monkeypatch, private):
    monkeypatch.setenv("VERNAL_POOL_DIR", str(tmp_path / "pool"))
    seed = Engine(init_workspace(tmp_path / "workspaces" / "launcher", collaboration_mode="files"),
                  settings=Settings(runtime_backend="local", collaboration_mode="files"))
    url = "https://example.invalid/team/sample.git"
    secret = "private-test-credential"
    destination = tmp_path / "remote.git"
    if private:
        source = tmp_path / "source"
        source.mkdir()
        gitops.ensure_repo(source, "main")
        gitops.git(["clone", "--bare", str(source), str(destination)], tmp_path)
        def transport(cwd, remote, username, token, args):
            assert (remote, username, token) == (url, "tester", secret)
            return gitops.git([str(destination) if arg == url else arg for arg in args], cwd).stdout.strip()
        monkeypatch.setattr(local_git_bridge, "remote_git", transport)
    body = {"project_id": "demo", "collaboration_mode": "git", "wish": "add a greeting",
            "mode": "git_repo" if private else "scratch"}
    if private:
        body.update(git_url=url, git_username="tester", git_token=secret)
    with TestClient(create_app(seed)) as client:
        response = client.post("/vernal-api/project/create", json=body)
        assert response.is_success, response.text
        assert secret not in response.text
    seed.db.close()
    root = Path(response.json()["workspace_root"])
    ws = Workspace(root)
    eng = Engine(ws)
    try:
        from autodev.scheduler import Scheduler
        eng.settings.runtime_backend = "loopforge"
        eng.settings.loopforge_token = "authenticated"
        Scheduler(eng).check_runtime()
        initial = gitops.head_sha(root)
        gitops.git(["checkout", "-b", "integrate/M1"], root)
        (root / "hello.txt").write_text("hello")
        gitops.commit_paths(root, ["hello.txt"], "greeting")
        gitops.git(["checkout", "main"], root)
        eng.db._conn.execute(
            "INSERT INTO milestones(milestone_id,seq_order,title,goal,status,branch_name) VALUES(?,?,?,?,?,?)",
            ("M1", 1, "deliver", "", "AWAITING_MERGE", "integrate/M1"))
        eng.merge_milestone("M1", "tester")
        branch = eng.db.meta_get("delivery_branch:M1")
        assert branch == "vernal/milestone-M1" and gitops.head_sha(root, branch) != initial
        if private:
            assert gitops.git(["show", branch + ":hello.txt"], destination).stdout == "hello"
            assert gitops.git(["show", "main:hello.txt"], destination, check=False).returncode != 0
            access = tmp_path / "pool/remote-access" / (eng.db.meta_get("private_remote") + ".json")
            assert access.stat().st_mode & 0o777 == 0o600
            assert secret not in ws.config_path.read_text() and secret not in (root / ".git/config").read_text()
    finally:
        eng.db.close()
