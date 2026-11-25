"""Regression contracts for native dispatch, candidate verification and delivery."""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from autodev import gitops, public_pool
from autodev.artifacts import FileExchange
from autodev.ci import service as ci
from autodev.context_bundle import build_task_input_bundle
from autodev.db import Database
from autodev.engine import Engine, init_workspace
from autodev.governance.audit import flush_decisions, verify_audit_chain
from autodev.loopforge.bootstrap import build_vernal_workflows, managed_asset, managed_clean
from autodev.loopforge.bridge import LoopForgeBridge
from autodev.loopforge.client import LoopForgeClient, LoopForgeError
from autodev.models import MilestoneStatus, PlanTask, TaskStatus
from autodev.process.runner import decide_gate, ensure_gate_record, GateConflictError
from autodev.scheduler import Scheduler
from autodev.server.app import create_app
from test_audit_fixes import _seed_plan
from test_unified_batch1_backbone import deployment, headers


def project(tmp_path, mode="files"):
    ws = init_workspace(tmp_path / "demo", collaboration_mode=mode)
    eng = Engine(ws)
    eng.settings.runtime_backend = "loopforge"
    eng.settings.loopforge_project_id = "demo"
    eng.settings.loopforge_token = "fixture"
    plan = PlanTask(task_id="T-01", module="core", milestone_id="M1", role="coder", title="add function",
        description="implement total", allowed_paths=["core/", "tests/"], acceptance_criteria=["returns two"],
        verify_cmds=["true"], depends_on=[], doc_slice_ids=[], priority=1, requirement_ids=["REQ-001"],
        design_refs=["design/core.md"])
    eng.db.save_plan(_seed_plan([plan]), max_retries=1)
    return eng


def test_remote_input_and_native_candidate_impact(tmp_path, monkeypatch):
    eng = project(tmp_path)
    design = eng.ws.root / "docs/design/core.md"
    design.parent.mkdir(parents=True, exist_ok=True)
    design.write_text("approved design details")
    mod = eng.ws.root / "maths.py"
    mod.write_text("def total():\n    return 1\n")
    task = eng.db.get_task("T-01")
    seen = {}
    bridge = LoopForgeBridge(eng.settings, eng.db)
    eng.settings.loopforge_workflow_id = "wf"
    monkeypatch.setattr(public_pool, "resolve", AsyncMock(return_value=("local_runner", bridge.client)))
    monkeypatch.setattr(bridge, "pick_workflow_id", AsyncMock(return_value="wf"))
    async def create(**kwargs):
        seen.update(kwargs)
        return {"id":"issue"}
    monkeypatch.setattr(bridge.client, "create_issue", create)
    monkeypatch.setattr(bridge.client, "dispatch_issue", AsyncMock(return_value={"id":"task","runId":"run","issueId":"issue"}))
    monkeypatch.setattr(bridge.client, "get_run", AsyncMock(return_value={"id":"run","status":"succeeded","nodes":{}}))
    assert asyncio.run(bridge.execute_task(task,"core","slot-1",asyncio.Event()))[0]
    bundle = build_task_input_bundle(eng.ws,eng.db,task)
    assert bundle in seen["description"]
    for text in ("approved design details","REQ-001","@pytest.mark.req","Repo Map","returns two","core/"):
        assert text in bundle
    from autodev.context_bundle import attach_native_review_context
    wf = attach_native_review_context(build_vernal_workflows("demo")["files"])
    script = next(n["script"] for n in wf["nodes"] if n["id"] == "package")
    (tmp_path / ".vernal-baseline.json").write_text(json.dumps({"maths.py":mod.read_text(),"use.py":"from maths import total\nprint(total())\n"}))
    (tmp_path / "vernal-files.json").write_text(json.dumps({"files":{"maths.py":"def total():\n    return 2\n"}}))
    subprocess.run(["sh","-c",script],cwd=tmp_path,check=True)
    assert "use.py" in (tmp_path/"vernal-review-context.md").read_text()
    eng.db.close()


@pytest.mark.parametrize("case", ["accepted","forbidden","sensitive","broken"])
def test_plain_remote_git_candidate_is_independently_verified(tmp_path, case):
    eng = project(tmp_path, "git")
    root = eng.ws.root
    gitops.commit_all(root, "fixture baseline")
    base = gitops.head_sha(root)
    testfile = root / "tests/test_math.py"
    testfile.parent.mkdir()
    testfile.write_text("import pytest\n@pytest.mark.req('REQ-001')\ndef test_math():\n    assert " +
                        ("len([1]) == 2" if case == "broken" else "sum([1,1]) == 2") + "\n")
    if case in ("forbidden", "sensitive"):
        path = root / ("wrong.py" if case == "forbidden" else "core/.env")
        path.parent.mkdir(exist_ok=True)
        path.write_text("TAG=123\n")
        gitops.git(["add","-f","--",str(path)],root)
    gitops.commit_all(root,"fixture candidate")
    sha = gitops.head_sha(root)
    eng.db.update_task("T-01",base_sha=base, verify_cmds=[f"{sys.executable} -m pytest -q tests/test_math.py"],
                       loopforge_issue_id="issue",loopforge_run_id="run")
    task = eng.db.get_task("T-01")
    coro = Scheduler(eng).verify_and_record_candidate(task, 1, FileExchange(eng.ws, eng.db), {}, [], sha, None,tmp_path/"verify.log")
    if case == "accepted":
        asyncio.run(coro)
        row = eng.db._query("SELECT * FROM candidate_evidence")[0]
        assert row["candidate"] == sha and row["run_id"] == "run"
        assert json.loads(row["verified_requirements"]) == ["REQ-001"]
        assert len(eng.db._query("SELECT DISTINCT gate FROM quality_results")) == 4
    else:
        with pytest.raises(ValueError):
            asyncio.run(coro)
        assert not eng.db._query("SELECT * FROM candidate_evidence")
    eng.db.close()


