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


def approval_readiness(engine, *, feature_id: str | None = None) -> dict:
    """Read-only approval precondition evaluation returning structured blockers without recursion."""
    blockers: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(code: str, message: str, fid: str = "") -> None:
        key = (code, fid)
        if key not in seen:
            seen.add(key)
            blockers.append({"code": code, "message": message, "feature_id": fid})

    # 1. Active mission lease or in-flight native dispatch blocks new approvals.
    raw_lease = engine.db.meta_get("mission.lease")
    if raw_lease:
        try:
            lease = json.loads(raw_lease)
        except ValueError:
            lease = {}
        lease_feature = str(lease.get("feature") or "")
        lease_step = str(lease.get("step") or "")
        if not feature_id or not lease_feature or lease_feature == feature_id:
            if lease_step == "checklist":
                add("CHECKLIST_RUNNING", "规格质量检查正在进行，完成后可审阅批准", lease_feature)
            elif lease_step in ("intake", "constitution"):
                add("ASSESSMENT_RUNNING", "回答已保存，正在重新整理规格", lease_feature)
            else:
                add("MISSION_RUNNING", "当前需求流程仍在执行，完成后可审阅批准", lease_feature)

    active_dispatches = engine.db._query(
        "SELECT task_id, command_key FROM dispatch_records WHERE state IN ('DISPATCHING','QUEUED','RUNNING','NEEDS_CONFIRMATION')"
    )
    if active_dispatches and not raw_lease:
        cmds = " ".join(f"{r['task_id'] or ''} {r['command_key'] or ''}" for r in active_dispatches)
        if "checklist" in cmds:
            add("CHECKLIST_RUNNING", "规格质量检查正在进行，完成后可审阅批准", feature_id or "")
        elif "intake" in cmds:
            add("ASSESSMENT_RUNNING", "需求评估正在进行，完成后可审阅批准", feature_id or "")
        else:
            add("MISSION_RUNNING", "仍有原生执行未结束或待确认，请核对后再审批", feature_id or "")

    # 2. Constitution and blocking clarifications across Spec Kit features.
    if service.enabled(engine):
        constitution = service.latest(engine, "project", "constitution")
        if not constitution:
            add("CONSTITUTION_MISSING", "尚未建立项目宪章，不能批准规格", "project")
        else:
            try:
                service.assert_source_current(engine, constitution)
            except (ValueError, OSError):
                add("FILE_MISSING_OR_DRIFTED", "项目宪章文件缺失或存在未归档改动，请先恢复或保存修订", "project")

        q_sql = "SELECT feature_id, question_key FROM clarifications WHERE severity='BLOCKING' AND status='OPEN'"
        q_params: tuple = ()
        if feature_id:
            q_sql += " AND feature_id=?"
            q_params = (feature_id,)
        open_qs = engine.db._query(q_sql + " ORDER BY feature_id, clarify_id", q_params)
        by_feature: dict[str, list[str]] = {}
        for row in open_qs:
            by_feature.setdefault(str(row["feature_id"]), []).append(str(row["question_key"]))
        for fid, keys in by_feature.items():
            add("OPEN_QUESTIONS", f"功能 {fid} 还有影响交付的关键问题未回答（{', '.join(keys)}）", fid)

    # 3. Mission-level assessment, prerequisites, and checklist readiness.
    missions_list = [m for m in all_missions(engine) if not feature_id or m["feature_id"] == feature_id]
    for mission in missions_list:
        fid = mission["feature_id"]
        spec_rev = service.latest(engine, fid)
        if not spec_rev:
            continue
        try:
            service.assert_source_current(engine, spec_rev)
        except (ValueError, OSError):
            add("FILE_MISSING_OR_DRIFTED", f"功能 {fid} 的规格文件缺失或存在未归档改动，请先修订或恢复", fid)
            continue
        try:
            curr_assess = assessment_current(engine, mission)
        except (ValueError, OSError):
            add("FILE_MISSING_OR_DRIFTED", f"功能 {fid} 的来源文件与已发布修订不一致，请先恢复或保存", fid)
            continue
        if not curr_assess:
            add("ASSESSMENT_PENDING", f"功能 {fid} 的回答或需求输入已变化，请先重新评估整理规格后再审批", fid)
        if mission.get("prerequisites"):
            unresolved = [p["key"] for p in mission["prerequisites"] if not mission.get("prerequisite_evidence", {}).get(p["key"])]
            if unresolved:
                add("PREREQUISITES_UNVERIFIED", f"功能 {fid} 的交付前置条件尚未提供核验依据（{', '.join(unresolved)}）", fid)
        check = service.latest(engine, fid, "checklist")
        if not check:
            add("CHECKLIST_MISSING", f"功能 {fid} 当前版本尚未完成规格质量检查", fid)
            continue
        try:
            checklist_curr = _source_current(engine, fid, "checklist")
        except (ValueError, OSError):
            add("FILE_MISSING_OR_DRIFTED", f"功能 {fid} 的质量检查文件缺失或存在未归档改动", fid)
            continue
        if not checklist_curr:
            add("CHECKLIST_STALE", f"功能 {fid} 的质量检查基于旧版本规格，请重新执行质量检查", fid)
            continue
        raw_findings = engine.db.meta_get(f"speckit.analysis:{check['rev_id']}")
        if raw_findings is None:
            add("CHECKLIST_FINDINGS_MISSING", f"功能 {fid} 的质量检查缺少结构化发现记录，不能视为无问题", fid)
            continue
        try:
            parsed_findings = json.loads(raw_findings)
        except ValueError:
            add("CHECKLIST_FINDINGS_MISSING", f"功能 {fid} 的质量检查发现记录损坏，请重新检查", fid)
            continue
        criticals = [f for f in parsed_findings if isinstance(f, dict) and f.get("severity") == "CRITICAL"]
        if criticals:
            add("CHECKLIST_CRITICAL_FINDINGS", f"功能 {fid} 的规格质量检查存在 {len(criticals)} 项严重问题（CRITICAL），请修订后重新检查", fid)

    return {"ready": not blockers, "blockers": blockers}


