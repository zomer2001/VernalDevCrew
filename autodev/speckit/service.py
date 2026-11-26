"""Immutable source revisions and one-way document projections."""
from __future__ import annotations
import json
import re

from autodev.db import now_iso
from autodev.models import ClarificationItem, HandoffBrief, ProductSpec
from autodev.process.targets import document_sha, normalize_document
from .assets import digest_json, load_assets, pin_assets
from .parser import parse_spec, parse_tasks, render_spec
from .paths import source_path, safe_path


class SpecConflictError(ValueError):
    pass


def enabled(engine) -> bool:
    return engine.db.meta_get("speckit.enabled") == "1"


def constitution_rule(engine) -> str:
    if not enabled(engine):
        return ""
    if not hasattr(engine,"ws"):
        from types import SimpleNamespace
        engine=SimpleNamespace(db=engine.db,ws=SimpleNamespace(root=engine.db.path.parent.parent))
    rev = latest(engine,"project","constitution")
    if not rev:
        return ""
    assert_source_current(engine,rev)
    return rev["content_text"]


async def sync_rule(engine,client,project_id) -> None:
    if enabled(engine):
        from autodev.loopforge.bootstrap import sync_decisions_rule
        await sync_decisions_rule(client,project_id,engine.db.list_decisions(active_only=True),
                                  version=int(engine.db.meta_get("decisions_rule_version") or 1),
                                  constitution=constitution_rule(engine))


def latest(engine, feature_id: str, kind: str = "spec") -> dict | None:
    source_path(feature_id, kind)
    feature_id = "project" if kind == "constitution" else feature_id
    rows = engine.db._query(
        "SELECT r.*,d.content_text FROM spec_revisions r JOIN doc_versions d ON d.id=r.content_version_id "
        "WHERE r.feature_id=? AND r.source_kind=? ORDER BY r.rev_no DESC LIMIT 1", (feature_id, kind))
    return dict(rows[0]) if rows else None


def current_specs(engine) -> list[dict]:
    return [dict(row) for row in engine.db._query(
        "SELECT r.*,d.content_text FROM spec_revisions r JOIN doc_versions d ON d.id=r.content_version_id "
        "JOIN (SELECT feature_id,MAX(rev_no) n FROM spec_revisions WHERE source_kind='spec' GROUP BY feature_id) l "
        "ON l.feature_id=r.feature_id AND l.n=r.rev_no WHERE r.source_kind='spec' ORDER BY r.feature_id")]


def assert_source_current(engine, rev: dict) -> None:
    path = safe_path(engine.ws.root, rev["rel_path"])
    if not path.is_file() or document_sha(path.read_text("utf-8")) != rev["content_sha"]:
        raise SpecConflictError("规格工作副本与已发布修订不一致，请显式保存或恢复后继续")


def restore_source(engine,feature_id: str,kind: str,*,expected_rev_id: int,actor: str) -> dict:
    """Repair disk drift without adopting an unreviewed external edit as authority."""
    from uuid import uuid4
    with engine.db.transaction():
        rev=latest(engine,feature_id,kind)
        if not rev or rev["rev_id"]!=expected_rev_id:
            raise SpecConflictError("当前修订已变化，请刷新后恢复")
        if engine.db._query("SELECT 1 FROM tasks WHERE status IN ('IN_PROGRESS','VERIFYING','REVIEWING')"):
            raise SpecConflictError("运行期间不能恢复来源文件")
        target=safe_path(engine.ws.root,rev["rel_path"])
        backup=""
        if target.is_file() and document_sha(target.read_text("utf-8"))!=rev["content_sha"]:
            recovery=engine.ws.state_dir/"spec-recovery";recovery.mkdir(exist_ok=True)
            saved=recovery/f"{uuid4().hex}.md";saved.write_bytes(target.read_bytes())
            backup=saved.relative_to(engine.ws.root).as_posix()
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(rev["content_text"],"utf-8")
        project_documents(engine)
        engine.db.add_event("speckit.source_restored",f"已恢复 {feature_id} 的 {kind} 工作副本；外部修改备份：{backup or '无需备份'}",actor=actor)
        return {"restored":rev["rel_path"],"backup_path":backup}


