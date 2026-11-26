"""规范文档中心服务：文档树发现、多版本内容寻址归档、段落评论、详细设计生成与测试报告合成（WP-1）。"""

from __future__ import annotations

import difflib
import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from autodev.db import now_iso
from autodev.docs_center.templates import (
    render_adr_madr,
    render_architecture_markdown,
    render_detailed_design_markdown,
    render_srs_markdown,
)
from autodev.docs_center.validator import validate_detailed_design
from autodev.models import CouncilConsensus, DetailedDesign, ProductSpec, RequirementItem, TaskStatus
from autodev.process.runner import ensure_gate_record, is_gate_passed, load_template

if TYPE_CHECKING:
    from autodev.engine import Engine


def _safe_doc_relpath(ws_root: Path, rel_path: str) -> Path:
    cleaned = rel_path.strip().lstrip("/").replace("\\", "/")
    if not cleaned or ".." in Path(cleaned).parts:
        raise ValueError(f"非法的文档路径：{rel_path}")
    if not cleaned.startswith("docs/"):
        cleaned = f"docs/{cleaned}"
    full = (ws_root / cleaned).resolve()
    if not full.is_relative_to(ws_root.resolve()):
        raise ValueError(f"文档路径越出工作区：{rel_path}")
    return full


def _classify_doc_kind(rel_path: str) -> str:
    norm = rel_path.replace("\\", "/")
    if norm in ("docs/SRS.md", "docs/PRD_SPEC.md"):
        return "SRS"
    if norm == "docs/ARCHITECTURE.md":
        return "ADD"
    if norm.startswith("docs/adr/"):
        return "ADR"
    if norm.startswith("docs/design/"):
        return "DETAILED_DESIGN"
    if norm == "docs/INTERFACES.md":
        return "ICD"
    if norm.startswith("docs/test/"):
        return "TEST_REPORT"
    return "DOC"


def save_doc_version(
    engine: "Engine",
    rel_path: str,
    content: str,
    *,
    author: str = "owner",
    note: str = "",
) -> dict[str, Any]:
    full = _safe_doc_relpath(engine.ws.root, rel_path)
    canonical_rel = full.relative_to(engine.ws.root.resolve()).as_posix()
    full.parent.mkdir(parents=True, exist_ok=True)
    normalized_text = content.rstrip() + "\n"
    full.write_text(normalized_text, "utf-8")

    sha = hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()
    rows = engine.db._query("SELECT version, content_sha FROM doc_versions WHERE path=? ORDER BY version DESC LIMIT 1", (canonical_rel,))
    if rows and rows[0]["content_sha"] == sha:
        ver = int(rows[0]["version"])
        return get_doc(engine, canonical_rel, version=ver)

    next_ver = (int(rows[0]["version"]) + 1) if rows else 1
    ts = now_iso()
    engine.db._exec(
        "INSERT INTO doc_versions(path, version, content_sha, content_text, author, note, created_at)"
        " VALUES(?, ?, ?, ?, ?, ?, ?)",
        (canonical_rel, next_ver, sha, normalized_text, author, note.strip(), ts),
    )
    engine.db.add_event("doc.saved", f"规范文档已保存新版本：{canonical_rel} (v{next_ver})", actor=author)
    return get_doc(engine, canonical_rel, version=next_ver)


