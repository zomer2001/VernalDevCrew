"""Versioned input shared by native Issue dispatch and local agents."""
from __future__ import annotations

import hashlib
from pathlib import Path


def build_task_input_bundle(ws, db, task, *, decisions=None, source_root=None, files=None):
    specification = ""
    if db is not None and db.meta_get("speckit.enabled") == "1":
        from autodev.speckit.compiler import assert_package_current
        assert_package_current(db,task)
    if db is not None and getattr(task,"feature_id",""):
        from autodev.speckit.compiler import assert_package_current
        package = assert_package_current(db,task)
        row = db._query("SELECT d.content_text FROM spec_revisions r JOIN doc_versions d ON d.id=r.content_version_id WHERE r.rev_id=?",
                        (package.spec_rev_id,))[0]
        specification = "\n## 已批准的功能规格\n" + row["content_text"]
        for kind,feature in (("constitution","project"),("plan",task.feature_id)):
            source=db._query("SELECT d.content_text FROM spec_revisions r JOIN doc_versions d ON d.id=r.content_version_id "
                             "WHERE r.feature_id=? AND r.source_kind=? ORDER BY r.rev_no DESC LIMIT 1",(feature,kind))[0]
            specification += "\n## " + ("项目宪章" if kind == "constitution" else "技术方案") + "\n" + source["content_text"]
        specification += "\n<details><summary>版本诊断</summary>\n" + package.model_dump_json() + "\n</details>\n"
    ws_root = ws if isinstance(ws, Path) else getattr(ws, "root", None)
    if decisions is None and db is not None:
        decisions = db.list_decisions(active_only=True)
    if files is not None:
        from tempfile import TemporaryDirectory
        from autodev.artifacts import materialize
        with TemporaryDirectory() as temp:
            materialize(files, Path(temp))
            return build_task_input_bundle(ws, db, task, decisions=decisions, source_root=Path(temp))
    source_root = source_root or ws_root
    if db is not None and task.design_refs:
        from autodev.process.runner import latest_gate_record
        gate = latest_gate_record(db,"detailed_design_approval","milestone",task.milestone_id)
        if gate and gate["status"] not in ("PASSED","WAIVED"):
            raise ValueError("任务引用的详细设计尚未批准")
    design_block = ""
    if task.design_refs or task.requirement_ids:
        from autodev.docs_center.service import resolve_design_refs_snippet

        snippet = resolve_design_refs_snippet(ws_root, task.design_refs, milestone_id=task.milestone_id) if (ws_root and task.design_refs) else ""
        req_line = f"- **关联需求编号**：{', '.join(task.requirement_ids)}（编写测试时请用 `@pytest.mark.req(\"REQ-xxx\")` 或注释标注对应需求 ID）\n" if task.requirement_ids else ""
        ref_line = f"- **关联详细设计引用**：{', '.join(task.design_refs)}\n" if task.design_refs else ""
        design_block = f"\n## 关联需求与详细设计约束\n\n{req_line}{ref_line}{snippet}\n"

    repomap_block = ""
    if source_root and source_root.is_dir():
        from autodev.codeintel.repomap import build_repo_map, extract_seed_identifiers

        seeds = extract_seed_identifiers(task.title, task.description, " ".join(task.design_refs))
        rmap = build_repo_map(source_root, max_tokens=1800, seed_paths=task.allowed_paths, seed_identifiers=seeds, persist_cache=False)
        if rmap.text:
            repomap_block = f"\n## 存量代码库关键符号地图（Repo Map · PageRank 个性化排序）\n\n```\n{rmap.text}\n```\n"

    from autodev.prompts import _decisions_block
    body = ("允许修改路径：" + ", ".join(task.allowed_paths) + "\n验收标准：\n" +
            "\n".join(task.acceptance_criteria) + specification + design_block + repomap_block + _decisions_block(decisions))
    digest = hashlib.sha256(body.encode()).hexdigest()
    return f"\n## Vernal Task Input v1 · {digest}\n{body}\n"


