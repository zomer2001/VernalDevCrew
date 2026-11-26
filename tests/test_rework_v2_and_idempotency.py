"""Real Git and scheduler, controlled upstream HTTP failures at the human gate."""
import asyncio
import sqlite3
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from autodev import gitops, local_git
from autodev.db import Database, SCHEMA
from autodev.engine import Engine, init_workspace
from autodev.harnesses.mock import MockHarness
from autodev.models import MilestoneStatus, ProjectStatus, TaskStatus
from autodev.scheduler import Scheduler
from autodev.server.app import create_app
from autodev.steering import reconcile_steering_checkpoint
from test_steering_and_global_rules import _make_steering_mock_lf, _seed_steering_project


@pytest.mark.parametrize("rolling_changed", [False, True], ids=["advance", "preserve_manual_commit"])
def test_rework_v2_and_idempotency(tmp_path, monkeypatch, rolling_changed):
    source = tmp_path / "original"
    source.mkdir()
    gitops.ensure_repo(source, "main")
    original = gitops.head_sha(source)
    root = tmp_path / "platform"
    local_git.clone_source(source, root, "main")
    ws = init_workspace(root)
    gitops.commit_paths(root, ["inputs/.gitkeep"], "seed project inputs")
    rounds = {"T-001": 0, "T-002": 0}

    def code(prompt, cwd, call):
        rounds[cwd.name] += 1
        file = "src/" + cwd.name
        (cwd / "src").mkdir(exist_ok=True)
        (cwd / file).write_text(str(rounds[cwd.name]))
        return dict(status="DONE", summary="implemented", files_changed=[file], tests_added=[], notes="")

    engine = Engine(ws, harness=MockHarness(coder=code))
    s = engine.settings
    s.runtime_backend = "local"
    s.loopforge_project_id = "proj-s1"
    s.loopforge_token = "fixture"
    s.require_merge_approval = True
    db = engine.db
    db.meta_set("git_delivery_source", str(source))
    _seed_steering_project(engine)
    db._exec("DELETE FROM tasks WHERE task_id='T-004'")  # fixture has three starting tasks
    db.update_milestone("M1", status=MilestoneStatus.PENDING)
    for tid in rounds:
        db.update_task(tid, status=TaskStatus.READY if tid == "T-001" else TaskStatus.BLOCKED,
                       verify_cmds=[f"test -s src/{tid}"])
    rolling = "vernal/proj-s1/M1"
    archive = rolling + "-versions"
    box = {}
    handle = _make_steering_mock_lf(box)
    failures = {"dispatch": True, "approve": True, "missing": False}

    def upstream(request):
        path = request.url.path
        if path.endswith("/dispatch") and failures["dispatch"]:
            return httpx.Response(503, json={"error": "unavailable"})
        if path.endswith("/approve") and failures["approve"]:
            return httpx.Response(503, json={"error": "unavailable"})
        if "/artifacts/" in path and failures["missing"]:
            return httpx.Response(404, json={"error": "pending"})
        return handle(request)

    async def scenario():
        assert await Scheduler(engine).run() != ProjectStatus.STALLED
        engine.merge_milestone("M1", "owner")
        first = gitops.head_sha(source, rolling)
        assert first == gitops.head_sha(source, archive + "/v1")
        s.runtime_backend = "loopforge"
        transport = httpx.MockTransport(upstream)
        client_cls = httpx.AsyncClient
        gateway = create_app(engine, proxy_transport=transport)
        async with client_cls(transport=httpx.ASGITransport(app=gateway), base_url="http://localhost") as client:
            monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client_cls(transport=transport))
            start = await client.post("/vernal-api/steering/start", json={"topic": "review"})
            assert start.status_code == 503
            cp = db.list_checkpoints()[-1]
            assert cp["status"] == "DISPATCH_FAILED" and cp["last_error"] and cp["loopforge_issue_id"]
            cid = cp["checkpoint_id"]
            route = f"/vernal-api/steering/{cid}"
            failures["dispatch"] = False
            retry = await client.post(route + "/retry", json={})
            assert retry.status_code == 200 and len(box["issues"]) == 1
            rid = retry.json()["checkpoint"]["loopforge_run_id"]
            run = box["runs"][rid]
            old = dict(summary="old", decisions_upsert=[], decisions_revoke=[], cancel_tasks=[],
                       reopen_tasks=[{"task_id": "T-001", "reason": "incorrect"}], new_tasks=[])
            fresh = dict(old, summary="new", reopen_tasks=[{"task_id": "T-002", "reason": "update"}],
                         cancel_tasks=["T-003"], new_tasks=[{"title": "follow up", "milestone_id": "M2"}])
            def gate(plan, generation):
                run.update(status="waiting_approval", nodes={"human_gate": {"status": "waiting_approval"},
                                                            "director_plan": {"reworkCount": generation}})
                aid = str(generation)
                run["artifacts"] = [{"id": aid, "nodeId": "director_plan", "name": "steering-plan.json"}]
                box.setdefault("artifact_blobs", {})[rid, aid] = plan
            gate(old, 0)
            assert (await reconcile_steering_checkpoint(engine, cid))["plan_revision"] == 1
            rejected = await client.post(route + "/reject", json={"reason": "只退回鉴权"})
            assert rejected.json()["checkpoint"]["draft_plan"] == {}
            run["status"] = "succeeded"
            failures["missing"] = True
            assert (await reconcile_steering_checkpoint(engine, cid))["status"] == "SYNC_PENDING"
            failures["missing"] = False
            assert (await reconcile_steering_checkpoint(engine, cid))["status"] == "SYNC_PENDING"
            assert db.get_task("T-001").status == TaskStatus.MERGED
            gate(fresh, 1)
            assert (await reconcile_steering_checkpoint(engine, cid))["plan_revision"] == 2
            assert (await client.post(route + "/approve", json={"expected_revision": 1})).status_code == 409
            # The upstream gate can change before the next local SSE refresh.
            box["artifact_blobs"][rid, "1"] = dict(fresh, summary="unseen revision")
            assert (await client.post(route + "/approve", json={"expected_revision": 2})).status_code == 409
            assert run["status"] == "waiting_approval" and db.get_task("T-004") is None
            box["artifact_blobs"][rid, "1"] = fresh
            response = await client.post(route + "/approve", json={"expected_revision": 2})
            assert response.status_code == 503
            assert db.get_checkpoint(cid)["status"] == "AWAITING_APPROVAL"
            assert db.get_task("T-004") is None
            failures["approve"] = False
            response, _ = await asyncio.gather(client.post(route + "/approve", json={"expected_revision": 2}),
                                               reconcile_steering_checkpoint(engine, cid))
            assert response.status_code == 200
            assert db.get_checkpoint(cid)["applied_summary"]["added_tasks"] == ["T-004"]
            assert db.get_task("T-004").loopforge_issue_id is None
            assert not any(i.get("title", "").startswith("[T-004]") for i in box["issues"].values())
            assert db.get_task("T-003").status == TaskStatus.CANCELLED
            assert db.get_task("T-002").status == TaskStatus.READY
            assert db.get_task("T-001").status == TaskStatus.MERGED
        s.runtime_backend = "local"
        assert await Scheduler(engine).run() != ProjectStatus.STALLED
        assert db.get_milestone("M1").status == MilestoneStatus.AWAITING_MERGE
        if rolling_changed:
            human = tmp_path / "human"
            gitops.git(["worktree", "add", str(human), rolling], source)
            (human / "manual.txt").write_text("human commit")
            gitops.commit_all(human, "human commit")
            manual_sha = gitops.head_sha(human)
            gitops.remove_worktree(source, human)
        engine.merge_milestone("M1", "owner")
        records = db.list_milestone_deliveries("M1")
        assert [r["version_no"] for r in records] == [1, 2]
        assert records[1]["prev_commit_sha"] == first
        second = records[1]["commit_sha"]
        assert gitops.head_sha(source, archive + "/v2") == second
        assert gitops.head_sha(source, rolling) == (manual_sha if rolling_changed else second)
        if rolling_changed:
            assert db.meta_get("delivery_branch:M1") == archive + "/v2"
            assert any(ev["event_type"] == "milestone.delivery_conflict" for ev in db.events_after(0))
        assert gitops.head_sha(source, "refs/tags/vernal-M1-v1") == first
        assert gitops.head_sha(source, "refs/tags/vernal-M1-v2") == second
        assert gitops.head_sha(source, archive + "/v1") == first
        assert gitops.git(["show", archive + "/v2:src/T-002"], source).stdout == "2"
        assert rounds == {"T-001": 1, "T-002": 2}
        assert gitops.head_sha(source) == original

    try:
        asyncio.run(scenario())
    finally:
        db.close()


