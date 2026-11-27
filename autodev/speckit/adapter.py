"""Spec Kit steps are ordinary LoopForge planning SOP executions, never a second runner."""
from __future__ import annotations
from contextvars import ContextVar
import asyncio
import json

from autodev.models import SpecStepResult, strict_json_schema
from autodev.process.runner import is_gate_passed
from autodev.prompts_style import COMMUNICATION_STYLE, validate_public_text
from .assets import ASSET_ROOT, digest_json, load_assets, step_assets
from .compiler import compile_tasks, contract_hash
from .paths import source_path
from .service import (SpecConflictError, add_clarifications, assert_no_blocking_questions, assert_source_current,
                      input_fingerprint, revision_inputs_current, latest, list_clarifications, publish_revision, quality_review, work_package_schema)

PLANNING_BASELINE = ContextVar("speckit_planning_baseline", default=None)
STEP_NAMES = {"constitution":"建立项目约束","specify":"整理功能规格","clarify":"澄清需求边界",
              "checklist":"检查规格质量","plan":"制定技术方案","tasks":"拆解实施任务","analyze":"核对交付一致性"}


async def _dispatch_lite(sched, task, before, package):
    from urllib.parse import quote, urlencode
    from autodev import gitops, local_git, public_pool
    from autodev.context_bundle import build_task_input_bundle
    from autodev.loopforge import bootstrap, dispatch
    from autodev.loopforge.bridge import LoopForgeBridge, build_restore_command, format_issue_description, sign_context_sig
    from autodev.loopforge.client import LoopForgeError

    db, settings = sched.db, sched.settings
    bridge = LoopForgeBridge(settings, db)
    opts = dict(stop=sched.stop_event, timeout=settings.agent_timeout_sec)
    rows = db._query("SELECT * FROM dispatch_records WHERE task_id=? AND (lf_run_id=? AND lf_run_id!='' "
                     "OR (exec_seq=? AND attempt_no=?) OR state NOT IN ('FAILED','FINISHED')) ORDER BY dispatch_id DESC LIMIT 1",
                     (task.task_id, task.loopforge_run_id or "", task.exec_seq, task.retry_count + 1))
    if rows:
        receipt = dict(rows[0])
        if receipt["compiled_task_hash"] != package.compiled_task_hash:
            raise SpecConflictError("派单收据与冻结工作包不匹配")
        settings.selected_pool = receipt["pool"]
        client = bridge.client
        if receipt["pool"] == "public_docker":
            if settings.loopforge_url.rstrip("/") != public_pool.server_url() or not public_pool.service_token():
                raise LoopForgeError("恢复公用派单需要原部署的服务身份")
            from autodev.loopforge.client import LoopForgeClient
            client = LoopForgeClient(public_pool.server_url(), public_pool.service_token())
        return receipt, await dispatch.observe_task(db, receipt, client, **opts)
    pool, client = await public_pool.resolve(settings, bridge.client)
    settings.selected_pool = pool
    assets = await bridge.ensure_project_and_assets()
    pid, mode = assets["project_id"], settings.collaboration_mode
    api = public_pool.context_url(settings).rstrip("/")
    managed_git = mode == "git" and bool(db.meta_get("git_delivery_source"))
    restore, source, repository, branch = "", None, "", ""
    if mode == "files":
        path = sched.ws.state_dir / "baselines" / (task.task_id + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(before, ensure_ascii=False), "utf-8")
        sig = sign_context_sig(settings.project_signing_key(), pid, "baseline:" + task.task_id)
        url = f"{api}/vernal-api/tasks/{quote(task.task_id, safe='')}/baseline.json?" + urlencode({"project":pid, "sig":sig})
        restore = build_restore_command(str(path.resolve()), url, save_baseline=True)
    else:
        source, base = sched._prepare_worktree(task)
        task = db.get_task(task.task_id)
        if managed_git:
            mirror = db.meta_get("gitea_mirror") == "1"
            snapshot = await asyncio.to_thread(local_git.prepare_snapshot, source, base, None if mirror else sched.ws.state_dir / "git-snapshots")
            if mirror:
                from autodev import local_git_bridge
                remote = await local_git_bridge.task_remote(source, settings, db, task.task_id, snapshot)
                restore = local_git_bridge.restore_command(remote, snapshot)
            else:
                sig = sign_context_sig(settings.project_signing_key(), pid, "git:" + snapshot)
                restore = local_git.restore_command(api, pid, snapshot, sig)
        else:
            repository, branch = settings.loopforge_repository_id, task.branch_name
            gitops.git(["push", "origin", f"{base}:refs/heads/{branch}"], source)
    wf = bootstrap.build_lite_delivery_workflow(pid, mode, assets["sandbox_id"], restore_cmd=restore,
        verify_cmds=package.task.verify_cmds, local_git=managed_git, upload_url=bootstrap.candidate_upload_url(settings,pid))
    wf = bootstrap.managed_asset(wf)
    prior = next((w for w in await bridge.client.list_workflows(pid) if w["id"] == wf["id"]), {})
    if prior and not bootstrap.managed_clean(prior, wf):
        raise LoopForgeError("轻量交付 SOP 已被手动修改，请核对后派发")
    if not prior or prior.get("description") != wf["description"]:
        await bridge.client.put_workflow({**wf, **{k:prior[k] for k in ("revision","version") if k in prior}})
        await bridge.client.publish_workflow(wf["id"])
    elif prior.get("status") != "published":
        await bridge.client.publish_workflow(wf["id"])
    await public_pool.check_workflow_sandbox(settings, bridge.client, wf["id"], pool)
    if settings.native_approver_user_id:
        client.issue_user_id = settings.native_executor_user_id
        client.approval_user_id = settings.native_approver_user_id
        await bootstrap.ensure_signoff_workflow(bridge.client, pid)
    description = format_issue_description(task, task.squad) + build_task_input_bundle(
        sched.ws, db, task, source_root=source, files=before if mode == "files" else None)
    with db.transaction():
        receipt, fresh = dispatch.reserve(db, task.task_id, task.exec_seq, task.retry_count + 1,
                                          pool, str(settings.native_executor_user_id or "deployment-service"))
        db._exec("UPDATE dispatch_records SET stage='lite_delivery',feature_id=?,expected_baseline_sha=? WHERE dispatch_id=?",
                 (task.feature_id, package.compiled_task_hash, receipt["dispatch_id"]))
        dispatch.bind_baseline(db, receipt, dict(spec_sha=package.spec_sha, contract_sha=package.contract_sha,
                               compiled_task_hash=package.compiled_task_hash, asset_versions=package.asset_versions))
    if fresh:
        db.meta_set(f"signoff-policy:{receipt['dispatch_id']}", json.dumps([settings.native_executor_user_id, settings.native_approver_user_id]))
        try:
            if sched.stop_event.is_set():
                dispatch.record(db, receipt, "FAILED")
                raise asyncio.CancelledError
            issue = await client.create_issue(project_id=pid, title=task.title, workflow_id=wf["id"],
                description=description, repository_id=repository, base_branch=branch)
            dispatch.record(db, receipt, "DISPATCHING", issue_id=issue["id"])
            if sched.stop_event.is_set():
                dispatch.record(db, receipt, "FAILED")
                raise asyncio.CancelledError
            native = await client.dispatch_issue(issue["id"])
            run = await dispatch.accept_task(db, receipt, client, native, **opts)
        except BaseException:
            if receipt["state"] == "DISPATCHING":
                dispatch.record(db, receipt, "NEEDS_CONFIRMATION")
            raise
    else:
        run = await dispatch.observe_task(db, receipt, client, **opts)
    return receipt, run