def test_verify_only_missing_candidate_does_not_dispatch(tmp_path,monkeypatch):
    eng = project(tmp_path)
    eng.db.update_task("T-01",status=TaskStatus.FUSED)
    dispatch = AsyncMock()
    monkeypatch.setattr(LoopForgeClient,"dispatch_issue",dispatch)
    with TestClient(create_app(eng)) as client:
        response = client.post("/vernal-api/tasks/T-01/steer",json={"verify_only":True,"note":"复验"})
        assert response.status_code == 409
    dispatch.assert_not_called()
    eng.db.close()


def test_parallel_ci_early_negative_callback(tmp_path,monkeypatch):
    eng = project(tmp_path)
    ms = eng.db.get_milestone("M1")
    digest = FileExchange(eng.ws,eng.db).put({"README.md":"v1"})
    calls = []
    class Provider:
        name = "webhook"
        async def trigger(self,*args):
            calls.append(args)
            await asyncio.sleep(0)
            _, sig = ci.make_callback_url(eng,"M1",1)
            ci.handle_ci_callback(eng,"demo","M1",1,sig,{"passed":"false"})
            return SimpleNamespace(external_id="ci-1",url="")
    monkeypatch.setattr(ci,"build_ci_provider",lambda *a,**kw:Provider())
    async def run():
        return await asyncio.gather(*(ci.trigger_milestone_ci(eng,ms,digest) for _ in range(2)))
    a,b = asyncio.run(run())
    assert a["ci_run_id"] == b["ci_run_id"] and len(calls) == 1
    assert ci.list_ci_runs(eng.db)[0]["status"] == "FAILURE"
    assert ci._parse_strict_bool("0") is False
    with pytest.raises(ValueError):
        ci._parse_strict_bool("truthy")
    eng.db.close()


def test_two_connections_decide_once_and_outbox_replay(tmp_path):
    eng = project(tmp_path)
    gate = ensure_gate_record(eng.db,stage_id="testing",gate_id="test-gate",kind="human_approval",
                              scope_type="task",scope_id="T-01")
    barrier = threading.Barrier(2)
    def sign(decision):
        db = Database(eng.db.path)
        try:
            barrier.wait()
            return decide_gate(db,gate["record_id"],decision,actor=decision)["status"]
        except GateConflictError:
            return "CONFLICT"
        finally:
            db.close()
    with ThreadPoolExecutor(2) as workers:
        results = list(workers.map(sign,["pass","fail"]))
    assert results.count("CONFLICT") == 1
    pdb = tmp_path / "platform.db"
    flush_decisions(eng.db,pdb)
    eng.db._exec("UPDATE audit_outbox SET delivered=0")
    flush_decisions(eng.db,pdb)
    with sqlite3.connect(pdb) as conn:
        assert conn.execute("SELECT count(*) FROM audit_log").fetchone()[0] == 1
    assert verify_audit_chain(pdb)["valid"]
    eng.db.close()