def test_historical_ids_and_concurrent_allocations(tmp_path):
    path = tmp_path / "state.db"
    db = Database(path)
    db.start_run("T-105", 1, "CODE")
    db.add_event("old", "archived", task_id="T-111")
    db.close()
    def allocate(_):
        db = Database(path)
        try:
            return db.next_task_id()
        finally:
            db.close()
    with ThreadPoolExecutor(4) as pool:
        assert sorted(pool.map(allocate, range(8))) == [f"T-{i}" for i in range(112, 120)]


def test_inplace_version_zip_and_manual_edits(tmp_path):
    import io
    import zipfile
    import pytest
    from fastapi.testclient import TestClient
    from autodev.artifacts import FileExchange
    from autodev.engine import OpsError

    ws = init_workspace(tmp_path / "files", collaboration_mode="files")
    engine = Engine(ws)
    db = engine.db
    exchange = FileExchange(ws, db)
    db.meta_set("source_mode", "local_folder")
    db._exec("INSERT INTO milestones(milestone_id,seq_order,title,goal,status) VALUES('M1',1,'files','','PENDING')")
    path = ws.root / "auth.txt"
    path.write_text("seed")
    db.meta_set("seed_baseline_digest", exchange.put({"auth.txt": "seed"}))
    for version in ("first", "second"):
        db.meta_set("delivery:M1", exchange.put({"auth.txt": version}))
        db.update_milestone("M1", status=MilestoneStatus.AWAITING_MERGE)
        if version == "second":
            path.write_text("manual")
            with pytest.raises(OpsError, match="冲突"):
                engine.merge_milestone("M1", "tester")
            assert path.read_text() == "manual" and len(db.list_milestone_deliveries()) == 1
            other = ws.root / "user-file.txt"
            other.write_text("first")
            path.unlink()
            path.symlink_to(other)
            with pytest.raises(OpsError, match="符号链接"):
                engine.merge_milestone("M1", "tester")
            assert other.read_text() == "first" and len(db.list_milestone_deliveries()) == 1
            path.unlink()
            path.write_text("first")
        engine.merge_milestone("M1", "tester")
        assert path.read_text() == version
    with TestClient(create_app(engine)) as client:
        for num, content in enumerate(("first", "second"), 1):
            response = client.get(f"/vernal-api/milestones/M1/files.zip?version={num}")
            assert zipfile.ZipFile(io.BytesIO(response.content)).read("auth.txt").decode() == content
    db.close()


