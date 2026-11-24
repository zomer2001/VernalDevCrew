import shlex
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from autodev import gitops, local_git
from autodev.config import Settings
from autodev.engine import Engine, OpsError, init_workspace
from autodev.loopforge.bridge import sign_context_sig
from autodev.models import MilestoneStatus, ProjectStatus
from autodev.server.app import create_app


def test_launch_and_deliver_with_dirty_source(tmp_path, monkeypatch):
    projects = tmp_path / "host projects"
    projects.mkdir()
    monkeypatch.setenv("VERNAL_HOST_PROJECTS_DIR", str(projects))
    source = projects / "demo"
    source.mkdir()
    (source / "existing.txt").write_text("committed\n")
    (source / "delete.txt").write_text("remove this\n")
    (source / "image.bin").write_bytes(b"\0original")
    gitops.ensure_repo(source, "main")
    gitops.git(["remote", "add", "origin", "https://example.invalid/original.git"], source)
    (source / "existing.txt").write_text("staged\n")
    gitops.git(["add", "existing.txt"], source)
    (source / "existing.txt").write_text("unstaged\n")
    (source / "untracked").write_text("private")
    original = gitops.head_sha(source)

    def user_state():
        return (gitops.head_sha(source), gitops.current_branch(source), gitops.git(["write-tree"], source).stdout,
                (source / "existing.txt").read_text(), (source / "untracked").read_text(),
                gitops.git(["remote", "-v"], source).stdout)

    before = user_state()
    platform = init_workspace(tmp_path / "workspaces" / "launcher", collaboration_mode="files")
    eng = Engine(platform, settings=Settings(runtime_backend="local", collaboration_mode="files"))
    with TestClient(create_app(eng)) as client:
        response = client.post("/vernal-api/project/create", json={
            "mode": "local_folder", "local_path": str(source), "project_id": "demo", "wish": "update files"
        })
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["slices"] and data["delivery_branch_prefix"] == "vernal/milestone-"
    eng.db.close()
    workspace = Path(data["workspace_root"])
    assert user_state() == before
    assert not (source / ".autodev").exists()
    assert not gitops.has_remote(workspace)
    assert (workspace / "existing.txt").read_text() == "committed\n"
    assert not (workspace / "untracked").exists()

    from autodev.config import Workspace
    ws = Workspace(workspace)
    settings = ws.load_settings()
    settings.loopforge_token = "fixture"
    settings.loopforge_project_id = "demo"
    engine = Engine(ws, settings=settings)
    base = gitops.head_sha(workspace)
    worktree = ws.worktrees_dir / "T1"
    gitops.create_worktree(workspace, worktree, "task/T1", base)
    packs = ws.state_dir / "git-snapshots"
    snapshot = local_git.prepare_snapshot(worktree, base, packs)
    assert local_git.prepare_snapshot(worktree, base, packs) == snapshot
    signature = sign_context_sig(settings.effective_loopforge_token(), "demo", f"git:{snapshot}")
    route = f"/vernal-api/git-snapshots/{snapshot}.pack"
    runner = tmp_path / "runner"
    runner.mkdir()
    with TestClient(create_app(engine)) as client:
        assert client.get(route, params={"project": "demo", "sig": "invalid"}).status_code == 403
        response = client.get(route, params={"project": "demo", "sig": signature})
        assert response.status_code == 200
        (runner / "download.pack").write_bytes(response.content)
    # 通过 curl 的 file 协议执行与真实 Runner 完全相同的 Git 导入、打包命令。
    restore = local_git.restore_command("http://gateway", "demo", snapshot, signature)
    endpoint = shlex.quote(f"http://gateway{route}?project=demo&sig={signature}")
    restore = restore.replace(endpoint, shlex.quote((runner / "download.pack").as_uri()))
    subprocess.run(["sh", "-ec", restore], cwd=runner, check=True, capture_output=True)
    checkout = runner / "vernal-source"
    assert gitops.git(["rev-list", "--count", "HEAD"], checkout).stdout.strip() == "1"
    assert gitops.git(["cat-file", "-e", original], checkout, check=False).returncode
    assert (checkout / "image.bin").read_bytes() == b"\0original"
    (checkout / "image.bin").write_bytes(b"\0changed")
    (checkout / "delete.txt").unlink()
    subprocess.run(["sh", "-ec", local_git.PACKAGE_PATCH], cwd=runner, check=True, capture_output=True)
    artifact = (runner / "vernal-patch.json").read_text()
    with pytest.raises(ValueError, match="范围"):
        local_git.apply_patch(worktree, base, artifact, ["image.bin"], [])
    candidate = local_git.apply_patch(worktree, base, artifact, ["delete.txt", "image.bin"], [])
    engine.db._conn.execute(
        "INSERT INTO milestones(milestone_id,seq_order,title,goal,status,branch_name) VALUES(?,?,?,?,?,?)",
        ("M1", 1, "deliver", "", "AWAITING_MERGE", "task/T1")
    )
    branch = "vernal/milestone-M1"
    gitops.git(["branch", branch, original], source)
    with pytest.raises(OpsError, match="交付分支"):
        engine.merge_milestone("M1", "test")
    assert engine.db.get_milestone("M1").status == MilestoneStatus.AWAITING_MERGE
    assert gitops.head_sha(source, branch) == original
    gitops.git(["branch", "-D", branch], source)
    engine.merge_milestone("M1", "test")
    assert engine.status == ProjectStatus.COMPLETED
    assert gitops.head_sha(source, branch) == candidate
    assert local_git.publish(source, workspace, "demo", "M1", candidate, branch=branch) == branch
    assert user_state() == before
    assert (source / "image.bin").read_bytes() == b"\0original"
    assert not (source / ".opencode").exists()
    engine.db.close()