def build_candidate_review_bundle(ws_root, task, diff, *, source_root=None):
    design_sec = ""
    if ws_root and task.design_refs:
        from autodev.docs_center.service import resolve_design_refs_snippet

        snip = resolve_design_refs_snippet(ws_root, task.design_refs, milestone_id=task.milestone_id)
        if snip:
            design_sec = f"\n## 详细设计对照片段\n\n{snip}\n"

    impact_sec = ""
    if ws_root and diff.strip():
        from autodev.codeintel.repomap import find_impact_callers

        impacts = find_impact_callers(source_root or ws_root, diff)
        if impacts:
            imp_lines = [f"- `{it['symbol']}`（定义于 `{it['defined_in']}`）被外部引用：`{', '.join(it['callers'])}`" for it in impacts]
            impact_sec = "\n## 变更符号影响面（外部调用方）\n\n" + "\n".join(imp_lines) + "\n"

    return design_sec + impact_sec


def resolve_candidate_artifact(ws, raw_text: str) -> str:
    """Resolve a LoopForge spillover stub back to the verified full candidate JSON on Vernal disk."""
    import json
    import re

    try:
        parsed = json.loads(raw_text)
    except Exception:
        return raw_text
    if not isinstance(parsed, dict) or "vernal_candidate_ref" not in parsed:
        return raw_text
    ref = str(parsed.get("vernal_candidate_ref") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", ref):
        raise ValueError("无效的 Vernal 候选产物引用")
    state_dir = ws.state_dir if hasattr(ws, "state_dir") else Path(ws) / ".autodev"
    blob_path = state_dir / "candidates" / f"{ref}.json"
    if not blob_path.is_file():
        raise ValueError(f"Vernal 网关缺少对应的候选产物缓存（{ref[:12]}）")
    raw = blob_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != ref:
        raise ValueError(f"Vernal 候选产物校验和异常（{ref[:12]}）")
    return raw.decode("utf-8")


def attach_native_review_context(workflow, *, upload_url: str = ""):
    """Bundle the same offline impact scanner and >48KB candidate spillover into the native packaging Script."""
    import copy
    import shlex
    from autodev.codeintel import repomap
    wf = copy.deepcopy(workflow)
    review = next((n for n in wf.get("nodes", []) if n["id"] == "review"), None)
    package = next((n for n in wf.get("nodes", []) if n["type"] == "script"), None)
    if not review or not package:
        return wf
    # A fresh Script process and isolated Python import path prevent candidate modules shadowing stdlib.
    if package["id"] == "record_mr":
        wf["nodes"].insert(0, {"id":"capture_base","name":"固定原生检出基线","type":"script","executionScope":"repositories","scriptLanguage":"shell",
            "script":"git rev-parse --verify refs/vernal/review-base >/dev/null 2>&1 || git update-ref refs/vernal/review-base HEAD"})
        wf["edges"].append({"id":"base_to_implement","source":"capture_base","target":"implement","when":"success"})
    scanner = Path(repomap.__file__).read_text()
    script = Path(__file__).with_name("native_review_context.py").read_text()
    env_prefix = f"VERNAL_CANDIDATE_UPLOAD_URL={shlex.quote(upload_url)} " if upload_url else ""
    package["script"] = (
        "(\n" + package["script"] + "\n) && " + env_prefix + "python3 -I - <<'VERNAL_IMPACT'\n"
        + scanner + "\n" + script + "\nVERNAL_IMPACT"
    )
    package.setdefault("artifactOutputs", []).append({"name":"vernal-review-context.md","path":"vernal-review-context.md",
                                                     "mediaType":"text/markdown","required":True})
    review["taskPrompt"] = (review.get("taskPrompt") or "") + " 对照 Issue 的已批详设和 vernal-review-context.md 审查真实候选。"
    return wf