def test_signed_v2_publication_has_no_write_lock_and_preserves_v1(tmp_path,monkeypatch):
    from autodev.loopforge.dispatch import reserve, record
    from autodev.review.service import list_reviews, decide_review
    from autodev.engine import OpsError
    eng = project(tmp_path)
    eng.settings.review_policy = "milestone"
    eng.settings.ci_provider = "webhook"
    eng.settings.require_merge_approval = True
    class Provider:
        name = "webhook"
        async def trigger(self, project_id, branch, sha, context):
            return SimpleNamespace(external_id=f"ci-{context['round']}",url="")
    monkeypatch.setattr(ci,"build_ci_provider",lambda *args,**kwargs:Provider())
    exchange = FileExchange(eng.ws,eng.db)
    publisher = eng._publish_candidate
    def slow(*args):
        with sqlite3.connect(eng.db.path, timeout=0) as other:
            other.execute("BEGIN IMMEDIATE")
            other.rollback()
        return publisher(*args)
    monkeypatch.setattr(eng,"_publish_candidate",slow)
    for version in (1,2):
        candidate = {"core/value.py":f"N={version}\n", "tests/test_value.py":
            f"import pytest\nfrom pathlib import Path\n@pytest.mark.req('REQ-001')\n"
            f"def test_value():\n    assert Path('core/value.py').read_text().strip() == 'N={version}'\n"}
        ref = exchange.put(candidate)
        eng.db.meta_set("delivery:M1",ref)
        eng.db.update_task("T-01", status=TaskStatus.VERIFYING,loopforge_issue_id="i",loopforge_run_id=f"r{version}",
                           verify_cmds=[f"{sys.executable} -m pytest -q tests/test_value.py"])
        row,_ = reserve(eng.db,"T-01",version,version,"public_docker","11","i",f"r{version}")
        record(eng.db,row,"FINISHED",lf_task_id=f"native-task-{version}")
        task = eng.db.get_task("T-01")
        key = exchange.patch(task, {}, candidate, [])
        asyncio.run(Scheduler(eng).verify_and_record_candidate(task,version,exchange,{},[],None,key,tmp_path/"verify.log"))
        eng.db.update_task("T-01", status=TaskStatus.APPROVED)
        handle = asyncio.run(ci.trigger_milestone_ci(eng,eng.db.get_milestone("M1"),ref))
        _,signature = ci.make_callback_url(eng,"M1",handle["round"])
        ci.handle_ci_callback(eng,"demo","M1",handle["round"],signature,{"passed":True})
        with pytest.raises(OpsError, match="人工代码审查"):
            eng.merge_milestone("M1","reviewer")
        decision = list_reviews(eng.db,scope_type="milestone",scope_id="M1")[0]
        decide_review(eng,decision["review_id"],"approve",actor="reviewer",expected_head=ref)
        eng.merge_milestone("M1","reviewer")
    with TestClient(create_app(eng)) as client:
        for version in (1,2):
            saved = client.get(f"/vernal-api/milestones/M1/deliveries/{version}/evidence").json()
            evidence = saved["evidence"]["tasks"][0]
            assert evidence["issue_id"] == "i" and evidence["run_id"] == f"r{version}"
            assert evidence["lf_task_id"] == f"native-task-{version}"
            archive = saved["evidence"]
            assert archive["ci"][0]["external_id"] == f"ci-{version}"
            assert archive["release_tag"] == f"vernal-M1-v{version}"
            assert any(r["head_ref"] == saved["artifact_digest"] and r["status"] == "APPROVED" for r in archive["reviews"])
            assert evidence["verified"] == ["REQ-001"] and evidence["diff_digest"]
            assert len(evidence["quality_ids"]) == 4 and evidence["design_refs"] == ["design/core.md"]
            assert exchange.get(saved["artifact_digest"])["core/value.py"] == f"N={version}\n"
    eng.db.close()


@pytest.mark.parametrize("action",["approve","reject"])
def test_only_assigned_native_user_may_sign_both_entries(deployment,monkeypatch,action):
    eng,ctx,browser,projects,calls = deployment
    run = {"id":"r","workflowId":"alpha-vernal-signoff","status":"waiting",
           "nodes":{"signoff":{"status":"waiting_approval"}},
           "executionContext":{"projectId":"alpha","responsibleUserId":2}}
    monkeypatch.setattr(LoopForgeClient,"get_run", AsyncMock(return_value=run))
    from autodev.server.proxy import LoopForgeProxy
    async def proxy(_self,request):
        assert request.headers["cookie"] == "session=bob"
        return JSONResponse({"native":True})
    monkeypatch.setattr(LoopForgeProxy,"forward",proxy)
    body = {"nodeId":"signoff","node_id":"signoff","reason":"fix","comment":"ok"}
    for route in (f"/api/runs/r/{action}",f"/vernal-api/runtime/runs/r/{action}"):
        assert browser.post(route, headers=headers("alice"),json=body).status_code == 403
        signed = browser.post(route, headers=headers("bob"),json=body)
        assert signed.status_code == 200, signed.text


def test_pool_tickets_do_not_follow_active_workspace(tmp_path,monkeypatch):
    monkeypatch.setenv("VERNAL_POOL_TOKEN","service")
    monkeypatch.setenv("DEEPSEEK_API_KEY","fixture")
    a = project(tmp_path / "a")
    b = project(tmp_path / "b")
    b.settings.loopforge_project_id = "second"
    b.settings.data_classification = "internal"
    app = create_app(a)
    app.state.ctx.register_slot(b)
    expiry = int(time.time())+60
    tickets = {p:public_pool.model_ticket(p,"deepseek",expiry) for p in ("demo","second")}
    with TestClient(app) as client:
        def call(pid, token):
            return client.get(f"/vernal-api/pool-model/{pid}/deepseek/v1/models",
                headers={"Authorization":"Bearer "+token,"X-Vernal-Project":"demo"})
        with ThreadPoolExecutor(4) as threads:
            requests = [(pid,token) for _ in range(3) for pid,token in tickets.items()]
            responses = list(threads.map(lambda args:call(*args).status_code,requests))
        assert responses == [200,403]*3
        assert call("second", tickets["demo"]).status_code == 401
    with pytest.raises(LoopForgeError):
        public_pool.check_egress(b.settings,"unregistered")
    client = LoopForgeClient("http://fixture")
    client.issue_user_id, client.approval_user_id, client.approval_workflow = 11,22,"review"
    steps = client.issue_bindings("dev")
    assert [s["assigneeUserId"] for s in steps] == [11,22]
    assert steps[1]["dependsOn"] == [steps[0]["id"]]
    a.db.close()
    b.db.close()


