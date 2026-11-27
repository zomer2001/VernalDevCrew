"""Reconcile native Run receipts under one crash-released workspace lease."""
from __future__ import annotations

import asyncio
import json
import logging

from autodev import public_pool
from autodev.engine import OpsError
from autodev.loopforge import dispatch
from autodev.loopforge.bridge import extract_run_usage, reconcile_waiting_nodes
from autodev.loopforge.client import LoopForgeClient, LoopForgeError, MissingArtifactError
from autodev.loopforge.completion import CompletionLedger
from autodev.loopforge.protocol import FailureCategory, native_progress
from autodev.scheduler import _FileLock
from autodev.speckit import adapter, assets, compiler, execution, missions, service
from autodev.speckit.validate_output import validate

log = logging.getLogger(__name__)


def _lease(engine):
    return _FileLock(engine.ws.state_dir / "run-reconciler.lock")


def _finish(engine, receipt, display, category, message, *, run=None, diagnostic=""):
    db = engine.db
    with db.transaction():
        mission = missions.get(engine, receipt["feature_id"])
        if mission.get("active_dispatch_id") != receipt["dispatch_id"]:
            return
        ok = not category
        dispatch.record(db, receipt, "FINISHED" if ok else "FAILED")
        mission.update(active_dispatch_id=None, run_id=receipt["lf_run_id"], native_display=display,
                       failure_category=category, last_error=message)
        usage = extract_run_usage(run or {})
        db.finish_run(mission.pop("local_run_id"), session_id=receipt["lf_run_id"], exit_code=0 if ok else 1,
                      verdict="PASS" if ok else "ERROR", summary_log=diagnostic or message,
                      **{k: getattr(usage, k) for k in ("input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens")})
        missions._store(engine, mission)
        db.add_event("mission.completed" if ok else "mission.failed", message or "已接收组合规划产物",
                     actor=receipt["actor"], payload={"run_id": receipt["lf_run_id"], "stage": receipt["stage"],
                                                      "failure_category": category})


async def _failed_planning_receipt(engine, feature):
    rows = engine.db._query(
        "SELECT * FROM dispatch_records WHERE feature_id=? AND stage IN ('lite_bundle','std_spec','std_plan') "
        "ORDER BY dispatch_id DESC LIMIT 1", (feature,))
    if not rows or rows[0]["state"] != "FAILED":
        raise service.SpecConflictError("仅可重试最近一次已失败的规划")
    receipt = dict(rows[0])
    if not receipt["issue_id"]:
        return receipt  # No native dispatch took place.
    try:
        client = receipt_client(engine, receipt)
        rid = receipt["lf_run_id"]
        if not rid and receipt["lf_task_id"]:
            task = await client.get_task(receipt["lf_task_id"])
            if task.get("id") != receipt["lf_task_id"] or task.get("issueId") != receipt["issue_id"]:
                raise service.SpecConflictError("原生 Task 与失败收据不匹配")
            rid = task.get("runId")
            if not rid and task.get("status") in ("failed", "cancelled"):
                return receipt
        if rid:
            run = await owned_run(engine, client, receipt, rid)
            if run.get("status") in ("succeeded", "failed", "cancelled"):
                return receipt
    except LoopForgeError as exc:
        raise service.SpecConflictError("无法核对原生执行状态，请稍后再试") from exc
    raise service.SpecConflictError("尚未确认上次原生执行已结束")


