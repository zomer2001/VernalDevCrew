"""Resumable intake checkpoints. Approvals and execution remain owned by existing engines."""
from __future__ import annotations

import json
import re

from autodev.db import now_iso
from autodev.process.runner import is_gate_passed, load_builtin_template, load_template, save_custom_template
from autodev.loopforge.protocol import FailureCategory
from . import assets, compiler, service, triage
from .paths import feature_spec_dir

INTENTS = {"new_system", "feature", "bugfix", "refactor", "documentation"}


def get(engine, feature: str) -> dict | None:
    feature_spec_dir(feature)
    return json.loads(engine.db.meta_get(f"mission:{feature}") or "null")


def _store(engine, mission):
    engine.db.meta_set(f"mission:{mission['feature_id']}", json.dumps(mission, ensure_ascii=False))


def record_progress(engine, feature, run_id, display, category=None, report=""):
    with engine.db.transaction():
        mission = get(engine, feature)
        facts = dict(run_id=run_id, native_display=display, failure_category=category, last_error=report)
        if mission and any(mission.get(key) != value for key, value in facts.items()):
            mission.update(facts)
            _store(engine, mission)


def preserve_confirmations(original: str, proposal: str) -> str:
    """User answers and attestation remain exact text even when the PM reconciles requirements."""
    pattern = r"^## ((?:澄清 |目标补充 |前置条件核验 )[^\n]*|交付前置条件|交付后人工体验项)\n.*?(?=^## |\Z)"
    sections = [m[0] for m in re.finditer(pattern, original, re.M | re.S)]
    proposal = re.sub(pattern, "", proposal, flags=re.M | re.S)
    return proposal.rstrip() + "\n\n" + "\n".join(sections)


def all_missions(engine) -> list[dict]:
    return [json.loads(row["value"]) for row in engine.db._query("SELECT value FROM meta WHERE key LIKE 'mission:%' ORDER BY key")]


def assert_idle(engine):
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
                   "intent": intent, "source_origin": triage.source_origin(engine), "updated_at": now_iso(), "last_error": "",
                   "native_display": None, "failure_category": None}
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

    active_dispatches = engine.db._query(
        "SELECT task_id, command_key FROM dispatch_records WHERE state IN ('DISPATCHING','QUEUED','RUNNING','NEEDS_CONFIRMATION')"
    )
    if active_dispatches:
        cmds = " ".join(f"{r['task_id'] or ''} {r['command_key'] or ''}" for r in active_dispatches)
        if "checklist" in cmds or "std_spec" in cmds:
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
        if refs := mission.get("lite_revisions"):
            for kind, rev_id in refs.items():
                current = service.latest(engine, fid, kind)
                try:
                    valid = current and current["rev_id"] == rev_id and _source_current(engine, fid, kind)
                except (OSError, ValueError):
                    valid = False
                if not valid:
                    add("LITE_BUNDLE_STALE", f"功能 {fid} 的 {kind} 已变化，请重新生成轻量规划组合", fid)
            continue
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


def _lite_process():
    template = service.specification_template(load_builtin_template("quick-prototype"))
    # The single SRS decision covers the plan; keep independent CI and candidate signoff.
    for stage in template.stages:
        stage.gates = [g for g in stage.gates if g.id != "architecture_approval"]
    template.review_policy = "milestone"
    return template