def test_managed_asset_retains_custom_mcp():
    template = {"id":"a","name":"planner","description":"A","mcpServers":["serena"],"skills":["spec-writing"]}
    old = managed_asset(template)
    new = managed_asset({**template,"description":"B"})
    assert managed_clean(old,new)
    assert not managed_clean({**old,"mcpServers":["custom"]},new)


def test_cli_backup_restore_all_persistent_components(tmp_path):
    import shutil
    root = tmp_path / "installed"
    deploy = root / "deploy/loopforge"
    deploy.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1]
    shutil.copy2(source / "vernal.sh", root / "vernal.sh")
    shutil.copy2(source / "deploy/loopforge/backup.sh", deploy / "backup.sh")
    volumes = tmp_path / "volumes"
    dirs = [root/"workspaces", deploy/".runner", deploy/".data",
            *(volumes / name for name in ("mysql-data","platform-data","gitea-data","gitea-runner-data"))]
    for directory in dirs:
        directory.mkdir(parents=True)
        (directory/"payload").write_text(directory.name)
    (deploy/".env").write_text("SECRET=fixture")
    docker = tmp_path / "docker"
    docker.write_text("""#!/usr/bin/env python3
import os,sys
from pathlib import Path
args=sys.argv[1:]
if args[0]=='run':
    assert '--network' in args and 'none' in args and args.count('--mount')==6
    import runpy
    assert args[-4:-2] == ['-m','autodev.deployment_backup'] and '0:0' in args
    ns=runpy.run_path(os.environ['FIXTURE_HELPER'])
    targets=ns['locations'](Path(os.environ['FIXTURE_ROOT']),Path(os.environ['FIXTURE_VOLUMES']))
    archive=Path(os.environ['FIXTURE_ARCHIVES']) / Path(args[-1]).name
    ns[{'--backup':'backup','--restore':'restore'}[args[-2]]](targets,archive)
elif 'ps' in args:
    print('mysql')
elif args[0] not in ('image','volume','compose'):
    sys.exit(3)
""")
    docker.chmod(0o755)
    archive_dir = tmp_path / "archives"
    env = {**os.environ,"PATH":str(tmp_path)+os.pathsep+os.environ["PATH"], "FIXTURE_ROOT":str(root),
           "FIXTURE_VOLUMES":str(volumes),"FIXTURE_ARCHIVES":str(archive_dir),
           "FIXTURE_HELPER":str(source/"autodev/deployment_backup.py")}
    subprocess.run([str(root/"vernal.sh"),"--backup",str(archive_dir)],check=True,env=env)
    archive, = archive_dir.glob("*.tar.gz")
    assert archive.stat().st_mode & 0o077 == 0
    for directory in dirs:
        (directory/"payload").write_text("changed")
    (deploy/".env").unlink()
    subprocess.run([str(root/"vernal.sh"),"--restore",str(archive)],check=True,env=env)
    assert all((directory/"payload").read_text() == directory.name for directory in dirs)
    assert (deploy/".env").read_text() == "SECRET=fixture"
    bad = archive_dir / "broken.tar.gz"
    bad.write_bytes(archive.read_bytes()[:120])
    assert subprocess.run([str(root/"vernal.sh"),"--restore",str(bad)],env=env).returncode
    assert all((directory/"payload").read_text() == directory.name for directory in dirs)

    # 未启用 --enable-ci（缺失可选 gitea-runner-data 卷）、显式指定 .tar.gz 目标文件且宿主含新源码时，备份与恢复均正常完成
    (root / "autodev").mkdir(parents=True, exist_ok=True)
    shutil.copy2(source / "autodev/deployment_backup.py", root / "autodev/deployment_backup.py")
    shutil.rmtree(volumes / "gitea-runner-data")
    docker_no_ci = tmp_path / "docker"
    docker_no_ci.write_text("""#!/usr/bin/env python3
import os,sys
from pathlib import Path
args=sys.argv[1:]
if args[:2]==['volume','inspect'] and args[2].endswith('_gitea-runner-data'):
    sys.exit(1)
if args[:2]==['volume','create'] and args[2].endswith('_gitea-runner-data'):
    (Path(os.environ['FIXTURE_VOLUMES'])/'gitea-runner-data').mkdir(parents=True, exist_ok=True)
    sys.exit(0)
if args[0]=='run':
    assert '--workdir' in args and '/project' in args
    import runpy
    assert args[-4:-2] == ['-m','autodev.deployment_backup'] and '0:0' in args
    ns=runpy.run_path(os.environ['FIXTURE_HELPER'])
    targets=ns['locations'](Path(os.environ['FIXTURE_ROOT']),Path(os.environ['FIXTURE_VOLUMES']))
    archive=Path(os.environ['FIXTURE_ARCHIVES']) / Path(args[-1]).name
    ns[{'--backup':'backup','--restore':'restore'}[args[-2]]](targets,archive)
elif 'ps' in args:
    print('mysql')
elif args[0] not in ('image','volume','compose'):
    sys.exit(3)
""")
    docker_no_ci.chmod(0o755)
    explicit_archive = archive_dir / "custom-pre-b3.tar.gz"
    subprocess.run([str(root/"vernal.sh"),"--backup",str(explicit_archive)],check=True,env=env)
    assert explicit_archive.is_file()
    (volumes / "mysql-data" / "payload").write_text("mutated")
    subprocess.run([str(root/"vernal.sh"),"--restore",str(explicit_archive)],check=True,env=env)
    assert (volumes / "mysql-data" / "payload").read_text() == "mysql-data"