async def start(engine, feature, revision, *, actor, retry_failed=False):
    db = engine.db
    mission = missions.get(engine, feature)
    if not mission or mission["revision"] != revision:
        raise service.SpecConflictError("目标已变化，请刷新后继续")
    if mission.get("active_dispatch_id"):
        row = db._query("SELECT * FROM dispatch_records WHERE dispatch_id=?", (mission["active_dispatch_id"],))[0]
        if row["lf_run_id"]:
            return {"started": True, "run_id": row["lf_run_id"], "dispatch_id": row["dispatch_id"]}
        if row["state"] == "QUEUED":
            return await _await_binding(engine, dict(row))
        raise service.SpecConflictError(mission.get("last_error") or "派发请求已登记，正在核对原生回执")
    try:
        with _lease(engine):
            previous = await _failed_planning_receipt(engine, feature) if retry_failed else None
            with db.transaction():
                missions.assert_idle(engine)
                mission = missions.get(engine, feature)
                if mission["revision"] != revision:
                    raise service.SpecConflictError("目标已变化，请刷新后继续")
                stage = missions.next_stage(engine, mission)
                if not stage:
                    return {"started": False}
                baseline = missions.planning_inputs(engine, mission)
                prompt, schema, role = adapter.planning_request(engine, mission, stage)
                key = f"mission:{feature}:{stage}:{baseline}"
                attempt = 1
                if previous:
                    if previous["stage"] != stage or previous["expected_baseline_sha"] != baseline:
                        raise service.SpecConflictError("规划输入已变化，请按当前版本继续")
                    attempt = previous["attempt_no"] + 1
                    key += f":attempt:{attempt}"
                receipt, fresh = dispatch.reserve(db, f"plan:{engine.settings.loopforge_project_id}:{feature}:{stage}",
                                                  0, attempt, "", actor, command=key)
                if not fresh:
                    if receipt["state"] == "FAILED":
                        raise service.SpecConflictError(mission.get("last_error") or "该输入的原生执行已失败，请核对记录后显式重试")
                    return {"started": False, "run_id": receipt["lf_run_id"]}
                receipt.update(feature_id=feature, stage=stage, expected_baseline_sha=baseline)
                db._exec("UPDATE dispatch_records SET feature_id=?,stage=?,expected_baseline_sha=? WHERE dispatch_id=?",
                         (feature, stage, baseline, receipt["dispatch_id"]))
                dispatch.bind_baseline(db, receipt, {"spec_sha": (service.latest(engine, feature) or {}).get("content_sha", ""),
                    "contract_sha": compiler.contract_hash(engine), "asset_versions": assets.generation_baseline(engine)["assets"]})
                mission.update(active_dispatch_id=receipt["dispatch_id"], native_display="RUNNING", failure_category=None,
                               last_error="", run_id="", local_run_id=db.start_run(f"_speckit_{feature}_{stage}", attempt,
                                                                                "SPEC_KIT", engine.harness.name))
                missions._store(engine, mission)
            try:
                rid = await engine.harness.dispatch_bundle(receipt, prompt, schema, role)
            except Exception as exc:
                if receipt["issue_id"] and receipt["state"] in ("DISPATCHING", "NEEDS_CONFIRMATION"):
                    dispatch.record(db, receipt, "NEEDS_CONFIRMATION")
                    mission.update(last_error="派单结果待核对，请确认原生工单记录", native_display="EXECUTION_FAILED",
                                   failure_category=FailureCategory.ENV_OR_CONFLICT_BLOCKED)
                    missions._store(engine, mission)
                else:
                    _finish(engine, receipt, "EXECUTION_FAILED", FailureCategory.ENV_OR_CONFLICT_BLOCKED, str(exc))
                raise service.SpecConflictError(str(exc)) from exc
            if rid:
                _bind_run(engine, receipt, rid)
                return {"started": True, "run_id": rid, "dispatch_id": receipt["dispatch_id"]}
    except OpsError as exc:
        raise service.SpecConflictError("正在核对原生执行，请刷新后继续") from exc
    return await _await_binding(engine, receipt)


async def _await_binding(engine, receipt):
    # The native Task is durable. Waiting for its Run ID holds neither a DB transaction nor the workspace lease.
    try:
        rid = await dispatch.observe_task(engine.db, receipt, receipt_client(engine, receipt),
                                         timeout=engine.settings.agent_timeout_sec)
    except LoopForgeError as exc:
        raise service.SpecConflictError(str(exc)) from exc
    _bind_run(engine, receipt, rid)
    return {"started": True, "run_id": rid, "dispatch_id": receipt["dispatch_id"]}


def _bind_run(engine, receipt, rid):
    with engine.db.transaction():
        mission = missions.get(engine, receipt["feature_id"])
        if mission.get("active_dispatch_id") != receipt["dispatch_id"] or mission.get("run_id") == rid:
            return
        mission["run_id"] = rid
        missions._store(engine, mission)
        role = "pm" if receipt["stage"] == "std_spec" else "director"
        execution.record(engine.db, receipt["feature_id"], {
            "run_id": rid, "project_id": engine.settings.loopforge_project_id, "role": role, "step": receipt["stage"],
            "pool": receipt["pool"], "executor": engine.settings.harness_for(role), "model": engine.settings.model_for(role),
            "effort": engine.settings.effort_for(role, step=receipt["stage"]), "started_at": receipt["created_at"]})


def receipt_client(engine, receipt):
    if receipt["pool"] == "public_docker":
        if engine.settings.loopforge_url.rstrip("/") != public_pool.server_url() or not public_pool.service_token():
            raise LoopForgeError("请恢复派单时绑定的公用执行身份", 403)
        return LoopForgeClient(public_pool.server_url(), public_pool.service_token())
    return LoopForgeClient(engine.settings.loopforge_url, engine.settings.service_token())