def input_fingerprint(engine, feature_id: str, kind: str) -> str:
    chain = {"constitution": [], "spec": ["constitution"], "plan": ["constitution", "spec"],
             "tasks": ["constitution", "spec", "plan"], "checklist": ["constitution", "spec"],
             "analyze": ["constitution", "spec", "plan", "tasks"]}[kind]
    versions = {}
    for dep in chain:
        rev = latest(engine, feature_id, dep)
        if rev:
            assert_source_current(engine, rev)
            versions[dep] = [rev["rev_id"], rev["content_sha"]]
    return digest_json(versions)


def list_clarifications(engine, feature_id: str) -> list[dict]:
    source_path(feature_id, "spec")
    rows = engine.db._query("SELECT * FROM clarifications WHERE feature_id=? ORDER BY clarify_id", (feature_id,))
    return [{**dict(r), "options": json.loads(r["options_json"])} for r in rows]


def assert_no_blocking_questions(engine, feature_id: str | None = None) -> None:
    sql = "SELECT question_key FROM clarifications WHERE status='OPEN' AND severity='BLOCKING'"
    params = ()
    if feature_id is not None:
        sql += " AND feature_id=?"
        params = (feature_id,)
    if engine.db._query(sql, params):
        raise SpecConflictError("仍有影响交付承诺的澄清问题未回答，不能批准或进入规划")


def _snapshot(engine, path: str, text: str, *, author: str, summary: str) -> dict:
    from autodev.docs_center.service import _save_snapshot
    version = _save_snapshot(engine, safe_path(engine.ws.root, path), text, author=author,
                             note=summary, refresh_gates=False)
    return dict(engine.db._query("SELECT * FROM doc_versions WHERE path=? AND version=?", (path, version))[0])


def project_documents(engine) -> list[str]:
    """No model calls and no reads from legacy PRD after migration."""
    specs = current_specs(engine)
    written = []
    if specs:
        body = "# 软件需求规格说明书\n\n本页由功能规格汇总，修改请进入对应功能。\n"
        for rev in specs:
            body += f"\n## 功能 {rev['feature_id']} · 修订 {rev['rev_no']}\n\n" + rev["content_text"]
        _snapshot(engine, "docs/SRS.md", body, author="SpecAdapter", summary="汇总当前功能规格")
        written.append("docs/SRS.md")
    constitution = latest(engine, "project", "constitution")
    if constitution:
        _snapshot(engine, "docs/CONSTITUTION.md", constitution["content_text"], author="SpecAdapter", summary="项目宪章展示投影")
        written.append("docs/CONSTITUTION.md")
    plans = [latest(engine, r["feature_id"], "plan") for r in specs]
    plans = [r for r in plans if r]
    if plans:
        text = "# 架构与功能技术方案\n" + "\n".join(
            f"\n## {r['feature_id']} · 修订 {r['rev_no']}\n{r['content_text']}" for r in plans)
        _snapshot(engine, "docs/ARCHITECTURE.md", text, author="SpecAdapter", summary="汇总当前技术方案")
        written.append("docs/ARCHITECTURE.md")
    from autodev.process.runner import refresh_document_gates
    refresh_document_gates(engine.db)
    return written