def test_native_broker_issues_only_responsible_users_scoped_lease(tmp_path,monkeypatch):
    eng = project(tmp_path)
    monkeypatch.setenv("VERNAL_POOL_TOKEN","company")
    stamp = {"id":"run","status":"running","executionContext":{"projectId":"demo","responsibleUserId":11,"executionMode":"cloud"},
             "workflowSnapshot":{"sandboxProfileId":"demo-vernal-docker-123"}}
    monkeypatch.setattr(LoopForgeClient,"get_run",AsyncMock(return_value=stamp))
    with TestClient(create_app(eng)) as client:
        for who in (11,22):
            response = client.post(f"/vernal-api/pool-broker/{who}/deepseek/exchange",
                headers={"Authorization":"Bearer "+public_pool.broker_key(who,"deepseek")},json={"run_id":"run"})
            if who == 22:
                assert response.status_code == 403
            else:
                ticket = response.json()["ticket"]
                assert public_pool.valid_ticket("demo","deepseek",ticket)
                assert not public_pool.valid_ticket("other","deepseek",ticket)
    eng.db.close()


def test_detached_stage_stale_issue_update_never_dispatches(tmp_path,monkeypatch):
    eng = project(tmp_path)
    task = eng.db.get_task("T-01")
    eng.db.update_task(task.task_id,loopforge_issue_id="old")
    bridge = LoopForgeBridge(eng.settings,eng.db)
    monkeypatch.setattr(public_pool,"resolve",AsyncMock(return_value=("local_runner",bridge.client)))
    monkeypatch.setattr(bridge,"pick_workflow_id",AsyncMock(return_value="workflow"))
    monkeypatch.setattr(bridge.client,"reopen_issue",AsyncMock())
    monkeypatch.setattr(bridge.client,"update_issue",AsyncMock(side_effect=LoopForgeError("503",503)))
    dispatch = AsyncMock()
    monkeypatch.setattr(bridge.client,"dispatch_issue",dispatch)
    ok, error = asyncio.run(bridge.execute_task(eng.db.get_task(task.task_id),"dev","slot-1",asyncio.Event()))
    assert not ok and "503" in error
    dispatch.assert_not_called()
    eng.db.close()


def test_sandbox_exchange_without_run_env(tmp_path, monkeypatch):
    from autodev.loopforge.bridge import sign_context_sig
    monkeypatch.setenv("VERNAL_POOL_TOKEN", "company")
    eng = project(tmp_path)
    eng.settings.native_executor_user_id = 11
    eng.settings.execution_pool = "public_docker"
    native = AsyncMock(return_value=[{"id":"demo", "ownerId":22, "members":[{"userId":11,"role":"member"}]}])
    monkeypatch.setattr(LoopForgeClient, "list_projects", native)
    expires = str(int(time.time()) + 60)
    proof = sign_context_sig(eng.settings.project_signing_key(), "demo", "model-sandbox:" + expires)
    with TestClient(create_app(eng)) as client:
        def exchange(uid, pid, sig):
            return client.post(f"/vernal-api/pool-broker/{uid}/deepseek/exchange",
                headers={"Authorization":"Bearer " + public_pool.broker_key(uid,"deepseek")},
                json={"project_id":pid, "expires":expires, "proof":sig})
        answer = exchange(11, "demo", proof)
        assert answer.status_code == 200, answer.text
        assert public_pool.valid_ticket("demo", "deepseek", answer.json()["ticket"])
        assert exchange(22, "demo", proof).status_code == 403
        assert exchange(11, "demo", "bad").status_code == 403
        assert exchange(11, "wrong", proof).status_code == 403
        native.return_value[0]["members"] = []
        assert exchange(11, "demo", proof).status_code == 403
    eng.db.close()


