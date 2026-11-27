"""Compile small draft steps into frozen, independently reviewable execution packages.

P1 rejects overlapping work and replaces no existing attempts. Lease-based parallel
resource scheduling and live supersession belong to the later change-management batch.
"""
from __future__ import annotations
import hashlib
import json

from autodev.models import CompiledWorkPackage, PlanTask, ProjectStatus, WorkPackageDraft
from autodev.process.runner import is_gate_passed
from autodev.process.targets import document_sha
from .assets import digest_json, load_assets
from .parser import parse_tasks, legacy_drafts_to_packages
from .paths import safe_path, tasks_json_path
from .validate_output import validate_work_packages, ordered, overlap as _overlap
from .service import (SpecConflictError, assert_no_blocking_questions, assert_source_current,
                      checklist_findings_digest, revision_inputs_current, latest, quality_review, work_package_schema)


class TaskDraftError(ValueError):
    """A task-content defect, distinct from approval, source or filesystem failures."""


def contract_hash(engine) -> str:
    files = {}
    root = engine.ws.root / "contracts"
    if root.is_dir():
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise ValueError("契约文件不得包含符号链接")
            if path.is_file():
                files[path.relative_to(engine.ws.root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest_json(files)


def compile_tasks(engine, feature_id: str, *, require_analysis: bool = True) -> list[CompiledWorkPackage]:
    assets, _ = load_assets(engine)
    assert_no_blocking_questions(engine, feature_id)
    if not is_gate_passed(engine.db,"srs_approval","project","project"):
        raise SpecConflictError("请先批准当前功能规格与宪章")
    revisions = {}
    for kind in ("constitution","spec","plan","tasks"):
        rev = latest(engine,feature_id,kind)
        if not rev:
            raise ValueError(f"缺少 {kind} 修订")
        assert_source_current(engine,rev)
        if not revision_inputs_current(engine, rev):
            raise SpecConflictError(f"{kind} 基于旧规格，请重新生成或确认修订")
        revisions[kind] = rev
    mission = json.loads(engine.db.meta_get(f"mission:{feature_id}") or "{}")
    lite = mission.get("lite_revisions")
    if lite and any(revisions[k]["rev_id"] != lite[k] for k in ("spec", "plan", "tasks")):
        raise SpecConflictError("轻量规划组合已变化，请重新生成并确认")
    qr = None
    checklist_rev = latest(engine, feature_id, "checklist")
    has_mission = engine.db.meta_get(f"mission:{feature_id}") is not None
    if not lite and (checklist_rev or has_mission):
        qr = quality_review(engine, feature_id)
        if not qr["is_current"]:
            raise SpecConflictError(qr["next_action"])
        if qr["counts"]["CRITICAL"] > 0:
            raise ValueError("规格质量检查存在严重问题（CRITICAL），不能编译发布任务")
    rev = revisions["tasks"]
    json_path = safe_path(engine.ws.root, tasks_json_path(feature_id))
    try:
        if json_path.exists():
            drafts = [WorkPackageDraft.model_validate(p) for p in json.loads(json_path.read_text("utf-8"))]
        else:
            drafts = legacy_drafts_to_packages(parse_tasks(rev["content_text"]))
        ids = {p.package_key: f"SK-{feature_id}-{p.package_key}" for p in drafts}
        dependencies = validate_work_packages([p.model_dump() for p in drafts],
            work_package_schema(engine, feature_id, exclude_task_ids=ids.values()), engine.ws.root)
        package_order = ordered(dependencies, "工作包")
        step_order = ordered({s.task_key: set(s.depends_on) for p in drafts for s in p.steps}, "步骤")
    except ValueError as exc:
        raise TaskDraftError(str(exc)) from exc
    by_key = {p.package_key: p for p in drafts}
    packages = []
    for key in package_order:
        draft = by_key[key]
        items = sorted(draft.steps, key=lambda step: step_order.index(step.task_key))
        unique = lambda field: list(dict.fromkeys(v for step in items for v in getattr(step, field)))
        task = PlanTask(task_id=ids[key], milestone_id=draft.milestone_id, squad=draft.squad, module=draft.module,
                        role=draft.role, title=draft.title, description="\n".join(t.description for t in items),
                        acceptance_criteria=draft.acceptance_criteria, depends_on=[ids[d] for d in sorted(dependencies[key])],
                        doc_slice_ids=[], allowed_paths=[p.rstrip("/") for p in draft.allowed_paths],
                        verify_cmds=list(dict.fromkeys(draft.verify_cmds + unique("verify_cmds"))),
                        priority=len(packages)+1, requirement_ids=[f"{feature_id}:{r}" for r in unique("requirement_ids")])
        packages.append(CompiledWorkPackage(task_key=key, source_task_keys=[t.task_key for t in items], feature_id=feature_id,
                        spec_rev_id=revisions["spec"]["rev_id"], spec_sha=revisions["spec"]["content_sha"],
                        constitution_sha=revisions["constitution"]["content_sha"], plan_sha=revisions["plan"]["content_sha"],
                        tasks_sha=rev["content_sha"], contract_sha=contract_hash(engine), asset_versions=assets,
                        checklist_rev_id=(qr or {}).get("checklist_rev_id", 0),
                        checklist_sha=(qr or {}).get("checklist_sha", ""),
                        checklist_findings_sha=(qr or {}).get("findings_sha", ""), task=task))
    if require_analysis and not lite:
        analysis = latest(engine,feature_id,"analyze")
        if not analysis or not revision_inputs_current(engine, analysis):
            raise SpecConflictError("请先对当前任务草案执行一致性检查")
        assert_source_current(engine,analysis)
        findings = engine.db.meta_get(f"speckit.analysis:{analysis['rev_id']}")
        if findings is None or any(x["severity"] == "CRITICAL" for x in json.loads(findings)):
            raise ValueError("一致性检查尚未通过，不能发布任务")
    for package in packages:
        package.compiled_task_hash = digest_json(package.model_dump(exclude={"compiled_task_hash"}))
    return packages


def publish_tasks(engine,feature_id: str,*,expected_hashes: list[str] | None,actor: str) -> list[str]:
    with engine.db.transaction():
        if engine.status == ProjectStatus.RUNNING:
            raise SpecConflictError("请等待当前执行结束再发布工作包")
        packages = compile_tasks(engine,feature_id)
        if expected_hashes is None and not json.loads(engine.db.meta_get(f"mission:{feature_id}") or "{}").get("lite_revisions"):
            raise SpecConflictError("仅轻量组合可在规格审批时同步冻结")
        if expected_hashes is not None and [p.compiled_task_hash for p in packages] != expected_hashes:
            raise SpecConflictError("工作包内容已变化，请重新预览")
        added = False
        for package in packages:
            task = package.task
            existing = engine.db.get_task(task.task_id)
            if existing:
                if existing.compiled_task_hash != package.compiled_task_hash:
                    raise SpecConflictError("已发布任务不能原地替换，请在后续变更流程中重编译")
                continue
            milestone=engine.db.get_milestone(task.milestone_id)
            if milestone and milestone.status != "PENDING":
                raise SpecConflictError("目标里程碑已进入集成或已结束，请在任务草案中使用新的里程碑编号")
            # Other features may not silently write the same territory either.
            for old in engine.db.list_tasks():
                if old.status not in ("CANCELLED","MERGED") and any(_overlap(p,q) for p in task.allowed_paths for q in old.allowed_paths):
                    raise ValueError("修改范围与已有任务重叠，发布已阻断")
            if not engine.db._query("SELECT 1 FROM milestones WHERE milestone_id=?",(task.milestone_id,)):
                seq = engine.db._query("SELECT COALESCE(MAX(seq_order),0)+1 n FROM milestones")[0]["n"]
                engine.db._exec("INSERT INTO milestones(milestone_id,seq_order,title,goal,integration_cmd,status,planned) "
                                "VALUES(?,?,?,?,?,'PENDING',1)",(task.milestone_id,seq,task.milestone_id,"完成功能验收"," && ".join(task.verify_cmds)))
            engine.db._exec("INSERT OR IGNORE INTO squads(squad_id,seq_order,name,mission,owned_paths_json,roles_json) VALUES(?,0,?,?,?,?)",
                            (task.squad,task.squad,task.module,json.dumps(task.allowed_paths),json.dumps([task.role])))
            # Single-package lite delivery exhausts its retry budget inside the native workflow.
            attempts = 1 if expected_hashes is None and len(packages) == 1 else engine.settings.max_retries
            engine.db.insert_single_task(task,max_retries=attempts)
            added = True
            engine.db.update_task(task.task_id,feature_id=feature_id,spec_rev_id=package.spec_rev_id,spec_sha=package.spec_sha,
                                  plan_sha=package.plan_sha,compiled_task_hash=package.compiled_task_hash,
                                  compiled_package_json=package.model_dump_json(),task_key=package.task_key)
        if added:
            for milestone in {p.task.milestone_id for p in packages}:
                cmds = list(dict.fromkeys(c for t in engine.db.list_tasks() if t.milestone_id == milestone for c in t.verify_cmds))
                engine.db._exec("UPDATE milestones SET integration_cmd=?,planned=1 WHERE milestone_id=?",(" && ".join(cmds),milestone))
            needs_approval = expected_hashes is not None and engine.settings.require_plan_approval
            engine.set_status(ProjectStatus.PLANNED if needs_approval else ProjectStatus.APPROVED)
            engine.db.meta_set("plan_summary",f"功能 {feature_id} 已编译发布 {len(packages)} 个工作包，请核对验收条件后启动。")
        engine.db.add_event("speckit.tasks_published",f"已发布 {len(packages)} 个工作包",actor=actor)
        return [p.task.task_id for p in packages]


def assert_package_current(db, task) -> CompiledWorkPackage | None:
    if not task.feature_id:
        if db.meta_get("speckit.enabled") == "1" and task.status not in ("MERGED","CANCELLED"):
            raise SpecConflictError("规格项目的待执行任务必须由工作包编译器发布")
        return None
    frozen = json.loads(task.compiled_package_json)
    package = CompiledWorkPackage.model_validate(frozen)
    # Hash the signed payload as stored. Model defaults added by newer releases
    # must not change the preimage of an already published work package.
    if digest_json({k: v for k, v in frozen.items() if k != "compiled_task_hash"}) != task.compiled_task_hash:
        raise SpecConflictError("工作包指纹校验失败")
    if package.compiled_task_hash != task.compiled_task_hash or package.feature_id != task.feature_id or package.spec_rev_id != task.spec_rev_id:
        raise SpecConflictError("任务锚点与冻结工作包不一致")
    for field in PlanTask.model_fields:
        if getattr(task,field,None) != getattr(package.task,field):
            raise SpecConflictError("已发布工作包内容被修改，请通过规格修订重新编译")
    for kind,expected in (("spec",package.spec_sha),("plan",package.plan_sha),("tasks",package.tasks_sha),("constitution",package.constitution_sha)):
        feature = "project" if kind == "constitution" else task.feature_id
        rows = db._query("SELECT r.*,d.content_text FROM spec_revisions r JOIN doc_versions d ON d.id=r.content_version_id WHERE r.feature_id=? AND r.source_kind=? ORDER BY r.rev_no DESC LIMIT 1",(feature,kind))
        if not rows or rows[0]["status"] == "STALE" or rows[0]["content_sha"] != expected or (kind == "spec" and rows[0]["rev_id"] != task.spec_rev_id):
            raise SpecConflictError("工作包所依据的规格已变化，旧任务不能继续执行")
        from types import SimpleNamespace
        assert_source_current(SimpleNamespace(ws=SimpleNamespace(root=db.path.parent.parent)), dict(rows[0]))
    if not is_gate_passed(db,"srs_approval","project","project"):
        raise SpecConflictError("当前功能规格尚未批准")
    from types import SimpleNamespace
    engine = SimpleNamespace(db=db,ws=SimpleNamespace(root=db.path.parent.parent,state_dir=db.path.parent))
    chk_rows = db._query("SELECT * FROM spec_revisions WHERE feature_id=? AND source_kind='checklist' ORDER BY rev_no DESC LIMIT 1",(task.feature_id,))
    if package.checklist_sha:
        if not chk_rows or chk_rows[0]["status"] == "STALE" or chk_rows[0]["content_sha"] != package.checklist_sha or (package.checklist_rev_id and chk_rows[0]["rev_id"] != package.checklist_rev_id):
            raise SpecConflictError("工作包所依据的规格质量检查已变化，旧任务不能继续执行")
        chk_path = db.path.parent.parent / chk_rows[0]["rel_path"]
        if chk_path.is_symlink() or not chk_path.resolve().is_relative_to(db.path.parent.parent.resolve()) or not chk_path.is_file() or document_sha(chk_path.read_text("utf-8")) != package.checklist_sha:
            raise SpecConflictError("工作包质量检查来源文件发生漂移，执行已阻断")
        cur_fsha = checklist_findings_digest(engine, chk_rows[0]["rev_id"])
        if cur_fsha in ("__MISSING__", "__CORRUPT__") or cur_fsha != package.checklist_findings_sha:
            raise SpecConflictError("工作包所依据的质量发现元数据缺失或已变化，旧任务不能继续执行")
    elif chk_rows and not (chk_rows[0]["status"] == "STALE" and json.loads(db.meta_get(f"mission:{task.feature_id}") or "{}").get("lite_revisions")):
        raise SpecConflictError("功能已新增规格质量检查，请重新编译并确认工作包")
    if contract_hash(engine) != package.contract_sha:
        raise SpecConflictError("契约基线已变化，旧工作包不能继续执行")
    lock,_ = load_assets(engine)
    if lock != package.asset_versions:
        raise SpecConflictError("项目工具版本与工作包不一致")
    return package