def publish_revision(engine, feature_id: str, kind: str, content: str, *, expected_rev_id: int,
                     author: str = "owner", summary: str = "人工修订", input_sha: str | None = None,
                     handoff: HandoffBrief | None = None) -> dict:
    path = source_path(feature_id, kind)
    feature_id = "project" if kind == "constitution" else feature_id
    if not enabled(engine):
        raise ValueError("请先启用功能规格流程")
    assets, _ = load_assets(engine)
    content = normalize_document(content)
    from autodev.prompts_style import validate_public_text
    validate_public_text(content)
    if not content.strip():
        raise ValueError("规格正文不能为空")
    if kind == "spec":
        parse_spec(content, feature_id)
    elif kind == "tasks":
        parse_tasks(content)
    elif kind == "plan":
        from .parser import section
        if any(not section(content,h) for h in ("需求映射","接口与数据","实施边界","验证")):
            raise ValueError("技术方案必须包含需求映射、接口与数据、实施边界和验证章节")
    with engine.db.transaction():
        active_sql = "SELECT 1 FROM tasks WHERE status IN ('IN_PROGRESS','VERIFYING','REVIEWING')"
        if kind != "constitution":
            active_sql += " AND feature_id=?"
        if engine.db._query(active_sql,() if kind == "constitution" else (feature_id,)):
            raise SpecConflictError("当前功能仍有任务运行，P1 不支持运行中替换来源，请先等待任务结束")
        old = latest(engine, feature_id, kind)
        if (old["rev_id"] if old else 0) != expected_rev_id:
            raise SpecConflictError("当前规格修订已变化，请刷新后重试")
        if old:
            assert_source_current(engine,old)
        elif safe_path(engine.ws.root,path).exists():
            raise SpecConflictError("目标路径已有未纳管文件，请保留备份并移出该路径后再保存")
        actual_input = input_fingerprint(engine, feature_id, kind)
        if input_sha is not None and input_sha != actual_input:
            raise SpecConflictError("生成期间上游规格已变化，旧结果已拒收")
        if old and old["content_sha"] == document_sha(content) and old["input_sha"] == actual_input:
            assert_source_current(engine, old)
            return old
        snap = _snapshot(engine, path, content, author=author, summary=summary)
        rev_no = old["rev_no"] + 1 if old else 1
        cursor = engine.db._exec(
            "INSERT INTO spec_revisions(feature_id,rev_no,source_kind,rel_path,content_sha,content_version_id,"
            "parent_rev_id,preset_id,preset_version,speckit_version,input_sha,author,summary,handoff_brief_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (feature_id,rev_no,kind,path,snap["content_sha"],snap["id"],old["rev_id"] if old else None,
             assets["preset_id"],assets["preset_version"],assets["speckit_version"],actual_input,author,summary,
             handoff.model_dump_json() if handoff else "{}",now_iso()))
        if old:
            engine.db._exec("UPDATE spec_revisions SET status='STALE' WHERE rev_id=?", (old["rev_id"],))
        downstream = {"constitution": ["spec","plan","tasks","checklist","analyze"],
                      "spec": ["plan","tasks","checklist","analyze"], "plan": ["tasks","analyze"],
                      "tasks": ["analyze"], "checklist": [], "analyze": []}[kind]
        if downstream:
            placeholders = ",".join("?" for _ in downstream)
            scope = "" if kind == "constitution" else " AND feature_id=?"
            engine.db._exec(f"UPDATE spec_revisions SET status='STALE' WHERE source_kind IN ({placeholders}){scope}",
                            (*downstream, *([] if kind == "constitution" else [feature_id])))
        project_documents(engine)
        if kind in ("spec", "constitution"):
            from autodev.process.runner import ensure_gate_record
            if current_specs(engine):
                ensure_gate_record(engine.db, stage_id="requirements", gate_id="srs_approval", kind="human_approval",
                                   scope_type="project", scope_id="project", summary="审阅功能规格与项目约束")
            if kind=="spec" and engine.status in ("CREATED","INGESTED"):
                from autodev.models import ProjectStatus
                engine.set_status(ProjectStatus.SPECIFIED)
        if kind == "plan":
            from autodev.process.runner import ensure_gate_record
            ensure_gate_record(engine.db, stage_id="architecture",gate_id="architecture_approval",kind="human_approval",
                               scope_type="project",scope_id="project",summary="审阅当前功能技术方案")
        engine.db.add_event("speckit.revision_published", f"{feature_id} 的 {kind} 已保存修订 {rev_no}", actor=author)
        return dict(engine.db._query("SELECT r.*,d.content_text FROM spec_revisions r JOIN doc_versions d "
                                     "ON d.id=r.content_version_id WHERE rev_id=?", (cursor.lastrowid,))[0])


