"""Resumable intake checkpoints. Approvals and execution remain owned by existing engines."""
from __future__ import annotations

import asyncio
import json
import re
from uuid import uuid4

from autodev.db import now_iso
from autodev.process.runner import is_gate_passed, load_builtin_template, load_template, save_custom_template
from . import adapter, assets, compiler, service, triage
from .paths import feature_spec_dir

INTENTS = {"new_system", "feature", "bugfix", "refactor", "documentation"}


def get(engine, feature: str) -> dict | None:
    feature_spec_dir(feature)
    return json.loads(engine.db.meta_get(f"mission:{feature}") or "null")


def _store(engine, mission):
    engine.db.meta_set(f"mission:{mission['feature_id']}", json.dumps(mission, ensure_ascii=False))


def preserve_confirmations(original: str, proposal: str) -> str:
    """User answers and attestation remain exact text even when the PM reconciles requirements."""
    pattern = r"^## ((?:澄清 |目标补充 |前置条件核验 )[^\n]*|交付前置条件|交付后人工体验项)\n.*?(?=^## |\Z)"
    sections = [m[0] for m in re.finditer(pattern, original, re.M | re.S)]
    proposal = re.sub(pattern, "", proposal, flags=re.M | re.S)
    return proposal.rstrip() + "\n\n" + "\n".join(sections)


def all_missions(engine) -> list[dict]:
    return [json.loads(row["value"]) for row in engine.db._query("SELECT value FROM meta WHERE key LIKE 'mission:%' ORDER BY key")]


def assert_idle(engine):
    if engine.db.meta_get("mission.lease"):
        raise service.SpecConflictError("当前需求流程仍在执行；中断恢复前请先核对执行记录")
    if engine.db._query("SELECT 1 FROM dispatch_records WHERE state IN ('DISPATCHING','QUEUED','RUNNING','NEEDS_CONFIRMATION')"):
        raise service.SpecConflictError("已有原生派发未结束或待确认，请先核对执行记录")
    if any(t.status in ("IN_PROGRESS", "VERIFYING", "REVIEWING") for t in engine.db.list_tasks()):
        raise service.SpecConflictError("项目仍有研发任务执行，请结束后继续需求规划")


def save(engine, feature: str, brief: str, intent: str, expected_revision: int, *, actor: str) -> dict:
    feature_spec_dir(feature)
    brief = brief.strip()
    if not brief or len(brief) > 50000 or intent not in INTENTS:
        raise ValueError("请填写有效研发目标和任务类型，目标不超过 50000 字")
    with engine.db.transaction():
        assert_idle(engine)
        old = get(engine, feature)
        if (old["revision"] if old else 0) != expected_revision:
            raise service.SpecConflictError("目标已有新修订，请刷新后编辑")
        if any(t.feature_id == feature for t in engine.db.list_tasks()):
            raise service.SpecConflictError("工作包已冻结，请新增功能；本批不支持替换已派发工作包")
        if old and (old["brief"], old["intent"]) == (brief, intent):
            return old
        mission = {**(old or {}), "feature_id": feature, "revision": expected_revision + 1, "brief": brief,
                   "intent": intent, "source_origin": triage.source_origin(engine), "updated_at": now_iso(), "last_error": ""}
        rev = service.latest(engine, feature)
        if rev:
            # Preserve exact user words in the same revision chain; no second requirements source.
            service.publish_revision(engine, feature, "spec", rev["content_text"] + f"\n## 目标补充 {mission['revision']}\n\n{brief}\n",
                                     expected_rev_id=rev["rev_id"], author=actor, summary="更新研发目标")
        _store(engine, mission)
        engine.db.add_event("mission.saved", f"研发目标已保存第 {mission['revision']} 版", actor=actor)
        return mission


def assessment_current(engine, mission) -> bool:
    return bool(mission.get("assessment") and mission.get("assessed_input") == triage.input_key(engine, mission))


def assert_ready_for_approval(engine):
    for mission in all_missions(engine):
        if not service.latest(engine, mission["feature_id"]):
            continue
        if not assessment_current(engine, mission):
            raise service.SpecConflictError("需求回答或输入已变化，请先重新评估后审批")
        feature = mission["feature_id"]
        if not _source_current(engine, feature, "checklist"):
            raise service.SpecConflictError("当前规格质量检查尚未完成")
        check = service.latest(engine, feature, "checklist")
        findings = engine.db.meta_get(f"speckit.analysis:{check['rev_id']}")
        if findings is None or any(f["severity"] == "CRITICAL" for f in json.loads(findings)):
            raise service.SpecConflictError("规格质量检查仍有严重问题，请修订后重新检查")
        if mission.get("prerequisites"):
            unresolved = [p for p in mission["prerequisites"] if not mission.get("prerequisite_evidence", {}).get(p["key"])]
            if unresolved:
                raise service.SpecConflictError("交付前置条件尚未确认，不能批准开工")


