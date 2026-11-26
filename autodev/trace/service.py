"""需求追溯矩阵聚合、断链检测、导出（CSV / DOCX）与 P0 覆盖门禁（WP-6）。"""

from __future__ import annotations

import csv
import io
import json
from typing import TYPE_CHECKING, Any

from autodev.ci.service import list_ci_runs
from autodev.db import now_iso
from autodev.docs_center.exporter import markdown_to_docx_bytes
from autodev.models import TaskStatus
from autodev.trace.scan import extract_requirements_from_sources, scan_workspace_tests
from autodev.docs_center.paths import iter_detailed_design_json_paths

if TYPE_CHECKING:
    from autodev.engine import Engine


def sync_requirements(engine: "Engine") -> list[dict[str, Any]]:
    """将 SRS / PRD 中的需求同步入库到 `requirements` 表。"""
    extracted = extract_requirements_from_sources(engine)
    ts = now_iso()
    with engine.db.transaction():
        # Parse and validate the complete source before retiring anything.
        engine.db._exec("UPDATE requirements SET status='RETIRED', updated_at=? WHERE status='ACTIVE'", (ts,))
        for req in extracted:
            engine.db._exec(
                "INSERT INTO requirements(req_id, title, priority, acceptance_json, source_section, status, updated_at)"
                " VALUES(?, ?, ?, ?, ?, 'ACTIVE', ?)"
                " ON CONFLICT(req_id) DO UPDATE SET title=excluded.title, priority=excluded.priority,"
                " acceptance_json=excluded.acceptance_json, source_section=excluded.source_section, status='ACTIVE', updated_at=excluded.updated_at",
                (
                    req["req_id"],
                    req["title"],
                    req["priority"],
                    json.dumps(req.get("acceptance") or [], ensure_ascii=False),
                    req.get("source_section") or "",
                    ts,
                ),
            )
    rows = engine.db._query("SELECT * FROM requirements WHERE status='ACTIVE' ORDER BY req_id ASC")
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        d["acceptance"] = json.loads(d.pop("acceptance_json", None) or "[]")
        out.append(d)
    return out


def _collect_design_links(engine: "Engine", milestone_id: str | None = None) -> dict[str, list[str]]:
    links: dict[str, list[str]] = {}
    design_dir = engine.ws.state_dir / "design"
    if not design_dir.is_dir():
        return links
    for jf in iter_detailed_design_json_paths(engine.ws, milestone_id):
        try:
            spec = json.loads(jf.read_text("utf-8"))
            if not isinstance(spec, dict):
                raise ValueError("详设顶层必须是对象")
        except (OSError, ValueError) as exc:
            engine.db.add_event("trace.design_failed", f"{jf.relative_to(design_dir)}: {exc}", actor="TraceService")
            raise ValueError(f"详设解析失败：{jf.name}") from exc
        mod = str(spec.get("squad_id") or spec.get("module") or jf.stem)
        if jf.parent != design_dir:
            mod = f"{jf.parent.name}/{mod}"
        for iface in spec.get("interfaces") or []:
            if isinstance(iface, dict):
                iname = str(iface.get("name") or iface.get("id") or "interface")
                for rid in iface.get("req_ids") or []:
                    links.setdefault(str(rid), []).append(f"{mod}#{iname}")
        for tc in spec.get("test_cases") or []:
            if isinstance(tc, dict):
                tc_id = str(tc.get("id") or tc.get("tc_id") or "TC")
                rid = str(tc.get("req_id") or "")
                if rid:
                    links.setdefault(rid, []).append(f"{mod}#{tc_id}")
                for r2 in tc.get("req_ids") or []:
                    links.setdefault(str(r2), []).append(f"{mod}#{tc_id}")
    return links