async def owned_run(engine, client, receipt, rid):
    run = await client.project_run(rid, engine.settings.loopforge_project_id)
    if ((run.get("executionContext") or {}).get("issueId") or run.get("issueId")) != receipt["issue_id"]:
        raise service.SpecConflictError("原生 Run 与派单工单不匹配")
    return run


async def reconcile_plan(engine, receipt):
    client = receipt_client(engine, receipt)
    db = engine.db
    rid = await dispatch.bound_task_run(db, receipt, client)
    if not rid:
        return
    _bind_run(engine, receipt, rid)
    run = await owned_run(engine, client, receipt, rid)
    mission = missions.get(engine, receipt["feature_id"])
    display, category, message = native_progress(run)
    if run["status"] in ("waiting", "waiting_approval"):
        _, _, denial = await reconcile_waiting_nodes(client, run, CompletionLedger(db, rid))
        if denial:
            _finish(engine, receipt, "EXECUTION_FAILED", FailureCategory.ENV_OR_CONFLICT_BLOCKED, denial)
            return
    if run["status"] in ("failed", "cancelled"):
        if display == "REWORK_EXHAUSTED":
            for node in ("quality_check", "consistency_review"):
                if (run.get("nodes", {}).get(node) or {}).get("status") == "failed":
                    try:
                        message = await client.named_artifact(run, "review-report.md", node_id=node)
                    except MissingArtifactError:
                        pass  # The native node diagnostic remains available.
                    break
        _finish(engine, receipt, display, category, message, run=run)
        return
    if run["status"] != "succeeded":
        missions.record_progress(engine, receipt["feature_id"], rid, display, category, message)
        return
    try:
        await accept_plan_result(engine, receipt, mission, run, client)
    except (ValueError, MissingArtifactError) as exc:
        from pydantic import ValidationError
        category = (FailureCategory.ENV_OR_CONFLICT_BLOCKED if isinstance(exc, service.SpecConflictError)
                    else FailureCategory.ARTIFACT_DEFECT)
        message = "规划产物未通过格式校验，请查看当前轮执行记录" if isinstance(exc, ValidationError) else str(exc)
        _finish(engine, receipt, "EXECUTION_FAILED", category, message, run=run, diagnostic=str(exc))


async def accept_plan_result(engine, receipt, mission, run, client):
    db = engine.db
    stage = receipt["stage"]
    validate_id = {"std_spec": "validate_spec", "std_plan": "validate_and_compile", "lite_bundle": "validate_lite_bundle"}[stage]
    data = json.loads(await client.named_artifact(run, "vernal-result.json", node_id=validate_id))
    review = None
    if stage != "lite_bundle":
        node_id = "quality_check" if stage == "std_spec" else "consistency_review"
        report = await client.named_artifact(run, "review-report.md", node_id=node_id)
        if report.strip().partition("\n")[0].strip() != "# PASS":
            raise ValueError("独立审查尚未通过：" + report[:1200])
        review = json.loads(await client.named_artifact(run, "vernal-quality.json", node_id=node_id))
    with service.bundle_publication(engine, receipt["feature_id"]):
        # Within this transaction the receipt no longer blocks the gate whose baseline we verify.
        dispatch.record(db, receipt, "FINISHED")
        if missions.planning_inputs(engine, mission) != receipt["expected_baseline_sha"]:
            raise service.SpecConflictError("规划期间输入已变化，旧结果已拒收")
        _, schema, _ = adapter.planning_request(engine, mission, stage)
        validate(data, schema)
        if review is not None:
            validate(review, schema["x-vernal-review-schema"])
        missions.accept_bundle(engine, mission, stage, data, review, receipt["actor"])
        _finish(engine, receipt, None, None, "", run=run)


