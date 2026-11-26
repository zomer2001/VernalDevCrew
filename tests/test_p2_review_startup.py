"""Review startup must make its fixture visible without replacing existing answers."""
import os
from pathlib import Path
import subprocess
import sys

from autodev.config import Workspace
from autodev.engine import Engine, init_workspace
from autodev.speckit import missions, service

ROOT = Path(__file__).resolve().parents[1]


def prepare(workspace, *args):
    env = {**os.environ, "VERNAL_REVIEW_WORKSPACE": str(workspace), "VERNAL_REVIEW_PYTHON": sys.executable}
    return subprocess.run([str(ROOT / "scripts/start_p2_review.sh"), "--prepare-only", *args],
                          env=env, cwd=ROOT, capture_output=True, text=True, timeout=30)


def test_unbound_review_starts_without_invisible_fixture(tmp_path):
    workspace = tmp_path / "review"
    result = prepare(workspace)
    assert result.returncode == 0, result.stderr
    assert "尚未绑定" in result.stdout
    engine = Engine(Workspace(workspace))
    try:
        assert not missions.all_missions(engine)
        assert not service.enabled(engine)
    finally:
        engine.db.close()
    result = prepare(workspace, "--fixture")
    assert result.returncode != 0 and "尚未绑定" in result.stderr


def test_bound_review_fixture_preserves_answers_on_rerun(tmp_path):
    ws = init_workspace(tmp_path / "bound-review", collaboration_mode="files")
    ws.config_path.write_text(ws.config_path.read_text().replace('loopforge_project_id = ""', 'loopforge_project_id = "review-only"'))
    result = prepare(ws.root, "--fixture")
    assert result.returncode == 0, result.stderr
    assert "review-only" in result.stdout
    engine = Engine(ws)
    try:
        q = service.list_clarifications(engine, "001-initial")[0]
        revision = service.latest(engine, "001-initial")
        answer = service.answer_clarification(engine, q["clarify_id"], "保留至负责人主动删除",
                                             expected_rev_id=revision["rev_id"], actor="test-owner", supplement="保留这段原文")
        result = prepare(ws.root, "--fixture")
        assert result.returncode == 0, result.stderr
        current = service.latest(engine, "001-initial")
        assert current["rev_id"] == answer["rev_id"]
        assert "保留这段原文" in current["content_text"]
        assert len(service.list_clarifications(engine, "001-initial")) == 1
        assert not engine.db.list_tasks()
        assert not engine.db._query("SELECT 1 FROM dispatch_records")
    finally:
        engine.db.close()
