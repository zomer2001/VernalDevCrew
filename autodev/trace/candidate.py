"""Match executed passing pytest cases to markers in the checked-out candidate."""
import ast
import os
import re
from pathlib import Path
from xml.etree import ElementTree


def candidate_test_markers(root):
    """Index the candidate before verification commands can change its source files."""
    markers = {}
    for directory, subdirs, names in os.walk(root, followlinks=False):
        subdirs[:] = [d for d in subdirs if d not in {".git",".venv",".autodev","node_modules","__pycache__"}]
        for name in names:
            path = Path(directory) / name
            if path.suffix != ".py" or path.is_symlink():
                continue
            source = path.read_text(encoding="utf-8", errors="replace")
            if "mark.req" not in source and "REQ-" not in source:
                continue
            try:
                module = ast.parse(source)
            except SyntaxError:
                continue
            prefix = ".".join(path.relative_to(root).with_suffix("").parts)
            lines = source.splitlines()
            def collect(parent, classname):
                for item in parent.body:
                    if isinstance(item, ast.ClassDef):
                        collect(item, classname + "." + item.name)
                    elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        ids = set()
                        for dec in item.decorator_list:
                            if isinstance(dec, ast.Call) and ast.unparse(dec.func) in ("pytest.mark.req", "mark.req"):
                                ids.update(arg.value for arg in dec.args if isinstance(arg,ast.Constant) and isinstance(arg.value,str))
                        for line in lines[item.lineno-1:item.end_lineno]:
                            if line.lstrip().startswith("#"):
                                ids.update(re.findall(r"\bREQ-[\w.-]+", line))
                        if ids:
                            markers[(classname, item.name)] = ids
            collect(module, prefix)
    return markers


def verified_pytest_requirements(markers, report):
    if not report.is_file():
        return set()
    try:
        cases = ElementTree.parse(report).iter("testcase")
    except ElementTree.ParseError:
        return set()
    verified = set()
    for case in cases:
        if not any(case.find(tag) is not None for tag in ("failure", "error", "skipped")):
            key = case.get("classname", ""), case.get("name", "").split("[", 1)[0]
            verified.update(markers.get(key, ()))
    return verified


def delivery_evidence(db, milestone, candidate, *, release_tag=None, publication=None):
    import json
    tasks = []
    for t in db.list_tasks(milestone_id=milestone):
        if t.status == "CANCELLED":
            continue
        ref = t.artifact_digest or t.candidate_commit_sha
        rows = db._query("SELECT * FROM candidate_evidence WHERE task_id=? AND candidate=? ORDER BY verified_at DESC LIMIT 1",
                         (t.task_id, ref))
        receipt = dict(rows[0]) if rows else {}
        native_run = receipt.get("run_id") or t.loopforge_run_id
        dispatch = db._query("SELECT lf_task_id,lf_run_id,actor,pool FROM dispatch_records "
                             "WHERE task_id=? AND lf_run_id=? ORDER BY dispatch_id DESC LIMIT 1", (t.task_id,native_run))
        approvals = db._query("SELECT lf_task_id,lf_run_id,actor,state FROM dispatch_records WHERE command_key=?",
                              (f"signoff:{native_run}:{ref}",))
        quality = db._query("SELECT id,gate,status,metrics_json,skipped_reason FROM quality_results WHERE task_id=? AND attempt=?",
                            (t.task_id, receipt.get("attempt", t.retry_count+1)))
        tasks.append({"task_id": t.task_id, "issue_id": receipt.get("issue_id") or t.loopforge_issue_id,
                      "lf_task_id": dispatch[0]["lf_task_id"] if dispatch else None, "run_id": native_run,
                      "native_dispatch": dict(dispatch[0]) if dispatch else None,
                      "native_signoff": dict(approvals[0]) if approvals else None,
                      "candidate":ref, "base":db.meta_get(f"verified-base:{t.task_id}:{ref}") or t.base_sha,
                      "requirements":t.requirement_ids, "design_refs":t.design_refs,
                      "review_context":receipt.get("review_context"), "diff_digest":receipt.get("diff_digest"),
                      "verified":json.loads(receipt.get("verified_requirements","[]")),
                      "quality_ids":[r["id"] for r in quality], "quality_results":[dict(r) for r in quality]})
    return {"candidate":candidate,"tasks":tasks,"release_tag":release_tag,"publication":publication,
            "design_gates":[dict(r) for r in db._query("SELECT record_id,gate_id,scope_id,round,status,actor,evidence_json "
                "FROM gate_records WHERE stage_id='detailed_design' AND (scope_type='project' OR scope_id=?)", (milestone,))],
            "reviews":[dict(r) for r in db._query("SELECT review_id,scope_id,round,status,head_ref,reviewer,decided_at FROM code_reviews WHERE milestone_id=?", (milestone,))],
            "ci":[dict(r) for r in db._query("SELECT ci_run_id,round,provider,external_id,status,commit_sha,candidate_ref,url FROM ci_runs WHERE milestone_id=? AND candidate_ref=?",
                                              (milestone,candidate))]}