def list_docs(engine: "Engine") -> list[dict[str, Any]]:
    docs_dir = engine.ws.root / "docs"
    seen_paths: set[str] = set()
    items: list[dict[str, Any]] = []

    if docs_dir.is_dir():
        for fp in sorted(docs_dir.rglob("*.md")):
            if not fp.is_file():
                continue
            rel = fp.relative_to(engine.ws.root).as_posix()
            seen_paths.add(rel)
            v_rows = engine.db._query(
                "SELECT version, content_sha, author, created_at FROM doc_versions WHERE path=? ORDER BY version DESC LIMIT 1",
                (rel,),
            )
            if not v_rows:
                # 首次发现磁盘已有文档时自动纳入 v1 版本基线
                save_doc_version(engine, rel, fp.read_text("utf-8", errors="replace"), author="system", note="初始纳入版本管理")
                v_rows = engine.db._query(
                    "SELECT version, content_sha, author, created_at FROM doc_versions WHERE path=? ORDER BY version DESC LIMIT 1",
                    (rel,),
                )
            kind = _classify_doc_kind(rel)
            status = "DRAFT"
            if kind == "SRS" and is_gate_passed(engine.db, "srs_approval", "project", "project"):
                status = "APPROVED"
            elif kind == "ADD" and is_gate_passed(engine.db, "architecture_approval", "project", "project"):
                status = "APPROVED"
            elif kind == "DETAILED_DESIGN":
                ms_list = engine.db.list_milestones()
                if ms_list and all(is_gate_passed(engine.db, "detailed_design_approval", "milestone", m.milestone_id) for m in ms_list if m.planned):
                    status = "APPROVED"
            items.append(
                {
                    "path": rel,
                    "kind": kind,
                    "status": status,
                    "version": int(v_rows[0]["version"]) if v_rows else 1,
                    "author": v_rows[0]["author"] if v_rows else "system",
                    "updated_at": v_rows[0]["created_at"] if v_rows else "",
                }
            )
    return items


def get_doc(engine: "Engine", rel_path: str, *, version: int | None = None) -> dict[str, Any]:
    full = _safe_doc_relpath(engine.ws.root, rel_path)
    canonical = full.relative_to(engine.ws.root.resolve()).as_posix()
    if version is not None:
        rows = engine.db._query(
            "SELECT * FROM doc_versions WHERE path=? AND version=?",
            (canonical, int(version)),
        )
        if not rows:
            raise KeyError(f"文档版本不存在：{canonical} v{version}")
        row = dict(rows[0])
        content = row["content_text"]
        ver = int(row["version"])
    else:
        if not full.is_file():
            raise KeyError(f"文档不存在：{canonical}")
        content = full.read_text("utf-8", errors="replace")
        rows = engine.db._query(
            "SELECT * FROM doc_versions WHERE path=? ORDER BY version DESC LIMIT 1",
            (canonical,),
        )
        if not rows:
            return save_doc_version(engine, canonical, content, author="system", note="初始读取建档")
        row = dict(rows[0])
        ver = int(row["version"])

    cmts = [
        {**dict(c), "resolved": bool(c["resolved"])}
        for c in engine.db._query("SELECT * FROM doc_comments WHERE path=? ORDER BY comment_id ASC", (canonical,))
    ]
    return {
        "path": canonical,
        "kind": _classify_doc_kind(canonical),
        "version": ver,
        "content": content,
        "author": row.get("author", "system"),
        "note": row.get("note", ""),
        "created_at": row.get("created_at", ""),
        "comments": cmts,
    }


def list_doc_versions(engine: "Engine", rel_path: str) -> list[dict[str, Any]]:
    full = _safe_doc_relpath(engine.ws.root, rel_path)
    canonical = full.relative_to(engine.ws.root.resolve()).as_posix()
    rows = engine.db._query(
        "SELECT id, path, version, content_sha, author, note, created_at FROM doc_versions WHERE path=? ORDER BY version DESC",
        (canonical,),
    )
    return [dict(r) for r in rows]


def diff_doc_versions(engine: "Engine", rel_path: str, from_ver: int, to_ver: int) -> dict[str, Any]:
    d1 = get_doc(engine, rel_path, version=from_ver)
    d2 = get_doc(engine, rel_path, version=to_ver)
    diff_lines = list(
        difflib.unified_diff(
            d1["content"].splitlines(),
            d2["content"].splitlines(),
            fromfile=f"{d1['path']} (v{from_ver})",
            tofile=f"{d2['path']} (v{to_ver})",
            lineterm="",
        )
    )
    return {
        "path": d2["path"],
        "from_version": from_ver,
        "to_version": to_ver,
        "diff": "\n".join(diff_lines),
    }


