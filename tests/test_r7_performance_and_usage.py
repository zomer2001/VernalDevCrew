"""R7 regression tests: usage accuracy, concise context assembly, effort policy, profile conversion, timing & local validator replay."""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from autodev.config import Settings
from autodev.engine import Engine, init_workspace
from autodev.harnesses.base import Usage
from autodev.harnesses.loopforge_harness import LoopForgeHarness
from autodev.loopforge.bootstrap import build_vernal_agents
from autodev.loopforge.bridge import extract_run_usage
from autodev.loopforge.client import LoopForgeClient
from autodev.models import SpecFinding
from autodev.process.runner import load_builtin_template, load_template, save_custom_template
from autodev.server.app import create_app
from autodev.speckit import adapter, assets, missions, service, triage
from test_speckit import BRIEF, SPEC, approve, save


def test_r7_extract_run_usage_avoids_double_counting_and_marks_unknown():
    # Live LoopForge 0.4.0 contract: run.usage ALREADY aggregates node.usage.
    run_both = {
        "usage": {"inputTokens": 548157, "cacheReadTokens": 499712, "outputTokens": 6001},
        "nodes": {
            "draft": {"usage": {"inputTokens": 548157, "cacheReadTokens": 499712, "outputTokens": 6001}},
        },
    }
    u1 = extract_run_usage(run_both)
    assert u1.telemetry_known is True
    assert u1.source == "run"
    assert u1.input_tokens == 548157
    assert u1.cached_tokens == 499712
    assert u1.output_tokens == 6001

    # Fallback: sum nodes ONLY when top-level usage is absent.
    run_nodes_only = {
        "nodes": {
            "n1": {"usage": {"inputTokens": 100, "outputTokens": 20}},
            "n2": {"usage": {"inputTokens": 50, "outputTokens": 10, "reasoningTokens": 5}},
        },
    }
    u2 = extract_run_usage(run_nodes_only)
    assert u2.telemetry_known is True
    assert u2.source == "nodes_sum"
    assert u2.input_tokens == 150
    assert u2.output_tokens == 30
    assert u2.reasoning_tokens == 5

    # Genuine zero vs unknown telemetry
    u_zero = extract_run_usage({"usage": {"inputTokens": 0, "outputTokens": 0}})
    assert u_zero.telemetry_known is True
    assert u_zero.source == "run"
    assert u_zero.input_tokens == 0

    u_unknown = extract_run_usage({"id": "run-empty", "status": "succeeded", "nodes": {"draft": {"status": "succeeded"}}})
    assert u_unknown.telemetry_known is False
    assert u_unknown.source == "unknown"