def assert_ready_for_approval(engine):
    readiness = approval_readiness(engine)
    if not readiness["ready"]:
        raise service.SpecConflictError(readiness["blockers"][0]["message"])


def config_alignment(engine, mission: dict) -> dict:
    """Compare recommended vs actual locked preset & process template with an explicit mismatch reason."""
    route = mission.get("route") or {}
    recommended_preset = str(route.get("available_preset") or route.get("recommended_preset") or "")
    recommended_process = str(route.get("process_template") or "")
    actual_preset = str(mission.get("actual_preset") or "")
    if service.enabled(engine):
        try:
            lock, _ = assets.load_assets(engine)
            actual_preset = lock["preset_id"]
        except (ValueError, OSError):
            pass
    actual_process = load_template(engine.ws, engine.settings).id
    aligned = (
        (not recommended_preset or actual_preset == recommended_preset)
        and (not recommended_process or actual_process == recommended_process)
    )
    has_tasks = bool(engine.db.list_tasks())
    is_custom_process = actual_process not in ("legacy", "standard", "quick-prototype", "feature", "bugfix")
    mismatch_reason = ""
    if not aligned:
        if has_tasks:
            mismatch_reason = (
                f"当前评估推荐 {recommended_preset}/{recommended_process}，但项目已锁定 {actual_preset or '未启用'}/{actual_process} 且已有研发任务，不能原地转换配置。"
            )
        elif is_custom_process:
            mismatch_reason = (
                f"当前评估推荐 {recommended_preset}/{recommended_process}，但项目使用自定义流程模板（{actual_process}），系统不会自动降级自定义门禁。"
            )
        else:
            mismatch_reason = (
                f"初次评估存在待确认问题时已锁定 {actual_preset or '未启用'}/{actual_process}；回答澄清后当前推荐 {recommended_preset}/{recommended_process}，"
                "为保留原有约束不会静默降级，如需切换请执行显式配置转换并保留审批记录。"
            )
    can_convert = (
        not aligned
        and bool(recommended_preset and recommended_process)
        and service.enabled(engine)
        and not has_tasks
        and not is_custom_process
        and not engine.db.meta_get("mission.lease")
    )
    return {
        "aligned": aligned,
        "recommended_preset": recommended_preset,
        "recommended_process": recommended_process,
        "actual_preset": actual_preset,
        "actual_process": actual_process,
        "mismatch_reason": mismatch_reason,
        "can_convert_to_recommended": can_convert,
    }