def test_commit_paths_preserves_unrelated_staging(tmp_path):
    gitops.ensure_repo(tmp_path, "main")
    (tmp_path / "personal").write_text("staged by user")
    (tmp_path / "owned").write_text("platform")
    gitops.git(["add", "personal"], tmp_path)
    assert gitops.commit_paths(tmp_path, ["owned"], "platform change")
    assert gitops.git(["diff", "--cached", "--name-only"], tmp_path).stdout.strip() == "personal"
    assert gitops.commit_paths(tmp_path, ["owned"], "no changes") is None


def test_loopforge_task_to_delivery_branch(tmp_path, monkeypatch):
    """Run through the scheduler, SOP publication, artifact check and real Git merge."""
    import asyncio
    import json
    import re

    import httpx
    from autodev.models import ProjectPlan
    from autodev.scheduler import Scheduler

    source = tmp_path / "user"
    source.mkdir()
    (source / "greeting").write_text("hello")
    gitops.ensure_repo(source, "main")
    head = gitops.head_sha(source)
    managed = tmp_path / "managed"
    local_git.clone_source(source, managed, "main")
    ws = init_workspace(managed, collaboration_mode="git")
    eng = Engine(ws, settings=Settings(
        loopforge_project_id="demo", loopforge_url="http://lf.test", loopforge_token="fixture",
        runtime_backend="loopforge", collaboration_mode="git", loopforge_workflow_id="existing",
        require_merge_approval=False, vernal_public_url="http://gateway",
    ))
    (ws.inputs_dir / "wish.md").write_text("Update the greeting in the local repository.")
    eng.ingest()
    eng.db.meta_set("source_mode", "local_folder")
    eng.db.meta_set("git_delivery_source", str(source))
    eng.db.meta_set("git_source_commit", head)
    task = dict(task_id="T1", milestone_id="M1", module="app", role="coder", title="Greeting",
                description="Change the greeting", acceptance_criteria=["updated"], depends_on=[],
                doc_slice_ids=[], allowed_paths=["greeting"], verify_cmds=["test -f greeting"], priority=1)
    eng.db.save_plan(ProjectPlan(summary="test", tech_stack="shell", agents_md="", modules=[],
                     milestones=[dict(milestone_id="M1", title="deliver", goal="greeting",
                                      integration_cmd="test -f greeting")], tasks=[task]), 1)
    eng.set_status(ProjectStatus.APPROVED)
    workflows = {}
    run = {}
    runner = tmp_path / "runner"
    runner.mkdir()
    proxy = httpx.MockTransport(lambda req: pytest.fail(f"Unexpected proxy request {req.url.path}"))
    with TestClient(create_app(eng, proxy_transport=proxy)) as gateway:
        def lf(request):
            path = request.url.path
            if path == "/api/runners":
                return httpx.Response(200, json=[{"id": "local", "name": "test", "status": "online", "isDefault": True, "runtimes": [{"provider": "codex"}]}])
            if path == "/api/workflows" and request.method == "GET":
                return httpx.Response(200, json=list(workflows.values()))
            if path == "/api/workflows" and request.method == "PUT":
                wf = json.loads(request.content)
                workflows[wf["id"]] = wf
                return httpx.Response(200, json=wf)
            if path == "/api/issues":
                issue = json.loads(request.content)
                assert not issue.get("repositoryId")
                wf = workflows[issue["workflowId"]]
                command = re.search(r"```sh\n(.*?)\n```", issue["description"], re.S)[1]
                url = re.search(r"'(http://gateway/[^']+)'", command)[1]
                response = gateway.get(url.removeprefix("http://gateway"))
                assert response.status_code == 200
                file = runner / "snapshot.pack"
                file.write_bytes(response.content)
                subprocess.run(["sh", "-ec", command.replace(shlex.quote(url), file.as_uri())],
                               cwd=runner, check=True, capture_output=True)
                (runner / "vernal-source" / "greeting").write_text("updated")
                subprocess.run(["sh", "-ec", wf["nodes"][1]["script"]], cwd=runner, check=True)
                run.update(id="run1", status="succeeded", artifacts=[
                    {"name": "vernal-patch.json", "content": (runner / "vernal-patch.json").read_text()}])
                return httpx.Response(201, json={"id": "issue1"})
            if path == "/api/issues/issue1/dispatch":
                return httpx.Response(200, json={"latestRunId": "run1"})
            if path == "/api/runs/run1":
                return httpx.Response(200, json=run)
            raise AssertionError((request.method, path))

        real = httpx.AsyncClient
        monkeypatch.setattr("autodev.loopforge.client.httpx.AsyncClient", lambda **kw: real(transport=httpx.MockTransport(lf)))
        assert asyncio.run(Scheduler(eng).run()) == ProjectStatus.COMPLETED
        assert gitops.git(["show", "vernal/demo/M1:greeting"], source).stdout == "updated"
        assert gitops.head_sha(source) == head
    eng.db.close()