def add_clarifications(engine, feature_id: str, spec_rev_id: int, questions: list[ClarificationItem], *, actor: str) -> None:
    if len({q.question_key for q in questions}) != len(questions):
        raise ValueError("澄清问题编号重复")
    with engine.db.transaction():
        rev = latest(engine, feature_id)
        if not rev or rev["rev_id"] != spec_rev_id:
            raise SpecConflictError("澄清所基于的规格已变化")
        assert_source_current(engine, rev)
        for q in questions:
            from autodev.prompts_style import validate_public_text
            for text in [q.question,q.rationale,*q.options]:
                validate_public_text(text)
            existing = engine.db._query("SELECT * FROM clarifications WHERE feature_id=? AND spec_rev_id=? AND question_key=?",
                                        (feature_id,spec_rev_id,q.question_key))
            if existing:
                row = dict(existing[0])
                if any(row[k] != getattr(q,k) for k in ("question","severity","category","default_option","rationale")) or json.loads(row["options_json"]) != q.options:
                    raise SpecConflictError("已有澄清问题内容不可覆盖，请使用新编号")
                continue
            inserted = engine.db._exec("INSERT INTO clarifications(feature_id,spec_rev_id,question_key,category,severity,question,"
                            "options_json,default_option,rationale,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (feature_id,spec_rev_id,q.question_key,q.category,q.severity,q.question,
                             json.dumps(q.options,ensure_ascii=False),q.default_option,q.rationale,now_iso(),now_iso()))
            if q.severity == "DEFAULTED":
                answer_clarification(engine,inserted.lastrowid,q.default_option,
                                     expected_rev_id=latest(engine,feature_id)["rev_id"],actor="recommended-default")
                engine.db._exec("UPDATE clarifications SET status='AUTO_DEFAULTED' WHERE clarify_id=?",(inserted.lastrowid,))
            else:
                from autodev.process.runner import ensure_gate_record
                ensure_gate_record(engine.db,stage_id="requirements",gate_id="srs_approval",kind="human_approval",
                                   scope_type="project",scope_id="project",summary="请先回答关键澄清问题再审批",force_new_round=True)
        engine.db.add_event("speckit.clarifications", f"已记录 {len(questions)} 个澄清问题", actor=actor)


def answer_clarification(engine, clarify_id: int, answer: str, *, expected_rev_id: int, actor: str, supplement: str = "") -> dict:
    with engine.db.transaction():
        rows = engine.db._query("SELECT * FROM clarifications WHERE clarify_id=?", (clarify_id,))
        if not rows:
            raise ValueError("澄清问题不存在")
        q = dict(rows[0])
        rev = latest(engine, q["feature_id"])
        if not rev or rev["rev_id"] != expected_rev_id or q["status"] not in ("OPEN","AUTO_DEFAULTED"):
            raise SpecConflictError("问题或规格已变化，请刷新后回答")
        answer = answer.strip()
        if not answer or (q["options_json"] != "[]" and answer not in json.loads(q["options_json"])):
            raise ValueError("请选择有效选项或填写非空回答")
        # User statements become part of the same authoritative source; no LLM reinterpretation.
        heading = f"澄清 {q['question_key']} · {q['clarify_id']}"
        content = re.sub(r"^## " + re.escape(heading) + r"\n.*?(?=^## |\Z)","",rev["content_text"],flags=re.M|re.S)
        content += f"\n## {heading}\n\n问题：{q['question']}\n\n确认：{answer}\n"
        if supplement.strip():
            content += f"\n补充：{supplement.strip()}\n"
        new = publish_revision(engine,q["feature_id"],"spec",content,expected_rev_id=expected_rev_id,
                               author=actor,summary="记录负责人澄清回答")
        engine.db._exec("UPDATE clarifications SET status='ANSWERED',answer=?,answered_by=?,applied_rev_id=?,updated_at=? "
                        "WHERE clarify_id=?", (answer,actor,new["rev_id"],now_iso(),clarify_id))
        return new


def product_spec(engine) -> ProductSpec:
    specs = [parse_spec(r["content_text"],r["feature_id"]) for r in current_specs(engine)]
    return ProductSpec(product_vision="\n".join(s.product_vision for s in specs),
                       core_workflows=[v for s in specs for v in s.core_workflows],
                       edge_cases=[v for s in specs for v in s.edge_cases],error_conventions=[v for s in specs for v in s.error_conventions],
                       global_acceptance_redlines=[v for s in specs for v in s.global_acceptance_redlines],
                       spec_markdown="\n".join(s.spec_markdown for s in specs),
                       requirements=[r for s in specs for r in s.requirements],nfrs=[r for s in specs for r in s.nfrs])