def build_trace_matrix(engine: "Engine") -> dict[str, Any]:
    reqs = sync_requirements(engine)
    design_links = _collect_design_links(engine)
    test_links = scan_workspace_tests(engine)
    all_tasks = [t for t in engine.db.list_tasks() if t.status != TaskStatus.CANCELLED]
    verified_by_task = {}
    for task in all_tasks:
        candidate = task.artifact_digest or task.candidate_commit_sha
        evidence = engine.db._query("SELECT verified_requirements FROM candidate_evidence WHERE task_id=? AND candidate=? "
                                    "ORDER BY verified_at DESC,attempt DESC LIMIT 1", (task.task_id, candidate))
        verified_by_task[task.task_id] = set(json.loads(evidence[0][0])) if evidence else set()
    ci_by_ms: dict[str, str] = {}
    for cr in list_ci_runs(engine.db):
        ms_id = str(cr["milestone_id"])
        if ms_id not in ci_by_ms:
            ci_by_ms[ms_id] = str(cr["status"])

    rows: list[dict[str, Any]] = []
    p0_total = 0
    p0_passed = 0
    covered_total = 0

    for req in reqs:
        rid = str(req["req_id"])
        prio = str(req["priority"]).upper()
        designs = sorted(set(design_links.get(rid, [])))
        linked_tasks = [t for t in all_tasks if rid in (t.requirement_ids or []) or rid in t.description or rid in t.title]
        task_ids = [t.task_id for t in linked_tasks]
        code_paths = sorted({p for t in linked_tasks for p in (t.allowed_paths or [])})
        tests = test_links.get(rid, [])

        broken: list[str] = []
        if not designs:
            broken.append("missing_design")
        if not task_ids:
            broken.append("missing_task")
        if not tests:
            broken.append("missing_test")

        # 判定验证结果
        if not tests:
            verify_status = "MISSING"
        elif linked_tasks and all(t.status in (TaskStatus.APPROVED, TaskStatus.MERGED) for t in linked_tasks):
            ms_ci_failed = any(ci_by_ms.get(t.milestone_id) == "FAILURE" for t in linked_tasks)
            verify_status = "FAILED" if ms_ci_failed else "PASSED"
        elif any(t.status == TaskStatus.FUSED for t in linked_tasks):
            verify_status = "FAILED"
        elif not linked_tasks:
            # 存量或独立补齐测试的场景：只要测试存在且最新 CI 未失败，记为 COVERED/PASSED
            any_ci_fail = any(v == "FAILURE" for v in ci_by_ms.values())
            verify_status = "FAILED" if any_ci_fail else "PASSED"
        else:
            verify_status = "PENDING"

        if verify_status != "PASSED":
            broken.append("not_verified")
        else:
            covered_total += 1

        if prio == "P0":
            p0_total += 1
            if verify_status == "PASSED":
                p0_passed += 1

        rows.append(
            {
                "req_id": rid,
                "title": req["title"],
                "priority": prio,
                "source_section": req.get("source_section") or "",
                "designs": designs,
                "tasks": task_ids,
                "code_paths": code_paths,
                "tests": tests,
                "candidate_verified": bool(linked_tasks) and all(rid in verified_by_task[t.task_id]
                    and ci_by_ms.get(t.milestone_id) not in {"FAILURE","CANCELLED","TIMEOUT"} for t in linked_tasks),
                "verify_status": verify_status,
                "broken_links": broken,
            }
        )

    p0_rate = 1.0 if p0_total == 0 else round(p0_passed / p0_total, 4)
    overall_rate = 1.0 if not rows else round(covered_total / len(rows), 4)
    return {
        "summary": {
            "total_requirements": len(rows),
            "covered_requirements": covered_total,
            "linked_coverage_rate": overall_rate,
            "verified_candidate_pass_rate": sum(r["candidate_verified"] for r in rows) / len(rows) if rows else 0.0,
            "overall_coverage_rate": overall_rate,
            "p0_total": p0_total,
            "p0_passed": p0_passed,
            "p0_coverage_rate": p0_rate,
        },
        "rows": rows,
    }


def check_p0_trace_gate(engine: "Engine", min_p0_rate: float = 1.0, *, verified_candidate: bool = False) -> tuple[bool, str]:
    """里程碑合入前的 P0 需求测试覆盖门禁。"""
    matrix = build_trace_matrix(engine)
    s = matrix["summary"]
    if s["p0_total"] == 0:
        return True, "当前无 P0 需求条目，追溯门禁放行"
    p0 = [r for r in matrix["rows"] if r["priority"] == "P0"]
    uncovered = [r["req_id"] for r in p0 if not (r["candidate_verified"] if verified_candidate else r["verify_status"] == "PASSED")]
    passed = len(p0) - len(uncovered)
    if passed / len(p0) < min_p0_rate:
        return (
            False,
            f"需求追溯门禁未通过：P0 需求测试覆盖率 {passed}/{s['p0_total']} "
            f"（未通过测试的 P0 需求：{', '.join(uncovered)}），禁止合入",
        )
    return True, f"P0 需求追溯门禁全部通过（{passed}/{s['p0_total']}）"


def export_trace_matrix_csv(matrix: dict[str, Any]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["REQ_ID", "Priority", "Title", "Designs", "Tasks", "CodePaths", "Tests", "VerifyStatus", "BrokenLinks"])
    for r in matrix.get("rows") or []:
        test_desc = "; ".join(f"{t['file']}::{t['test']}" for t in (r.get("tests") or []))
        writer.writerow(
            [
                r["req_id"],
                r["priority"],
                r["title"],
                "; ".join(r.get("designs") or []),
                "; ".join(r.get("tasks") or []),
                "; ".join(r.get("code_paths") or []),
                test_desc,
                r["verify_status"],
                "; ".join(r.get("broken_links") or []),
            ]
        )
    return buf.getvalue()


def export_trace_matrix_docx(matrix: dict[str, Any], *, project_name: str = "Vernal Project") -> bytes:
    s = matrix.get("summary") or {}
    md_lines = [
        f"# 需求双向追溯矩阵（RTM）· {project_name}",
        "",
        f"- **需求总数**：{s.get('total_requirements', 0)}",
        f"- **P0 需求通过率**：{s.get('p0_passed', 0)}/{s.get('p0_total', 0)} ({float(s.get('p0_coverage_rate', 1.0)) * 100:.1f}%)",
        f"- **全量需求验证覆盖率**：{float(s.get('overall_coverage_rate', 1.0)) * 100:.1f}%",
        "",
        "## 追溯明细表",
        "",
        "| 需求编号 | 优先级 | 需求标题 | 详设引用 | 关联任务 | 测试用例 | 验证状态 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in matrix.get("rows") or []:
        t_str = ", ".join(f"{t['file']}::{t['test']}" for t in (r.get("tests") or [])) or "缺失"
        md_lines.append(
            f"| {r['req_id']} | {r['priority']} | {r['title']} | "
            f"{', '.join(r.get('designs') or []) or '-'} | {', '.join(r.get('tasks') or []) or '-'} | "
            f"{t_str} | {r['verify_status']} |"
        )
    return markdown_to_docx_bytes("\n".join(md_lines), lab_name=f"{project_name} · 需求追溯矩阵")