async def _deliver_native(sched, task, exchange, before, package, bound):
    db, settings = sched.db, sched.settings
    receipt, run = await _dispatch_lite(sched, task, before, package)
    if not db.update_task_if_owned(task.task_id, expected_exec_seq=task.exec_seq,
                                   loopforge_issue_id=receipt["issue_id"], loopforge_run_id=run):
        raise SpecConflictError("工作包已由其他执行接管")
    db.meta_set(f"pool.run:{run}", receipt["pool"])
    if bound is not None and not bound.done():
        bound.set_result(run)
    # Reuse the bound Run's ordinary evidence, diff and signoff path. No queue scan or second dispatch.
    protected = [] if task.role in ("contract","bootstrap") else settings.protected_paths
    await sched._execute_remote(db.get_task(task.task_id), task.squad, "slot-1", exchange, before, protected, exec_seq=task.exec_seq)


async def deliver_lite_package(sched, task_id, *, bound=None):
    from copy import copy
    from autodev.artifacts import FileExchange
    from autodev.models import ProjectStatus
    from autodev.scheduler import _FileLock
    from .compiler import assert_package_current
    db = sched.db
    sched.settings = copy(sched.settings)
    sched.settings.require_merge_approval = True
    sched.settings.director_max_arbitrations = 0  # Native rejects own rework for this single Run.
    with _FileLock(sched.ws.lock_path):
        seq = None
        delivery_failed = False
        interrupted = False
        try:
            task = db.get_task(task_id)
            package = assert_package_current(db, task)
            sched.check_runtime()
            if task.status == "APPROVED":
                milestone = db.get_milestone(task.milestone_id)
                if milestone.status == "PENDING" and all(t.status in ("APPROVED","MERGED","CANCELLED")
                        for t in db.list_tasks(milestone_id=task.milestone_id)):
                    await sched._integrate_milestone(milestone)
                return
            if task.status in ("FUSED","AWAITING_HUMAN_REVIEW", "MERGED", "CANCELLED"):
                return
            if task.depends_on:
                raise SpecConflictError("轻量直通需要一个可独立派发的冻结工作包")
            if task.status in ("BLOCKED", "READY") and not task.loopforge_run_id and sched.settings.token_budget_limit:
                spent = sum(u["input"] + u["output"] + u["reasoning"] for u in db.usage_by_task().values())
                if spent >= sched.settings.token_budget_limit:
                    raise SpecConflictError("项目 Token 预算已用尽，轻量派单已暂停")
            db.ensure_slots(sched.settings.max_concurrency)
            with db.transaction():
                if task.status in ("BLOCKED","READY"):
                    db.update_task(task_id, status="READY")
                    seq = db.claim_task_execution(task_id, allow_from_ready=True)
                else:
                    seq = task.exec_seq
                task = db.get_owned_task(task_id, expected_exec_seq=seq)
                sched.engine.set_status(ProjectStatus.RUNNING)
                db.update_slot("slot-1", status="WORKING", task_id=task_id, current_action="轻量交付")
            exchange = FileExchange(sched.ws, db)
            before = {}
            if sched.settings.collaboration_mode == "files":
                bound_receipt = db._query("SELECT 1 FROM dispatch_records WHERE task_id=? AND stage='lite_delivery' "
                                          "AND expected_baseline_sha=? AND state IN ('QUEUED','RUNNING')",
                                          (task_id, package.compiled_task_hash))
                if bound_receipt:
                    before = json.loads((sched.ws.state_dir / "baselines" / (task_id + ".json")).read_text("utf-8"))
                else:
                    before = exchange.baseline([])
            await _deliver_native(sched, task, exchange, before, package, bound)
            current = db.get_task(task_id)
            if current and current.exec_seq == seq and current.status == "APPROVED" and all(t.status in ("APPROVED","MERGED","CANCELLED")
                   for t in db.list_tasks(milestone_id=task.milestone_id)):
                await sched._integrate_milestone(db.get_milestone(task.milestone_id))
        except (Exception, asyncio.CancelledError) as exc:
            delivery_failed = True
            interrupted = isinstance(exc, asyncio.CancelledError) and asyncio.current_task().cancelling() > 0
            if seq is not None:
                paused = isinstance(exc, asyncio.CancelledError) or sched.stop_event.is_set()
                db.update_task_if_owned(task_id, expected_exec_seq=seq, status="READY" if paused else "FUSED",
                                        failure_reason=str(exc))
            db.add_event("task.lite_delivery_failed", str(exc) or "交付已暂停", task_id=task_id)
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            with db.transaction():
                if seq is not None:
                    db.release_slot("slot-1")
                final_status = ProjectStatus.STALLED
                tasks = db.list_tasks()
                current = next((t for t in tasks if t.task_id == task_id), None)
                if tasks and all(t.status in ("MERGED", "CANCELLED") for t in tasks):
                    final_status = ProjectStatus.COMPLETED
                elif current and not delivery_failed and not sched.stop_event.is_set():
                    milestone = db.get_milestone(current.milestone_id)
                    if current.status in ("APPROVED", "AWAITING_HUMAN_REVIEW") or (
                        milestone and milestone.status == "AWAITING_MERGE"
                    ):
                        final_status = ProjectStatus.APPROVED
                from autodev.loopforge import dispatch
                from autodev.loopforge.protocol import FailureCategory
                from . import missions
                rows = db._query("SELECT * FROM dispatch_records WHERE task_id=? AND stage='lite_delivery' "
                                 "AND state IN ('RUNNING','QUEUED')", (task_id,))
                for row in rows:
                    if interrupted:
                        final_status = ProjectStatus.RUNNING
                    elif not sched.stop_event.is_set():
                        ok = final_status in (ProjectStatus.APPROVED, ProjectStatus.COMPLETED)
                        dispatch.record(db, dict(row), "FINISHED" if ok else "FAILED")
                        mission = missions.get(sched.engine, row["feature_id"])
                        if ok:
                            missions.record_progress(sched.engine, row["feature_id"], row["lf_run_id"], "WAITING_DECISION",
                                                     FailureCategory.USER_DECISION_REQUIRED)
                        elif mission and mission.get("native_display") not in ("REWORK_EXHAUSTED", "EXECUTION_FAILED"):
                            missions.record_progress(sched.engine, row["feature_id"], row["lf_run_id"], "EXECUTION_FAILED",
                                                     FailureCategory.ENV_OR_CONFLICT_BLOCKED,
                                                     current.failure_reason or current.last_feedback or "请核对轻量交付执行记录")
                        db.add_event("dispatch.reconciled", "轻量交付已核对候选结果", task_id=task_id,
                                     payload={"run_id": row["lf_run_id"], "accepted": ok})
                sched.engine.set_status(final_status)