def test_r7_triage_records_usage_on_success_and_failure_and_keeps_prompt_concise(tmp_path, monkeypatch):
    ws = init_workspace(tmp_path / "r7-triage", name="R7Triage", collaboration_mode="files")
    settings = Settings(
        collaboration_mode="files",
        runtime_backend="loopforge",
        loopforge_project_id="r7-proj",
        loopforge_url="http://lf.test",
        loopforge_token="tok",
    )
    eng = Engine(ws, settings=settings)
    try:
        eng._harness = LoopForgeHarness(settings, eng.db, ws, poll_interval=0.001)
        mission = missions.save(eng, "001-save", "可靠保存回答，刷新后仍可读取。", "feature", 0, actor="owner")
        # Duplicate wish slice to verify deduplication
        eng.db._exec(
            "INSERT INTO doc_slices(slice_id,source_file,heading_path,summary,content,char_count) VALUES(?,?,?,?,?,?)",
            ("S-01", "inputs/01-wish.md", "wish", "wish", "可靠保存回答，刷新后仍可读取。", 15),
        )
        seen_prompts: list[str] = []

        # 1. Success path records usage & session_id
        async def ok_run(prompt, *, schema_path, cwd, log_dir, role):
            seen_prompts.append(prompt)
            return SimpleNamespace(
                ok=True,
                session_id="run-intake-ok",
                structured=triage.Assessment(
                    goal_clear=True,
                    scope_clear=True,
                    acceptance_clear=True,
                    local_impact=True,
                    change_type="feature",
                    risk="low",
                    rationale="目标清晰",
                    questions=[],
                    prerequisites=[],
                    experience_items=[],
                    proposed_spec=SPEC,
                    handoff_brief=BRIEF,
                ).model_dump(),
                usage=Usage(input_tokens=437087, cached_tokens=400000, output_tokens=4440, reasoning_tokens=120, telemetry_known=True, source="run"),
                error="",
            )

        monkeypatch.setattr(eng.harness, "run_structured_readonly", ok_run)
        asyncio.run(triage.assess(eng, mission, actor="owner"))
        runs = eng.db.list_runs("_speckit_intake_001-save")
        assert len(runs) == 1
        assert runs[0]["session_id"] == "run-intake-ok"
        assert runs[0]["input_tokens"] == 437087
        assert runs[0]["cached_tokens"] == 400000
        assert runs[0]["output_tokens"] == 4440
        assert runs[0]["reasoning_tokens"] == 120
        # Prompt strips 38KB raw English upstream CLI scripts while keeping Chinese methodology & rules
        assert len(seen_prompts[0].encode("utf-8")) < 8_000
        assert "same_as_goal" in seen_prompts[0]
        assert "ajv" not in seen_prompts[0] or "不要" in seen_prompts[0] or "请勿搜索" in seen_prompts[0]

        # 2. Failure path after result STILL preserves usage & session_id
        async def invalid_run(prompt, *, schema_path, cwd, log_dir, role):
            return SimpleNamespace(
                ok=True,
                session_id="run-intake-bad",
                structured={"invalid": True},
                usage=Usage(input_tokens=12345, cached_tokens=10000, output_tokens=321, reasoning_tokens=0, telemetry_known=True, source="run"),
                error="",
            )

        monkeypatch.setattr(eng.harness, "run_structured_readonly", invalid_run)
        with pytest.raises(ValueError, match="格式校验"):
            asyncio.run(triage.assess(eng, mission, actor="owner"))
        runs2 = eng.db.list_runs("_speckit_intake_001-save")
        assert len(runs2) == 2
        assert runs2[1]["session_id"] == "run-intake-bad"
        assert runs2[1]["input_tokens"] == 12345
        assert runs2[1]["output_tokens"] == 321
    finally:
        eng.db.close()