def convert_profile(
    engine,
    feature: str,
    *,
    target_preset: str,
    target_process: str,
    expected_revision: int,
    actor: str,
    reason: str = "",
) -> dict:
    """Explicitly convert preset and built-in process template when no tasks have been published."""
    feature_spec_dir(feature)
    if target_preset not in ("vernal-core", "vernal-lite"):
        raise ValueError("仅支持转换为 vernal-core 或 vernal-lite 预设")
    if target_process not in ("standard", "quick-prototype", "feature", "bugfix"):
        raise ValueError("仅支持转换为内置流程模板（standard/quick-prototype/feature/bugfix）")
    with engine.db.transaction():
        assert_idle(engine)
        if engine.db.list_tasks():
            raise service.SpecConflictError("已有已发布研发任务，不能转换项目预设或流程模板")
        mission = get(engine, feature)
        if not mission or mission["revision"] != expected_revision:
            raise service.SpecConflictError("需求目标修订已变化，请刷新后再转换配置")
        if not service.enabled(engine):
            raise ValueError("请先启用功能规格流程")
        current_tpl = load_template(engine.ws, engine.settings)
        if current_tpl.id not in ("legacy", "standard", "quick-prototype", "feature", "bugfix"):
            raise service.SpecConflictError(f"当前项目使用自定义流程模板（{current_tpl.id}），拒绝自动或批量降级")
    prev_lock, _ = assets.load_assets(engine)
    prev_process = current_tpl.id
    upgrade_res = assets.upgrade_assets(
        engine,
        target_preset,
        actor=actor,
        reason=reason or f"显式配置转换：{prev_lock['preset_id']}/{prev_process} -> {target_preset}/{target_process}",
    )
    new_tpl = load_builtin_template(target_process)
    save_custom_template(engine.ws, new_tpl.model_dump_json())
    engine.settings.process_template = new_tpl.id
    # Always preserve blocking human srs_approval gate even in quick-prototype.
    service.ensure_specification_gate(engine)
    with engine.db.transaction():
        invalidated_kinds = ["plan", "tasks", "analyze"]
        if upgrade_res["changed"]:
            invalidated_kinds.insert(0, "checklist")
        placeholders = ",".join("?" for _ in invalidated_kinds)
        engine.db._exec(
            f"UPDATE spec_revisions SET status='STALE' WHERE feature_id=? AND source_kind IN ({placeholders})",
            (feature, *invalidated_kinds),
        )
        mission = get(engine, feature)
        history = list(mission.get("profile_history") or [])
        entry = {
            "from": {"preset": prev_lock["preset_id"], "preset_version": prev_lock["preset_version"], "process": prev_process},
            "to": {"preset": upgrade_res["current"]["preset_id"], "preset_version": upgrade_res["current"]["preset_version"], "process": new_tpl.id},
            "invalidated": invalidated_kinds,
            "actor": actor,
            "reason": reason.strip(),
            "at": now_iso(),
        }
        history.append(entry)
        mission.update(
            revision=mission["revision"] + 1,
            actual_preset=upgrade_res["current"]["preset_id"],
            actual_process=new_tpl.id,
            profile_history=history,
            updated_at=now_iso(),
        )
        _store(engine, mission)
        engine.db.add_event(
            "mission.profile_converted",
            f"项目配置已显式转换：{prev_lock['preset_id']}/{prev_process} -> {target_preset}/{new_tpl.id}（已保留原规格、澄清回答与规格审批门禁）",
            actor=actor,
            payload=entry,
        )
    return {"mission": view(engine, mission), "conversion": entry}


