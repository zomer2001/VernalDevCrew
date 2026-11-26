"""P0 boundary probes use real persistence, document conversion, gates and trace readers."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from autodev.council import synthesize_council
from autodev.db import Database
from autodev.docs_center.service import (
    DocumentConflictError, get_doc, list_docs, save_doc_version, sync_standard_docs_from_artifacts,
)
from autodev.engine import Engine, init_workspace
from autodev.harnesses.mock import MockHarness
from autodev.models import ProductSpec, RequirementItem
from autodev.process.runner import decide_gate, ensure_gate_record, is_gate_passed, latest_gate_record
from autodev.process.runner import GateConflictError
from autodev.process.runner import load_builtin_template, save_custom_template
from autodev.server.app import create_app
from autodev.trace.scan import extract_requirements_from_sources
from autodev.trace.service import _collect_design_links, sync_requirements


def test_application_imports_in_a_fresh_process():
    import subprocess
    import sys
    result = subprocess.run([sys.executable, "-c", "from autodev.server.app import create_app; from autodev.cli import app"],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.fixture
def eng(tmp_path):
    engine = Engine(init_workspace(tmp_path / "project", name="P0", collaboration_mode="files"), harness=MockHarness())
    yield engine
    engine.db.close()


def gate(eng, *, force=False, gate_id="srs_approval", scope_id="project"):
    return ensure_gate_record(eng.db, stage_id="requirements", gate_id=gate_id, kind="human_approval",
                              scope_type="project" if scope_id == "project" else "milestone", scope_id=scope_id,
                              force_new_round=force)


def test_pending_and_approved_document_revisions_cannot_be_reapproved(eng):
    first = save_doc_version(eng, "SRS.md", "# v1")
    pending = gate(eng)
    save_doc_version(eng, "SRS.md", "# v2", expected_sha=first["content_sha"])
    with pytest.raises(GateConflictError):
        decide_gate(eng.db, pending["record_id"], "pass", expected_sha=first["content_sha"])
    second = latest_gate_record(eng.db, "srs_approval", "project", "project")
    assert second["round"] == 2 and second["target_sha"] != first["content_sha"]
    decide_gate(eng.db, second["record_id"], "pass", expected_sha=second["target_sha"])
    assert is_gate_passed(eng.db, "srs_approval", "project", "project")
    save_doc_version(eng, "SRS.md", "# v3")
    assert not is_gate_passed(eng.db, "srs_approval", "project", "project")
    history = dict(eng.db._query("SELECT * FROM gate_records WHERE record_id=?", (second["record_id"],))[0])
    assert history["status"] == "STALE" and history["decision_status"] == "PASSED"
    assert list_docs(eng)[0]["status"] == "STALE"
    assert get_doc(eng, "SRS.md", version=1)["content"] == "# v1\n"
    snapshot = json.loads(history["target_snapshot_json"])[0]
    assert eng.db._query("SELECT content_text FROM doc_versions WHERE id=?", (snapshot["id"],))[0][0] == "# v2\n"


def test_force_new_pending_round_is_real_and_old_round_cannot_decide(eng):
    save_doc_version(eng, "SRS.md", "# Spec")
    old = gate(eng)
    new = gate(eng, force=True)
    assert new["round"] == old["round"] + 1
    with pytest.raises(GateConflictError):
        decide_gate(eng.db, old["record_id"], "pass")
    assert gate(eng)["record_id"] == new["record_id"]


def test_external_edits_and_missing_artifacts_fail_closed(eng):
    missing = gate(eng)
    with pytest.raises(GateConflictError):
        decide_gate(eng.db, missing["record_id"], "pass")
    save_doc_version(eng, "SRS.md", "# Original")
    current = gate(eng)
    decide_gate(eng.db, current["record_id"], "pass")
    (eng.ws.root / "docs/SRS.md").write_text("# Changed outside the API", encoding="utf-8")
    assert not is_gate_passed(eng.db, "srs_approval", "project", "project")
    with pytest.raises(GateConflictError):
        decide_gate(eng.db, current["record_id"], "pass")
    doc = get_doc(eng, "SRS.md")
    assert doc["version"] == 2
    assert gate(eng)["target_sha"] == doc["content_sha"]


def test_concurrent_editors_have_one_winner_and_recoverable_history(eng):
    original = save_doc_version(eng, "SRS.md", "# Original")
    def edit(n):
        try:
            save_doc_version(eng, "SRS.md", f"# Editor {n}", expected_sha=original["content_sha"])
            return "saved"
        except DocumentConflictError:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(edit, (1, 2)))
    assert sorted(results) == ["conflict", "saved"]
    assert get_doc(eng, "SRS.md")["version"] == 2
    assert get_doc(eng, "SRS.md", version=1)["content"] == "# Original\n"


def test_http_revision_conflicts_and_reads_do_not_regenerate_srs(eng):
    save_doc_version(eng, "SRS.md", "# Original")
    rec = gate(eng)
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"ok": True}))
    client = TestClient(create_app(eng, proxy_transport=transport))
    old = client.get("/vernal-api/docs/docs/SRS.md").json()
    saved = client.put("/vernal-api/docs/docs/SRS.md", json={"content": "# Human revision", "expected_sha": old["content_sha"]})
    assert saved.status_code == 200
    assert client.put("/vernal-api/docs/docs/SRS.md", json={"content": "# Stale editor", "expected_sha": old["content_sha"]}).status_code == 409
    assert client.post(f"/vernal-api/gates/{rec['record_id']}/decide", json={"decision": "pass", "expected_sha": rec["target_sha"]}).status_code == 409
    # A malformed source cannot overwrite a valid human revision on a GET or export.
    eng.ws.prd_json_path.write_text("{broken", encoding="utf-8")
    assert client.get("/vernal-api/docs").status_code == 200
    assert client.get("/vernal-api/docs/export?kinds=SRS&format=md").text == "# Human revision\n"
    assert client.post("/vernal-api/docs/sync").status_code == 422


def test_council_schema_to_srs_to_requirements_and_nested_design(eng):
    requirement = {"req_id": "REQ-001", "title": "保存回答", "statement": "提交后持久化回答", "priority": "P0", "acceptance": ["重启后仍可读取"]}
    def director(*_):
        return {"executive_summary": "澄清", "resolved_debates": [], "open_risks": [], "contract_strategy": "增量",
                "adrs": [{"adr_id": "ADR-001", "title": "保存", "decision": "SQLite", "rationale": "离线", "affected_squads": ["core"]}],
                "spec_markdown": "# 澄清", "requirements": [requirement], "nfrs": []}
    eng.harness.role_handlers["director"] = director
    asyncio.run(synthesize_council(eng))
    ProductSpec.model_validate_json(eng.ws.prd_json_path.read_text())
    assert "REQ-001" in get_doc(eng, "SRS.md")["content"]
    assert sync_requirements(eng)[0]["acceptance"] == ["重启后仍可读取"]
    design = eng.ws.state_dir / "design/M1/core.json"
    design.parent.mkdir(parents=True)
    design.write_text(json.dumps({"squad_id": "core", "interfaces": [{"name": "save", "req_ids": ["REQ-001"]}],
                                 "test_cases": [{"tc_id": "TC-001", "req_ids": ["REQ-001"]}]}))
    assert set(_collect_design_links(eng)["REQ-001"]) == {"M1/core#save", "M1/core#TC-001"}


def test_requirement_removal_restoration_and_duplicate_rejection(eng):
    text = "| REQ-001 | P0 | First | Saved |\n| REQ-002 | P1 | Second | Read |\n"
    save_doc_version(eng, "SRS.md", text)
    assert len(sync_requirements(eng)) == 2
    save_doc_version(eng, "SRS.md", text.splitlines()[0])
    assert [r["req_id"] for r in sync_requirements(eng)] == ["REQ-001"]
    assert eng.db._query("SELECT status FROM requirements WHERE req_id='REQ-002'")[0][0] == "RETIRED"
    save_doc_version(eng, "SRS.md", text)
    assert len(sync_requirements(eng)) == 2
    save_doc_version(eng, "SRS.md", text + "| REQ-001 | P0 | Collision | Other |")
    with pytest.raises(ValueError, match="重复需求"):
        sync_requirements(eng)
    assert eng.db._query("SELECT COUNT(*) FROM requirements WHERE status='ACTIVE'")[0][0] == 2


def test_feature_namespaces_preserve_both_requirements_and_nfr_acceptance(eng):
    items = [RequirementItem(req_id="REQ-001", feature_id=f, title=f, statement="保存", acceptance=[f]) for f in ("login", "export")]
    spec = ProductSpec(product_vision="demo", core_workflows=[], edge_cases=[], error_conventions=[], global_acceptance_redlines=[],
                       spec_markdown="", requirements=items, nfrs=[RequirementItem(req_id="NFR-001", title="延迟", statement="延迟有界", acceptance=["P95 < 200ms"])])
    raw = spec.model_dump(); raw["adrs"] = [{"title": "legacy preserved"}]
    original = json.dumps(raw)
    eng.ws.prd_json_path.write_text(original)
    sync_standard_docs_from_artifacts(eng)
    requirements = {r["req_id"]: r for r in extract_requirements_from_sources(eng)}
    assert set(requirements) == {"login:REQ-001", "export:REQ-001", "NFR-001"}
    assert requirements["NFR-001"]["acceptance"] == ["P95 < 200ms"]
    assert eng.ws.prd_json_path.read_text() == original


def test_explicitly_empty_canonical_requirements_retire_stale_markdown(eng):
    save_doc_version(eng, "SRS.md", "| REQ-001 | P0 | Old requirement | Old acceptance |")
    assert len(sync_requirements(eng)) == 1
    spec = ProductSpec(product_vision="Removed", core_workflows=[], edge_cases=[], error_conventions=[],
                       global_acceptance_redlines=[], spec_markdown="", requirements=[], nfrs=[])
    eng.ws.prd_json_path.write_text(spec.model_dump_json())
    assert sync_requirements(eng) == []
    assert eng.db._query("SELECT status FROM requirements WHERE req_id='REQ-001'")[0][0] == "RETIRED"


def test_milestone_design_approval_covers_all_members_and_isolated_scope(eng):
    save_doc_version(eng, "design/M1/core.md", "# M1 core")
    save_doc_version(eng, "design/M2/core.md", "# M2 core")
    first = gate(eng, gate_id="detailed_design_approval", scope_id="M1")
    second = gate(eng, gate_id="detailed_design_approval", scope_id="M2")
    decide_gate(eng.db, first["record_id"], "pass")
    decide_gate(eng.db, second["record_id"], "pass")
    save_doc_version(eng, "design/M1/api.md", "# Another squad")
    assert not is_gate_passed(eng.db, "detailed_design_approval", "milestone", "M1")
    assert is_gate_passed(eng.db, "detailed_design_approval", "milestone", "M2")


def test_database_reopen_preserves_snapshot_and_approval(eng):
    save_doc_version(eng, "SRS.md", "# Stable")
    rec = gate(eng)
    decide_gate(eng.db, rec["record_id"], "pass")
    other = Database(eng.ws.db_path)
    try:
        assert is_gate_passed(other, "srs_approval", "project", "project")
        assert other._query("SELECT content_text FROM doc_versions")[0][0] == "# Stable\n"
    finally:
        other.close()


@pytest.mark.parametrize("path,content", [("docs/design/M1/new.md", "# New member"),
                                        (".autodev/design/M1/new.json", '{"interfaces": []}')])
def test_unarchived_design_member_blocks_approval_until_reviewed(eng, path, content):
    for scope in ("M1", "M2"):
        save_doc_version(eng, f"design/{scope}/core.md", f"# {scope}")
        rec = gate(eng, gate_id="detailed_design_approval", scope_id=scope)
        decide_gate(eng.db, rec["record_id"], "pass")
    new_file = eng.ws.root / path
    new_file.parent.mkdir(parents=True, exist_ok=True)
    new_file.write_text(content, encoding="utf-8")
    assert not is_gate_passed(eng.db, "detailed_design_approval", "milestone", "M1")
    assert is_gate_passed(eng.db, "detailed_design_approval", "milestone", "M2")
    docs = list_docs(eng)
    assert next(d for d in docs if d["path"] == "docs/design/M1/core.md")["status"] == "STALE"
    assert next(d for d in docs if d["path"] == "docs/design/M2/core.md")["status"] == "APPROVED"
    pending = latest_gate_record(eng.db, "detailed_design_approval", "milestone", "M1")
    assert pending["status"] == "PENDING" and pending["round"] == 2
    assert path in {item["path"] for item in json.loads(pending["target_snapshot_json"])}
    decide_gate(eng.db, pending["record_id"], "pass")
    assert is_gate_passed(eng.db, "detailed_design_approval", "milestone", "M1")


@pytest.mark.parametrize("runtime", ["local", "loopforge"])
def test_stale_spec_blocks_shared_scheduler_and_publication(eng, runtime):
    from autodev.models import PlanTask, ProjectStatus
    from autodev.engine import OpsError
    from autodev.scheduler import Scheduler
    from test_audit_fixes import _seed_plan
    eng.settings.runtime_backend = runtime
    tpl = load_builtin_template("standard")
    tpl.review_policy = "off"
    for stage in tpl.stages:
        stage.gates = [g for g in stage.gates if g.id == "srs_approval"]
    save_custom_template(eng.ws, json.dumps(tpl.model_dump()))
    task = PlanTask(task_id="T-001", milestone_id="M1", module="core", role="coder", title="Implement", description="Implement",
                    allowed_paths=["core/"], acceptance_criteria=["Works"], depends_on=[], doc_slice_ids=[], verify_cmds=["true"], priority=1)
    eng.db.save_plan(_seed_plan([task]), max_retries=1)
    eng.set_status(ProjectStatus.APPROVED)
    save_doc_version(eng, "SRS.md", "# First")
    rec = gate(eng)
    decide_gate(eng.db, rec["record_id"], "pass")
    save_doc_version(eng, "SRS.md", "# Changed")
    scheduler = Scheduler(eng)
    scheduler._dispatch_ready("M1")
    assert not scheduler.running_tasks
    assert not eng.harness.calls
    with pytest.raises(OpsError, match="当前文档版本尚未批准"):
        eng._reserve_publication("M1")


def test_structured_design_drift_invalidates_rendered_document_approval(eng):
    from autodev.docs_center.service import _save_snapshot
    raw = eng.ws.state_dir / "design/M1/core.json"
    _save_snapshot(eng, raw, '{"interfaces": []}', author="Architect", note="Design snapshot")
    save_doc_version(eng, "design/M1/core.md", "# Rendered design")
    rec = gate(eng, gate_id="detailed_design_approval", scope_id="M1")
    decide_gate(eng.db, rec["record_id"], "pass")
    raw.write_text('{"interfaces": ["changed"]}')
    assert not is_gate_passed(eng.db, "detailed_design_approval", "milestone", "M1")
    with pytest.raises(GateConflictError):
        decide_gate(eng.db, rec["record_id"], "pass")


def test_unchanged_generator_does_not_erase_human_revision(eng):
    spec = ProductSpec(product_vision="Demo", core_workflows=[], edge_cases=[], error_conventions=[],
                       global_acceptance_redlines=[], spec_markdown="# Draft")
    eng.ws.prd_json_path.write_text(spec.model_dump_json())
    sync_standard_docs_from_artifacts(eng)
    save_doc_version(eng, "SRS.md", "# Human revision")
    assert "docs/SRS.md" not in sync_standard_docs_from_artifacts(eng)
    assert get_doc(eng, "SRS.md")["content"] == "# Human revision\n"


def test_failed_file_write_rolls_back_snapshot_and_preserves_approval(eng, monkeypatch):
    import autodev.docs_center.service as docs
    save_doc_version(eng, "SRS.md", "# Stable")
    rec = gate(eng)
    decide_gate(eng.db, rec["record_id"], "pass")
    def fail(*_):
        raise OSError("disk write failed")
    monkeypatch.setattr(docs.os, "replace", fail)
    with pytest.raises(OSError):
        save_doc_version(eng, "SRS.md", "# Must not be approved")
    assert len(eng.db._query("SELECT id FROM doc_versions")) == 1
    assert get_doc(eng, "SRS.md")["content"] == "# Stable\n"
    assert is_gate_passed(eng.db, "srs_approval", "project", "project")


def test_two_database_connections_create_one_pending_round(eng):
    save_doc_version(eng, "SRS.md", "# Concurrent request")
    def request(_):
        db = Database(eng.ws.db_path)
        try:
            return ensure_gate_record(db, stage_id="requirements", gate_id="srs_approval", kind="human_approval",
                                      scope_type="project", scope_id="project")["record_id"]
        finally:
            db.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(request, (1, 2)))
    assert ids[0] == ids[1]