def is_custom_process(template) -> bool:
    if template.id not in ("legacy", "standard", "quick-prototype", "feature", "bugfix"):
        return True
    builtin = load_builtin_template(template.id)
    if template.id == "quick-prototype" and template == _lite_process():
        return False
    return template != builtin and template != service.specification_template(builtin)


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
    actual_template = load_template(engine.ws, engine.settings)
    actual_process = actual_template.id
    aligned = (
        (not recommended_preset or actual_preset == recommended_preset)
        and (not recommended_process or actual_process == recommended_process)
    )
    has_tasks = bool(engine.db.list_tasks())
    custom_process = is_custom_process(actual_template)
    mismatch_reason = ""
    if not aligned:
        if has_tasks:
            mismatch_reason = (
                f"当前评估推荐 {recommended_preset}/{recommended_process}，但项目已锁定 {actual_preset or '未启用'}/{actual_process} 且已有研发任务，不能原地转换配置。"
            )
        elif custom_process:
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
        and not custom_process
        and not mission.get("active_dispatch_id")
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
    process_path = engine.ws.state_dir / "process.yaml"
    with engine.db.transaction():
        previous_file = process_path.read_bytes() if process_path.exists() else None
        previous_setting = engine.settings.process_template
        try:
            assert_idle(engine)
            if engine.db.list_tasks():
                raise service.SpecConflictError("已有已发布研发任务，不能转换项目预设或流程模板")
            mission = get(engine, feature)
            if not mission or mission["revision"] != expected_revision:
                raise service.SpecConflictError("需求目标修订已变化，请刷新后再转换配置")
            if not service.enabled(engine):
                raise ValueError("请先启用功能规格流程")
            current_tpl = load_template(engine.ws, engine.settings)
            if is_custom_process(current_tpl):
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
            invalidated_kinds = ["plan", "tasks", "analyze"]
            if upgrade_res["changed"]:
                invalidated_kinds.insert(0, "checklist")
            placeholders = ",".join("?" for _ in invalidated_kinds)
            engine.db._exec(
                f"UPDATE spec_revisions SET status='STALE' WHERE source_kind IN ({placeholders})",
                tuple(invalidated_kinds),
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
        except BaseException:
            engine.settings.process_template = previous_setting
            if previous_file is None:
                process_path.unlink(missing_ok=True)
            elif not process_path.exists() or process_path.read_bytes() != previous_file:
                process_path.write_bytes(previous_file)
            raise
    return {"mission": view(engine, mission), "conversion": entry}


def view(engine, mission) -> dict:
    feature = mission["feature_id"]
    rev = service.clarification_source(engine, feature)
    result = {**mission, "spec_rev_id": (rev or {}).get("rev_id", 0), "spec_rev_no": (rev or {}).get("rev_no", 0),
              "questions": service.list_clarifications(engine, feature), "state": "NEEDS_ASSESSMENT"}
    result["prerequisites"] = [p for p in mission.get("prerequisites", []) if not mission.get("prerequisite_evidence", {}).get(p["key"])]
    readiness = approval_readiness(engine, feature_id=feature)
    result["approval_blockers"] = readiness["blockers"]
    result["quality_review"] = service.quality_review(engine, feature) if service.enabled(engine) else None
    result["config_alignment"] = config_alignment(engine, mission)
    result["repair"] = {}
    blocker_codes = {b["code"] for b in readiness["blockers"]}
    try:
        lite = mission.get("lite_mode") in ("READY_FOR_APPROVAL", "NEEDS_CLARIFICATION")
        previous_lite = mission.get("route", {}).get("clarity_level") == "READY_LITE" and not lite
        if any(q["status"] == "OPEN" and q["severity"] == "BLOCKING" for q in result["questions"]):
            result["state"] = "WAITING_ANSWERS"
        elif previous_lite:
            frozen = [t for t in engine.db.list_tasks() if t.feature_id == feature]
            result["state"] = "PUBLISHED" if frozen else "NEEDS_ASSESSMENT"
        elif assessment_current(engine, mission):
            if result["prerequisites"]:
                result["state"] = "WAITING_PREREQUISITES"
            elif lite:
                if mission["lite_mode"] == "READY_FOR_APPROVAL":
                    if blocker_codes - {"MISSION_RUNNING"}:
                        result["state"] = "REVIEW_BLOCKED"
                        result["blocker"] = readiness["blockers"][0]["message"]
                    elif is_gate_passed(engine.db, "srs_approval", "project", "project"):
                        packages = compiler.compile_tasks(engine, feature)
                        result["packages"] = [p.model_dump() for p in packages]
                        result["state"] = "PUBLISHED" if all(engine.db.get_task(p.task.task_id) for p in packages) else "WAITING_APPROVAL"
                    else:
                        result["state"] = "WAITING_APPROVAL"
            elif not _source_current(engine, feature, "checklist"):
                result["state"] = "NEEDS_CHECKLIST"
            elif blocker_codes & {"CHECKLIST_CRITICAL_FINDINGS", "CHECKLIST_FINDINGS_MISSING"}:
                result["state"] = "CHECKLIST_BLOCKED"
                first = next(b for b in readiness["blockers"] if b["code"] in {"CHECKLIST_CRITICAL_FINDINGS", "CHECKLIST_FINDINGS_MISSING"})
                result["blocker"] = first["message"]
            elif not is_gate_passed(engine.db, "srs_approval", "project", "project"):
                result["state"] = "WAITING_APPROVAL"
            elif not all(_source_current(engine, feature, kind) for kind in ("plan", "tasks")):
                result["state"] = "NEEDS_PLAN"
            else:
                compiler.compile_tasks(engine, feature, require_analysis=False)
                if not _source_current(engine, feature, "analyze"):
                    result["state"] = "NEEDS_PLAN"
                else:
                    packages = compiler.compile_tasks(engine, feature)
                    result["packages"] = [p.model_dump() for p in packages]
                    result["state"] = "PUBLISHED" if all(engine.db.get_task(p.task.task_id) for p in packages) else "READY_TO_PUBLISH"
    except (ValueError, OSError) as exc:
        result["blocker"] = str(exc)
        result["state"] = "REVIEW_BLOCKED"
    if mission.get("active_dispatch_id"):
        result["state_before_running"] = result["state"]
        result["state"] = "RUNNING"
    result["display_state"] = mission.get("native_display") or "WAITING_DECISION"
    result["failure_category"] = mission.get("failure_category") or (
        FailureCategory.USER_DECISION_REQUIRED if result["display_state"] == "WAITING_DECISION" else None)
    if result["display_state"] == "REWORK_EXHAUSTED":
        result["state"] = "REWORK_STOPPED"
        result["blocker"] = mission.get("last_error", "")
    elif result["display_state"] == "EXECUTION_FAILED":
        result["state"] = "REVIEW_BLOCKED"
        result["blocker"] = mission.get("last_error", "")
    return result


def _source_current(engine, feature, kind):
    rev = service.latest(engine, feature, kind)
    if not rev:
        return False
    service.assert_source_current(engine, rev)
    return service.revision_inputs_current(engine, rev)


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


def decide_and_freeze(engine, record_id, decision, **options):
    from autodev.process.runner import decide_gate
    with engine.db.transaction():
        result = decide_gate(engine.db, record_id, decision, **options)
        if result["gate_id"] == "srs_approval" and result["status"] in ("PASSED", "WAIVED"):
            for mission in all_missions(engine):
                refs = mission.get("lite_revisions")
                if not refs:
                    continue
                feature = mission["feature_id"]
                existing = [t for t in engine.db.list_tasks() if t.feature_id == feature]
                if existing:
                    continue
                frozen = compiler.publish_tasks(engine, feature, expected_hashes=None, actor=options["actor"])
                engine.db._exec("UPDATE spec_revisions SET status='APPROVED',approved_gate_id=? WHERE rev_id IN (?,?,?)",
                                (record_id, refs["spec"], refs["plan"], refs["tasks"]))
                result.setdefault("frozen_task_ids", []).extend(frozen)
        return result


def _lite_candidate(engine, mission):
    if mission.get("lite_mode") == "ESCALATE_STANDARD":
        return False
    process = load_template(engine.ws, engine.settings)
    if is_custom_process(process) or process.id not in ("legacy", "quick-prototype"):
        return False
    if service.enabled(engine):
        return assets.load_assets(engine)[0]["preset_id"] == "vernal-lite"
    return True


def planning_inputs(engine, mission):
    feature = mission["feature_id"]
    inputs = [triage.input_key(engine, mission), service.work_package_boundaries(engine, ())]
    for kind in ("intake", "checklist", "plan", "tasks", "analyze"):
        rev = service.latest(engine, feature, kind)
        if rev:
            service.assert_source_current(engine, rev)
        inputs.append(rev["rev_id"] if rev else 0)
    inputs.extend([compiler.contract_hash(engine), load_template(engine.ws, engine.settings).model_dump(),
                   is_gate_passed(engine.db, "srs_approval", "project", "project")])
    return assets.digest_json(inputs)


def _accept_lite_bundle(engine, mission, bundle, actor):
    feature = mission["feature_id"]
    origin = triage.source_origin(engine)
    mission.update(lite_mode=bundle.mode, assessment={"risk":bundle.risk, "rationale":bundle.rationale},
                   lite_revisions={})
    if bundle.mode == "ESCALATE_STANDARD":
        mission["route"] = {"clarity_level":"READY_STANDARD", "source_origin":origin,
                            "available_preset":"vernal-core", "process_template":"standard"}
        mission.pop("assessed_input", None)
        _store(engine, mission)
        return
    ready = bundle.mode == "READY_FOR_APPROVAL"
    route = dict(clarity_level="READY_LITE" if ready else "NEEDS_CLARIFICATION", source_origin=origin,
                 recommended_preset="vernal-lite", available_preset="vernal-lite", process_template="quick-prototype")
    mission["route"] = route
    old = service.clarification_source(engine, feature)
    used = {q["question_key"] for q in service.list_clarifications(engine, feature)}
    if any(q.question_key in used for q in bundle.clarifications):
        raise ValueError("不能复用已有的澄清问题编号")
    prior = {p["key"]:p for p in mission.get("prerequisites", [])}
    for p in bundle.prerequisites:
        if p.key in prior and prior[p.key] != p.model_dump():
            raise ValueError("前置条件已变化，请提供新的编号")
        prior[p.key] = p.model_dump()
    mission.update(prerequisites=list(prior.values()), experience_items=[x.model_dump() for x in bundle.experience_items])
    if ready:
        from .parser import parse_spec
        parsed = parse_spec(bundle.spec_markdown, feature)
        if old and old["source_kind"] == "spec":
            former = parse_spec(old["content_text"], feature)
            if {r.req_id for r in former.requirements + former.nfrs} - {r.req_id for r in parsed.requirements + parsed.nfrs}:
                raise ValueError("不能删除既有需求编号")
        if not service.enabled(engine):
            _choose_process(engine, route)
            service.enable(engine, "vernal-lite", actor=actor)
        process = load_template(engine.ws, engine.settings)
        lite_process = _lite_process()
        if process != lite_process:
            save_custom_template(engine.ws, lite_process.model_dump_json())
            engine.settings.process_template = lite_process.id
        lock, _ = assets.load_assets(engine)
        mission.update(actual_preset=lock["preset_id"], actual_process=load_template(engine.ws, engine.settings).id)
        if not service.latest(engine, "project", "constitution"):
            service.publish_revision(engine, "project", "constitution", "# 项目约束\n功能规格登记需求，工作包限定修改范围，交付按冻结的验收条件和验证命令核验。\n",
                                     expected_rev_id=0, author=actor, summary="采用轻量预设的交付约束")
        content = preserve_confirmations(old["content_text"], bundle.spec_markdown) if old else bundle.spec_markdown
        if mission["prerequisites"]:
            content += "\n## 本轮交付前置\n" + "\n".join(p["description"] + "：" + p["evidence_needed"] for p in prior.values())
        for kind, text in (("spec",content), ("plan",bundle.plan_markdown), ("tasks","")):
            previous = service.latest(engine, feature, kind) or {}
            rev = service.publish_revision(engine, feature, kind, text, expected_rev_id=previous.get("rev_id",0),
                author=actor, summary="轻量规划组合待批准", handoff=bundle.handoff_brief,
                work_packages=bundle.work_packages if kind == "tasks" else None)
            mission["lite_revisions"][kind] = rev["rev_id"]
    else:
        if not old:
            old = service.publish_revision(engine, feature, "intake", mission["brief"], expected_rev_id=0,
                                           author=actor, summary="记录澄清所依据的原始目标")
        service.add_clarifications(engine, feature, old["rev_id"], bundle.clarifications, actor=actor)
    mission["assessed_input"] = triage.input_key(engine, mission)
    _store(engine, mission)
    if ready:
        service.project_documents(engine)
        from autodev.process.runner import ensure_gate_record
        ensure_gate_record(engine.db, stage_id="requirements", gate_id="srs_approval", kind="human_approval",
                           scope_type="project", scope_id="project", summary="一次审阅功能规格、技术方案与工作包")



def next_stage(engine, mission):
    feature = mission["feature_id"]
    if any(t.feature_id == feature for t in engine.db.list_tasks()):
        return None
    if any(q["severity"] == "BLOCKING" and q["status"] == "OPEN"
           for q in service.list_clarifications(engine, feature)):
        return None
    if _lite_candidate(engine, mission):
        refs = mission.get("lite_revisions", {})
        if not assessment_current(engine, mission) or not refs or any(
            not _source_current(engine, feature, k) or service.latest(engine, feature, k)["rev_id"] != v
            for k, v in refs.items()
        ):
            return "lite_bundle"
        return None
    if mission.get("lite_mode") == "ESCALATE_STANDARD" and service.enabled(engine):
        if assets.load_assets(engine)[0]["preset_id"] == "vernal-lite":
            raise service.SpecConflictError("请先将已升级的需求显式转换为标准项目")
    if not assessment_current(engine, mission) or not _source_current(engine, feature, "checklist"):
        return "std_spec"
    if not is_gate_passed(engine.db, "srs_approval", "project", "project"):
        return None
    if not all(_source_current(engine, feature, k) for k in ("plan", "tasks", "analyze")):
        return "std_plan"
    return None


def accept_bundle(engine, mission, stage, data, review_data, actor):
    from autodev.models import LitePlanBundle, SpecStepResult, StandardPlanBundle
    from .parser import parse_spec
    from autodev.prompts_style import validate_public_text
    feature = mission["feature_id"]
    if stage == "lite_bundle":
        _accept_lite_bundle(engine, mission, LitePlanBundle.model_validate(data), actor)
        return
    review = SpecStepResult.model_validate(review_data)
    spec_stage = stage == "std_spec"
    kind = "checklist" if spec_stage else "analyze"
    questions = review.clarifications
    findings = [f.model_dump() for f in review.findings]
    if review.step != kind or not review.content.strip() or any(q.severity != "BLOCKING" for q in questions):
        raise ValueError("独立审查的步骤、正文或问题无效")
    criticals = [f for f in review.findings if f.severity == "CRITICAL"]
    if criticals and (not questions or any(f.repair_target != "spec" for f in criticals)):
        raise ValueError("独立审查仍有未解决的工程阻断项")

    def publish(kind, content, handoff, **extra):
        previous = service.latest(engine, feature, kind) or {}
        return service.publish_revision(engine, feature, kind, content, expected_rev_id=previous.get("rev_id", 0),
                                        author=actor, summary="原生组合规划独立审查", handoff=handoff, **extra)

    if spec_stage:
        draft = triage.StandardSpecBundle.model_validate(data)
        for text in (draft.rationale, *draft.handoff_brief.model_dump().values()):
            validate_public_text(text)
        route = triage.recommendation(draft, triage.source_origin(engine))
        if route["clarity_level"] == "READY_LITE":
            route["clarity_level"] = "READY_STANDARD"
        if not service.enabled(engine):
            _choose_process(engine, {"process_template": "standard"})
            service.enable(engine, "vernal-core", actor=actor)
        if not service.latest(engine, feature, "constitution"):
            publish("constitution", draft.constitution_markdown, draft.handoff_brief)
        original = service.clarification_source(engine, feature)
        content = draft.proposed_spec
        if original:
            if original["source_kind"] == "spec":
                old = parse_spec(original["content_text"], feature)
                new = parse_spec(content, feature)
                if {r.req_id for r in old.requirements + old.nfrs} - {r.req_id for r in new.requirements + new.nfrs}:
                    raise ValueError("不能删除既有需求编号")
            content = preserve_confirmations(original["content_text"], content)
        prior = {p["key"]: p for p in mission.get("prerequisites", [])}
        for p in draft.prerequisites:
            if p.key in prior and prior[p.key] != p.model_dump():
                raise ValueError("已记录前置条件不可静默覆盖")
            prior[p.key] = p.model_dump()
        if prior:
            content += "\n## 交付前置条件\n" + "\n".join(p["description"] + "：" + p["evidence_needed"] for p in prior.values())
        if draft.experience_items:
            content += "\n## 交付后人工体验项\n" + "\n".join(p.description for p in draft.experience_items)
        publish("spec", content, draft.handoff_brief)
        questions = [*draft.questions, *questions]
        mission.update(assessment=draft.model_dump(), route=route, prerequisites=list(prior.values()),
                       actual_process=load_template(engine.ws, engine.settings).id,
                       actual_preset=assets.load_assets(engine)[0]["preset_id"],
                       experience_items=[x.model_dump() for x in draft.experience_items])
    else:
        if not is_gate_passed(engine.db, "srs_approval", "project", "project"):
            raise service.SpecConflictError("规格批准已失效")
        draft = StandardPlanBundle.model_validate(data)
        publish("plan", draft.plan_markdown, draft.handoff_brief)
        publish("tasks", "", draft.handoff_brief, work_packages=draft.work_packages)
    publish(kind, review.content, review.handoff_brief, findings=findings)
    if questions:
        used = {q["question_key"] for q in service.list_clarifications(engine, feature)}
        if len(questions) > 3 or any(q.question_key in used for q in questions):
            raise ValueError("问题编号已使用或本轮问题超过三条")
        service.add_clarifications(engine, feature, service.latest(engine, feature)["rev_id"], questions, actor=actor)
    elif not spec_stage:
        compiler.compile_tasks(engine, feature)
    if spec_stage:
        mission["assessed_input"] = triage.input_key(engine, mission)
    _store(engine, mission)