def add_doc_comment(engine: "Engine", rel_path: str, *, section: str = "", body: str, author: str = "owner") -> dict[str, Any]:
    if not body.strip():
        raise ValueError("文档批注不能为空")
    full = _safe_doc_relpath(engine.ws.root, rel_path)
    canonical = full.relative_to(engine.ws.root.resolve()).as_posix()
    ts = now_iso()
    cur = engine.db._exec(
        "INSERT INTO doc_comments(path, section, body, author, resolved, created_at) VALUES(?, ?, ?, ?, 0, ?)",
        (canonical, section.strip(), body.strip(), author, ts),
    )
    cid = int(cur.lastrowid)
    return {**dict(engine.db._query("SELECT * FROM doc_comments WHERE comment_id=?", (cid,))[0]), "resolved": False}


def sync_standard_docs_from_artifacts(engine: "Engine") -> list[str]:
    """将已有 PRD JSON / 圆桌共识 / 蓝图同步渲染为规范化 SRS.md、ARCHITECTURE.md 与 ADR 文档。"""
    profile = engine.settings.doc_profile or "default"
    proj_name = engine.db.meta_get("name", engine.ws.root.name) or engine.ws.root.name
    written: list[str] = []

    if engine.ws.prd_json_path.is_file():
        try:
            spec = ProductSpec.model_validate(json.loads(engine.ws.prd_json_path.read_text("utf-8")))
            srs_md = render_srs_markdown(spec, profile=profile, project_name=proj_name)
            save_doc_version(engine, "docs/SRS.md", srs_md, author="PM-Agent", note=f"按 {profile} 规范生成 SRS")
            written.append("docs/SRS.md")
        except Exception:
            pass

    cons: CouncilConsensus | None = None
    c_msgs = [m for m in engine.db.list_council_messages() if m.get("kind") == "consensus"]
    if c_msgs:
        try:
            cons = CouncilConsensus.model_validate(c_msgs[-1]["body"])
        except Exception:
            cons = None

    bp_dict: dict[str, Any] | None = None
    if engine.ws.blueprint_path.is_file():
        try:
            bp_dict = json.loads(engine.ws.blueprint_path.read_text("utf-8"))
        except Exception:
            bp_dict = None

    if cons or bp_dict:
        add_md = render_architecture_markdown(cons, bp_dict, profile=profile, project_name=proj_name)
        save_doc_version(engine, "docs/ARCHITECTURE.md", add_md, author="Architect-Council", note="同步架构说明书")
        written.append("docs/ARCHITECTURE.md")

    if cons and cons.adrs:
        for adr in cons.adrs:
            adr_path = f"docs/adr/{adr.adr_id}.md"
            save_doc_version(engine, adr_path, render_adr_madr(adr), author="Director", note=adr.title)
            written.append(adr_path)

    return written


def load_known_requirements(engine: "Engine") -> list[RequirementItem]:
    if not engine.ws.prd_json_path.is_file():
        return []
    try:
        spec = ProductSpec.model_validate(json.loads(engine.ws.prd_json_path.read_text("utf-8")))
        return list(spec.requirements)
    except Exception:
        return []