def test_windows_node_reconnect_cancel_expiry_and_receipt(tmp_path):
    from fastapi import HTTPException
    from autodev.harnesses import link_node
    from autodev.loopforge.bridge import sign_context_sig
    from autodev.loopforge.dispatch import reserve
    eng = project(tmp_path)
    eng.db._exec("INSERT INTO host_links(id,owner,token_hash,kind,platform,clis,seen,expires_at) "
                 "VALUES('dev','alice','hash','git','win32','[]',?,?)", (time.time(), time.time()+200))
    eng.db._exec("INSERT INTO link_jobs(id,link_id,state,payload,expires) VALUES('job','dev','awaiting_node','{}',?)",
                 (time.time()+60,))
    sig = sign_context_sig(eng.settings.project_signing_key(), "demo", "link-node:job")
    assert link_node.node_tick(eng,"job",sig) == {"waiting":True}
    eng.db._exec("UPDATE link_jobs SET state='done',result='{}' WHERE id='job'")
    receipt = link_node.node_tick(eng,"job",sig)
    row,_ = reserve(eng.db, "node", 1, 1, "local_runner", "alice", "issue", "native")
    client = SimpleNamespace(project_run=AsyncMock(side_effect=[
        LoopForgeError("connection interrupted",503), {"status":"succeeded"}]),
        named_artifact=AsyncMock(return_value=json.dumps(receipt)))
    handle = client,"native",row
    asyncio.run(link_node.confirm(eng,"job","{}",handle,time.time()+3))
    client.project_run.side_effect = None
    client.project_run.return_value = {"status":"succeeded"}
    with pytest.raises(ValueError, match="不一致"):
        asyncio.run(link_node.confirm(eng,"job",'{"x":1}',handle,time.time()+2))
    with pytest.raises(ValueError, match="超时"):
        asyncio.run(link_node.confirm(eng,"job","{}",handle,time.time()-1))
    eng.planning_stop.set()
    with pytest.raises(ValueError, match="取消"):
        asyncio.run(link_node.confirm(eng,"job","{}",handle,time.time()+1))
    eng.db._exec("UPDATE link_jobs SET state='cancelled' WHERE id='job'")
    with pytest.raises(HTTPException) as cancelled:
        link_node.node_tick(eng,"job",sig)
    assert cancelled.value.status_code == 410
    eng.db.close()


def test_signoff_reuses_native_receipt_and_frozen_assignee(tmp_path, monkeypatch):
    from autodev.loopforge.dispatch import reserve
    eng = project(tmp_path)
    eng.db.update_task("T-01",loopforge_run_id="code",loopforge_issue_id="issue")
    task = eng.db.get_task("T-01")
    row,_ = reserve(eng.db,"T-01",1,1,"local_runner","11","issue","code")
    eng.db.meta_set(f"signoff-policy:{row['dispatch_id']}", "[11,22]")
    bridge = LoopForgeBridge(eng.settings,eng.db)
    client = bridge.client
    gate = "demo-vernal-signoff"
    run = {"id":"signature","workflowId":gate,"executionContext":{"projectId":"demo","responsibleUserId":22},
           "status":"succeeded","nodes":{"signoff":{"status":"succeeded"}},
           "workflowSnapshot":{"nodes":[{"id":"signoff","type":"approval"}]}}
    monkeypatch.setattr(client,"_req",AsyncMock(return_value={}))
    monkeypatch.setattr(client,"list_issue_runs",AsyncMock(return_value=[{"workflowId":"development"}]))
    monkeypatch.setattr(client,"dispatch_issue",AsyncMock(return_value={"id":"task","runId":"signature"}))
    monkeypatch.setattr(client,"get_run",AsyncMock(return_value=run))
    paused = asyncio.Event()
    paused.set()
    with pytest.raises(LoopForgeError):
        asyncio.run(bridge.await_candidate_signoff(task,"candidate",paused))
    asyncio.run(bridge.await_candidate_signoff(task,"candidate",asyncio.Event()))
    client.dispatch_issue.assert_awaited_once_with("issue",rerun=False,workflow_id=gate)
    assert eng.db._query("SELECT state FROM dispatch_records WHERE lf_run_id='signature'")[0]["state"] == "FINISHED"
    run["nodes"]["signoff"]["status"] = "skipped"
    with pytest.raises(LoopForgeError,match="通过证据"):
        asyncio.run(bridge.await_candidate_signoff(task,"candidate",asyncio.Event()))
    eng.db.close()


def test_scheduler_fetches_plain_git_from_origin_and_reverifies_without_code(tmp_path, monkeypatch):
    eng = project(tmp_path, "git")
    root = eng.ws.root
    gitops.commit_all(root,"baseline")
    base = gitops.head_sha(root)
    origin = tmp_path / "upstream.git"
    gitops.git(["clone","--bare",str(root),str(origin)],tmp_path)
    gitops.git(["remote","add","origin",str(origin)],root)
    agent = tmp_path/"agent"
    gitops.git(["clone",str(origin),str(agent)],tmp_path)
    (agent/"core").mkdir()
    (agent/"core/value.py").write_text("VALUE = 7\n")
    gitops.commit_all(agent,"test native result")
    sha = gitops.head_sha(agent)
    gitops.git(["push","origin","HEAD:refs/heads/candidate"],agent)
    eng.db.update_task("T-01",status=TaskStatus.READY,base_sha=base,loopforge_issue_id="issue",
                       loopforge_run_id="coder",verify_cmds=[f"{sys.executable} -m py_compile core/value.py"])
    observer = AsyncMock(return_value=(True,"completed"))
    monkeypatch.setattr(LoopForgeBridge,"execute_task",observer)
    monkeypatch.setattr(LoopForgeClient,"get_run",AsyncMock(return_value={"id":"coder","status":"succeeded"}))
    async def artifact(self,run,name):
        return sha if name == "mr-body.md" else "# PASS"
    monkeypatch.setattr(LoopForgeClient,"named_artifact",artifact)
    scheduler = Scheduler(eng)
    exchange = FileExchange(eng.ws, eng.db)
    def execute():
        asyncio.run(scheduler._execute_remote(eng.db.get_task("T-01"), "dev", "slot-1", exchange, {}, []))
    execute()
    assert eng.db.get_task("T-01").status == TaskStatus.APPROVED
    assert eng.db.get_task("T-01").candidate_commit_sha == sha
    assert len(eng.db._query("SELECT DISTINCT gate FROM quality_results")) == 4
    eng.steer("T-01","复验","operator",verify_only=True)
    execute()
    assert eng.db.get_task("T-01").status == TaskStatus.APPROVED
    assert observer.await_count == 1
    eng.db.close()