def test_r7_effort_policy_and_harness_timing_and_dedup_schema(tmp_path):
    s_adaptive = Settings(harness="codex", reasoning_effort_policy="adaptive")
    assert s_adaptive.effort_for("director") == "high"
    assert s_adaptive.effort_for("coder") == "high"
    assert s_adaptive.effort_for("reviewer") == "high"
    assert s_adaptive.effort_for("pm") == "medium"
    assert s_adaptive.effort_for("qa_lead") == "medium"
    assert s_adaptive.effort_for("pm", risk="high") == "high"
    assert s_adaptive.effort_for("qa_lead", step="analyze") == "high"
    assert 'model_reasoning_effort="high"' in " ".join(s_adaptive.codex_extra_args)

    s_override = Settings(harness="codex", reasoning_effort_policy="adaptive", pm_reasoning_effort="low")
    assert s_override.effort_for("pm") == "low"

    agents = {a["id"]: a for a in build_vernal_agents("p", s_adaptive)}
    assert agents["p-vernal-coder"]["effort"] == "high"
    assert agents["p-vernal-reviewer"]["effort"] == "high"
    assert agents["p-vernal-planner"]["effort"] == "high"
    assert agents["p-vernal-planner-pm"]["effort"] == "medium"
    assert agents["p-vernal-planner-qa"]["effort"] == "medium"

    # Harness writes timing.json, avoids duplicating schema in PLAN_PROMPT.md, and skips redundant rule PUTs
    ws = init_workspace(tmp_path / "r7-harness", name="R7Harness", collaboration_mode="files")
    settings = Settings(
        collaboration_mode="files",
        runtime_backend="loopforge",
        loopforge_project_id="r7-h",
        loopforge_url="http://lf.test",
        loopforge_token="tok",
    )
    eng = Engine(ws, settings=settings)
    try:
        service.enable(eng, "vernal-core")
        save(eng, "constitution", "# 项目宪章\n交付须有验收依据。\n")
        save(eng, "spec", SPEC)
        rule_puts = 0

        def handler(req: httpx.Request) -> httpx.Response:
            nonlocal rule_puts
            p = req.url.path
            if p == "/api/runners":
                return httpx.Response(200, json=[{"id": "r1", "status": "online", "isDefault": True, "runtimes": [{"provider": "codex"}]}])
            if p == "/api/sandboxes":
                return httpx.Response(200, json=[{"id": "local"}])
            if p == "/api/rules" and req.method == "POST":
                return httpx.Response(200, json={**json.loads(req.content), "status": "draft"})
            if p.startswith("/api/rules/") and req.method == "PUT":
                rule_puts += 1
                return httpx.Response(200, json={"status": "published"})
            if p in ("/api/rules", "/api/agents", "/api/skills", "/api/mcp-servers", "/api/workflows"):
                return httpx.Response(200, json=[] if req.method == "GET" else {**json.loads(req.content), "status": "draft"})
            if p.endswith("/publish"):
                return httpx.Response(200, json={"status": "published"})
            if p == "/api/issues" and req.method == "POST":
                return httpx.Response(201, json={"id": "iss-1", **json.loads(req.content)})
            if p.endswith("/dispatch"):
                return httpx.Response(200, json={"latestRunId": "run-r7-1"})
            if p == "/api/runs/run-r7-1":
                payload = dict(step="checklist", content="# 规格检查\n通过。\n", clarifications=[], findings=[], handoff_brief=BRIEF)
                return httpx.Response(200, json={
                    "id": "run-r7-1",
                    "status": "succeeded",
                    "usage": {"inputTokens": 2000, "outputTokens": 300},
                    "nodes": {
                        "draft": {"status": "succeeded", "attempt": 1, "usage": {"inputTokens": 2000, "outputTokens": 300}},
                        "validate": {"status": "succeeded", "attempt": 1},
                    },
                    "artifacts": [{"name": "vernal-result.json", "content": json.dumps(payload, ensure_ascii=False)}],
                })
            return httpx.Response(200, json={})

        client = LoopForgeClient(settings.loopforge_url, settings.loopforge_token, transport=httpx.MockTransport(handler))
        eng._harness = LoopForgeHarness(settings, eng.db, ws, poll_interval=0.001, client=client)
        asyncio.run(adapter.run_step(eng, "001-save", "checklist"))

        # Verify bundle deduplicates schema out of PLAN_PROMPT.md
        bundles = list((ws.state_dir / "baselines").glob("plan-speckit_checklist_*.json"))
        assert len(bundles) == 1
        bundle_files = json.loads(bundles[0].read_text("utf-8"))["files"]
        assert ".vernal-schema.json" in bundle_files
        assert "PLAN_PROMPT.md" in bundle_files
        assert '"additionalProperties": false' not in bundle_files["PLAN_PROMPT.md"]
        assert len(bundle_files["PLAN_PROMPT.md"].encode("utf-8")) < 8_000

        # Verify timing.json written with run correlation, single-counted usage, and unknown_intervals
        timings = list((ws.runs_dir / "speckit" / "001-save" / "checklist").rglob("timing.json"))
        assert len(timings) == 1
        tdata = json.loads(timings[0].read_text("utf-8"))
        assert tdata["run_id"] == "run-r7-1"
        assert tdata["effort"] == "medium"
        assert tdata["usage"]["input_tokens"] == 2000
        assert tdata["usage"]["source"] == "run"
        assert tdata["usage"]["telemetry_known"] is True
        assert tdata["poll_timing"]["unknown_intervals"]

        # Consecutive _ensure_ready with unchanged rule/constitution skips redundant sync_rule PUT
        puts_before = rule_puts
        asyncio.run(eng._harness._ensure_ready(client, "qa_lead"))
        assert rule_puts == puts_before
    finally:
        eng.db.close()


