"""项目启动台（新建 / 原地接管目录 / Git 分支克隆 / 浏览器导入）与 Runner 执行器状态/CodeBuddy 凭据联动回归测试。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from autodev import gitops
from autodev.artifacts import FileExchange
from autodev.config import Settings, Workspace
from autodev.db import Database
from autodev.engine import Engine, OpsError, init_workspace
from autodev.models import MilestoneStatus, ProjectStatus
from autodev.server.app import create_app


def _make_mock_lf(state_box: dict):
    def _lf(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p == "/api/auth/current":
            return httpx.Response(
                200,
                json={
                    "user": {
                        "id": 7,
                        "loginName": "alice",
                        "isGuest": False,
                        "codeBuddyApiKeyConfigured": state_box.get("cb_key_configured", False),
                        "codeBuddyInternetEnvironment": state_box.get("cb_env", ""),
                    }
                },
            )
        if p == "/api/users/7/credentials" and req.method == "PUT":
            body = json.loads(req.content)
            state_box["saved_cb_payload"] = body
            state_box["cb_key_configured"] = bool(body.get("codeBuddyApiKey"))
            state_box["cb_env"] = body.get("codeBuddyInternetEnvironment", "")
            return httpx.Response(200, json={"ok": True})
        if p == "/api/runners":
            return httpx.Response(200, json=state_box.get("runners", []))
        if p == "/api/projects":
            if req.method == "GET":
                return httpx.Response(200, json=list(state_box.get("projects", {}).values()))
            body = json.loads(req.content)
            state_box.setdefault("projects", {})[body["id"]] = body
            return httpx.Response(200, json=body)
        if p == "/api/repositories":
            if req.method == "GET":
                return httpx.Response(200, json=list(state_box.get("repos", {}).values()))
            body = json.loads(req.content)
            state_box.setdefault("repos", {})[body["id"]] = body
            return httpx.Response(200, json=body)
        if p in ("/api/sandboxes", "/api/agents", "/api/workflows", "/api/skills", "/api/mcp-servers") and req.method == "GET":
            return httpx.Response(200, json=[{"id": "local"}] if p == "/api/sandboxes" else [])
        if req.method == "PUT" or p.endswith("/publish") or p == "/api/runners/default":
            return httpx.Response(200, json={"status": "published", "ok": True})
        return httpx.Response(200, json={})

    return _lf


def test_host_dir_browse_and_path_traversal_guard(tmp_path: Path, monkeypatch) -> None:
    host_root = tmp_path / "host_home"
    proj_git = host_root / "my-git-app"
    proj_git.mkdir(parents=True)
    (proj_git / "README.md").write_text("# My App\n", "utf-8")
    gitops.ensure_repo(proj_git, "main")
    gitops.git(["checkout", "-b", "feat/ui"], proj_git)

    (host_root / ".ssh").mkdir()
    (host_root / "plain-dir").mkdir()
    outside = tmp_path / "secret-outside"
    outside.mkdir()

    monkeypatch.setenv("VERNAL_HOST_PROJECTS_DIR", str(host_root))
    ws = init_workspace(tmp_path / "workspaces" / "default", name="Default", collaboration_mode="files")
    eng = Engine(ws, settings=Settings(collaboration_mode="files", runtime_backend="local"))
    app = create_app(eng)

    with TestClient(app, client=("127.0.0.1", 51001)) as local:
        r = local.get("/vernal-api/host/dirs")
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is True
        names = [e["name"] for e in body["entries"]]
        assert "my-git-app" in names and "plain-dir" in names
        assert ".ssh" not in names  # 敏感目录自动屏蔽
        git_ent = next(e for e in body["entries"] if e["name"] == "my-git-app")
        assert git_ent["is_git"] is True
        assert git_ent["git_branch"] == "feat/ui"
        assert git_ent["has_readme"] is True

        # 路径穿越必须被拦截
        bad = local.get(f"/vernal-api/host/dirs?path={outside}")
        assert bad.status_code == 400

    # 未登录局域网访问宿主机目录必须返回 401
    with TestClient(app, client=("192.168.1.50", 51002)) as lan:
        assert lan.get("/vernal-api/host/dirs").status_code == 401

    eng.db.close()


def test_launch_project_scratch_local_inplace_and_git_repo(tmp_path: Path, monkeypatch) -> None:
    host_root = tmp_path / "host_projects"
    host_root.mkdir()
    monkeypatch.setenv("VERNAL_HOST_PROJECTS_DIR", str(host_root))

    # 准备一个已有本地 Git 项目用于「原地接管」
    existing_local = host_root / "existing-service"
    existing_local.mkdir()
    (existing_local / "app.py").write_text("print('keep me')\n", "utf-8")
    (existing_local / "README.md").write_text("# Existing Service\n", "utf-8")
    gitops.ensure_repo(existing_local, "main")
    gitops.commit_all(existing_local, "init existing")
    gitops.git(["checkout", "-b", "feat/v2"], existing_local)

    # 准备一个本地 bare Git 远端（带 dev 分支）用于「接入 Git 仓库与分支」
    seed_repo = tmp_path / "seed_repo"
    seed_repo.mkdir()
    (seed_repo / "main.py").write_text("x = 1\n", "utf-8")
    gitops.ensure_repo(seed_repo, "main")
    gitops.commit_all(seed_repo, "init main")
    gitops.git(["checkout", "-b", "dev"], seed_repo)
    (seed_repo / "dev_feature.py").write_text("dev = True\n", "utf-8")
    gitops.commit_all(seed_repo, "feat dev")
    bare_remote = tmp_path / "remote-demo.git"
    subprocess.run(["git", "clone", "--bare", str(seed_repo), str(bare_remote)], check=True, capture_output=True)

    lf_state = {"projects": {}, "repos": {}, "runners": []}
    real_async = httpx.AsyncClient
    transport = httpx.MockTransport(_make_mock_lf(lf_state))

    ws_root = tmp_path / "workspaces"
    ws_default = init_workspace(ws_root / "default", name="Default", collaboration_mode="files")
    eng = Engine(
        ws_default,
        settings=Settings(
            collaboration_mode="files",
            runtime_backend="loopforge",
            loopforge_url="http://lf.test",
            loopforge_token="afp_tok",
            loopforge_project_id="default",
        ),
    )
    app = create_app(eng, proxy_transport=transport)

    with patch("autodev.loopforge.client.httpx.AsyncClient", lambda **kw: real_async(transport=transport)):
        with TestClient(app, client=("127.0.0.1", 51003)) as client:
            # 1. 从零新建项目（含需求文本与附带文档）
            r1 = client.post(
                "/vernal-api/project/create",
                json={
                    "mode": "scratch",
                    "name": "Star App",
                    "wish": "做一个轻量任务看板",
                    "docs": [{"filename": "arch.md", "content": "# 架构说明\n前后端分离"}],
                    "collaboration_mode": "files",
                },
            )
            assert r1.status_code == 200, r1.text
            b1 = r1.json()
            assert b1["project_id"] == "star-app"
            assert b1["slices"] >= 2
            assert b1["loopforge_synced"] is True
            assert "star-app" in lf_state["projects"]

            # 2. 原地接管宿主机文件夹（保留原源码、自动识别 feat/v2 分支并写入代码库概览文档）
            r2 = client.post(
                "/vernal-api/project/create",
                json={
                    "mode": "local_folder",
                    "local_path": str(existing_local),
                    "wish": "补充健康检查接口",
                },
            )
            assert r2.status_code == 200, r2.text
            b2 = r2.json()
            assert b2["project_id"] == "existing-service"
            assert b2["workspace_root"] == str(existing_local.resolve())
            assert b2["collaboration_mode"] == "git"
            assert b2["branch"] == "feat/v2"
            assert (existing_local / "app.py").read_text("utf-8") == "print('keep me')\n"
            assert (existing_local / "inputs" / "00-existing-codebase.md").is_file()
            slices_local = client.get("/vernal-api/slices", headers={"X-Vernal-Project": "existing-service"}).json()
            assert any("app.py" in s["content"] and "keep me" in s["content"] for s in slices_local)

            # 3. 接入已有 Git 仓库并检出指定分支 dev + 自动向 LoopForge 注册默认仓库
            r3 = client.post(
                "/vernal-api/project/create",
                json={
                    "mode": "git_repo",
                    "git_url": str(bare_remote),
                    "branch": "dev",
                    "name": "Remote Demo",
                },
            )
            assert r3.status_code == 200, r3.text
            b3 = r3.json()
            assert b3["project_id"] == "remote-demo"
            assert b3["branch"] == "dev"
            assert b3["repository_id"] == "remote-demo-repo"
            assert (Path(b3["workspace_root"]) / "dev_feature.py").is_file()
            assert lf_state["projects"]["remote-demo"]["defaultRepositoryId"] == "remote-demo-repo"
            assert lf_state["repos"]["remote-demo-repo"]["defaultBranch"] == "dev"

    eng.db.close()

    # 模拟服务重启：必须能重新打开目录，且调度器实际读取的 settings.base_branch 保留 dev 与 feat/v2（绝不回退为 main）
    assert Workspace(existing_local).load_settings().base_branch == "feat/v2"
    assert Workspace(ws_root / "remote-demo").load_settings().base_branch == "dev"

    eng2 = Engine(ws_default, settings=Settings(collaboration_mode="files", runtime_backend="loopforge", loopforge_url="http://lf.test", loopforge_token="afp_tok"))
    app2 = create_app(eng2, proxy_transport=transport)
    with patch("autodev.loopforge.client.httpx.AsyncClient", lambda **kw: real_async(transport=transport)):
        with TestClient(app2, client=("127.0.0.1", 51004)) as client2:
            st = client2.get("/vernal-api/state", headers={"X-Vernal-Project": "existing-service"})
            assert st.status_code == 200
            assert st.json()["workspace_root"] == str(existing_local.resolve())
            assert st.json()["git_branch"] == "feat/v2"

            st_git = client2.get("/vernal-api/state", headers={"X-Vernal-Project": "remote-demo"})
            assert st_git.status_code == 200
            assert st_git.json()["git_branch"] == "dev"
    eng2.db.close()


def test_browser_folder_import_seeds_baseline_planner_slices_and_blocks_overwrite(tmp_path: Path) -> None:
    ws = init_workspace(tmp_path / "workspaces" / "default", name="Default", collaboration_mode="files")
    eng = Engine(ws, settings=Settings(collaboration_mode="files", runtime_backend="local"))
    app = create_app(eng)

    with TestClient(app, client=("127.0.0.1", 51005)) as client:
        res = client.post(
            "/vernal-api/project/import-folder",
            json={
                "name": "Uploaded Web",
                "wish": "给问候函数加上国际化支持",
                "files": [
                    {"path": "src/greeting.py", "content": "def greet(name):\n    return f'hi {name}'\n"},
                    {"path": "node_modules/pkg/a.js", "content": "ignored"},
                    {"path": "../escape.txt", "content": "blocked"},
                ],
            },
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["imported_files"] == 1
        root = Path(body["workspace_root"])
        assert (root / "src" / "greeting.py").is_file()
        assert not (root / "node_modules").exists()
        assert not (root.parent / "escape.txt").exists()
        assert (root / "inputs" / "00-existing-codebase.md").is_file()

        # 1. 开发任务文件交接基线必须包含导入的 src/greeting.py（不再是空工程）
        imp_ws = Workspace(root)
        imp_db = Database(imp_ws.db_path)
        base = FileExchange(imp_ws, imp_db).baseline([])
        assert base == {"src/greeting.py": "def greet(name):\n    return f'hi {name}'\n"}
        imp_db.close()

        # 2. 规划器切片（/vernal-api/slices）必须同时包含现有代码概览（含 src/greeting.py）与用户需求
        slices = client.get("/vernal-api/slices", headers={"X-Vernal-Project": body["project_id"]}).json()
        joined = "\n".join(s["content"] for s in slices)
        assert "src/greeting.py" in joined and "def greet(name):" in joined
        assert "国际化支持" in joined

        # 3. 再次用同名项目导入必须返回 409 Conflict，绝不静默覆盖已有文件
        dup = client.post(
            "/vernal-api/project/import-folder",
            json={
                "name": "Uploaded Web",
                "files": [{"path": "src/greeting.py", "content": "overwritten"}],
            },
        )
        assert dup.status_code == 409
        assert "def greet(name):" in (root / "src" / "greeting.py").read_text("utf-8")
    eng.db.close()


def test_offline_runner_never_reports_ready_even_when_local_cli_installed(tmp_path: Path) -> None:
    """LoopForge 底座下无任何在线 Runner 时，即使本机装了 Codex，也绝不能同时返回 runner_online=False 与 ready=True。"""
    ws = init_workspace(tmp_path / "workspaces" / "proj-off", name="Off", collaboration_mode="files")
    lf_state = {"cb_key_configured": False, "runners": []}
    real_async = httpx.AsyncClient
    transport = httpx.MockTransport(_make_mock_lf(lf_state))
    eng = Engine(
        ws,
        settings=Settings(
            collaboration_mode="files",
            runtime_backend="loopforge",
            loopforge_url="http://lf.test",
            loopforge_token="afp_tok",
            loopforge_project_id="proj-off",
        ),
    )
    app = create_app(eng, proxy_transport=transport)

    fake_local_rt = {
        "codex": {"label": "Codex", "installed": True, "ready": True, "version": "1.0", "bin": "/usr/local/bin/codex", "note": "已就绪"},
        "codebuddy": {"label": "CodeBuddy", "installed": True, "ready": True, "version": "2.0", "bin": "/usr/local/bin/codebuddy", "note": "已就绪"},
    }
    with (
        patch("autodev.loopforge.client.httpx.AsyncClient", lambda **kw: real_async(transport=transport)),
        patch.object(eng, "runtimes_status", return_value=fake_local_rt),
    ):
        with TestClient(app, client=("127.0.0.1", 51007)) as client:
            st = client.get("/vernal-api/state").json()
            assert st["runtimes"]["codex"]["runner_online"] is False
            assert st["runtimes"]["codex"]["ready"] is False
            assert st["runtimes"]["codebuddy"]["runner_online"] is False
            assert st["runtimes"]["codebuddy"]["ready"] is False
    eng.db.close()


def test_runner_unified_runtimes_and_codebuddy_key_save(tmp_path: Path) -> None:
    ws = init_workspace(tmp_path / "workspaces" / "proj-rt", name="RT", collaboration_mode="files")
    lf_state = {
        "cb_key_configured": False,
        "cb_env": "",
        "runners": [
            {
                "id": "r-mac",
                "name": "Vernal-Mac-Runner",
                "status": "online",
                "runtimes": [
                    {"provider": "codex", "status": "online", "authMode": "local", "version": "0.99.0", "binaryPath": "/usr/local/bin/codex"},
                    {"provider": "codebuddy", "status": "online", "authMode": "personal", "version": "2.1.0", "binaryPath": "/usr/local/bin/codebuddy"},
                ],
            }
        ],
    }
    real_async = httpx.AsyncClient
    transport = httpx.MockTransport(_make_mock_lf(lf_state))
    eng = Engine(
        ws,
        settings=Settings(
            collaboration_mode="files",
            runtime_backend="loopforge",
            loopforge_url="http://lf.test",
            loopforge_token="afp_tok",
            loopforge_project_id="proj-rt",
        ),
    )
    app = create_app(eng, proxy_transport=transport)

    with patch("autodev.loopforge.client.httpx.AsyncClient", lambda **kw: real_async(transport=transport)):
        with TestClient(app, client=("127.0.0.1", 51006)) as client:
            st1 = client.get("/vernal-api/state").json()
            assert st1["runner_summary"]["source"] == "loopforge_runner"
            assert st1["runtimes"]["codex"]["installed"] is True
            assert st1["runtimes"]["codex"]["ready"] is True
            assert st1["runtimes"]["codex"]["needs_api_key"] is False
            assert st1["runtimes"]["codebuddy"]["installed"] is True
            assert st1["runtimes"]["codebuddy"]["ready"] is False
            assert st1["runtimes"]["codebuddy"]["needs_api_key"] is True

            scan = client.post("/vernal-api/agent/rescan").json()
            assert scan["preferred_harness"] == "codex"

            save = client.post(
                "/vernal-api/credentials/codebuddy",
                json={"api_key": "cb_secret_123", "internet_environment": "internal"},
            )
            assert save.status_code == 200
            assert lf_state["saved_cb_payload"] == {
                "codeBuddyApiKey": "cb_secret_123",
                "codeBuddyInternetEnvironment": "internal",
            }

            st2 = client.get("/vernal-api/state").json()
            assert st2["runtimes"]["codebuddy"]["ready"] is True
            assert st2["runner_summary"]["codebuddy_api_key_configured"] is True

    eng.db.close()


def test_inplace_local_folder_files_mode_writes_back_and_detects_user_conflict(tmp_path: Path, monkeypatch) -> None:
    """文件模式原地接管目录：批准里程碑时把变更写回原目录；若任务运行期间用户手工改了同一文件则报冲突不覆盖。"""
    host_root = tmp_path / "host_plain"
    folder = host_root / "plain-app"
    folder.mkdir(parents=True)
    (folder / "src").mkdir()
    (folder / "src" / "app.py").write_text("VERSION = 1\n", "utf-8")
    (folder / "obsolete.txt").write_text("to delete\n", "utf-8")
    monkeypatch.setenv("VERNAL_HOST_PROJECTS_DIR", str(host_root))

    ws_default = init_workspace(tmp_path / "workspaces" / "default", name="Default", collaboration_mode="files")
    eng_host = Engine(ws_default, settings=Settings(collaboration_mode="files", runtime_backend="local"))
    app = create_app(eng_host)

    with TestClient(app, client=("127.0.0.1", 51008)) as client:
        r = client.post(
            "/vernal-api/project/create",
            json={"mode": "local_folder", "local_path": str(folder), "wish": "升级到 v2"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["collaboration_mode"] == "files"
    eng_host.db.close()

    # 模拟里程碑 M1 产出交付包：修改 src/app.py、新增 src/new.py、删除 obsolete.txt
    eng = Engine(Workspace(folder))
    ex = FileExchange(eng.ws, eng.db)
    delivery_key = ex.put({"src/app.py": "VERSION = 2\n", "src/new.py": "NEW = True\n"})
    eng.db.meta_set("delivery:M1", delivery_key)
    eng.db._conn.execute(
        "INSERT INTO milestones (milestone_id, seq_order, title, goal, integration_cmd, status, planned) VALUES (?,?,?,?,?,?,?)",
        ("M1", 1, "M1", "升级", "", MilestoneStatus.AWAITING_MERGE.value, 1),
    )

    # 1. 模拟用户在运行期间手工改了 src/app.py -> 必须提示冲突、绝不覆盖，且里程碑保持 AWAITING_MERGE
    (folder / "src" / "app.py").write_text("VERSION = 'manual-edit'\n", "utf-8")
    with pytest.raises(OpsError, match="src/app.py"):
        eng.merge_milestone("M1", actor="owner")
    assert (folder / "src" / "app.py").read_text("utf-8") == "VERSION = 'manual-edit'\n"
    assert eng.db.get_milestone("M1").status == MilestoneStatus.AWAITING_MERGE

    # 2. 用户同步/恢复原文件后再次批准 -> 变更原子写回原目录，删除旧文件，标为 COMPLETED
    (folder / "src" / "app.py").write_text("VERSION = 1\n", "utf-8")
    eng.merge_milestone("M1", actor="owner")
    assert (folder / "src" / "app.py").read_text("utf-8") == "VERSION = 2\n"
    assert (folder / "src" / "new.py").read_text("utf-8") == "NEW = True\n"
    assert not (folder / "obsolete.txt").exists()
    assert eng.db.get_milestone("M1").status == MilestoneStatus.MERGED
    assert eng.status == ProjectStatus.COMPLETED
    eng.db.close()


def test_import_folder_enforces_same_4mib_limit_as_file_exchange_and_lists_affected_files(tmp_path: Path) -> None:
    """导入与 FileExchange 共用 4 MiB 上限：4.2 MB 的 12 个文件直接报错列出超限文件，3.6 MB 的 12 个文件全部进入基线不截断。"""
    ws = init_workspace(tmp_path / "workspaces" / "default", name="Default", collaboration_mode="files")
    eng = Engine(ws, settings=Settings(collaboration_mode="files", runtime_backend="local"))
    app = create_app(eng)

    with TestClient(app, client=("127.0.0.1", 51009)) as client:
        # 1. 12 个文件共 ~4.2 MiB（12 * 368 KB）-> 必须拒绝导入、提示 4 MiB 上限与受影响文件，且不留残缺目录
        big_chunk = "a" * (368 * 1024)
        big_files = [{"path": f"src/f{i:02d}.txt", "content": big_chunk} for i in range(1, 13)]
        r_big = client.post("/vernal-api/project/import-folder", json={"name": "Too Big App", "files": big_files})
        assert r_big.status_code == 400
        detail = r_big.json()["detail"]
        assert "4 MiB" in detail
        assert "src/f12.txt" in detail
        assert not (tmp_path / "workspaces" / "too-big-app").exists()

        # 2. 12 个文件共 ~3.6 MiB（12 * 310 KB < 4 MiB）-> 全部导入，且 FileExchange.baseline([]) 包含全部 12 个文件
        ok_chunk = "b" * (310 * 1024)
        ok_files = [{"path": f"src/f{i:02d}.txt", "content": ok_chunk} for i in range(1, 13)]
        r_ok = client.post("/vernal-api/project/import-folder", json={"name": "Exact Fit App", "files": ok_files})
        assert r_ok.status_code == 200, r_ok.text
        assert r_ok.json()["imported_files"] == 12
        root = Path(r_ok.json()["workspace_root"])
        imp_ws = Workspace(root)
        imp_db = Database(imp_ws.db_path)
        base = FileExchange(imp_ws, imp_db).baseline([])
        assert len(base) == 12
        assert "src/f12.txt" in base
        imp_db.close()
    eng.db.close()