def view(engine, mission) -> dict:
    feature = mission["feature_id"]
    rev = service.latest(engine, feature)
    result = {**mission, "spec_rev_id": (rev or {}).get("rev_id", 0), "spec_rev_no": (rev or {}).get("rev_no", 0),
              "questions": service.list_clarifications(engine, feature), "state": "NEEDS_ASSESSMENT"}
    result["prerequisites"] = [p for p in mission.get("prerequisites", []) if not mission.get("prerequisite_evidence", {}).get(p["key"])]
    try:
        if any(q["status"] == "OPEN" and q["severity"] == "BLOCKING" for q in result["questions"]):
            result["state"] = "WAITING_ANSWERS"
        elif assessment_current(engine, mission):
            if result["prerequisites"]:
                result["state"] = "WAITING_PREREQUISITES"
            elif not _source_current(engine, feature, "checklist"):
                result["state"] = "NEEDS_CHECKLIST"
            elif not is_gate_passed(engine.db, "srs_approval", "project", "project"):
                result["state"] = "WAITING_APPROVAL"
            elif not _source_current(engine, feature, "analyze"):
                result["state"] = "NEEDS_PLAN"
            else:
                packages = compiler.compile_tasks(engine, feature)
                result["packages"] = [p.model_dump() for p in packages]
                result["state"] = "PUBLISHED" if all(engine.db.get_task(p.task.task_id) for p in packages) else "READY_TO_PUBLISH"
    except (ValueError, OSError) as exc:
        result["blocker"] = str(exc)
    if engine.db.meta_get("mission.lease"):
        result["state_before_running"] = result["state"]
        result["state"] = "RUNNING"
    return result


def _source_current(engine, feature, kind):
    rev = service.latest(engine, feature, kind)
    if not rev:
        return False
    service.assert_source_current(engine, rev)
    return rev["status"] != "STALE" and rev["input_sha"] == service.input_fingerprint(engine, feature, kind)


def reserve(engine, feature, expected_revision):
    with engine.db.transaction():
        assert_idle(engine)
        mission = get(engine, feature)
        if not mission or mission["revision"] != expected_revision:
            raise service.SpecConflictError("目标已变化，请刷新后继续")
        token = uuid4().hex
        engine.db.meta_set("mission.lease", json.dumps({"token": token, "feature": feature, "started_at": now_iso()}))
        return token


def release(engine, token):
    with engine.db.transaction():
        lease = json.loads(engine.db.meta_get("mission.lease") or "{}")
        if lease.get("token") == token:
            engine.db.meta_set("mission.lease", None)


def recover(engine, *, actor: str):
    """Only release an abandoned checkpoint after native dispatch has a terminal receipt."""
    if engine.db._query("SELECT 1 FROM dispatch_records WHERE state IN ('DISPATCHING','QUEUED','RUNNING','NEEDS_CONFIRMATION')"):
        raise service.SpecConflictError("原生执行尚未终止或结果待确认，请先在执行记录中处理，不能重复派发")
    engine.db.meta_set("mission.lease", None)
    engine.db.add_event("mission.recovered", "已核对原生执行收据，允许从已保存的步骤继续", actor=actor)


def prerequisite_evidence(engine, feature, key, evidence, expected_revision, *, actor):
    with engine.db.transaction():
        assert_idle(engine)
        mission = get(engine, feature)
        if not mission or mission["revision"] != expected_revision:
            raise service.SpecConflictError("目标已变化，请刷新")
        if key not in {p["key"] for p in mission.get("prerequisites", [])} or not evidence.strip():
            raise ValueError("前置条件不存在或缺少核验依据")
        mission.setdefault("prerequisite_evidence", {})[key] = {"evidence": evidence.strip(), "actor": actor, "at": now_iso()}
        rev = service.latest(engine, feature)
        if rev:
            service.publish_revision(engine, feature, "spec", rev["content_text"] + f"\n## 前置条件核验 {key}\n\n负责人提供：{evidence.strip()}\n",
                                     expected_rev_id=rev["rev_id"], author=actor, summary="记录前置条件核验依据")
        mission["revision"] += 1
        _store(engine, mission)
        engine.db.add_event("mission.prerequisite_attested", f"负责人提交前置条件 {key} 的核验依据", actor=actor)
        return mission


def _choose_process(engine, route):
    # Apply only to the new intake lifecycle. Never weaken an established/custom workflow.
    if engine.db.list_tasks() or service.current_specs(engine):
        return
    existing = load_template(engine.ws, engine.settings)
    if (engine.ws.state_dir / "process.yaml").exists() or existing.id not in ("legacy", "standard", "quick-prototype"):
        return
    template = load_builtin_template(route["process_template"])
    save_custom_template(engine.ws, template.model_dump_json())
    engine.settings.process_template = template.id