def test_clean_legacy_asset_adopted_custom_script_preserved():
    from copy import deepcopy
    from autodev.loopforge.bootstrap import _sync_workflow_template
    raw = build_vernal_workflows("demo")["files"]
    client = SimpleNamespace(put_workflow=AsyncMock(return_value={}), publish_workflow=AsyncMock())
    assert asyncio.run(_sync_workflow_template(client,raw,{raw["id"]:deepcopy(raw)}))
    upgraded = client.put_workflow.await_args.args[0]
    assert "vernal-managed-v" in upgraded["description"]
    wire = deepcopy(upgraded)
    wire.update(revision=7, publishedRevision=7)
    wire["nodes"][0].setdefault("position", {"x":0, "y":0})
    updated_template = {**raw, "name":"new clean release"}
    assert asyncio.run(_sync_workflow_template(client,updated_template,{raw["id"]:wire}))
    assert client.put_workflow.await_args.args[0]["revision"] == 7
    assert "VERNAL_IMPACT" in next(n["script"] for n in upgraded["nodes"] if n["id"]=="package")
    edited = deepcopy(upgraded)
    edited["nodes"][1]["script"] += "\necho custom"
    assert not asyncio.run(_sync_workflow_template(client,raw,{raw["id"]:edited}))
    assert client.put_workflow.await_count == 2


def test_temporary_mirror_gc_requires_terminal_task_ownership_and_expired_lease(tmp_path,monkeypatch):
    import hashlib
    import httpx
    from autodev import local_git_bridge as mirror
    eng = project(tmp_path)
    eng.db.meta_set("gitea_mirror","1")
    key = hashlib.sha256(b"demo:T-01").hexdigest()
    repo = "vernal-task-" + key[:32]
    eng.db.meta_set("gitea_task:"+repo,"T-01")
    passwd = tmp_path/"gitea-pass"
    passwd.write_text("fixture")
    monkeypatch.setattr(mirror,"password_path",lambda:passwd)
    deleted = []
    info = {"description":"manually-owned","private":True}
    def dispatch(request):
        assert request.url.path.endswith(repo)
        if request.method == "DELETE":
            deleted.append(repo)
            return httpx.Response(204)
        return httpx.Response(200,json=info)
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx,"AsyncClient",lambda **kw:real(transport=httpx.MockTransport(dispatch),**kw))
    eng.db.meta_set("gitea_expiry:"+repo,"1")
    asyncio.run(mirror.collect_task_mirrors(eng))
    eng.db.update_task("T-01",status=TaskStatus.MERGED)
    asyncio.run(mirror.collect_task_mirrors(eng))
    info["description"] = "Vernal task " + key
    eng.db.meta_set("gitea_expiry:"+repo,str(int(time.time()+90)))
    asyncio.run(mirror.collect_task_mirrors(eng))
    assert deleted == []
    eng.db.meta_set("gitea_expiry:"+repo,"1")
    asyncio.run(mirror.collect_task_mirrors(eng))
    asyncio.run(mirror.collect_task_mirrors(eng))
    assert deleted == [repo]
    eng.db.close()


def test_interrupted_restore_rolls_back_all_installed_parts(tmp_path,monkeypatch):
    from autodev import deployment_backup as backup
    targets = backup.locations(tmp_path/"project",tmp_path/"volumes")
    for name, path in targets.items():
        (path if name != "env" else path.parent).mkdir(parents=True,exist_ok=True)
        (path/"data" if name != "env" else path).write_text("old")
    archive = tmp_path/"backup.tgz"
    backup.backup(targets,archive)
    for name,path in targets.items():
        (path/"data" if name != "env" else path).write_text("current")
    copytree = backup.shutil.copytree
    def fail_at_mysql(src,dst,*args,**kwargs):
        if Path(dst) == targets["runner"]:
            raise OSError("simulated write failure")
        return copytree(src,dst,*args,**kwargs)
    monkeypatch.setattr(backup.shutil,"copytree",fail_at_mysql)
    with pytest.raises(OSError,match="simulated"):
        backup.restore(targets,archive)
    for name,path in targets.items():
        assert (path/"data" if name != "env" else path).read_text() == "current"
    assert not (targets["env"].parent/".restore-transaction.json").exists()


