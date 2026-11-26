"""进展与问题评审及转向变更单引擎：打通 LoopForge 原生并发评审 SOP、全局 Rule 与局部 Issue 返工。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from autodev.db import now_iso
from autodev.loopforge.bootstrap import classify_lane, ensure_vernal_loopforge_assets, lane_workflow_id, review_workflow_id, sync_decisions_rule
from autodev.loopforge.bridge import build_restore_command, sign_context_sig
from autodev.loopforge.client import LoopForgeClient, LoopForgeError
from autodev.loopforge.completion import CompletionCategory, CompletionLedger, evaluate_waiting_node
from autodev.models import ACTIVE_TASK_STATUSES, MilestoneStatus, ProjectStatus, TaskStatus

if TYPE_CHECKING:
    from autodev.engine import Engine
    from autodev.models import CouncilConsensus

STEERING_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "decisions_upsert", "decisions_revoke", "reopen_tasks", "cancel_tasks", "new_tasks"],
    "properties": {
        "summary": {"type": "string"},
        "decisions_upsert": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["decision_id", "title", "rule_text", "rationale", "affected_paths"],
                "properties": {
                    "decision_id": {"type": "string"},
                    "title": {"type": "string"},
                    "rule_text": {"type": "string"},
                    "rationale": {"type": "string"},
                    "affected_paths": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "decisions_revoke": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["decision_id", "reason"],
                "properties": {"decision_id": {"type": "string"}, "reason": {"type": "string"}},
            },
        },
        "reopen_tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["task_id", "reason"],
                "properties": {"task_id": {"type": "string"}, "reason": {"type": "string"}},
            },
        },
        "cancel_tasks": {"type": "array", "items": {"type": "string"}},
        "new_tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "milestone_id",
                    "title",
                    "description",
                    "role",
                    "squad",
                    "depends_on",
                    "allowed_paths",
                    "verify_cmds",
                    "acceptance_criteria",
                ],
                "properties": {
                    "milestone_id": {"type": "string"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "role": {"type": "string"},
                    "squad": {"type": "string"},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                    "allowed_paths": {"type": "array", "items": {"type": "string"}},
                    "verify_cmds": {"type": "array", "items": {"type": "string"}},
                    "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
}


def current_rule_version(engine: "Engine") -> int:
    return int(engine.db.meta_get("decisions_rule_version") or 0)


async def _push_decisions_to_loopforge(engine: "Engine", version: int) -> bool:
    s = engine.settings
    if s.runtime_backend != "loopforge" or not s.effective_loopforge_token() or not s.loopforge_project_id:
        return False
    client = LoopForgeClient(s.loopforge_url, s.effective_loopforge_token())
    try:
        from autodev.speckit.service import constitution_rule
        await sync_decisions_rule(client, s.loopforge_project_id, engine.db.list_decisions(active_only=True), version=version,
                                  constitution=constitution_rule(engine))
        return True
    except LoopForgeError:
        return False


async def sync_council_adrs_to_decisions(engine: "Engine", consensus: "CouncilConsensus") -> int:
    """将立项圆桌收敛出的 ADR 自动登记为项目生效规矩并同步至 LoopForge 全局 Rule。"""
    if not consensus.adrs:
        return current_rule_version(engine)
    cp = engine.db.create_checkpoint(
        kind="INITIAL",
        trigger_topic="立项架构圆桌共识",
        human_note=consensus.executive_summary[:500],
        status="APPLIED",
    )
    cid = int(cp["checkpoint_id"])
    upserted: list[str] = []
    for adr in consensus.adrs:
        engine.db.upsert_decision(
            decision_id=adr.adr_id,
            title=adr.title,
            rule_text=adr.decision,
            rationale=adr.rationale,
            affected_paths=[],
            status="ACTIVE",
            checkpoint_id=cid,
        )
        upserted.append(adr.adr_id)
    ver = current_rule_version(engine) + 1
    engine.db.meta_set("decisions_rule_version", str(ver))
    synced = await _push_decisions_to_loopforge(engine, ver)
    engine.db.update_checkpoint(
        cid,
        rule_version=ver,
        applied_summary={"summary": consensus.executive_summary, "decisions_upserted": upserted, "loopforge_rule_synced": synced},
    )
    return ver


def build_review_context_markdown(
    engine: "Engine",
    *,
    topic: str,
    human_note: str = "",
    kind: str = "REVIEW",
) -> str:
    decisions = engine.db.list_decisions(active_only=True)
    milestones = engine.db.list_milestones()
    tasks = engine.db.list_tasks()
    slices = engine.db.list_slices()

    dec_lines = [
        f"- **[{d['decision_id']}] {d['title']}** (v{d['version']})：{d['rule_text']}"
        for d in decisions
    ] or ["- （当前暂无已生效规矩）"]

    ms_lines: list[str] = []
    int_errors: list[str] = []
    for m in milestones:
        ms_lines.append(f"- **{m.milestone_id} · {m.title}** [`{m.status.value}`] — 目标：{m.goal}")
        if m.failure_reason:
            int_errors.append(f"### 里程碑 {m.milestone_id} 失败详情\n```\n{m.failure_reason[:3000]}\n```")
        int_log = engine.ws.runs_dir / "_integration" / f"{m.milestone_id}.log"
        if m.status == MilestoneStatus.FAILED and int_log.is_file():
            int_errors.append(f"### {m.milestone_id} 集成日志尾部\n```\n{int_log.read_text('utf-8', errors='ignore')[-3000:]}\n```")

    task_lines = [
        f"- **{t.task_id} [{t.status.value}]**（{t.milestone_id} / {t.role}，重试 {t.retry_count} 次，白名单 `{', '.join(t.allowed_paths)}`）：{t.title}"
        + (f"\n  - 最近阻塞/反馈：{t.failure_reason[:300]}" if t.failure_reason else "")
        for t in tasks
    ] or ["- （当前尚未拆解任务卡）"]

    slice_lines = [f"- `{s.slice_id}` {s.heading_path}：{s.summary}" for s in slices[:25]] or ["- （无资料切片）"]

    parts = [
        f"# 项目进展与问题评审简报（类型：{kind}）",
        "",
        f"## 本次讨论议题\n**{topic.strip()}**",
        f"\n## 负责人补充说明 / 期望方向\n{human_note.strip() or '（请结合当前进展与暴露问题给出最小侵入调整方案）'}",
        "\n## 当前已生效的项目硬规矩（LoopForge 全局 Rule）",
        *dec_lines,
        "\n## 里程碑进展",
        *(ms_lines or ["- （尚无里程碑）"]),
        "\n## 任务卡实时状态与问题暴露",
        *task_lines,
    ]
    if int_errors:
        parts.extend(["\n## 集成/联调失败现场", *int_errors])
    parts.extend(["\n## 项目资料与代码概览索引", *slice_lines])
    return "\n".join(parts).strip() + "\n"


async def start_steering_review(
    engine: "Engine",
    *,
    topic: str,
    human_note: str = "",
    kind: str = "REVIEW",
    actor: str = "owner",
) -> dict[str, Any]:
    from autodev.engine import OpsError

    clean_topic = topic.strip() or "阶段进展与问题复盘"
    if not engine.db.list_slices():
        try:
            engine.ingest()
        except OpsError:
            pass

    cp = engine.db.create_checkpoint(
        kind=kind,
        trigger_topic=clean_topic,
        human_note=human_note.strip(),
        status="DISPATCHING",
    )
    return await _dispatch_checkpoint(engine, cp, actor)


def _serialized(action):
    @wraps(action)
    async def wrapped(engine, checkpoint_id, *args, **kwargs):
        locks = engine.__dict__.setdefault("_checkpoint_locks", {})
        async with locks.setdefault(checkpoint_id, asyncio.Lock()):
            return await action(engine, checkpoint_id, *args, **kwargs)
    return wrapped


@_serialized
async def retry_steering_checkpoint(engine, checkpoint_id, actor="owner"):
    cp = engine.db.get_checkpoint(checkpoint_id)
    if cp is None:
        raise LoopForgeError("评审不存在", 404)
    updated = engine.db._exec(
        "UPDATE evolution_checkpoints SET status='DISPATCHING' WHERE checkpoint_id=? AND status='DISPATCH_FAILED'",
        (checkpoint_id,),
    )
    if not updated.rowcount:
        raise LoopForgeError("仅派单失败的评审可以重试", 409)
    return await _dispatch_checkpoint(engine, cp, actor)


async def _dispatch_checkpoint(engine, cp, actor):
    try:
        return await _dispatch(engine, cp, actor)
    except Exception as exc:
        engine.db.update_checkpoint(cp["checkpoint_id"], status="DISPATCH_FAILED", last_error=str(exc))
        if isinstance(exc, LoopForgeError):
            raise
        raise LoopForgeError(str(exc), 502) from exc


async def _dispatch(engine, cp, actor):
    clean_topic, human_note, kind = cp["trigger_topic"], cp["human_note"], cp["kind"]
    cid = int(cp["checkpoint_id"])
    round_no = int(cp["round_no"])
    context_md = build_review_context_markdown(engine, topic=clean_topic, human_note=human_note, kind=kind)

    s = engine.settings
    if s.runtime_backend == "loopforge":
        if not s.effective_loopforge_token() or not s.loopforge_project_id:
            raise LoopForgeError("评审派单需要已绑定的 LoopForge 项目和访问凭据", 503)
        client = LoopForgeClient(s.loopforge_url, s.effective_loopforge_token())
        await ensure_vernal_loopforge_assets(client, s.loopforge_project_id, s)
        wf_id = review_workflow_id(s.loopforge_project_id)

        desc = context_md
        if len(context_md.encode("utf-8")) > 36_000:
            stage_key = f"review-{cid}"
            bdir = engine.ws.state_dir / "baselines"
            bdir.mkdir(parents=True, exist_ok=True)
            bfile = bdir / f"plan-{stage_key}.json"
            bfile.write_text(json.dumps({"files": {"REVIEW_CONTEXT.md": context_md}}, ensure_ascii=False), "utf-8")
            base_url = (s.vernal_public_url or "http://127.0.0.1:8787").rstrip("/")
            sig = sign_context_sig(s.project_signing_key(), s.loopforge_project_id, f"plan:{stage_key}")
            q = urlencode({k: v for k, v in {"project": s.loopforge_project_id, "sig": sig}.items() if v})
            restore = build_restore_command(str(bfile), f"{base_url}/vernal-api/plan-context/{stage_key}.json?{q}", nested_key="files")
            desc = (
                f"# [评审 #{round_no}] {clean_topic}\n\n"
                f"上下文较长，请先在当前目录执行以下命令还原 `REVIEW_CONTEXT.md` 后阅读：\n```sh\n{restore}\n```\n"
            )

        from autodev.loopforge.dispatch import reserve, record, accept_task, observe_task
        receipt, fresh = reserve(engine.db, f"steering:{cid}", 1, 1, s.execution_pool, actor, cp.get("loopforge_issue_id"))
        issue_id = receipt["issue_id"]
        if fresh and not issue_id:
            issue = await client.create_issue(
                project_id=s.loopforge_project_id,
                title=f"[评审 #{round_no}] {clean_topic[:72]}",
                description=desc,
                workflow_id=wf_id,
                priority="high",
            )
            issue_id = str(issue.get("id") or "")
            if not issue_id:
                raise LoopForgeError("创建评审工单未返回 ID", 502)
            engine.db.update_checkpoint(cid, loopforge_issue_id=issue_id)
            record(engine.db, receipt, "DISPATCHING", issue_id=issue_id)
        if not fresh and receipt["state"] == "REJECTED" and issue_id:
            # Explicit retry only after both native histories confirm no accepted work.
            runs = await client.list_issue_runs(issue_id)
            tasks = await client.list_issue_tasks(issue_id)
            if not runs and not tasks:
                fresh = engine.db._exec("UPDATE dispatch_records SET state='DISPATCHING' WHERE dispatch_id=? AND state='REJECTED'",
                                        (receipt["dispatch_id"],)).rowcount == 1
        if fresh:
            try:
                native = await client.dispatch_issue(issue_id, rerun=False)
            except LoopForgeError as exc:
                record(engine.db, receipt, "REJECTED" if exc.status_code else "NEEDS_CONFIRMATION")
                raise
            run_id = await accept_task(engine.db, receipt, client, native, timeout=s.agent_timeout_sec)
        else:
            run_id = await observe_task(engine.db, receipt, client, timeout=s.agent_timeout_sec)
        engine.db.update_checkpoint(cid, status="RUNNING", loopforge_run_id=run_id, last_error=None)
        engine.db.add_event(
            "steering.started",
            f"已发起原生并发评审 #{round_no}「{clean_topic}」（Issue {issue_id} / Run {run_id}）",
            actor=actor,
            payload={"checkpoint_id": cid, "run_id": run_id},
        )
        return engine.db.get_checkpoint(cid) or cp

    # Local 离线模式回退：直接调用只读结构化规划产出变更单草案，停在 AWAITING_APPROVAL
    schema_dir = engine.ws.state_dir / "schemas"
    schema_dir.mkdir(parents=True, exist_ok=True)
    schema_path = schema_dir / "steering_plan.json"
    schema_path.write_text(json.dumps(STEERING_PLAN_SCHEMA, ensure_ascii=False, indent=2), "utf-8")
    log_dir = engine.ws.runs_dir / "_steering" / f"cp-{cid}"
    prompt = (
        f"{context_md}\n\n请作为研发总监综合以上进展与负责人诉求，输出符合给定 JSON Schema 的转向变更单 JSON。"
    )
    res = await engine.harness.run_structured_readonly(
        prompt,
        schema_path=schema_path,
        cwd=engine.ws.root,
        log_dir=log_dir,
        role="director",
    )
    if not res.ok or not isinstance(res.structured, dict):
        engine.db.update_checkpoint(cid, status="FAILED")
        engine.db.add_event("steering.failed", f"评审 #{round_no} 生成变更单失败：{res.error}", actor="Orchestrator")
        return engine.db.get_checkpoint(cid) or cp
    engine.db.update_checkpoint(cid, status="AWAITING_APPROVAL", draft_plan=res.structured, plan_revision=cp["plan_revision"] + 1, last_error=None)
    engine.db.add_event("steering.awaiting_approval", f"评审 #{round_no} 变更单已就绪，等待负责人拍板", actor="Orchestrator")
    return engine.db.get_checkpoint(cid) or cp


async def _read_run_json_artifact(
    client: LoopForgeClient, run: dict[str, Any], filename: str, *, node_id: str = ""
) -> dict[str, Any] | None:
    try:
        raw = await client.named_artifact(run, filename, node_id=node_id)
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        return parsed if isinstance(parsed, dict) else None
    except (LoopForgeError, json.JSONDecodeError, ValueError):
        return None


async def _current_draft(client, run, cp):
    nodes = run.get("nodes") or {}
    director = nodes.get("director_plan") or {}
    draft = None
    if int(director.get("reworkCount") or 0) >= cp["min_director_rework"]:
        # Older upstream versions omit nodeId on run-level artifacts.
        def current_outputs(records):
            return [item for item in records or [] if isinstance(item, dict)
                    and item.get("nodeId", "director_plan") == "director_plan"
                    and all(item.get(k) is None or director.get(k) is None
                            or str(item[k]) == str(director[k]) for k in ("attempt", "reworkCount"))]

        scoped = {**run, "artifacts": current_outputs(run.get("artifacts")),
                  "nodes": {"director_plan": {**director, "artifacts": current_outputs(director.get("artifacts"))}}}
        draft = await _read_run_json_artifact(client, scoped, "steering-plan.json")
    if (not draft or not isinstance(draft.get("summary"), str)
            or not all(isinstance(draft.get(k), list) for k in STEERING_PLAN_SCHEMA["required"] if k != "summary")
            or draft in cp["rejected_plans"]):
        draft = None
    return draft


@_serialized
async def reconcile_steering_checkpoint(engine: "Engine", checkpoint_id: int) -> dict[str, Any] | None:
    """与 LoopForge Run 双向对账：提取并发专家意见与变更单；原生 UI 点通过或驳回时自动同步落库。"""
    cp = engine.db.get_checkpoint(checkpoint_id)
    if cp is None or cp["status"] not in ("AWAITING_APPROVAL", "RUNNING", "SYNC_PENDING"):
        return cp
    s = engine.settings
    run_id = str(cp.get("loopforge_run_id") or "")
    if s.runtime_backend != "loopforge" or not run_id or not s.effective_loopforge_token():
        return cp

    client = LoopForgeClient(s.loopforge_url, s.effective_loopforge_token())
    try:
        run = await client.get_run(run_id)
    except LoopForgeError:
        return cp

    st = str(run.get("status") or "")
    nodes = run.get("nodes") or {}
    wf_nodes = {
        str(n.get("id")): n
        for n in ((run.get("workflowSnapshot") or {}).get("nodes") or [])
        if isinstance(n, dict) and n.get("id")
    }

    # 与主执行路径共用同一完成策略：明确权限拒绝、未知节点、缺失当轮产物和 human_gate 均绝不自动放行
    permission_error = ""
    if st in ("waiting", "waiting_approval"):
        ledger = CompletionLedger(db=engine.db, run_id=run_id)
        for nid, nr in list(nodes.items()):
            if not isinstance(nr, dict) or nr.get("status") not in ("waiting", "waiting_approval"):
                continue
            decision = await evaluate_waiting_node(client, run, nid, nr, wf_nodes.get(nid))
            if decision.category == CompletionCategory.CALLBACK_PERMISSION_DENIED:
                permission_error = decision.reason
                engine.db.add_event(
                    "steering.permission_denied",
                    decision.reason,
                    actor="VernalSteering",
                    payload={"checkpoint_id": checkpoint_id, "run_id": run_id, "node_id": nid},
                )
                continue
            if decision.action != "approve_recovery":
                continue
            dup_or_stale, _why = ledger.is_duplicate_or_stale(decision)
            if dup_or_stale:
                continue
            try:
                await client.approve_run(
                    run_id,
                    comment=(
                        f"Vernal 转向受控恢复完成确认（node={nid} attempt={decision.attempt} "
                        f"rework={decision.rework_count} digest={decision.artifact_digest} "
                        f"key={decision.idempotency_key}）"
                    ),
                    node_id=nid,
                )
                ledger.record(decision)
                run = await client.get_run(run_id)
                st = str(run.get("status") or "")
                nodes = run.get("nodes") or {}
            except LoopForgeError:
                pass

    opinions = dict(cp.get("opinions") or {})
    for slug in ("pm", "arch", "qa"):
        if slug not in opinions:
            op = await _read_run_json_artifact(client, run, f"opinion-{slug}.json")
            if not op:
                op = await _read_run_json_artifact(client, run, f"{slug}.json", node_id=f"{slug}_opinion")
            if op:
                opinions[slug] = op

    draft = await _current_draft(client, run, cp)
    gate = (nodes.get("human_gate") or {}).get("status")
    with engine.db.transaction():
        current = engine.db.get_checkpoint(checkpoint_id)
        if (current["status"], current["plan_revision"], current["min_director_rework"]) != (
            cp["status"], cp["plan_revision"], cp["min_director_rework"]
        ):
            return current
        fields = {"opinions": opinions}
        if permission_error:
            fields["last_error"] = permission_error
        if draft:
            digest = hashlib.sha256(json.dumps(draft, sort_keys=True).encode()).hexdigest()
            if digest != cp["draft_artifact_id"]:
                fields.update(plan_revision=cp["plan_revision"] + 1, draft_plan=draft, draft_artifact_id=digest)
        if st in ("failed", "cancelled", "canceled"):
            fields["status"] = "FAILED"
            engine.db.add_event("steering.failed", f"评审 #{cp['round_no']} 运行失败（Run {run_id}）", actor="VernalSteering")
        elif st == "succeeded":
            fields["status"] = "SYNC_PENDING"
        elif st in ("waiting", "waiting_approval") and draft and gate in ("waiting", "waiting_approval"):
            fields["status"] = "AWAITING_APPROVAL"
        elif st == "running" or not draft:
            fields["status"] = "RUNNING"
        engine.db.update_checkpoint(checkpoint_id, **fields)
    if st == "succeeded" and draft:
        return await apply_steering_plan(engine, checkpoint_id, draft, actor="loopforge-gate")

    return engine.db.get_checkpoint(checkpoint_id)


async def apply_steering_plan(engine, checkpoint_id, plan, *, actor="owner"):
    if engine.db.meta_get("speckit.enabled") == "1":
        raise LoopForgeError("P1 规格项目请通过功能规格修订和工作包编译执行变更；运行中的转向变更在后续批次提供",409)
    db = engine.db
    try:
        with db.transaction():
            cp = db.get_checkpoint(checkpoint_id)
            if not cp:
                raise ValueError("评审不存在")
            if not plan or plan != cp["draft_plan"]:
                raise LoopForgeError("草案已更新，请确认最新版", 409)
            claim = db._exec("UPDATE evolution_checkpoints SET status='APPLYING' WHERE checkpoint_id=? "
                             "AND status IN ('AWAITING_APPROVAL','RUNNING','SYNC_PENDING')", (checkpoint_id,))
            if not claim.rowcount:
                return cp
            summary = _apply_local(engine, cp, plan, actor)
    except LoopForgeError:
        raise
    except Exception as exc:
        db._exec("UPDATE evolution_checkpoints SET status='SYNC_PENDING',last_error=? WHERE checkpoint_id=? "
                 "AND status IN ('AWAITING_APPROVAL','RUNNING','SYNC_PENDING')", (str(exc), checkpoint_id))
        raise
    s = engine.settings
    if s.runtime_backend == "loopforge" and s.effective_loopforge_token() and s.loopforge_project_id:
        client = LoopForgeClient(s.loopforge_url, s.effective_loopforge_token())
        for old_run_id in summary.get("interrupted_runs") or []:
            try:
                await client.pause_run(old_run_id)
            except LoopForgeError:
                pass
        summary["loopforge_rule_synced"] = await _push_decisions_to_loopforge(engine, summary["rule_version"])
        for action in ("reopened_tasks", "cancelled_tasks"):
            for tid in summary[action]:
                task = db.get_task(tid)
                if task is None:
                    continue
                try:
                    if task.loopforge_issue_id and action == "reopened_tasks":
                        await client.reopen_issue(task.loopforge_issue_id)
                    elif task.loopforge_issue_id:
                        await client.update_issue(issue_id=task.loopforge_issue_id, project_id=s.loopforge_project_id,
                                                  title=task.title, description=task.description, status="closed")
                except LoopForgeError as exc:
                    db.update_checkpoint(checkpoint_id, last_error=str(exc))
                    db.add_event("steering.issue_sync_failed", str(exc), task_id=tid)
        db.update_checkpoint(checkpoint_id, applied_summary=summary)
    return db.get_checkpoint(checkpoint_id)


def _apply_local(engine, cp, data, actor):
    checkpoint_id, round_no = cp["checkpoint_id"], cp["round_no"]
    s = engine.settings
    # 1. 规矩升版与废止，并同步至 LoopForge 全局 Rule
    upserted_ids: list[str] = []
    revoked_ids: list[str] = []
    for item in data.get("decisions_upsert") or []:
        if not isinstance(item, dict) or not str(item.get("rule_text") or "").strip():
            continue
        did = str(item.get("decision_id") or f"D-{len(engine.db.list_decisions()) + 1:02d}").strip()
        engine.db.upsert_decision(
            decision_id=did,
            title=str(item.get("title") or did).strip(),
            rule_text=str(item.get("rule_text") or "").strip(),
            rationale=str(item.get("rationale") or "").strip(),
            affected_paths=[str(p) for p in (item.get("affected_paths") or []) if str(p).strip()],
            status="ACTIVE",
            checkpoint_id=checkpoint_id,
        )
        upserted_ids.append(did)

    for item in data.get("decisions_revoke") or []:
        did = str(item.get("decision_id") if isinstance(item, dict) else item or "").strip()
        reason = str(item.get("reason") or "") if isinstance(item, dict) else ""
        if did and engine.db.revoke_decision(did, reason=reason, checkpoint_id=checkpoint_id):
            revoked_ids.append(did)

    ver = current_rule_version(engine)
    lf_rule_synced = False
    if upserted_ids or revoked_ids or ver == 0:
        ver += 1
        engine.db.meta_set("decisions_rule_version", str(ver))

    interrupted_runs: list[str] = []

    # 2. 先关闭取消尚未合入的任务，避免退回或依赖计算引用已取消任务
    cancelled_ids: list[str] = []
    for raw_tid in data.get("cancel_tasks") or []:
        tid = str(raw_tid or "").strip()
        task = engine.db.get_task(tid)
        if task is None or task.status == TaskStatus.MERGED:
            continue
        if task.status in ACTIVE_TASK_STATUSES and task.loopforge_run_id:
            interrupted_runs.append(task.loopforge_run_id)
        engine.db.update_task(tid, status=TaskStatus.CANCELLED, assigned_slot=None, active_agent_label=None,
                              loopforge_run_id=None, steer_note=f"[评审 #{round_no} 已取消]")
        milestone = engine.db.get_milestone(task.milestone_id)
        if milestone and milestone.status in (MilestoneStatus.AWAITING_MERGE, MilestoneStatus.FAILED):
            engine.db.update_milestone(task.milestone_id, status=MilestoneStatus.PENDING, failure_reason=None)
        cancelled_ids.append(tid)
    cancel_set = set(cancelled_ids)

    # 3. 局部退回重做（保持 DAG 拓扑依赖阻塞：若上游也被退回或未完成，下游退回单置为 BLOCKED）
    reopen_entries: list[tuple[str, str]] = []
    for item in data.get("reopen_tasks") or []:
        if not isinstance(item, dict):
            continue
        tid = str(item.get("task_id") or "").strip()
        reason = str(item.get("reason") or "评审决定局部退回返工").strip()
        task = engine.db.get_task(tid)
        if task is None or task.status == TaskStatus.CANCELLED or tid in cancel_set:
            continue
        reopen_entries.append((tid, reason))

    reopen_target_set = {tid for tid, _ in reopen_entries}
    done_after_reopen = {
        t.task_id
        for t in engine.db.list_tasks()
        if t.status in (TaskStatus.APPROVED, TaskStatus.MERGED) and t.task_id not in reopen_target_set
    }

    reopened_ids: list[str] = []
    for tid, reason in reopen_entries:
        task = engine.db.get_task(tid)
        if task is None:
            continue
        if task.status in ACTIVE_TASK_STATUSES and task.loopforge_run_id:
            interrupted_runs.append(task.loopforge_run_id)
        clean_deps = [d for d in task.depends_on if d not in cancel_set]
        next_status = TaskStatus.READY if all(d in done_after_reopen for d in clean_deps) else TaskStatus.BLOCKED
        engine.db.update_task(
            tid,
            status=next_status,
            depends_on=clean_deps,
            retry_count=0,
            arbitration_count=0,
            assigned_slot=None,
            active_agent_label=None,
            steer_note=f"[评审 #{round_no} 局部返工] {reason}",
            failure_reason=None,
            loopforge_run_id=None,
        )
        ms = engine.db.get_milestone(task.milestone_id)
        if ms and ms.status in (MilestoneStatus.FAILED, MilestoneStatus.AWAITING_MERGE, MilestoneStatus.MERGED):
            engine.db.update_milestone(ms.milestone_id, status=MilestoneStatus.PENDING, failure_reason=None)
        reopened_ids.append(tid)

    if cancel_set or reopen_target_set:
        for t in engine.db.list_tasks():
            if t.task_id in cancel_set or t.task_id in reopen_target_set:
                continue
            new_deps = [d for d in t.depends_on if d not in cancel_set]
            deps_changed = new_deps != t.depends_on
            # 若未合入任务的上游被退回重做，重新回退至 BLOCKED 等待上游新版契约/产物完成
            if t.status == TaskStatus.READY and not all(d in done_after_reopen for d in new_deps):
                engine.db.update_task(t.task_id, depends_on=new_deps, status=TaskStatus.BLOCKED)
            elif deps_changed:
                new_st = TaskStatus.READY if (t.status == TaskStatus.BLOCKED and all(d in done_after_reopen for d in new_deps)) else t.status
                engine.db.update_task(t.task_id, depends_on=new_deps, status=new_st)

    # 4. 新增任务并同步创建对应 LoopForge Issue
    added_ids: list[str] = []
    existing_ms = {m.milestone_id: m for m in engine.db.list_milestones()}
    cancelled = {t.task_id for t in engine.db.list_tasks(status=TaskStatus.CANCELLED)}
    for idx, nt in enumerate(data.get("new_tasks") or []):
        if not isinstance(nt, dict) or not str(nt.get("title") or "").strip():
            continue
        tid = engine.db.next_task_id()
        mid = str(nt.get("milestone_id") or (next(iter(existing_ms), "M1"))).strip() or "M1"
        if mid not in existing_ms:
            ms_order = len(existing_ms) + 1
            engine.db._exec(
                "INSERT INTO milestones(milestone_id, seq_order, title, goal, integration_cmd, status, planned) VALUES(?,?,?,?,?,?,1)",
                (mid, ms_order, mid, str(nt.get("title") or mid), "", MilestoneStatus.PENDING.value),
            )
            existing_ms = {m.milestone_id: m for m in engine.db.list_milestones()}
        elif existing_ms[mid].status in (MilestoneStatus.MERGED, MilestoneStatus.AWAITING_MERGE, MilestoneStatus.FAILED):
            engine.db.update_milestone(mid, status=MilestoneStatus.PENDING, failure_reason=None)

        deps = [str(d) for d in (nt.get("depends_on") or []) if str(d).strip() and str(d) not in cancelled]
        done_ids = {t.task_id for t in engine.db.list_tasks() if t.status in (TaskStatus.APPROVED, TaskStatus.MERGED)}
        st_val = TaskStatus.READY if all(d in done_ids for d in deps) else TaskStatus.BLOCKED
        role = str(nt.get("role") or "coder").strip()
        squad = str(nt.get("squad") or "core").strip()
        title = str(nt.get("title") or tid).strip()
        desc = str(nt.get("description") or title).strip()
        allowed = [str(p) for p in (nt.get("allowed_paths") or ["src/"]) if str(p).strip()]
        vcmds = [str(c) for c in (nt.get("verify_cmds") or []) if str(c).strip()]
        crit = [str(c) for c in (nt.get("acceptance_criteria") or [title]) if str(c).strip()]

        engine.db._exec(
            "INSERT INTO tasks(task_id, milestone_id, squad, module, role, title, description,"
            " acceptance_criteria_json, depends_on_json, doc_slice_ids_json, allowed_paths_json,"
            " verify_cmds_json, priority, status, max_retries, source_checkpoint_id, source_item_idx, updated_at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, 100, ?, ?, ?, ?, ?)",
            (
                tid,
                mid,
                squad,
                squad,
                role,
                title,
                desc,
                json.dumps(crit, ensure_ascii=False),
                json.dumps(deps),
                json.dumps(allowed, ensure_ascii=False),
                json.dumps(vcmds, ensure_ascii=False),
                st_val.value,
                s.max_retries,
                checkpoint_id, idx,
                now_iso(),
            ),
        )
        added_ids.append(tid)

    applied_summary = {
        "summary": str(data.get("summary") or ""),
        "rule_version": ver,
        "loopforge_rule_synced": lf_rule_synced,
        "decisions_upserted": upserted_ids,
        "decisions_revoked": revoked_ids,
        "reopened_tasks": reopened_ids,
        "cancelled_tasks": cancelled_ids,
        "added_tasks": added_ids,
        "interrupted_runs": interrupted_runs,
    }
    engine.db.update_checkpoint(checkpoint_id, status="APPLIED", rule_version=ver, draft_plan=data,
                                applied_summary=applied_summary, last_error=None)
    if (reopened_ids or added_ids or cancelled_ids) and engine.status in (
        ProjectStatus.CREATED,
        ProjectStatus.INGESTED,
        ProjectStatus.SPECIFIED,
        ProjectStatus.PLANNED,
        ProjectStatus.STALLED,
        ProjectStatus.COMPLETED,
    ):
        engine.set_status(ProjectStatus.APPROVED)

    engine.db.add_event(
        "steering.applied",
        f"评审 #{round_no} 已生效：规矩 v{ver}（+{len(upserted_ids)}/-{len(revoked_ids)}），局部退回 {len(reopened_ids)} 单，新增 {len(added_ids)} 单，关闭 {len(cancelled_ids)} 单",
        actor=actor,
        payload={"checkpoint_id": checkpoint_id, **applied_summary},
    )
    return applied_summary


@_serialized
async def approve_steering_checkpoint(
    engine: "Engine",
    checkpoint_id: int,
    *,
    actor: str = "owner",
    comment: str = "",
    override_plan: dict[str, Any] | None = None,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    cp = engine.db.get_checkpoint(checkpoint_id)
    if cp is None:
        raise ValueError(f"演进节点不存在: {checkpoint_id}")
    if expected_revision is not None and expected_revision != cp["plan_revision"]:
        raise LoopForgeError("草案已更新，请确认最新版", 409)
    if cp["status"] == "APPLIED":
        return cp
    if cp["status"] != "AWAITING_APPROVAL" or not cp["draft_plan"] or override_plan and override_plan != cp["draft_plan"]:
        raise LoopForgeError("当前没有可批准的匹配草案", 409)
    s = engine.settings
    run_id = str(cp.get("loopforge_run_id") or "")
    if run_id:
        if not s.effective_loopforge_token():
            raise LoopForgeError("无法确认远端审批，缺少 LoopForge 凭据", 503)
        client = LoopForgeClient(s.loopforge_url, s.effective_loopforge_token())
        remote = await client.project_run(run_id, s.loopforge_project_id)
        if await _current_draft(client, remote, cp) != cp["draft_plan"]:
            raise LoopForgeError("远端草案已更改或尚未就绪，请刷新后确认", 409)
        try:
            await client.approve_run(run_id, comment=comment or f"由 {actor} 拍板通过转向变更单", node_id="human_gate")
        except LoopForgeError as exc:
            try:
                remote = await client.project_run(run_id, s.loopforge_project_id)
            except LoopForgeError:
                remote = {}
            if remote.get("status") != "succeeded":
                raise LoopForgeError(str(exc), 503 if exc.status_code == 503 else 502) from exc
        else:
            try:
                remote = await client.project_run(run_id, s.loopforge_project_id)
            except LoopForgeError:
                remote = {}
        confirmed = await _current_draft(client, remote, cp) if remote else None
        if confirmed != cp["draft_plan"]:
            engine.db._exec("UPDATE evolution_checkpoints SET status='SYNC_PENDING' WHERE checkpoint_id=? "
                            "AND status='AWAITING_APPROVAL' AND plan_revision=?", (checkpoint_id, cp["plan_revision"]))
            return engine.db.get_checkpoint(checkpoint_id)
    return await apply_steering_plan(engine, checkpoint_id, cp["draft_plan"], actor=actor)


@_serialized
async def reject_steering_checkpoint(
    engine: "Engine",
    checkpoint_id: int,
    *,
    actor: str = "owner",
    reason: str,
) -> dict[str, Any]:
    cp = engine.db.get_checkpoint(checkpoint_id)
    if cp is None:
        raise ValueError(f"演进节点不存在: {checkpoint_id}")
    if cp["status"] != "AWAITING_APPROVAL":
        raise LoopForgeError("当前评审未进入审批门禁", 409)
    watermark = cp["min_director_rework"]
    clean_reason = reason.strip() or "请调整变更方案"
    new_note = f"{cp.get('human_note') or ''}\n[驳回重议意见] {clean_reason}".strip()
    s = engine.settings
    run_id = str(cp.get("loopforge_run_id") or "")
    if run_id:
        if not s.effective_loopforge_token():
            raise LoopForgeError("无法向远端提交驳回意见，缺少 LoopForge 凭据", 503)
        client = LoopForgeClient(s.loopforge_url, s.effective_loopforge_token())
        run = await client.project_run(run_id, s.loopforge_project_id)
        watermark = max(watermark, int(((run.get("nodes") or {}).get("director_plan") or {}).get("reworkCount") or 0)) + 1
        await client.reject_run(run_id, reason=clean_reason, target_node="director_plan", node_id="human_gate")
    with engine.db.transaction():
        current = engine.db.get_checkpoint(checkpoint_id)
        if current["status"] not in ("RUNNING", "AWAITING_APPROVAL") or (
            current["plan_revision"], current["min_director_rework"]
        ) != (cp["plan_revision"], cp["min_director_rework"]):
            raise LoopForgeError("评审状态已变更，请刷新", 409)
        rejected = cp["rejected_plans"]
        if cp["draft_plan"]:
            rejected = [*rejected, cp["draft_plan"]]
        engine.db.update_checkpoint(checkpoint_id, status="RUNNING", human_note=new_note, draft_plan={},
                                    rejected_plans=rejected, min_director_rework=watermark)
    if not run_id:
        return await _dispatch_checkpoint(engine, engine.db.get_checkpoint(checkpoint_id), actor)
    engine.db.add_event(
        "steering.rejected",
        f"评审 #{cp['round_no']} 已带意见打回总监重议：{clean_reason[:120]}",
        actor=actor,
        payload={"checkpoint_id": checkpoint_id, "run_id": run_id},
    )
    return engine.db.get_checkpoint(checkpoint_id) or cp
