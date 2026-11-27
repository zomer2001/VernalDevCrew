"""Batch A: real Python/Node processes, persisted authority and bounded native workflows."""
import asyncio
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import httpx
import pytest

from autodev.models import WorkPackageDraft, WorkPackageStep, SpecStepResult, strict_json_schema
from autodev.loopforge.bootstrap import build_plan_workflow, build_plan_prepare_script, managed_asset
from autodev.loopforge.bridge import build_restore_command
from autodev.loopforge.client import LoopForgeClient, LoopForgeError
from autodev.speckit import adapter, compiler, service
from autodev.speckit.parser import legacy_drafts_to_packages, render_work_packages_markdown, render_tasks
from autodev.speckit.validate_output import canonical_json, validate_work_packages
from test_speckit import BRIEF, SPEC, eng, prepared, draft, save, work_packages

ROOT = Path(__file__).resolve().parents[1]


def package(key="save", task="T001", territory="src/save.py", **changes):
    value = work_packages()[0]
    value.update(package_key=key, allowed_paths=[territory], **changes)
    value["steps"][0]["task_key"] = task
    return value


def contract():
    schema = strict_json_schema(SpecStepResult)
    schema["properties"]["step"]["enum"] = ["tasks"]
    schema["properties"]["work_packages"]["minItems"] = 1
    schema["properties"]["clarifications"]["maxItems"] = 0
    schema.update({"x-vernal-step": "tasks", "x-vernal-req-ids": ["REQ-001", "NFR-002"],
                   "x-vernal-p0-req-ids": ["REQ-001"], "x-vernal-protected-paths": ["contracts/*"],
                   "x-vernal-existing-paths": [], "x-vernal-single-milestone": True,
                   "x-vernal-tasks-json-path": "specs/001-save/tasks.json",
                   "x-vernal-artifact-path": "specs/001-save/tasks.md"})
    return schema


def run_native(folder, runtime, schema, packages, *, link=None):
    folder.mkdir()
    (folder / ".vernal-schema.json").write_text(json.dumps(schema, ensure_ascii=False))
    payload = dict(step="tasks", content="模型手写内容不应成为真源", clarifications=[], findings=[],
                   handoff_brief=BRIEF, work_packages=packages)
    output = folder / "vernal-result.json"
    output.write_text(json.dumps(payload, ensure_ascii=False))
    original = output.read_bytes()
    if link:
        target, destination = link
        (folder / target).parent.mkdir(parents=True, exist_ok=True)
        (folder / target).symlink_to(destination, target_is_directory=True)
    executable = sys.executable if runtime == "python" else shutil.which("node")
    assert executable, "双运行时验收必须安装 Node.js"
    script = ROOT / "autodev/speckit" / ("validate_output.py" if runtime == "python" else "validate_output.cjs")
    result = subprocess.run([executable, str(script)], cwd=folder, capture_output=True, text=True, timeout=10)
    if result.returncode:
        assert output.read_bytes() == original
        assert not (folder / "specs/001-save/tasks.json").is_file()
        return result.returncode, result.stderr, None
    artifacts = {name: (folder / name).read_bytes() for name in
                 ["vernal-result.json", "specs/001-save/tasks.json", "specs/001-save/tasks.md"]}
    return result.returncode, result.stderr, artifacts


CASES = ["valid_steps", "valid_packages", "explicit_dependency", "uncovered_p0", "unknown_req", "no_req",
         "overlap", "protected", "contract_role", "existing", "step_cycle", "package_cycle", "derived_package_cycle",
         "unknown_step", "unknown_package", "duplicate_step", "duplicate_package", "bad_package", "bad_task",
         "bad_milestone", "bad_squad", "bad_module", "multiple_milestones", "allow_multiple_milestones",
         "blank_acceptance", "no_acceptance", "no_paths", "no_commands", "blank_command", "bad_quote", "bad_step_quote",
         "trailing_escape", "empty_quoted_command", "valid_quotes", "unicode_120", "unicode_121", "blank_description",
         "repeated_fields", "identifier_newline", "artifact_escape", "json_escape"]