def test_model_usage_and_budget_reservation_before_remote_io(tmp_path,monkeypatch):
    import httpx
    from autodev.governance.audit import platform_db_path_for
    monkeypatch.setenv("VERNAL_POOL_TOKEN","fixture")
    monkeypatch.setenv("DEEPSEEK_API_KEY","fixture-key")
    monkeypatch.setenv("VERNAL_POOL_DIR",str(tmp_path/"pool"))
    eng = project(tmp_path)
    eng.settings.token_budget_limit = 500
    begun, release = threading.Event(), threading.Event()
    captured = []
    async def upstream(req):
        captured.append(json.loads(req.content))
        begun.set()
        assert await asyncio.to_thread(release.wait, 5)
        return httpx.Response(200,json={"usage":{"total_tokens":17}})
    monkeypatch.setattr(public_pool,"create_async_http_client",
        lambda base,**opts:httpx.AsyncClient(transport=httpx.MockTransport(upstream),**opts))
    app = create_app(eng)
    with TestClient(app) as client:
        token = public_pool.model_ticket("demo","deepseek",int(time.time())+90)
        def submit():
            return client.post("/vernal-api/pool-model/demo/deepseek/v1/chat/completions",
                    headers={"Authorization":"Bearer "+token},json={"model":"deepseek-chat","max_tokens":250,
                    "messages":[{"role":"user","content":"sk-ABCDEFGHIJKLMNOPQRST"}]})
        with ThreadPoolExecutor(1) as executor:
            first = executor.submit(submit)
            try:
                assert begun.wait(4)
                assert submit().status_code == 429
            finally:
                release.set()
            response = first.result()
            assert response.status_code == 200, response.text
        assert captured[0]["messages"][0]["content"] == "[REDACTED_SECRET]"
        row = eng.db._query("SELECT tokens,finalized FROM model_budget_reservations")[0]
        assert tuple(row) == (17,1)
        with sqlite3.connect(platform_db_path_for(app.state.ctx.workspaces_parent())) as db:
            assert db.execute("SELECT project,model,status FROM model_gateway_calls WHERE status='ALLOWED'").fetchone() == (
                    "demo","deepseek-chat","ALLOWED")
    eng.db.close()


def test_service_identity_is_idempotent_and_private(tmp_path,monkeypatch):
    import shutil
    import stat
    from autodev.config import Settings
    script = tmp_path/"vernal.sh"
    shutil.copy2(Path(__file__).resolve().parents[1]/"vernal.sh", script)
    token = tmp_path/"deploy/loopforge/.runner/deployment.token"
    first = subprocess.run([str(script),"--service-identity"], input="fixture-service\n",
                           text=True,capture_output=True,check=True)
    subprocess.run([str(script),"--service-identity"],input="",text=True,capture_output=True,check=True)
    assert token.read_text() == "fixture-service" and "fixture-service" not in first.stdout
    assert stat.S_IMODE(token.stat().st_mode) == 0o600
    monkeypatch.delenv("AGENTFLOW_TOKEN",raising=False)
    monkeypatch.setenv("VERNAL_SERVICE_TOKEN_FILE",str(token))
    assert Settings().service_token() == "fixture-service"


def test_internal_preflight_checks_actual_native_workflow_image(tmp_path,monkeypatch):
    from autodev.loopforge.bridge import sign_context_sig
    eng = project(tmp_path)
    s = eng.settings
    s.data_classification = "internal"
    monkeypatch.setenv("VERNAL_TRUSTED_RUNNERS","docker:company/image:1")
    expiry = str(int(time.time())+120)
    proof = sign_context_sig(s.project_signing_key(),"demo","model-sandbox:"+expiry)
    source = {"id":"image","projectId":"demo","content":"FROM company/image:1\n" +
              f"ENV VERNAL_NATIVE_PROJECT=demo VERNAL_NATIVE_EXPIRY={expiry} VERNAL_NATIVE_PROOF={proof}\n"}
    async def get(method,path,**opts):
        return {"id":"wf","projectId":"demo","sandboxProfileId":"box"} if "workflows" in path else [source]
    api = SimpleNamespace(_req=get,list_sandboxes=AsyncMock(return_value=[
        {"id":"box","projectId":"demo","dockerfileId":"image","driver":"docker"}]))
    asyncio.run(public_pool.check_workflow_sandbox(s,api,"wf","public_docker"))
    source["content"] += "RUN echo customized\n"
    with pytest.raises(LoopForgeError,match="配方"):
        asyncio.run(public_pool.check_workflow_sandbox(s,api,"wf","public_docker"))
    eng.db.close()


def test_trace_uses_markers_from_before_verification(tmp_path):
    from autodev.trace.candidate import candidate_test_markers, verified_pytest_requirements
    path = tmp_path/"test_math.py"
    path.write_text("@pytest.mark.req('REQ-1')\ndef test_sum():\n    assert sum([1,2]) == 3\n")
    frozen = candidate_test_markers(tmp_path)
    path.write_text(path.read_text().replace("REQ-1","REQ-2"))
    report = tmp_path/"report.xml"
    report.write_text('<testsuite><testcase classname="test_math" name="test_sum[small]"/></testsuite>')
    assert verified_pytest_requirements(frozen,report) == {"REQ-1"}
    report.write_text('<testsuite><testcase classname="test_math" name="test_sum"><skipped/></testcase></testsuite>')
    assert not verified_pytest_requirements(frozen,report)
