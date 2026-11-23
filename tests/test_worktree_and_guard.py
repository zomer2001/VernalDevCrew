"""测试真实 Git 仓库中的 worktree 隔离、PathGuard 越界回滚（含新建未跟踪文件）与密钥拦截。"""

from __future__ import annotations

from pathlib import Path

from autodev import gitops, verifier


def test_worktree_isolation_and_path_guard_reverts_tracked_and_untracked(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    gitops.ensure_repo(repo, "main")

    # 预先在 main 上提交受保护的契约文件
    (repo / "contracts").mkdir()
    (repo / "contracts" / "api.py").write_text("VERSION = 1\n", "utf-8")
    gitops.commit_all(repo, "add contract")

    wt = tmp_path / "wt-t1"
    gitops.create_worktree(repo, wt, "autodev/T-001", "main")

    # 模拟编码会话：既改了白名单内的文件，又改了契约，又新建了越界文件与 .env
    (wt / "backend").mkdir()
    (wt / "backend" / "svc.py").write_text("def ok(): return True\n", "utf-8")
    (wt / "contracts" / "api.py").write_text("VERSION = 999\n", "utf-8")
    (wt / "sdk").mkdir()
    (wt / "sdk" / "rogue.py").write_text("bad\n", "utf-8")
    (wt / ".env").write_text("SECRET=123\n", "utf-8")

    guard = verifier.guard_worktree(wt, allowed_paths=["backend/"], protected_paths=["contracts/"])
    assert guard.ok is False
    assert "contracts/api.py" in guard.out_of_scope
    assert "sdk/rogue.py" in guard.out_of_scope
    assert ".env" in guard.secrets

    # 越界修改已被完整恢复，新建越界文件与 .env 已被删除，白名单内合法文件完好保留
    assert (wt / "contracts" / "api.py").read_text("utf-8") == "VERSION = 1\n"
    assert not (wt / "sdk" / "rogue.py").exists()
    assert not (wt / ".env").exists()
    assert (wt / "backend" / "svc.py").exists()

    # 再次检查应全部通过
    guard2 = verifier.guard_worktree(wt, allowed_paths=["backend/"], protected_paths=["contracts/"])
    assert guard2.ok is True