@pytest.mark.parametrize("case", CASES)
def test_cross_runtime_compilation_contract(tmp_path, case):
    packages, schema = [package()], contract()
    first, step = packages[0], packages[0]["steps"][0]
    good = case in {"valid_steps", "valid_packages", "explicit_dependency", "contract_role", "allow_multiple_milestones", "valid_quotes", "unicode_120"}
    if case == "valid_steps":
        first["steps"].append({**copy.deepcopy(step), "task_key": "T002", "depends_on": ["T001"]})
    elif case in ("valid_packages", "explicit_dependency", "package_cycle", "derived_package_cycle", "multiple_milestones", "allow_multiple_milestones"):
        second = package("test", "T002", "tests")
        packages.append(second)
        if case in ("valid_packages", "derived_package_cycle"):
            second["steps"][0]["depends_on"] = ["T001"]
        if case == "explicit_dependency": second["depends_on_packages"] = ["save"]
        if case == "package_cycle":
            first["depends_on_packages"] = ["test"]; second["depends_on_packages"] = ["save"]
        if case == "derived_package_cycle":
            first["steps"].append({**copy.deepcopy(step), "task_key": "T003", "depends_on": ["T002"]})
        if case in ("multiple_milestones", "allow_multiple_milestones"):
            second["milestone_id"] = "M2"
            if case == "allow_multiple_milestones": schema["x-vernal-single-milestone"] = False
    elif case == "uncovered_p0": schema["x-vernal-p0-req-ids"].append("NFR-002")
    elif case == "unknown_req": step["requirement_ids"] = ["REQ-999"]
    elif case == "no_req": step["requirement_ids"] = []
    elif case == "overlap": packages.append(package("other", "T002", "src"))
    elif case in ("protected", "contract_role"):
        first["allowed_paths"] = ["contracts/api.json"]
        if case == "contract_role": first["role"] = "contract"
    elif case == "existing": schema["x-vernal-existing-paths"] = ["src"]
    elif case == "step_cycle": step["depends_on"] = ["T001"]
    elif case == "unknown_step": step["depends_on"] = ["T999"]
    elif case == "unknown_package": first["depends_on_packages"] = ["missing"]
    elif case == "duplicate_step": first["steps"].append(copy.deepcopy(step))
    elif case == "duplicate_package": packages.append(package("save", "T002", "tests"))
    elif case == "bad_package": first["package_key"] = "../bad"
    elif case == "bad_task": step["task_key"] = "T01"
    elif case in ("bad_milestone", "bad_squad", "bad_module"):
        first[{"bad_milestone": "milestone_id", "bad_squad": "squad", "bad_module": "module"}[case]] = "../bad"
    elif case == "blank_acceptance": first["acceptance_criteria"] = [" "]
    elif case == "no_acceptance": first["acceptance_criteria"] = []
    elif case == "no_paths": first["allowed_paths"] = []
    elif case == "no_commands": first["verify_cmds"] = []
    elif case == "blank_command": first["verify_cmds"] = [" "]
    elif case == "bad_quote": first["verify_cmds"] = ['echo "open']
    elif case == "bad_step_quote": step["verify_cmds"] = ["echo 'open"]
    elif case == "trailing_escape": first["verify_cmds"] = ["echo \\"]
    elif case == "empty_quoted_command": first["verify_cmds"] = ['""']
    elif case == "valid_quotes": first["verify_cmds"] = ['python -c "print(\'ok\')"', "printf '%s' 'ok'"]
    elif case in ("unicode_120", "unicode_121"): step["title"] = "😀" * (120 if case == "unicode_120" else 121)
    elif case == "blank_description": step["description"] = " "
    elif case == "repeated_fields": step["acceptance_criteria"] = ["不允许复制包字段"]
    elif case == "identifier_newline": first["package_key"] = "save\n"
    elif case == "artifact_escape": schema["x-vernal-artifact-path"] = "../tasks.md"
    elif case == "json_escape": schema["x-vernal-tasks-json-path"] = "../tasks.json"
    python = run_native(tmp_path / "py", "python", schema, packages)
    node = run_native(tmp_path / "js", "node", schema, packages)
    assert python == node, (case, python[:2], node[:2])
    assert python[0] == (0 if good else 1), python[1]
    if good:
        parsed = [WorkPackageDraft.model_validate(p) for p in packages]
        assert python[2]["specs/001-save/tasks.md"].decode() == render_work_packages_markdown(parsed)
        assert python[2]["specs/001-save/tasks.json"].decode() == canonical_json(packages)