def planning_request(engine, mission, stage):
    from autodev.models import LitePlanBundle, StandardPlanBundle
    from autodev.speckit import service, triage
    feature = mission["feature_id"]
    context = service.clarification_source(engine, feature) or {}
    _, root = load_assets(engine) if service.enabled(engine) else (None, ASSET_ROOT)
    core = json.loads((root / "presets/vernal-core.json").read_text("utf-8"))
    from autodev.prompts_style import _INTERNAL
    model, role, steps = {
        "lite_bundle": (LitePlanBundle, "director", ("specify", "plan", "tasks")),
        "std_spec": (triage.StandardSpecBundle, "pm", ("constitution", "specify", "checklist")),
        "std_plan": (StandardPlanBundle, "director", ("plan", "tasks", "analyze")),
    }[stage]
    schema = {**strict_json_schema(model), "x-vernal-step": stage}
    if stage == "std_plan":
        schema.update(work_package_schema(engine, feature))
        quality = quality_review(engine, feature)
        schema["x-vernal-require-findings-ids"] = [f["id"] for f in quality["findings"]]
    else:
        quality = None
        schema.update(service.work_package_boundaries(engine))
    if stage == "lite_bundle":
        schema["x-vernal-public-pattern"] = _INTERNAL.pattern
        instruction = ("一次判断目标、范围、验收与影响面。未决承诺返回一至三个阻塞问题，"
                       "超出轻量范围说明理由并升级。清晰的低风险需求返回完整规格、四章方案和一至两个工作包。")
    else:
        review = strict_json_schema(SpecStepResult)
        review["properties"]["step"]["enum"] = ["checklist" if stage == "std_spec" else "analyze"]
        review["properties"]["work_packages"]["maxItems"] = 0
        review["properties"]["clarifications"]["maxItems"] = 3
        schema["x-vernal-review-schema"] = review
        instruction = ("给出完整功能规格与项目宪章，保留用户逐字确认和既有需求编号。"
                       if stage == "std_spec" else
                       "依据已批准的规格产出四章详细方案与结构化工作包。检查发现处理章节逐条覆盖质量发现编号。")
    prompt = "\n".join([COMMUNICATION_STYLE, instruction, core["instructions"], *(core["steps"][s] for s in steps),
        json.dumps({"goal":mission["brief"], "intent":mission["intent"], "source":triage.source_origin(engine),
                    "existing":context.get("content_text",""), "questions":service.list_clarifications(engine, feature),
                    "prerequisites":mission.get("prerequisites", []), "evidence":mission.get("prerequisite_evidence", {}),
                    "constitution":service.constitution_rule(engine), "quality":quality,
                    "documents":[{"id": s.slice_id, "same_as_goal": True} if s.content.strip() == mission["brief"].strip()
                                 else {"id": s.slice_id, "content": s.content} for s in engine.db.list_slices()]}, ensure_ascii=False)])
    return prompt, schema, role