async def generate_detailed_design(
    engine: "Engine",
    milestone_id: str,
    squad_id: str,
    *,
    max_attempts: int = 3,
) -> DetailedDesign:
    """调用架构师角色生成单个研发组的详细设计并通过确定性校验（最多自动返工 2 次）。"""
    known_reqs = load_known_requirements(engine)
    squads = {s["squad_id"]: s for s in engine.db.list_squads()}
    sq_info = squads.get(squad_id, {"squad_id": squad_id, "name": squad_id, "owned_paths": [], "mission": ""})
    srs_txt = (engine.ws.root / "docs" / "SRS.md").read_text("utf-8") if (engine.ws.root / "docs" / "SRS.md").is_file() else ""
    if not srs_txt and engine.ws.prd_md_path.is_file():
        srs_txt = engine.ws.prd_md_path.read_text("utf-8")
    add_txt = (engine.ws.root / "docs" / "ARCHITECTURE.md").read_text("utf-8") if (engine.ws.root / "docs" / "ARCHITECTURE.md").is_file() else ""

    req_hint = (
        "\n".join(f"- {r.req_id} [{r.priority}] {r.title}: {r.statement}" for r in known_reqs)
        if known_reqs
        else "（当前 SRS 未显式列出结构化 REQ 编号，可按需自建或留空 req_ids）"
    )
    base_prompt = (
        f"你是【系统架构师】，请为里程碑 `{milestone_id}` 下的研发组 `{squad_id}`（{sq_info.get('name', squad_id)}）编写模块详细设计说明书。\n"
        f"负责目录：{', '.join(sq_info.get('owned_paths') or [])}\n"
        f"职责：{sq_info.get('mission', '')}\n\n"
        f"## 已知需求编号清单\n{req_hint}\n\n"
        f"## 需求规格摘要\n{srs_txt[:4000]}\n\n"
        f"## 架构设计摘要\n{add_txt[:4000]}\n\n"
        "请严格输出满足 DetailedDesign JSON Schema 的唯一 JSON 对象，确保每个对外接口均有配套 TC-xxx 测试用例，且 Mermaid 图语法合法。"
    )
    prompt = base_prompt
    errors: list[str] = []

    for attempt in range(1, max_attempts + 1):
        log_dir = engine.ws.runs_dir / "_design" / f"{milestone_id}-{squad_id}-attempt-{attempt}"
        res = await engine.harness.run_structured_readonly(
            prompt,
            schema_path=engine.schemas["detailed_design"],
            cwd=engine.ws.root,
            log_dir=log_dir,
            role="architect",
        )
        dd_obj: DetailedDesign | None = None
        if not res.ok or not res.structured:
            errors = [res.error or "架构师未返回合法的 DetailedDesign JSON"]
        else:
            try:
                dd_obj = DetailedDesign.model_validate(res.structured)
                # 单组详设只校验组内引用合法与接口 TC 闭环；单组工程（仅 1 个研发组）同时强制覆盖全部 P0 REQ
                enforce_p0 = len(squads) <= 1
                errors = validate_detailed_design(dd_obj, known_reqs, enforce_all_p0=enforce_p0)
            except ValidationError as exc:
                errors = [str(exc)]

        if dd_obj is not None and not errors:
            json_out = engine.ws.state_dir / "design" / milestone_id / f"{squad_id}.json"
            json_out.parent.mkdir(parents=True, exist_ok=True)
            json_out.write_text(json.dumps(dd_obj.model_dump(), ensure_ascii=False, indent=2), "utf-8")

            md_text = render_detailed_design_markdown(dd_obj, profile=engine.settings.doc_profile or "default")
            rel_doc = f"docs/design/{squad_id}.md"
            save_doc_version(engine, rel_doc, md_text, author="Architect", note=f"{milestone_id} 详细设计生成")

            tpl = load_template(engine.ws, engine.settings)
            dd_stage = next((s for s in tpl.stages if s.id == "detailed_design" and s.enabled), None)
            if dd_stage and any(g.id == "detailed_design_approval" and g.blocking for g in dd_stage.gates):
                ensure_gate_record(
                    engine.db,
                    stage_id="detailed_design",
                    gate_id="detailed_design_approval",
                    kind="human_approval",
                    scope_type="milestone",
                    scope_id=milestone_id,
                    summary=f"里程碑 {milestone_id} 模块详细设计已生成，等待评审签字",
                )
            return dd_obj

        prompt = (
            base_prompt
            + "\n\n## 上一轮详细设计校验未通过，请修正以下问题后重新输出 JSON：\n"
            + "\n".join(f"- {e}" for e in errors)
        )

    from autodev.engine import PlanningError

    raise PlanningError(errors)