def view(engine, mission) -> dict:
    feature = mission["feature_id"]
    rev = service.latest(engine, feature)
    result = {**mission, "spec_rev_id": (rev or {}).get("rev_id", 0), "spec_rev_no": (rev or {}).get("rev_no", 0),
              "questions": service.list_clarifications(engine, feature), "state": "NEEDS_ASSESSMENT"}
    result["prerequisites"] = [p for p in mission.get("prerequisites", []) if not mission.get("prerequisite_evidence", {}).get(p["key"])]
    readiness = approval_readiness(engine, feature_id=feature)
    result["approval_blockers"] = readiness["blockers"]
    result["quality_review"] = service.quality_review(engine, feature) if service.enabled(engine) else None
    result["config_alignment"] = config_alignment(engine, mission)
    blocker_codes = {b["code"] for b in readiness["blockers"]}
    try:
        if any(q["status"] == "OPEN" and q["severity"] == "BLOCKING" for q in result["questions"]):
            result["state"] = "WAITING_ANSWERS"
        elif assessment_current(engine, mission):
            if result["prerequisites"]:
                result["state"] = "WAITING_PREREQUISITES"
            elif not _source_current(engine, feature, "checklist"):
                result["state"] = "NEEDS_CHECKLIST"
            elif blocker_codes & {"CHECKLIST_CRITICAL_FINDINGS", "CHECKLIST_FINDINGS_MISSING"}:
                result["state"] = "CHECKLIST_BLOCKED"
                first = next(b for b in readiness["blockers"] if b["code"] in {"CHECKLIST_CRITICAL_FINDINGS", "CHECKLIST_FINDINGS_MISSING"})
                result["blocker"] = first["message"]
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


def _update_lease_step(engine, token: str, step: str) -> None:
    raw = engine.db.meta_get("mission.lease")
    if not raw:
        return
    try:
        lease = json.loads(raw)
    except ValueError:
        return
    if lease.get("token") == token:
        lease["step"] = step
        engine.db.meta_set("mission.lease", json.dumps(lease, ensure_ascii=False))


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
                _update_lease_step(engine, token, "intake")
                assessment = await triage.assess(engine, mission, actor=actor)
                mission["assessment_checkpoint"] = {"input": key, "result": assessment.model_dump()}
                _store(engine, mission)
            route = triage.recommendation(assessment, triage.source_origin(engine))
            if not service.enabled(engine):
                _choose_process(engine, route)
                service.enable(engine, route["available_preset"], actor=actor)
            lock, _ = assets.load_assets(engine)
            if not service.latest(engine, "project", "constitution"):
                _update_lease_step(engine, token, "constitution")
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
            _update_lease_step(engine, token, "checklist")
            await adapter.run_step(engine, feature, "checklist", lock["preset_id"], instructions=instructions, actor=actor)
        check = service.latest(engine, feature, "checklist")
        raw_findings = engine.db.meta_get(f"speckit.analysis:{check['rev_id']}")
        if raw_findings is None:
            raise ValueError("规格质量检查缺少发现元数据，不能视为无问题")
        findings = json.loads(raw_findings)
        if any(f["severity"] == "CRITICAL" for f in findings):
            raise ValueError("规格质量检查发现严重问题，请在高级规格修订中修正，再继续")
        if not is_gate_passed(engine.db, "srs_approval", "project", "project"):
            return
        for kind in ("plan", "tasks", "analyze"):
            if not _source_current(engine, feature, kind):
                _update_lease_step(engine, token, kind)
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