def _format_quality_context(qr: dict, checklist_text: str) -> str:
    lines = [
        f"## 已确认来源：checklist（第 {qr['checklist_rev_no']} 版 · 修订 #{qr['checklist_rev_id']}）",
        checklist_text.strip(),
        "",
        "### 当前结构化质量发现（只读原始记录，含稳定发现标识）",
    ]
    if not qr["findings"]:
        lines.append("- 当前质量检查无未结发现（findings=[]）。")
    else:
        lines.append("| 发现标识 | 严重级别 | 分类 | 位置 | 关联需求 | 说明 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for f in qr["findings"]:
            reqs = ", ".join(f.get("requirement_ids") or []) or "-"
            msg = str(f.get("message") or "").replace("\n", " ").replace("|", "/")
            tgt = str(f.get("target") or "").replace("\n", " ").replace("|", "/")
            lines.append(f"| {f['id']} | {f['severity']} | {f['category']} | {tgt} | {reqs} | {msg} |")
    return "\n".join(lines)


async def run_step(engine, feature_id: str, step_name: str, preset_id: str = "vernal-core", *,
                   instructions: str = "", actor: str = "owner") -> dict:
    from autodev.harnesses.loopforge_harness import LoopForgeHarness
    if not isinstance(engine.harness, LoopForgeHarness):
        raise ValueError("功能规格步骤须使用 LoopForge 规划执行链路，请先绑定执行池")
    if step_name not in STEP_NAMES:
        raise ValueError("不支持的规格步骤")
    assets = step_assets(engine,step_name)
    if assets["versions"]["preset_id"] != preset_id:
        raise ValueError("请求预设与项目锁定预设不同")
    kind = "spec" if step_name in ("specify","clarify") else step_name
    source_path(feature_id,kind)
    if step_name != "constitution" and not latest(engine,"project","constitution"):
        raise ValueError("请先建立项目宪章")
    if step_name in ("plan","tasks","analyze"):
        assert_no_blocking_questions(engine,feature_id)
        if not is_gate_passed(engine.db,"srs_approval","project","project"):
            raise SpecConflictError("请先批准当前功能规格，再生成技术方案和任务")
    needed = {"constitution":[],"specify":["constitution"],"clarify":["constitution","spec"],
              "checklist":["constitution","spec"],"plan":["constitution","spec"],
              "tasks":["constitution","spec","plan"],"analyze":["constitution","spec","plan","tasks"]}[step_name]
    context = []
    for dep in needed:
        rev = latest(engine,feature_id,dep)
        if not rev:
            raise ValueError(f"请先完成 {dep} 步骤")
        assert_source_current(engine,rev)
        if not revision_inputs_current(engine, rev):
            raise SpecConflictError(f"{dep} 基于旧来源，请先重新确认对应修订")
        context.append(f"## 已确认来源：{dep}\n{rev['content_text']}")
    qr = None
    checklist_rev = latest(engine, feature_id, "checklist")
    has_mission = engine.db.meta_get(f"mission:{feature_id}") is not None
    if step_name in ("plan", "tasks", "analyze") and (checklist_rev or has_mission):
        qr = quality_review(engine, feature_id)
        if qr["status"] == "MISSING":
            raise SpecConflictError("请先完成当前版本规格的质量检查，再进入后续方案或一致性步骤")
        if qr["status"] == "MISSING_METADATA":
            raise SpecConflictError("质量检查缺少结构化发现记录，不能视为无问题，请重新执行质量检查")
        if not qr["is_current"]:
            raise SpecConflictError("质量检查基于旧版本规格，请重新执行质量检查后再继续")
        if qr["counts"]["CRITICAL"] > 0:
            raise SpecConflictError("质量检查存在严重问题（CRITICAL），请先修订规格并重新检查")
        if step_name in ("plan", "analyze") and checklist_rev:
            context.append(_format_quality_context(qr, checklist_rev["content_text"]))
    if step_name == "analyze":
        compile_tasks(engine,feature_id,require_analysis=False)
    current = latest(engine,feature_id,kind)
    if current and kind not in needed:
        context.append("## 当前待修订正文\n" + current["content_text"])
    questions=list_clarifications(engine,feature_id)
    if questions:
        context.append("## 已记录问题，不重复提问；新问题使用未占用编号\n" + "\n".join(
            f"{q['question_key']}：{q['question']}；状态 {q['status']}；回答 {q['answer'] or '尚未回答'}" for q in questions))
    expected = current["rev_id"] if current else 0
    fingerprint = input_fingerprint(engine,feature_id,kind)
    role = "pm" if step_name in ("specify","clarify") else "qa_lead" if step_name in ("checklist","analyze") else "director"
    model = engine.settings.model_for(role)
    executor = engine.settings.harness_for(role)
    if executor in ("dsh","deepseek","deepseek-harness") and not model:
        raise ValueError("DeepSeek 执行器需要明确配置模型，不能依赖默认模型推测")
    signature = digest_json([feature_id,step_name,expected,fingerprint,assets["versions"],assets["prompt_template_version"],instructions])[:20]
    schema_path = engine.ws.state_dir / "schemas" / f"speckit_{step_name}_{feature_id}_{signature}.schema.json"
    schema_path.parent.mkdir(parents=True,exist_ok=True)
    schema = strict_json_schema(SpecStepResult)
    schema["properties"]["step"]["enum"] = [step_name]
    schema["properties"]["clarifications"]["maxItems"] = 3 if step_name == "clarify" else 0
    if step_name != "tasks":
        schema["properties"]["content"]["pattern"] = r"^\s*$" if step_name == "clarify" else r"\S"
    schema["properties"]["work_packages"]["minItems" if step_name == "tasks" else "maxItems"] = 1 if step_name == "tasks" else 0
    schema["x-vernal-step"] = step_name
    if step_name == "tasks":
        schema.update(work_package_schema(engine, feature_id))
        # Old pinned presets remain readable, but their old output instructions cannot override the v2 schema.
        _, locked_root = load_assets(engine)
        old_instruction = json.loads((locked_root / "presets/vernal-core.json").read_text("utf-8"))["steps"]["tasks"]
        instruction = json.loads((ASSET_ROOT / "presets/vernal-core.json").read_text("utf-8"))["steps"]["tasks"]
        assets["instructions"] = assets["instructions"].replace(old_instruction, instruction)
    schema["x-vernal-artifact-path"] = source_path(feature_id,kind) if step_name != "clarify" else ""
    if step_name == "plan" and qr and qr["findings"]:
        schema["x-vernal-require-findings-ids"] = [f["id"] for f in qr["findings"]]
    schema_path.write_text(json.dumps(schema,ensure_ascii=False,indent=2),"utf-8")
    findings_guidance = ""
    if step_name == "plan" and qr and qr["findings"]:
        findings_guidance = (
            "\n质量发现处理要求（必须遵守）：当前质量检查共有 "
            f"{len(qr['findings'])} 条发现，方案正文必须包含「## 检查发现处理」章节，用 Markdown 表格逐条列出："
            "发现标识 | 处理位置 | 验证方式 | 是否需规格变更。每条必须覆盖原始发现标识（CHK-...）；"
            "纯工程实现细节可在方案中落实，涉及改变业务承诺或未确认范围的必须标注「是」并退回规格修订重新审批，不得替负责人自行承诺。"
        )
    elif step_name == "analyze" and qr and qr["findings"]:
        findings_guidance = (
            "\n质量发现核验要求（必须遵守）：必须逐条对照当前质量检查的每一条发现标识（CHK-...），"
            "核实规格、方案、接口、任务和验证命令是否真正落实，不能仅凭方案写有「已处理」就放行；"
            "若仍存在影响业务承诺的未决项或未映射的适用 NFR，必须写入 findings。"
        )
    if step_name in ("checklist", "analyze"):
        findings_guidance += (
            "\n当前处于编码前规划阶段。检查已批准需求是否有完整的实现与可执行验证设计。"
            "产品功能测试尚未运行属于后续验收阶段，不得仅因此阻断开发任务发布。"
            "每条 CRITICAL 必须指定 repair_target：方案缺口为 plan，任务或验证命令缺口为 tasks，"
            "改变已确认范围、费用、兼容承诺或缺少用户必要决策才为 spec。"
            "在既定需求内选择错误提示、失败回滚和数据保护方式属于工程实现，交给 plan 落实。"
            "修复只调整目标文档，保留已批准需求与验收要求；原问题已解决时从本轮 findings 移除。"
        )
    if step_name == "checklist":
        findings_guidance += (
            "\n规格质量检查时，可在技术方案中落实的工程细节列为 WARNING 并指定 plan 或 tasks。"
            "CRITICAL 用于规格矛盾或缺少必要用户决策，并指定 spec。"
        )
    # Deduplicate user slices against instructions and already-embedded context while preserving all unique materials.
    seen_texts = {instructions.strip()}
    user_slices: list[str] = []
    for s in engine.db.list_slices():
        if s.slice_id == "PRD-S01":
            continue
        cleaned = s.content.strip()
        if not cleaned or cleaned in seen_texts:
            continue
        seen_texts.add(cleaned)
        user_slices.append(f"[{s.slice_id}] {cleaned}")
    # Platform-owned concise step methodology replaces 10-38KB raw upstream English CLI boilerplate,
    # keeping all verified specs, clarifications, quality findings, and mandatory principles lossless.
    prompt = (f"本次任务：{STEP_NAMES[step_name]}。\n" + COMMUNICATION_STYLE + "\n"
              + assets["methodology"] + "\n"
              + "平台已接管上游模板中的分支、脚本、扩展、交互提问与步骤切换，仅按下列项目格式约束和 `.vernal-schema.json` 输出结构化产物；"
              + "下游确定性校验节点会自动执行完整 Schema 校验，不要搜索或安装 `ajv`/`jsonschema` 等外部包。\n"
              + "本项目格式与范围约束（优先遵循）：\n" + assets["instructions"]
              + findings_guidance
              + "\n" + "\n".join(context)
              + (("\n用户输入资料：\n" + "\n".join(user_slices)) if user_slices else "")
              + (("\n本次补充：\n" + instructions) if instructions.strip() else "")
              + f"\n仅产出当前步骤 {step_name} 的结果，不执行下一步骤。"
              + ("澄清步骤的 content 必须为空字符串，clarifications 至多三个问题。"
                 if step_name == "clarify" else
                 "work_packages 是唯一任务真源，content 可为空；clarifications 必须为空数组。"
                 if step_name == "tasks" else
                 "content 放完整非空 Markdown 正文；clarifications 必须为空数组，本步骤不得发布澄清问题。未决事项可在正文中标记待确认，不替用户作答。")
              + "findings 无内容时用空数组。不得修改已确认输入。")
    mission = json.loads(engine.db.meta_get(f"mission:{feature_id}") or "{}")
    risk = (mission.get("assessment") or {}).get("risk", "")
    baseline = {"feature_id": feature_id, "step": step_name, "risk": risk, "spec_sha":(latest(engine,feature_id) or {}).get("content_sha",""),
                "checklist_sha": (qr or {}).get("checklist_sha", ""),
                "checklist_findings_sha": (qr or {}).get("findings_sha", ""),
                "contract_sha":contract_hash(engine), "compiled_task_hash":"",
                "asset_versions":{**assets["versions"], "prompt_template_version": assets["prompt_template_version"]}}
    token = PLANNING_BASELINE.set(baseline)
    log_dir = engine.ws.runs_dir / "speckit" / feature_id / step_name / signature
    run_id = engine.db.start_run(f"_speckit_{feature_id}_{step_name}",1,"SPEC_KIT",engine.harness.name)
    engine.db.add_event("speckit.step_started",STEP_NAMES[step_name],actor=actor)
    try:
        result = await engine.harness.run_structured_readonly(prompt,schema_path=schema_path,cwd=engine.ws.root,
                                                            log_dir=log_dir,role=role)
    except (Exception, asyncio.CancelledError) as exc:
        engine.db.finish_run(run_id,session_id="",exit_code=1,verdict="ERROR",summary_log=str(exc) or "执行已取消",log_path=str(log_dir))
        engine.db.add_event("speckit.step_failed",str(exc) or "执行已取消",actor=actor)
        raise
    finally:
        PLANNING_BASELINE.reset(token)
    try:
        if not result.ok or not result.structured:
            raise ValueError(result.error or "规格步骤未返回结构化产物")
        parsed = SpecStepResult.model_validate(result.structured)
        if parsed.step != step_name:
            raise ValueError("规格步骤与返回产物不匹配")
        for value in parsed.handoff_brief.model_dump().values():
            validate_public_text(value)
        with engine.db.transaction():
            now = latest(engine,feature_id,kind)
            if (now["rev_id"] if now else 0) != expected or input_fingerprint(engine,feature_id,kind) != fingerprint:
                raise SpecConflictError("生成期间规格已更新，旧结果已拒收")
            if step_name == "clarify":
                if parsed.content.strip() or len(parsed.clarifications)>3:
                    raise ValueError("澄清步骤只返回至多三个问题，不改写规格")
                add_clarifications(engine,feature_id,expected,parsed.clarifications,actor=actor)
                revision = latest(engine,feature_id)
            else:
                if parsed.clarifications:
                    raise ValueError("澄清问题须通过澄清步骤发布")
                findings_payload = [f.model_dump() for f in parsed.findings] if step_name in ("checklist", "analyze") else None
                revision = publish_revision(engine,feature_id,kind,parsed.content,expected_rev_id=expected,
                                            author=actor,summary=STEP_NAMES[step_name],input_sha=fingerprint,
                                            work_packages=parsed.work_packages if step_name == "tasks" else None,
                                            handoff=parsed.handoff_brief,findings=findings_payload)
                if step_name in ("checklist","analyze") and engine.db.meta_get(f"speckit.analysis:{revision['rev_id']}") is None:
                    engine.db.meta_set(f"speckit.analysis:{revision['rev_id']}",json.dumps(findings_payload,ensure_ascii=False))
            engine.db.meta_set(f"speckit.last_step:{feature_id}",parsed.model_dump_json())
        engine.db.finish_run(run_id,session_id=result.session_id,exit_code=0,input_tokens=result.usage.input_tokens,
                            cached_tokens=result.usage.cached_tokens,output_tokens=result.usage.output_tokens,
                            reasoning_tokens=result.usage.reasoning_tokens,verdict="PASS",summary_log=parsed.handoff_brief.delivered,
                            log_path=str(log_dir))
        engine.db.add_event("speckit.step_completed",parsed.handoff_brief.delivered,actor=actor)
        return {"revision":revision,"handoff_brief":parsed.handoff_brief.model_dump(),"findings":[f.model_dump() for f in parsed.findings]}
    except (ValueError,OSError) as exc:
        engine.db.finish_run(run_id,session_id=result.session_id,exit_code=1,verdict="INVALID",summary_log=str(exc),log_path=str(log_dir),
                            input_tokens=result.usage.input_tokens,cached_tokens=result.usage.cached_tokens,output_tokens=result.usage.output_tokens,
                            reasoning_tokens=result.usage.reasoning_tokens)
        engine.db.add_event("speckit.step_failed",str(exc),actor=actor)
        raise