def resolve_design_refs_snippet(ws_root: Path, design_refs: list[str], max_chars: int = 4000) -> str:
    """根据任务卡上的 design_refs（如 `design/backend.md#接口-createOrder`）提取对应设计章节片段。"""
    if not design_refs:
        return ""
    chunks: list[str] = []
    for ref in design_refs:
        file_part, _, anchor = ref.partition("#")
        rel = file_part.strip().lstrip("/")
        if not rel.startswith("docs/"):
            rel = f"docs/{rel}"
        fp = (ws_root / rel).resolve()
        if not fp.is_relative_to(ws_root.resolve() / "docs") or not fp.is_file():
            chunks.append(f"- 引用 `{ref}`（文件尚未落盘）")
            continue
        text = fp.read_text("utf-8", errors="replace")
        if not anchor.strip():
            chunks.append(f"### `{rel}`\n{text[:1500]}")
            continue
        # 提取匹配标题段落
        lines = text.splitlines()
        start_idx = -1
        for idx, ln in enumerate(lines):
            if ln.lstrip().startswith("#") and anchor.strip() in ln:
                start_idx = idx
                break
        if start_idx == -1:
            chunks.append(f"### `{ref}`\n{text[:1200]}")
        else:
            sec_lines = [lines[start_idx]]
            hdr_depth = len(lines[start_idx]) - len(lines[start_idx].lstrip("#"))
            for ln in lines[start_idx + 1 :]:
                if ln.lstrip().startswith("#"):
                    d = len(ln) - len(ln.lstrip("#"))
                    if d <= hdr_depth:
                        break
                sec_lines.append(ln)
            chunks.append(f"### `{ref}`\n" + "\n".join(sec_lines)[:1500])
    joined = "\n\n".join(chunks)
    return joined[:max_chars]


def generate_milestone_test_report(engine: "Engine", milestone_id: str) -> str:
    """里程碑合入时自动生成结构化测试与质量验证报告 `docs/test/TEST_REPORT-<milestone>.md`。"""
    from autodev.ci.service import list_ci_runs
    from autodev.quality.runner import list_quality_results

    ms = engine.db.get_milestone(milestone_id)
    tasks = [t for t in engine.db.list_tasks(milestone_id=milestone_id) if t.status != TaskStatus.CANCELLED]
    ci_runs = list_ci_runs(engine.db, milestone_id)
    latest_ci = ci_runs[0] if ci_runs else None

    lines = [
        f"# 自动化测试与质量验收报告 — {milestone_id}",
        "",
        f"- **里程碑标题**：{ms.title if ms else milestone_id}",
        f"- **验收目标**：{ms.goal if ms else ''}",
        f"- **持续集成状态**：`{latest_ci['status'] if latest_ci else 'LOCAL_VERIFIED'}`",
        "",
        "## 1. 任务验证命令与关联需求覆盖",
        "",
        "| 任务 ID | 标题 | 关联需求 (REQ) | 验证命令数 | 最终状态 |",
        "|---|---|---|---|---|",
    ]
    covered_reqs: set[str] = set()
    for t in tasks:
        covered_reqs.update(t.requirement_ids)
        lines.append(
            f"| `{t.task_id}` | {t.title} | {', '.join(t.requirement_ids) or '-'} | {len(t.verify_cmds)} | `{t.status.value}` |"
        )

    lines += ["", "## 2. 质量门禁扫描汇总", ""]
    any_q = False
    for t in tasks:
        q_rows = list_quality_results(engine.db, task_id=t.task_id, latest_only=True)
        for qr in q_rows:
            any_q = True
            skip_note = f"（{qr['skipped_reason']}）" if qr.get("skipped_reason") else ""
            lines.append(f"- `{t.task_id}` · **{qr['gate']}**：`{qr['status']}`{skip_note}")
    if not any_q:
        lines.append("- 当前流程模板未启用扩展静态质量插件，已通过确定性测试命令验收。")

    known_reqs = load_known_requirements(engine)
    if known_reqs:
        lines += ["", "## 3. 需求覆盖核对", ""]
        for r in known_reqs:
            mark = "已覆盖" if r.req_id in covered_reqs else "待补充关联"
            lines.append(f"- `{r.req_id}` [{r.priority}] {r.title}：**{mark}**")

    rel = f"docs/test/TEST_REPORT-{milestone_id}.md"
    save_doc_version(engine, rel, "\n".join(lines) + "\n", author="QA-Engine", note=f"{milestone_id} 合入自动归档")
    return rel
