"""阶段评审立规（Steering & Checkpoints）、原生三专家并发 SOP、全局 Rule 与局部返工双向对账回归测试。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from autodev.config import Settings
from autodev.engine import Engine, init_workspace
from autodev.loopforge.bootstrap import (
    VALIDATE_STEERING_SHELL_SCRIPT,
    decisions_rule_id,
    ensure_vernal_loopforge_assets,
    review_workflow_id,
)
from autodev.loopforge.client import LoopForgeClient
from autodev.models import CouncilADR, CouncilConsensus, MilestoneStatus, ProjectStatus, TaskStatus
from autodev.server.app import create_app
from autodev.steering import sync_council_adrs_to_decisions


def _make_steering_mock_lf(box: dict):
    box.setdefault("agents", {})
    box.setdefault("rules", {})
    box.setdefault("workflows", {})
    box.setdefault("issues", {})
    box.setdefault("runs", {})
    box.setdefault("approvals", [])
    box.setdefault("rejections", [])

    def _handler(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        m = req.method
        if p == "/api/sandboxes" and m == "GET":
            return httpx.Response(200, json=[{"id": "local"}])
        if p == "/api/skills" and m == "GET":
            return httpx.Response(200, json=[])
        if p == "/api/mcp-servers" and m == "GET":
            return httpx.Response(200, json=[])
        if p == "/api/runners" and m == "GET":
            return httpx.Response(200, json=[])
        if p == "/api/projects":
            if m == "GET":
                return httpx.Response(200, json=[{"id": "proj-s1", "name": "SteeringApp"}])
            body = json.loads(req.content)
            return httpx.Response(200, json=body)
        if p == "/api/agents":
            if m == "GET":
                return httpx.Response(200, json=list(box["agents"].values()))
            body = json.loads(req.content)
            box["agents"][body["id"]] = body
            return httpx.Response(200, json=body)
        if p == "/api/rules":
            if m == "GET":
                return httpx.Response(200, json=list(box["rules"].values()))
            body = json.loads(req.content)
            box["rules"][body["id"]] = body
            return httpx.Response(200, json=body)
        if p == "/api/workflows":
            if m == "GET":
                return httpx.Response(200, json=list(box["workflows"].values()))
            body = json.loads(req.content)
            box["workflows"][body["id"]] = body
            return httpx.Response(200, json=body)
        if p.startswith("/api/workflows/") and p.endswith("/publish") and m == "POST":
            wf_id = p.split("/")[3]
            if wf_id in box["workflows"]:
                box["workflows"][wf_id]["status"] = "published"
            return httpx.Response(200, json={"status": "published"})
        if p == "/api/tasks" and m == "GET":
            return httpx.Response(200, json=[])
        if p.startswith("/api/issues/") and p.endswith("/runs") and m == "GET":
            return httpx.Response(200, json=[r for r in box["runs"].values() if r["issueId"] == p.split("/")[3]])
        if p == "/api/issues" and m == "POST":
            body = json.loads(req.content)
            iid = f"iss-{len(box['issues']) + 10}"
            rec = {"id": iid, "status": "todo", **body}
            box["issues"][iid] = rec
            return httpx.Response(200, json=rec)
        if p.startswith("/api/issues/") and p.endswith("/dispatch") and m == "POST":
            iid = p.split("/")[3]
            rid = f"run-{len(box['runs']) + 100}"
            wf = box["workflows"].get(box["issues"][iid].get("workflowId"), {})
            run_rec = {
                "id": rid,
                "issueId": iid,
                "executionContext": {"projectId": "proj-s1", "issueId": iid},
                "status": "running",
                "workflowSnapshot": wf,
                "nodes": {"pm_opinion": {"status": "running"}, "arch_opinion": {"status": "running"}, "qa_opinion": {"status": "running"}},
                "artifacts": [],
            }
            box["runs"][rid] = run_rec
            return httpx.Response(200, json={"latestRunId": rid, "run": run_rec})
        if p == "/api/issues" and m == "PUT":
            body = json.loads(req.content)
            iid = body["id"]
            box["issues"].setdefault(iid, {"id": iid}).update(body)
            return httpx.Response(200, json=box["issues"][iid])
        if p.startswith("/api/issues/") and p.endswith("/reopen") and m == "POST":
            iid = p.split("/")[3]
            box["issues"].setdefault(iid, {"id": iid})["status"] = "todo"
            return httpx.Response(200, json=box["issues"][iid])
        if p.startswith("/api/issues/") and m == "PATCH":
            iid = p.split("/")[3]
            body = json.loads(req.content)
            box["issues"].setdefault(iid, {"id": iid}).update(body)
            return httpx.Response(200, json=box["issues"][iid])
        if p.startswith("/api/runs/") and "/artifacts/" in p and m == "GET":
            parts = p.split("/")
            rid, aid = parts[3], parts[5]
            payload = (box.get("artifact_blobs") or {}).get((rid, aid), {})
            return httpx.Response(200, json=payload)
        if p.startswith("/api/runs/") and p.endswith("/approve") and m == "POST":
            rid = p.split("/")[3]
            body = json.loads(req.content)
            box["approvals"].append({"run_id": rid, **body})
            if rid in box["runs"]:
                box["runs"][rid]["status"] = "succeeded"
                box["runs"][rid].setdefault("nodes", {})["human_gate"] = {"status": "succeeded"}
            return httpx.Response(200, json={"ok": True})
        if p.startswith("/api/runs/") and p.endswith("/reject") and m == "POST":
            rid = p.split("/")[3]
            body = json.loads(req.content)
            box["rejections"].append({"run_id": rid, **body})
            if rid in box["runs"]:
                box["runs"][rid]["status"] = "running"
                box["runs"][rid].setdefault("nodes", {})["human_gate"] = {"status": "pending"}
                box["runs"][rid]["nodes"]["director_plan"] = {"status": "running", "reworkCount": 1}
            return httpx.Response(200, json={"ok": True})
        if p.startswith("/api/runs/") and m == "GET":
            rid = p.split("/")[3]
            return httpx.Response(200, json=box["runs"][rid])
        return httpx.Response(200, json={})

    return _handler


def test_bootstrap_provisions_parallel_review_sop_and_global_decisions_rule() -> None:
    box: dict = {}
    transport = httpx.MockTransport(_make_steering_mock_lf(box))
    real_async = httpx.AsyncClient
    settings = Settings(
        collaboration_mode="files",
        runtime_backend="loopforge",
        loopforge_url="http://127.0.0.1:3100",
        loopforge_token="tok_test",
        loopforge_project_id="proj-s1",
    )
    with patch("autodev.loopforge.client.httpx.AsyncClient", lambda **kw: real_async(transport=transport)):
        client = LoopForgeClient(settings.loopforge_url, settings.loopforge_token)
        res = asyncio.run(ensure_vernal_loopforge_assets(client, "proj-s1", settings))

    rev_wf_id = review_workflow_id("proj-s1")
    assert res["review_workflow_id"] == rev_wf_id
    wf = box["workflows"][rev_wf_id]
    assert wf["status"] == "published"
    nodes_by_id = {n["id"]: n for n in wf["nodes"]}
    assert set(nodes_by_id) == {
        "pm_opinion",
        "arch_opinion",
        "qa_opinion",
        "director_plan",
        "validate_plan",
        "human_gate",
    }
    # 3 个专家节点必须使用不同 agentId，避免 LoopForge per-agent 并发锁串行化
    specialist_agents = {
        nodes_by_id["pm_opinion"]["agentId"],
        nodes_by_id["arch_opinion"]["agentId"],
        nodes_by_id["qa_opinion"]["agentId"],
    }
    assert len(specialist_agents) == 3
    incoming = {nid: [] for nid in nodes_by_id}
    for e in wf["edges"]:
        incoming[e["target"]].append(e["source"])
    assert incoming["pm_opinion"] == []
    assert incoming["arch_opinion"] == []
    assert incoming["qa_opinion"] == []
    assert set(incoming["director_plan"]) == {"pm_opinion", "arch_opinion", "qa_opinion"}
    assert nodes_by_id["human_gate"]["type"] == "approval"
    assert nodes_by_id["human_gate"]["rejectTo"] == "director_plan"

    dec_id = decisions_rule_id("proj-s1")
    assert dec_id in box["rules"]
    assert box["rules"][dec_id]["global"] is True
    assert dec_id in box["agents"]["proj-s1-vernal-coder"]["rules"]
    assert dec_id in box["agents"]["proj-s1-vernal-reviewer"]["rules"]


def test_validate_steering_shell_script_dual_runtime_python_and_node(tmp_path: Path) -> None:
    valid_plan = {
        "summary": "切换无状态 JWT 并退回 T-002",
        "decisions_upsert": [
            {
                "decision_id": "D-02",
                "title": "JWT 鉴权",
                "rule_text": "所有接口统一采用 Bearer JWT",
                "rationale": "移动端兼容",
                "affected_paths": ["src/auth/"],
            }
        ],
        "decisions_revoke": [{"decision_id": "D-01", "reason": "废止 Cookie Session"}],
        "reopen_tasks": [{"task_id": "T-002", "reason": "替换为 JWT 签发"}],
        "cancel_tasks": ["T-003"],
        "new_tasks": [],
    }
    work = tmp_path / "work"
    work.mkdir()
    (work / "steering-plan.json").write_text(json.dumps(valid_plan, ensure_ascii=False), "utf-8")

    # 1) 默认环境（Python）
    r1 = subprocess.run(["/bin/sh", "-c", VALIDATE_STEERING_SHELL_SCRIPT], cwd=work, capture_output=True, text=True)
    assert r1.returncode == 0, r1.stderr

    # 2) 零 Python 宿主机环境（仅暴露 node）
    node_bin = shutil.which("node")
    assert node_bin is not None
    bin_dir = tmp_path / "node_only_bin"
    bin_dir.mkdir()
    os.symlink(node_bin, bin_dir / "node")
    env_no_py = {"PATH": f"{bin_dir}:/bin"}
    r2 = subprocess.run(
        ["/bin/sh", "-c", VALIDATE_STEERING_SHELL_SCRIPT], cwd=work, env=env_no_py, capture_output=True, text=True
    )
    assert r2.returncode == 0, r2.stderr

    # 3) 空 summary 或非法列表字段时两种运行时都必须拦截
    bad_plan = {"summary": "", "reopen_tasks": "invalid-list"}
    (work / "steering-plan.json").write_text(json.dumps(bad_plan), "utf-8")
    r3 = subprocess.run(["/bin/sh", "-c", VALIDATE_STEERING_SHELL_SCRIPT], cwd=work, capture_output=True, text=True)
    r4 = subprocess.run(
        ["/bin/sh", "-c", VALIDATE_STEERING_SHELL_SCRIPT], cwd=work, env=env_no_py, capture_output=True, text=True
    )
    assert r3.returncode != 0
    assert r4.returncode != 0


def test_council_adrs_auto_sync_to_decisions_and_global_rule(tmp_path: Path) -> None:
    box: dict = {}
    transport = httpx.MockTransport(_make_steering_mock_lf(box))
    real_async = httpx.AsyncClient
    ws = init_workspace(tmp_path / "ws_council", name="CouncilRuleApp", collaboration_mode="files")
    eng = Engine(
        ws,
        settings=Settings(
            collaboration_mode="files",
            runtime_backend="loopforge",
            loopforge_url="http://127.0.0.1:3100",
            loopforge_token="tok_test",
            loopforge_project_id="proj-s1",
        ),
    )
    consensus = CouncilConsensus(
        executive_summary="统一采用 SQLite WAL 与 Cookie Session",
        resolved_debates=["Session vs JWT"],
        open_risks=[],
        contract_strategy="OpenAPI 优先",
        adrs=[
            CouncilADR(
                adr_id="ADR-01",
                title="Cookie Session 鉴权",
                decision="所有 Web 接口使用 HttpOnly Session Cookie",
                rationale="首期简单可靠",
                affected_squads=["backend"],
            )
        ],
        spec_markdown="# Spec",
    )
    with patch("autodev.loopforge.client.httpx.AsyncClient", lambda **kw: real_async(transport=transport)):
        ver = asyncio.run(sync_council_adrs_to_decisions(eng, consensus))

    assert ver == 1
    decs = eng.db.list_decisions(active_only=True)
    assert len(decs) == 1
    assert decs[0]["decision_id"] == "ADR-01"
    rule_id = decisions_rule_id("proj-s1")
    assert rule_id in box["rules"]
    assert box["rules"][rule_id]["global"] is True
    assert "[ADR-01]" in box["rules"][rule_id]["content"]
    eng.db.close()


def _seed_steering_project(eng: Engine) -> None:
    eng.set_status(ProjectStatus.RUNNING)
    eng.db._exec(
        "INSERT INTO milestones(milestone_id, seq_order, title, goal, integration_cmd, status, planned)"
        " VALUES('M1', 1, '底座与鉴权', '完成底座', '', ?, 1), ('M2', 2, '业务扩展', '完成订单', '', ?, 1)",
        (MilestoneStatus.MERGED.value, MilestoneStatus.PENDING.value),
    )
    rows = [
        ("T-001", "M1", "backend", "基础数据库层", "[]", TaskStatus.MERGED.value, "iss-1"),
        ("T-002", "M1", "backend", "Session 登录鉴权", '["T-001"]', TaskStatus.MERGED.value, "iss-2"),
        ("T-003", "M2", "frontend", "旧版 Session 前端守卫", '["T-002"]', TaskStatus.READY.value, "iss-3"),
        ("T-004", "M2", "backend", "订单查询接口", '["T-003"]', TaskStatus.BLOCKED.value, "iss-4"),
    ]
    for tid, mid, sq, title, deps, st, iid in rows:
        eng.db._exec(
            "INSERT INTO tasks(task_id, milestone_id, squad, module, role, title, description,"
            " acceptance_criteria_json, depends_on_json, doc_slice_ids_json, allowed_paths_json,"
            " verify_cmds_json, priority, status, max_retries, loopforge_issue_id, updated_at)"
            " VALUES(?, ?, ?, ?, 'coder', ?, 'desc', '[\"ok\"]', ?, '[]', '[\"src/\"]', '[\"pytest\"]', 100, ?, 3, ?, '2026-11-24T00:00:00Z')",
            (tid, mid, sq, sq, title, deps, st, iid),
        )
    eng.db.upsert_decision(
        decision_id="D-01",
        title="Session Cookie 鉴权",
        rule_text="使用服务端 Session",
        rationale="初期方案",
        affected_paths=["src/auth/"],
        checkpoint_id=0,
    )


def test_steering_reject_loop_and_surgical_reopen_preserves_passed_tasks(tmp_path: Path) -> None:
    box: dict = {"issues": {"iss-1": {"id": "iss-1", "status": "done"}, "iss-2": {"id": "iss-2", "status": "done"}, "iss-3": {"id": "iss-3", "status": "todo"}}}
    transport = httpx.MockTransport(_make_steering_mock_lf(box))
    real_async = httpx.AsyncClient

    ws = init_workspace(tmp_path / "ws_pivot", name="PivotApp", collaboration_mode="files")
    eng = Engine(
        ws,
        settings=Settings(
            collaboration_mode="files",
            runtime_backend="loopforge",
            loopforge_url="http://127.0.0.1:3100",
            loopforge_token="tok_test",
            loopforge_project_id="proj-s1",
        ),
    )
    _seed_steering_project(eng)
    app = create_app(eng, proxy_transport=transport)

    with patch("autodev.loopforge.client.httpx.AsyncClient", lambda **kw: real_async(transport=transport)):
        with TestClient(app, client=("127.0.0.1", 51101)) as client:
            r_start = client.post(
                "/vernal-api/steering/start",
                json={"topic": "鉴权切换为 JWT", "human_note": "保留数据库底座 T-001，只重做鉴权 T-002", "kind": "PIVOT"},
            )
            assert r_start.status_code == 200, r_start.text
            cp = r_start.json()["checkpoint"]
            cid = cp["checkpoint_id"]
            run_id = cp["loopforge_run_id"]
            assert cp["status"] == "RUNNING"
            assert box["issues"][cp["loopforge_issue_id"]]["workflowId"] == review_workflow_id("proj-s1")

            # 模拟三专家并发产出意见 + 总监产出初版草案，Run 停在 human_gate 等待人工确认
            draft_v1 = {
                "summary": "初版方案：切换 JWT",
                "decisions_upsert": [],
                "decisions_revoke": [],
                "reopen_tasks": [{"task_id": "T-001", "reason": "误伤底座"}],
                "cancel_tasks": [],
                "new_tasks": [],
            }
            box["artifact_blobs"] = {
                (run_id, "art-pm"): {"headline": "支持切换 JWT 支持移动端", "keep": ["M1 DB"], "change": ["JWT"], "rework_tasks": ["T-002"], "risks": []},
                (run_id, "art-arch"): {"headline": "仅需退回 T-002", "keep": ["T-001"], "change": ["Bearer Token"], "rework_tasks": ["T-002"], "risks": []},
                (run_id, "art-plan-1"): draft_v1,
            }
            box["runs"][run_id]["status"] = "waiting_approval"
            box["runs"][run_id]["nodes"] = {"human_gate": {"status": "waiting_approval"}}
            box["runs"][run_id]["artifacts"] = [
                {"id": "art-pm", "name": "opinion-pm.json", "nodeId": "pm_opinion"},
                {"id": "art-arch", "name": "opinion-arch.json", "nodeId": "arch_opinion"},
                {"id": "art-plan-1", "name": "steering-plan.json", "nodeId": "director_plan"},
            ]

            r_poll = client.get("/vernal-api/steering")
            assert r_poll.status_code == 200
            cp_wait = r_poll.json()["checkpoints"][-1]
            assert cp_wait["status"] == "AWAITING_APPROVAL"
            assert cp_wait["opinions"]["pm"]["headline"] == "支持切换 JWT 支持移动端"
            # 绝不能自动放行 human_gate！
            assert box["approvals"] == []

            # 负责人不满意初版误伤 T-001，带意见打回 director_plan 重议
            r_rej = client.post(
                f"/vernal-api/steering/{cid}/reject",
                json={"actor": "alice", "reason": "绝不要动 T-001，只退回 T-002 并废止 D-01"},
            )
            assert r_rej.status_code == 200
            assert r_rej.json()["checkpoint"]["status"] == "RUNNING"
            assert len(box["rejections"]) == 1
            assert box["rejections"][0]["targetId"] == "director_plan"
            assert box["rejections"][0]["nodeId"] == "human_gate"

            # 总监重议后给出精准修订版变更单，再次停在 human_gate
            draft_v2 = {
                "summary": "保留 T-001 数据库底座，立规 D-02 无状态 JWT，仅退回 T-002，取消旧前端守卫 T-003，新增 JWT 守卫 T-005",
                "decisions_upsert": [
                    {
                        "decision_id": "D-02",
                        "title": "无状态 Bearer JWT",
                        "rule_text": "鉴权统一采用 Authorization: Bearer <JWT>",
                        "rationale": "跨端统一",
                        "affected_paths": ["src/auth/", "src/web/"],
                    }
                ],
                "decisions_revoke": [{"decision_id": "D-01", "reason": "已被 JWT 替代"}],
                "reopen_tasks": [{"task_id": "T-002", "reason": "按 D-02 改造为 JWT 签发与校验"}],
                "cancel_tasks": ["T-003"],
                "new_tasks": [
                    {
                        "milestone_id": "M2",
                        "title": "前端 JWT 路由守卫",
                        "description": "接入 Bearer Token 自动携带",
                        "role": "coder",
                        "squad": "frontend",
                        "depends_on": ["T-002"],
                        "allowed_paths": ["src/web/"],
                        "verify_cmds": ["pytest"],
                        "acceptance_criteria": ["支持 401 跳转与 Bearer 头"],
                    }
                ],
            }
            box["artifact_blobs"][(run_id, "art-plan-2")] = draft_v2
            box["runs"][run_id]["status"] = "waiting_approval"
            box["runs"][run_id]["nodes"] = {"human_gate": {"status": "waiting_approval"}, "director_plan": {"reworkCount": 1}}
            box["runs"][run_id]["artifacts"].append({"id": "art-plan-2", "name": "steering-plan.json", "nodeId": "director_plan"})

            client.get("/vernal-api/steering")
            r_app = client.post(f"/vernal-api/steering/{cid}/approve", json={"actor": "alice", "comment": "同意修订方案"})
            assert r_app.status_code == 200, r_app.text
            app_body = r_app.json()
            assert app_body["checkpoint"]["status"] == "APPLIED"
            assert len(box["approvals"]) == 1
            assert box["approvals"][0]["nodeId"] == "human_gate"

            # 1) 验证 LoopForge 全局 Rule 已热更新为 D-02，且剔除了已废止的 D-01
            rule_doc = box["rules"][decisions_rule_id("proj-s1")]
            assert rule_doc["global"] is True
            assert "[D-02]" in rule_doc["content"]
            assert "[D-01]" not in rule_doc["content"]

            # 2) 验证 T-001 原封不动保持 MERGED，仅 T-002 被局部退回 READY 且对应 Issue 重新打开为 todo
            t1 = eng.db.get_task("T-001")
            t2 = eng.db.get_task("T-002")
            t3 = eng.db.get_task("T-003")
            t4 = eng.db.get_task("T-004")
            t5 = eng.db.get_task("T-005")
            assert t1 is not None and t1.status == TaskStatus.MERGED
            assert t2 is not None and t2.status == TaskStatus.READY
            assert "D-02" in (t2.steer_note or "") or "JWT" in (t2.steer_note or "")
            assert box["issues"]["iss-2"]["status"] == "todo"
            assert eng.db.get_milestone("M1").status == MilestoneStatus.PENDING

            # 3) 验证 T-003 已移除并关闭 Issue，下游 T-004 自动清理对 T-003 的依赖并解锁为 READY
            assert t3 is not None and t3.status == TaskStatus.CANCELLED
            assert box["issues"]["iss-3"]["status"] == "closed"
            assert t4 is not None and t4.depends_on == [] and t4.status == TaskStatus.READY

            # 新增任务等待依赖解锁，实际派发时创建包含完整输入包的原生 Issue。
            assert t5 is not None and t5.status == TaskStatus.BLOCKED
            assert t5.loopforge_issue_id is None
            assert not any(i.get("title","").startswith("[T-005]") for i in box["issues"].values())

    eng.db.close()


def test_native_loopforge_gate_approval_syncs_back_to_vernal(tmp_path: Path) -> None:
    """当用户在 LoopForge 原生「待我确认」页面直接点击通过时，Vernal 对账后同样自动应用变更单。"""
    box: dict = {}
    transport = httpx.MockTransport(_make_steering_mock_lf(box))
    real_async = httpx.AsyncClient

    ws = init_workspace(tmp_path / "ws_native_gate", name="NativeGateApp", collaboration_mode="files")
    eng = Engine(
        ws,
        settings=Settings(
            collaboration_mode="files",
            runtime_backend="loopforge",
            loopforge_url="http://127.0.0.1:3100",
            loopforge_token="tok_test",
            loopforge_project_id="proj-s1",
        ),
    )
    _seed_steering_project(eng)
    app = create_app(eng, proxy_transport=transport)

    with patch("autodev.loopforge.client.httpx.AsyncClient", lambda **kw: real_async(transport=transport)):
        with TestClient(app, client=("127.0.0.1", 51102)) as client:
            cp = client.post("/vernal-api/steering/start", json={"topic": "补充错误码规范"}).json()["checkpoint"]
            run_id = cp["loopforge_run_id"]
            plan_json = {
                "summary": "新增统一错误码规矩 D-03",
                "decisions_upsert": [
                    {
                        "decision_id": "D-03",
                        "title": "统一错误体",
                        "rule_text": "错误响应统一返回 {code, message}",
                        "rationale": "前后端解耦",
                        "affected_paths": ["src/"],
                    }
                ],
                "decisions_revoke": [],
                "reopen_tasks": [],
                "cancel_tasks": [],
                "new_tasks": [],
            }
            # 模拟用户在原生 LoopForge UI 点了通过，Run 状态直接变为 succeeded
            box["runs"][run_id]["status"] = "succeeded"
            box["runs"][run_id]["artifacts"] = [
                {"id": "art-p", "name": "steering-plan.json", "content": json.dumps(plan_json, ensure_ascii=False)}
            ]

            res = client.get("/vernal-api/steering").json()
            latest = res["checkpoints"][-1]
            assert latest["status"] == "APPLIED"
            assert any(d["decision_id"] == "D-03" and d["status"] == "ACTIVE" for d in res["decisions"])
            assert "[D-03]" in box["rules"][decisions_rule_id("proj-s1")]["content"]

    eng.db.close()


def test_steering_reconcile_enforces_shared_completion_policy(tmp_path: Path) -> None:
    """Steering reconciliation must reject permission denials, missing artifacts, unknown nodes, and never auto-pass human_gate."""
    box: dict = {}
    transport = httpx.MockTransport(_make_steering_mock_lf(box))
    real_async = httpx.AsyncClient
    ws = init_workspace(tmp_path / "ws_steering_r1", name="SteeringPolicyApp", collaboration_mode="files")
    eng = Engine(
        ws,
        settings=Settings(
            collaboration_mode="files",
            runtime_backend="loopforge",
            loopforge_url="http://127.0.0.1:3100",
            loopforge_token="tok_test",
            loopforge_project_id="proj-s1",
        ),
    )
    _seed_steering_project(eng)
    app = create_app(eng, proxy_transport=transport)

    with patch("autodev.loopforge.client.httpx.AsyncClient", lambda **kw: real_async(transport=transport)):
        with TestClient(app, client=("127.0.0.1", 51103)) as client:
            cp = client.post("/vernal-api/steering/start", json={"topic": "权限策略检查"}).json()["checkpoint"]
            run_id = cp["loopforge_run_id"]

            # 1) Explicit permission denial on agent node must NEVER call approve_run
            box["runs"][run_id]["status"] = "waiting"
            box["runs"][run_id]["nodes"] = {
                "pm_opinion": {
                    "status": "waiting_approval",
                    "attempt": 1,
                    "reworkCount": 0,
                    "exitCode": 0,
                    "error": "MCP tool call requires approval, but approval policy is never",
                }
            }
            res1 = client.get("/vernal-api/steering").json()["checkpoints"][-1]
            assert box["approvals"] == []
            assert "审批策略" in (res1.get("last_error") or "")

            # 2) Non-agent / unknown node type or missing required artifact must NEVER call approve_run
            box["runs"][run_id]["nodes"] = {
                "director_plan": {
                    "status": "waiting_approval",
                    "attempt": 1,
                    "reworkCount": 0,
                    "exitCode": 0,
                    "error": "未收到 submit_gate_result",
                },
                "human_gate": {
                    "status": "waiting_approval",
                    "attempt": 1,
                    "reworkCount": 0,
                    "error": "submit_gate_result",
                },
            }
            box["runs"][run_id]["artifacts"] = []
            client.get("/vernal-api/steering")
            assert box["approvals"] == []

            # 3) Controlled recovery succeeds ONCE when current-turn required artifacts exist and stays idempotent
            box["runs"][run_id]["nodes"] = {
                "pm_opinion": {
                    "status": "waiting_approval",
                    "attempt": 1,
                    "reworkCount": 0,
                    "exitCode": 0,
                    "error": "未收到 submit_gate_result",
                }
            }
            box["runs"][run_id]["artifacts"] = [
                {"id": "art-pm-1", "nodeId": "pm_opinion", "attempt": 1, "name": "opinion-pm.json", "content": '{"headline":"ok"}'}
            ]
            # Keep run in waiting on approve to test duplicate reconcile idempotency
            orig_approve_override = []
            client.get("/vernal-api/steering")
            assert len(box["approvals"]) == 1
            assert box["approvals"][0]["nodeId"] == "pm_opinion"
            # Revert status back to waiting with same attempt/rework -> ledger must prevent duplicate approve_run
            box["runs"][run_id]["status"] = "waiting"
            client.get("/vernal-api/steering")
            assert len(box["approvals"]) == 1
    eng.db.close()