@pytest.mark.parametrize("path", ["/tmp/escape", "C:/escape", "src\\file.py", "src/../escape", "src/./file", "src//file",
                                  ".git/config", "src/.autodev/file", ".specify/file", "specs/001/tasks.json",
                                  "docs", "docs/SRS.md", "src/*", "src/?", "src/[a]", "src/a]"])
def test_rejects_unsafe_paths_identically(tmp_path, path):
    value = package(); value["allowed_paths"] = [path]
    results = [run_native(tmp_path / runtime, runtime, contract(), [value]) for runtime in ("python", "node")]
    assert results[0] == results[1] and results[0][0] == 1


@pytest.mark.parametrize("link_path", ["specs", "specs/001-save/tasks.json", "src"])
def test_no_symlink_output_or_allowed_paths(tmp_path, link_path):
    outside = tmp_path / "outside"; outside.mkdir()
    results = [run_native(tmp_path / runtime, runtime, contract(), [package()], link=(link_path, outside))
               for runtime in ("python", "node")]
    assert results[0] == results[1] and results[0][0] == 1
    assert not list(outside.iterdir())


def test_strict_models_and_schema():
    schema = strict_json_schema(SpecStepResult)
    def check(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for child in node.values(): check(child)
        elif isinstance(node, list):
            for child in node: check(child)
    check(schema)
    assert len(WorkPackageStep.model_fields) == 6
    with pytest.raises(ValueError):
        SpecStepResult(step="tasks", content=render_tasks([draft()]), handoff_brief=BRIEF)
    assert SpecStepResult(step="tasks", content="", work_packages=[package()], handoff_brief=BRIEF)


def test_legacy_tasks_read_only_compatibility_merges_all_criteria(eng):
    prepared(eng)
    first = draft()
    second = first.model_copy(update={"task_key": "T002", "acceptance_criteria": ["补充验收"], "verify_cmds": ["echo checked"]})
    save(eng, "tasks", render_tasks([first, second]))
    path = eng.ws.root / "specs/001-save/tasks.md"
    before = path.read_bytes()
    compiled = compiler.compile_tasks(eng, "001-save", require_analysis=False)
    assert compiled[0].task.acceptance_criteria == first.acceptance_criteria + second.acceptance_criteria
    assert compiled[0].task.verify_cmds == first.verify_cmds + second.verify_cmds
    assert not path.with_suffix(".json").exists() and path.read_bytes() == before


def publish_packages(engine):
    old = service.latest(engine, "001-save", "tasks")
    return service.publish_revision(engine, "001-save", "tasks", "ignored", expected_rev_id=old["rev_id"],
                                    work_packages=[WorkPackageDraft.model_validate(package())])


def test_json_authority_fingerprint_projection_and_publication(eng):
    prepared(eng)
    revision = publish_packages(eng)
    assert revision["rel_path"] == "specs/001-save/tasks.json"
    assert json.loads(revision["content_text"]) == [package()]
    analysis = save(eng, "analyze", "# 一致性检查\n已核对完整工作包。\n")
    eng.db.meta_set(f"speckit.analysis:{analysis['rev_id']}", "[]")
    packages = compiler.compile_tasks(eng, "001-save")
    assert packages[0].tasks_sha == revision["content_sha"]
    ids = compiler.publish_tasks(eng, "001-save", expected_hashes=[p.compiled_task_hash for p in packages], actor="owner")
    assert compiler.publish_tasks(eng, "001-save", expected_hashes=[p.compiled_task_hash for p in packages], actor="owner") == ids
    compiler.assert_package_current(eng.db, eng.db.get_task(ids[0]))
    json_path = eng.ws.root / revision["rel_path"]
    json_path.write_text(json_path.read_text().replace("重启后仍可读取", "未经确认的新条件"))
    with pytest.raises(service.SpecConflictError): compiler.assert_package_current(eng.db, eng.db.get_task(ids[0]))


@pytest.mark.parametrize("change", ["delete_json", "modify_markdown", "legacy_write", "orphan_json"])
def test_never_falls_back_from_json_authority(eng, change):
    prepared(eng)
    folder = eng.ws.root / "specs/001-save"
    if change == "orphan_json": (folder / "tasks.json").write_text("[]")
    else: publish_packages(eng)
    if change == "delete_json": (folder / "tasks.json").unlink()
    if change == "modify_markdown": (folder / "tasks.md").write_text(render_tasks([draft()]))
    if change == "legacy_write":
        with pytest.raises(service.SpecConflictError, match="只读"):
            save(eng, "tasks", render_tasks([draft()]))
    else:
        with pytest.raises(service.SpecConflictError): compiler.compile_tasks(eng, "001-save", require_analysis=False)


@pytest.mark.parametrize("runtime", ["python", "node"])
def test_adapter_injects_compiler_baseline_and_persists_native_result(eng, tmp_path, monkeypatch, runtime):
    prepared(eng)
    async def execute(prompt, *, schema_path, **kwargs):
        schema = json.loads(schema_path.read_text())
        assert schema["x-vernal-req-ids"] == schema["x-vernal-p0-req-ids"] == ["REQ-001"]
        assert schema["x-vernal-protected-paths"] == list(eng.settings.protected_paths)
        assert schema["x-vernal-existing-paths"] == []
        result = run_native(tmp_path / runtime, runtime, schema, [package()])
        assert result[0] == 0, result[1]
        payload = json.loads(result[2]["vernal-result.json"])
        return SimpleNamespace(ok=True, structured=payload, session_id="native-contract", usage=SimpleNamespace(
            input_tokens=1, cached_tokens=0, output_tokens=1, reasoning_tokens=0))
    monkeypatch.setattr(eng.harness, "run_structured_readonly", execute)
    asyncio.run(adapter.run_step(eng, "001-save", "tasks"))
    assert compiler.compile_tasks(eng, "001-save", require_analysis=False)[0].task.title == package()["title"]


def test_three_node_workflow_contract():
    workflow = build_plan_workflow("project")
    assert [n["id"] for n in workflow["nodes"]] == ["prepare", "draft", "validate"]
    assert [(e["source"], e["target"], e["when"]) for e in workflow["edges"]] == [("prepare", "draft", "success"), ("draft", "validate", "success")]
    prepare, draft_node, validator = workflow["nodes"]
    assert prepare["type"] == "script" and prepare["maxAttempts"] == 1
    assert draft_node["maxAttempts"] == 2
    assert (validator["rejectTo"], validator["onFailure"], validator["maxReworks"], validator["maxAttempts"]) == ("draft", "reject", 2, 1)
    assert "python3" in validator["script"] and "node" in validator["script"]


@pytest.mark.parametrize("runtime", ["python", "node"])
@pytest.mark.parametrize("mode", ["inline", "restore", "missing", "empty", "failed_restore"])
def test_prepare_runs_offline_and_fails_before_draft(tmp_path, runtime, mode):
    bindir = tmp_path / "bin"; bindir.mkdir()
    exe = sys.executable if runtime == "python" else shutil.which("node")
    assert exe
    (bindir / ("python3" if runtime == "python" else "node")).symlink_to(exe)
    cwd = tmp_path / "workspace"; cwd.mkdir()
    files = {"PLAN_PROMPT.md": "只读任务说明", ".vernal-schema.json": '{"type":"object"}', "inputs/context.md": "只读上下文"}
    if mode == "missing": files.pop("PLAN_PROMPT.md")
    if mode == "empty": files[".vernal-schema.json"] = " "
    bundle = tmp_path / "context.json"; bundle.write_text(json.dumps({"files": files}))
    command = "" if mode == "inline" else "exit 7" if mode == "failed_restore" else build_restore_command(str(bundle), "http://127.0.0.1:1/unreachable", nested_key="files")
    script = build_plan_prepare_script(command)
    result = subprocess.run(["/bin/sh", "-c", script], cwd=cwd, env={**os.environ, "PATH": str(bindir)}, capture_output=True, text=True, timeout=10)
    assert result.returncode == (0 if mode in ("inline", "restore") else 1), result.stderr
    if mode == "restore":
        assert (cwd / "PLAN_PROMPT.md").read_text() == files["PLAN_PROMPT.md"]
        assert (cwd / "inputs/context.md").read_text() == files["inputs/context.md"]
    elif mode not in ("inline",): assert "规划准备环境故障" in result.stderr


def test_a2_workflow_reuses_id_updates_context_and_preserves_custom_assets(eng):
    source = managed_asset(build_plan_workflow("spec-test"))
    workflows, puts, publishes = {source["id"]: source}, [], []
    def handler(request):
        if request.method == "GET": return httpx.Response(200, json=list(workflows.values()))
        if request.url.path.endswith("/publish"):
            wid = request.url.path.split("/")[-2]; workflows[wid]["status"] = "published"; publishes.append(wid)
            return httpx.Response(200, json=workflows[wid])
        value = json.loads(request.content); puts.append(value); workflows[value["id"]] = value
        return httpx.Response(200, json={**value, "status": "draft"})
    client = LoopForgeClient("http://lf.test", transport=httpx.MockTransport(handler))
    async def prepare(stage, command, role="director", effort="high"):
        async with eng.harness._assets_lock:
            return await eng.harness._prepare_workflow(client, "spec-test", source["id"], role, stage, effort, command)
    stage = "speckit_tasks_001-save_" + "a" * 20
    wid = asyncio.run(prepare(stage, "true"))
    assert asyncio.run(prepare(stage, "true")) == wid and len(puts) == len(publishes) == 1
    assert asyncio.run(prepare(stage[:-20] + "b" * 20, "echo new")) == wid
    assert len(puts) == len(publishes) == 2 and len(workflows) == 2
    assert "echo new" in workflows[wid]["nodes"][0]["script"]
    assert asyncio.run(prepare(stage, "true", effort="medium")) != wid
    workflows[wid]["nodes"][1]["maxAttempts"] = 9
    with pytest.raises(LoopForgeError, match="手动修改"): asyncio.run(prepare(stage, "true"))
    assert workflows[source["id"]] == source


@pytest.mark.parametrize("space", ["\u0085", "\u001c", "\u2003", "\ufeff"])
@pytest.mark.parametrize("field", ["title", "acceptance_criteria", "verify_cmds"])
def test_unicode_whitespace_is_identical(tmp_path, space, field):
    value = package()
    value[field] = space if field == "title" else [space]
    results = [run_native(tmp_path / runtime, runtime, contract(), [value]) for runtime in ("python", "node")]
    assert results[0] == results[1]
    assert results[0][0] == (0 if space == "\ufeff" else 1)


@pytest.mark.parametrize("missing", ["x-vernal-artifact-path", "x-vernal-tasks-json-path"])
def test_tasks_cannot_succeed_without_persistence_contract(tmp_path, missing):
    schema = contract(); del schema[missing]
    results = [run_native(tmp_path / runtime, runtime, schema, [package()]) for runtime in ("python", "node")]
    assert results[0] == results[1] and results[0][0] == 1


def test_unmanaged_markdown_is_not_overwritten_by_first_json_revision(eng):
    prepared(eng)
    folder = eng.ws.root / "specs/002-save"; folder.mkdir()
    save(eng, "spec", SPEC, "002-save")
    (folder / "tasks.md").write_text("人工草稿请保留")
    with pytest.raises(service.SpecConflictError, match="未纳管"):
        service.publish_revision(eng, "002-save", "tasks", "", expected_rev_id=0,
                                 work_packages=[WorkPackageDraft.model_validate(package())])
    assert (folder / "tasks.md").read_text() == "人工草稿请保留"
    assert not (folder / "tasks.json").exists()