def test_r7_profile_mismatch_and_explicit_conversion_preserves_answers_and_gate(tmp_path):
    ws = init_workspace(tmp_path / "r7-profile", name="R7Profile", collaboration_mode="files")
    settings = Settings(
        collaboration_mode="files",
        runtime_backend="loopforge",
        loopforge_project_id="r7-p",
        loopforge_url="http://lf.test",
        loopforge_token="tok",
    )
    eng = Engine(ws, settings=settings)
    try:
        service.enable(eng, "vernal-core")
        tpl = load_builtin_template("standard")
        save_custom_template(ws, tpl.model_dump_json())
        eng.settings.process_template = "standard"

        save(eng, "constitution", "# 项目宪章\n保持测试底线。\n")
        mission = missions.save(eng, "001-save", "保存回答并刷新恢复", "feature", 0, actor="owner")
        spec_rev = save(eng, "spec", SPEC)
        service.publish_revision(
            eng, "001-save", "checklist", "# 规格质量检查\n通过。\n",
            expected_rev_id=0, author="owner", findings=[SpecFinding(severity="WARNING", message="提醒").model_dump()],
        )
        assessment = triage.Assessment(
            goal_clear=True, scope_clear=True, acceptance_clear=True, local_impact=True,
            change_type="feature", risk="low", rationale="局部低风险",
            questions=[], prerequisites=[], experience_items=[], proposed_spec="", handoff_brief=BRIEF,
        )
        mission.update(
            assessment=assessment.model_dump(),
            route=triage.recommendation(assessment, "greenfield"),
            actual_preset="vernal-core",
            actual_process="standard",
            assessed_input=triage.input_key(eng, mission),
        )
        missions._store(eng, mission)

        v = missions.view(eng, mission)
        ca = v["config_alignment"]
        assert ca["aligned"] is False
        assert ca["recommended_preset"] == "vernal-lite"
        assert ca["recommended_process"] == "quick-prototype"
        assert ca["actual_preset"] == "vernal-core"
        assert ca["actual_process"] == "standard"
        assert ca["can_convert_to_recommended"] is True
        assert "vernal-core/standard" in ca["mismatch_reason"]

        _, old_asset_dir = assets.load_assets(eng)
        with TestClient(create_app(eng, proxy_transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))) as client:
            resp = client.post(
                "/vernal-api/missions/001-save/convert-profile",
                json={
                    "expected_revision": mission["revision"],
                    "target_preset": "vernal-lite",
                    "target_process": "quick-prototype",
                    "reason": "明确无阻塞问题，转为轻量预设",
                },
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["conversion"]["from"]["preset"] == "vernal-core"
            assert body["conversion"]["to"]["preset"] == "vernal-lite"
            assert body["mission"]["config_alignment"]["aligned"] is True

        # Original spec, old immutable asset directory, and blocking srs_approval gate are all preserved
        assert service.latest(eng, "001-save")["rev_id"] == spec_rev["rev_id"]
        assert old_asset_dir.is_dir()
        assert service.latest(eng, "001-save", "checklist")["status"] == "STALE"
        cur_tpl = load_template(ws, eng.settings)
        req_stage = next(s for s in cur_tpl.stages if s.id == "requirements")
        assert any(g.id == "srs_approval" and g.blocking and g.kind == "human_approval" for g in req_stage.gates)

        # Custom workflow cannot be silently downgraded
        custom = load_builtin_template("standard")
        custom.id = "custom-org-workflow"
        save_custom_template(ws, custom.model_dump_json())
        eng.settings.process_template = "custom-org-workflow"
        m2 = missions.get(eng, "001-save")
        with pytest.raises(service.SpecConflictError, match="自定义流程模板"):
            missions.convert_profile(eng, "001-save", target_preset="vernal-core", target_process="standard", expected_revision=m2["revision"], actor="owner")
    finally:
        eng.db.close()


@pytest.mark.parametrize("runtime", ["python", "node"])
def test_r7_isolated_local_checklist_validator_3x_replay_under_1s(tmp_path, runtime):
    executable = sys.executable if runtime == "python" else shutil.which("node")
    if not executable:
        pytest.skip("Node is unavailable")
    validator = Path(__file__).parents[1] / "autodev/speckit" / ("validate_output.py" if runtime == "python" else "validate_output.cjs")
    from autodev.models import SpecStepResult, strict_json_schema
    schema = strict_json_schema(SpecStepResult)
    schema["properties"]["step"]["enum"] = ["checklist"]
    schema["properties"]["clarifications"]["maxItems"] = 0
    schema["x-vernal-step"] = "checklist"
    schema["x-vernal-artifact-path"] = "specs/001-initial/checklist.md"
    payload = {
        "step": "checklist",
        "content": "# 规格质量检查\n\n- 检查项 1：反馈状态明确。\n",
        "clarifications": [],
        "findings": [SpecFinding(severity="WARNING", message=f"W{i}", requirement_ids=["REQ-001"]).model_dump() for i in range(1, 8)],
        "handoff_brief": BRIEF,
    }
    work = tmp_path / f"replay-{runtime}"
    work.mkdir()
    (work / ".vernal-schema.json").write_text(json.dumps(schema, ensure_ascii=False), "utf-8")
    durations = []
    for _ in range(3):
        (work / "vernal-result.json").write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
        t0 = time.monotonic()
        res = subprocess.run([executable, str(validator)], cwd=work, capture_output=True, text=True, timeout=10)
        durations.append(time.monotonic() - t0)
        assert res.returncode == 0, res.stderr
    assert all(d < 1.0 for d in durations), f"{runtime} replay durations exceeded 1s: {durations}"