def answer_batch(engine, feature: str, answers: list[dict], *, expected_rev_id: int, actor: str) -> dict:
    """Validate the whole round before writing one authoritative revision."""
    if not 1 <= len(answers) <= 3 or len({a["clarify_id"] for a in answers}) != len(answers):
        raise ValueError("每轮应包含一至三个不同问题的回答")
    with engine.db.transaction():
        rev = latest(engine, feature)
        if not rev or rev["rev_id"] != expected_rev_id:
            raise SpecConflictError("当前规格已变化，请刷新后回答")
        rows = {q["clarify_id"]: q for q in list_clarifications(engine, feature)}
        content = rev["content_text"]
        normalized = []
        for item in answers:
            q = rows.get(item["clarify_id"])
            if not q:
                raise ValueError("问题不属于当前功能")
            if q["status"] not in ("OPEN", "AUTO_DEFAULTED"):
                raise SpecConflictError("问题已经回答，请刷新")
            answer = item["answer"].strip()
            if not answer or (q["options"] and answer not in q["options"]):
                raise ValueError("请选择有效选项或填写非空回答")
            heading = f"澄清 {q['question_key']} · {q['clarify_id']}"
            content = re.sub(r"^## " + re.escape(heading) + r"\n.*?(?=^## |\Z)", "", content, flags=re.M | re.S)
            content += f"\n## {heading}\n\n问题：{q['question']}\n\n确认：{answer}\n"
            if item.get("supplement", "").strip():
                content += f"\n补充：{item['supplement'].strip()}\n"
            normalized.append((q["clarify_id"], answer))
        new = publish_revision(engine, feature, "spec", content, expected_rev_id=expected_rev_id,
                               author=actor, summary="记录本轮负责人回答")
        for clarify_id, answer in normalized:
            engine.db._exec("UPDATE clarifications SET status='ANSWERED',answer=?,answered_by=?,applied_rev_id=?,updated_at=? WHERE clarify_id=?",
                            (answer, actor, new["rev_id"], now_iso(), clarify_id))
        return new


def ensure_specification_gate(engine) -> None:
    from autodev.process.model import GateDef,StageDef
    from autodev.process.runner import load_template,save_custom_template
    template=load_template(engine.ws,engine.settings)
    stage=next((s for s in template.stages if s.id=="requirements"),None)
    if stage is None:
        stage=StageDef(id="requirements",name="需求分析")
        template.stages.insert(0,stage)
    gate=next((g for g in stage.gates if g.id=="srs_approval"),None)
    if stage.enabled and gate and gate.blocking and gate.kind=="human_approval" and gate.scope=="project":
        return
    stage.enabled=True
    stage.gates=[g for g in stage.gates if g.id!="srs_approval"] + [GateDef(
        id="srs_approval",name="功能规格与宪章批准",kind="human_approval",scope="project",blocking=True)]
    save_custom_template(engine.ws,template.model_dump_json())
    engine.db.add_event("speckit.approval_enabled","已在现有流程模板中启用需求规格审批",actor="SpecAdapter")


def enable(engine, preset_id: str, *, migrate: bool = False, actor: str = "owner") -> dict:
    if enabled(engine):
        lock=pin_assets(engine,preset_id)
        ensure_specification_gate(engine)
        return lock
    if any(t.status not in ("MERGED","CANCELLED") for t in engine.db.list_tasks()):
        raise SpecConflictError("迁移前请完成或取消已有任务，避免旧任务继续使用旧规格")
    if not engine.ws.prd_json_path.is_file() and (engine.ws.root / "docs/SRS.md").is_file():
        raise SpecConflictError("发现只有文档形式的旧需求，请先整理为结构化产品规格后迁移，原文档不会覆盖")
    legacy = None
    if engine.ws.prd_json_path.is_file():
        if not migrate:
            raise ValueError("发现旧产品规格，请选择保留原文件并迁移为功能规格")
        from autodev.docs_center.service import load_product_spec
        legacy = load_product_spec(engine)
        if not legacy.requirements and not legacy.nfrs:
            raise ValueError("旧规格没有结构化需求，需先整理需求条目后迁移")
        if any(r.feature_id for r in legacy.requirements + legacy.nfrs):
            raise ValueError("旧规格含多个功能命名空间，请逐功能迁移，不能扁平合并")
    lock = pin_assets(engine,preset_id)
    with engine.db.transaction():
        engine.db.meta_set("speckit.enabled","1")
        if legacy:
            content = render_spec(legacy,non_goals=["原规格未明确，待负责人确认范围。"])
            rev = publish_revision(engine,"001-migrated","spec",content,expected_rev_id=0,author=actor,summary="旧规格无损迁移")
            add_clarifications(engine,"001-migrated",rev["rev_id"],[ClarificationItem(
                question_key="Q-01",category="non_goal",severity="BLOCKING",question="本轮明确不做哪些内容？")],actor=actor)
        engine.db.add_event("speckit.enabled","已启用功能规格流程，旧规格文件保留只读",actor=actor)
    ensure_specification_gate(engine)
    return lock