async def reconcile_once(ctx, slot):
    engine = slot.engine
    rows = engine.db._query("SELECT * FROM dispatch_records WHERE stage!='' "
                            "AND state IN ('DISPATCHING','QUEUED','RUNNING') ORDER BY dispatch_id")
    legacy = engine.db._query("SELECT 1 FROM dispatch_records WHERE stage='' AND state='NEEDS_CONFIRMATION' "
                              "AND task_id LIKE 'plan:%' AND lf_run_id!='' LIMIT 1")
    if not rows and not legacy:
        return
    try:
        with _lease(engine):
            if legacy:
                await dispatch.reconcile_completed_plans(engine.db, engine.settings.loopforge_project_id,
                                                         ctx.lf_client(slot), "system")
            for candidate in rows:
                current = engine.db._query("SELECT * FROM dispatch_records WHERE dispatch_id=?", (candidate["dispatch_id"],))[0]
                if current["state"] not in ("DISPATCHING", "QUEUED", "RUNNING"):
                    continue
                receipt = dict(current)
                if receipt["stage"] == "lite_delivery":
                    await reconcile_delivery(ctx, slot, receipt)
                    continue
                if receipt["state"] == "DISPATCHING" and not (receipt["lf_task_id"] or receipt["lf_run_id"]):
                    with engine.db.transaction():
                        dispatch.record(engine.db, receipt, "NEEDS_CONFIRMATION")
                        mission = missions.get(engine, receipt["feature_id"])
                        mission.update(native_display="EXECUTION_FAILED", failure_category=FailureCategory.ENV_OR_CONFLICT_BLOCKED,
                                       last_error="派单结果待核对，请确认原生工单记录")
                        missions._store(engine, mission)
                    continue
                try:
                    await reconcile_plan(engine, receipt)
                except LoopForgeError as exc:
                    if exc.status_code not in (401, 403, 404, 409, 422):
                        continue  # A failed observation does not imply a failed Run.
                    _finish(engine, receipt, "EXECUTION_FAILED", FailureCategory.ENV_OR_CONFLICT_BLOCKED, str(exc))
                except OSError as exc:
                    # Publication rolled back. Keep the same receipt observable after storage recovers.
                    mission = missions.get(engine, receipt["feature_id"])
                    mission.update(last_error=str(exc), native_display="EXECUTION_FAILED",
                                   failure_category=FailureCategory.ENV_OR_CONFLICT_BLOCKED)
                    missions._store(engine, mission)
                except ValueError as exc:
                    conflict = isinstance(exc, service.SpecConflictError)
                    category = FailureCategory.ENV_OR_CONFLICT_BLOCKED if conflict else FailureCategory.ARTIFACT_DEFECT
                    _finish(engine, receipt, "EXECUTION_FAILED", category, str(exc))
    except OpsError:
        return  # Another process owns this workspace pass.


async def reconcile_delivery(ctx, slot, receipt):
    from autodev.server.routers.speckit import start_lite_delivery
    if ctx.is_scheduler_running(slot) or slot.engine.planning_stop.is_set() or slot.engine.status == "STALLED":
        return
    engine = slot.engine
    task = engine.db.get_task(receipt["task_id"])
    try:
        if not task or task.feature_id != receipt["feature_id"] or task.compiled_task_hash != receipt["expected_baseline_sha"]:
            raise service.SpecConflictError("冻结工作包与轻量交付收据不符")
        # A second Web worker must not interpret an in-flight POST as an abandoned dispatch.
        try:
            with _FileLock(engine.ws.lock_path):
                pass
        except OpsError:
            return
        client = receipt_client(engine, receipt)
        rid = await dispatch.bound_task_run(engine.db, receipt, client)
        if not rid:
            return
        run = await owned_run(engine, client, receipt, rid)
        if run["status"] != "succeeded":
            missions.record_progress(engine, task.feature_id, rid, *native_progress(run))
        if run["status"] in ("waiting", "waiting_approval"):
            _, _, denial = await reconcile_waiting_nodes(client, run, CompletionLedger(engine.db, rid))
            if denial:
                raise service.SpecConflictError(denial)
        if run["status"] in ("succeeded", "failed", "cancelled"):
            # Reuse candidate verification, current-turn evidence, CI and human signoff under the delivery lock.
            start_lite_delivery(ctx, slot, [task.task_id])
    except (LoopForgeError, service.SpecConflictError) as exc:
        if isinstance(exc, LoopForgeError) and exc.status_code not in (401, 403, 404, 409, 422):
            return
        with engine.db.transaction():
            dispatch.record(engine.db, receipt, "FAILED" if receipt["lf_run_id"] else "NEEDS_CONFIRMATION")
            engine.db.add_event("task.lite_delivery_failed", str(exc), task_id=receipt["task_id"])
            from autodev.models import ProjectStatus
            engine.set_status(ProjectStatus.STALLED)


async def serve(ctx):
    while True:
        for slot in list(ctx.all_slots):
            try:
                await reconcile_once(ctx, slot)
            except Exception:
                log.exception("Failed to reconcile project %s", slot.project_key)
        await asyncio.sleep(2.5)