async def advance(engine, feature, token, *, actor):
    """Bounded progression through existing steps, stopping at business decisions or publication."""
    mission = get(engine, feature)
    try:
        lease = json.loads(engine.db.meta_get("mission.lease") or "{}")
        if lease.get("token") != token or lease.get("feature") != feature:
            raise service.SpecConflictError("需求执行租约已失效")
        mission["last_error"] = ""
        _store(engine, mission)
        open_questions = service.list_clarifications(engine, feature)
        if any(q["status"] == "OPEN" and q["severity"] == "BLOCKING" for q in open_questions):
            return
        if not assessment_current(engine, mission):
            key = triage.input_key(engine, mission)
            checkpoint = mission.get("assessment_checkpoint", {})
            if checkpoint.get("input") == key:
                assessment = triage.Assessment.model_validate(checkpoint["result"])
            else:
                assessment = await triage.assess(engine, mission, actor=actor)
                mission["assessment_checkpoint"] = {"input": key, "result": assessment.model_dump()}
                _store(engine, mission)
            route = triage.recommendation(assessment, triage.source_origin(engine))
            if not service.enabled(engine):
                _choose_process(engine, route)
                service.enable(engine, route["available_preset"], actor=actor)
            lock, _ = assets.load_assets(engine)
            if not service.latest(engine, "project", "constitution"):
                await adapter.run_step(engine, feature, "constitution", lock["preset_id"], instructions=mission["brief"], actor=actor)
                if triage.input_key(engine, mission, constitution_rev_id=0) != key:
                    raise service.SpecConflictError("生成项目约束期间输入已变化，请重新评估")
                # Constitution creation was part of this saved assessment, so resume can reuse it.
                mission["assessment_checkpoint"]["input"] = triage.input_key(engine, mission)
                _store(engine, mission)
            with engine.db.transaction():
                rev = service.latest(engine, feature)
                if not rev:
                    content = assessment.proposed_spec
                    if assessment.prerequisites:
                        content += "\n## 交付前置条件\n" + "\n".join(f"- {p.key}：{p.description}。核验依据：{p.evidence_needed}" for p in assessment.prerequisites) + "\n"
                    if assessment.experience_items:
                        content += "\n## 交付后人工体验项\n" + "\n".join(f"- {p.description}" for p in assessment.experience_items) + "\n"
                    rev = service.publish_revision(engine, feature, "spec", content, expected_rev_id=0,
                                                   author=actor, summary="首次需求评估形成待审规格", handoff=assessment.handoff_brief)
                elif assessment.proposed_spec.strip():
                    content = preserve_confirmations(rev["content_text"], assessment.proposed_spec)
                    rev = service.publish_revision(engine, feature, "spec", content, expected_rev_id=rev["rev_id"],
                                                   author=actor, summary="根据负责人回答整理待审规格", handoff=assessment.handoff_brief)
                used = {q["question_key"] for q in service.list_clarifications(engine, feature)}
                if any(q.question_key in used for q in assessment.questions):
                    raise ValueError("重新评估重复了已记录问题，请使用新编号并只询问未决事项")
                service.add_clarifications(engine, feature, rev["rev_id"], assessment.questions, actor=actor)
                prior = {p["key"]: p for p in mission.get("prerequisites", [])}
                for p in assessment.prerequisites:
                    if p.key in prior and prior[p.key] != p.model_dump():
                        raise ValueError("已记录前置条件不可静默覆盖，请使用新编号")
                    prior[p.key] = p.model_dump()
                mission.update(assessment=assessment.model_dump(), route=route, prerequisites=list(prior.values()),
                               actual_preset=lock["preset_id"], actual_process=load_template(engine.ws, engine.settings).id,
                               assessed_input=triage.input_key(engine, mission), assessment_checkpoint={})
                _store(engine, mission)
        if any(q["status"] == "OPEN" and q["severity"] == "BLOCKING" for q in service.list_clarifications(engine, feature)):
            return
        if any(not mission.get("prerequisite_evidence", {}).get(p["key"]) for p in mission.get("prerequisites", [])):
            return
        lock, _ = assets.load_assets(engine)
        instructions = mission["brief"] + "\n" + ("采用轻量规格与方案，最多两个工作包，免圆桌和独立详设。" if mission["route"]["clarity_level"] == "READY_LITE" else "保留标准流程门禁。")
        if not _source_current(engine, feature, "checklist"):
            await adapter.run_step(engine, feature, "checklist", lock["preset_id"], instructions=instructions, actor=actor)
        check = service.latest(engine, feature, "checklist")
        findings = json.loads(engine.db.meta_get(f"speckit.analysis:{check['rev_id']}") or "[]")
        if any(f["severity"] == "CRITICAL" for f in findings):
            raise ValueError("规格质量检查发现严重问题，请在高级规格修订中修正，再继续")
        if not is_gate_passed(engine.db, "srs_approval", "project", "project"):
            return
        for kind in ("plan", "tasks", "analyze"):
            if not _source_current(engine, feature, kind):
                await adapter.run_step(engine, feature, kind, lock["preset_id"], instructions=instructions, actor=actor)
        compiler.compile_tasks(engine, feature)
    except (Exception, asyncio.CancelledError) as exc:
        mission = get(engine, feature)
        mission["last_error"] = str(exc) or "执行已取消；可从已保存步骤继续"
        _store(engine, mission)
        engine.db.add_event("mission.failed", mission["last_error"], actor=actor)
        raise
    finally:
        release(engine, token)