def test_existing_database_migration_twice(tmp_path):
    path = tmp_path / "state.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(SCHEMA.split("CREATE TABLE IF NOT EXISTS link_codes")[0])
    legacy.execute("INSERT INTO evolution_checkpoints(round_no,trigger_topic,created_at,updated_at) "
                   "VALUES(7,'existing review','old','old')")
    legacy.commit()
    legacy.close()
    for _ in range(2):
        db = Database(path)
        try:
            record = db.get_checkpoint(1)
            assert record["round_no"] == 7
            assert record["rejected_plans"] == [] and record["plan_revision"] == 0
            assert record["min_director_rework"] == 0 and record["draft_artifact_id"] is None
            assert not db.list_milestone_deliveries()
            assert "idx_tasks_checkpoint_item" in {r["name"] for r in db._query("PRAGMA index_list(tasks)")}
        finally:
            db.close()


def test_approval_input_survives_refresh_and_revision():
    module = Path(__file__).resolve().parents[1] / "autodev/server/static/js/steering.js"
    # The same input node retains focus and selection even during an IME composition.
    script = """
import assert from 'node:assert/strict';
const { renderSteering } = await import(process.argv[1]);
let input;
const card = {
  hidden: true, dataset: {},
  set innerHTML(value) { input = new EventTarget(); },
  querySelector(key) { return key === '#st-reject-reason-input' ? input : null; }
};
globalThis.CSS = { escape: s => s };
globalThis.document = { getElementById: () => null,
  querySelector: key => key === '#steering-approval-card' ? card : null };
const cp = {checkpoint_id: 1, status: 'AWAITING_APPROVAL', plan_revision: 1};
const state = {project_key: 'demo', checkpoints: [cp]};
renderSteering(state);
const first = input;
input.value = '只退回鉴权';
input.dispatchEvent(new Event('input'));
renderSteering({...state, tasks: [{status: 'MERGED'}]});
assert.equal(input, first);
renderSteering({...state, checkpoints: [{...cp, plan_revision: 2}]});
assert.notEqual(input, first);
assert.equal(input.value, '只退回鉴权');
renderSteering({...state, project_key: 'other'});
assert.equal(input.value, '');
"""
    subprocess.run(["node", "--input-type=module", "-e", script, module.as_uri()], check=True)


def test_apply_race_across_database_connections_and_recover_rollback(tmp_path, monkeypatch):
    from threading import Barrier
    from autodev import steering

    ws = init_workspace(tmp_path / "race", collaboration_mode="files")
    first, second = Engine(ws), Engine(ws)
    try:
        first.settings.runtime_backend = second.settings.runtime_backend = "local"
        db = first.db
        plan = {"summary": "one task", "new_tasks": [{"title": "task", "milestone_id": "M1"}]}
        cp = db.create_checkpoint(kind="REVIEW", trigger_topic="race", status="AWAITING_APPROVAL")
        cid = cp["checkpoint_id"]
        db.update_checkpoint(cid, draft_plan=plan, plan_revision=1)
        with monkeypatch.context() as scoped:
            def fail_after_edits(*args, **kwargs):
                raise RuntimeError("interrupted before commit")
            scoped.setattr(db, "add_event", fail_after_edits)
            with pytest.raises(RuntimeError, match="interrupted"):
                asyncio.run(steering.apply_steering_plan(first, cid, plan))
        assert db.get_checkpoint(cid)["status"] == "SYNC_PENDING"
        assert db.list_tasks() == [] and db.meta_get("task_seq") is None
        gate = Barrier(2)

        def approve(engine):
            gate.wait(timeout=10)
            return asyncio.run(steering.apply_steering_plan(engine, cid, plan))

        with ThreadPoolExecutor(2) as pool:
            assert [r["status"] for r in pool.map(approve, [first, second])] == ["APPLIED", "APPLIED"]
        assert [t.task_id for t in db.list_tasks()] == ["T-001"]
        assert db.get_checkpoint(cid)["last_error"] is None
        assert sum(e["event_type"] == "steering.applied" for e in db.events_after(0)) == 1
    finally:
        first.db.close()
        second.db.close()
