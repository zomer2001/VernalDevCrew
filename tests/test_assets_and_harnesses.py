"""多 Harness 自动扫描、内置 Skill/SOP 泳道与网页编队配置接口测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from autodev.assets import builtin_mcp_templates, load_builtin_skills
from autodev.config import Settings, detect_preferred_harness, normalize_harness_name, probe_agent_runtimes
from autodev.engine import Engine, init_workspace
from autodev.harnesses import _build_single_harness
from autodev.harnesses.cli_generic import _parse_stream_json_line
from autodev.harnesses.base import Usage
from autodev.loopforge.bootstrap import (
    build_lane_workflows,
    build_vernal_agents,
    classify_lane,
    lane_workflow_id,
    skill_asset_id,
)
from autodev.server.app import create_app


def _fake_bin(tmp_path: Path, name: str) -> Path:
    p = tmp_path / name
    p.write_text("#!/bin/sh\necho 1.0.0\n")
    p.chmod(0o755)
    return p


def test_normalize_and_probe_all_harnesses(tmp_path: Path) -> None:
    assert normalize_harness_name("DeepSeek") == "dsh"
    assert normalize_harness_name("claude") == "claude-code"
    s = Settings(
        codebuddy_bin=str(_fake_bin(tmp_path, "codebuddy")),
        dsh_bin=str(_fake_bin(tmp_path, "dsh")),
    )
    rt = probe_agent_runtimes(s)
    assert {"codex", "codebuddy", "opencode", "dsh", "claude-code"} <= set(rt)
    assert rt["codebuddy"]["installed"] and rt["dsh"]["installed"]


def test_auto_harness_falls_back_to_installed_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("autodev.config.CODEX_FALLBACK_PATHS", ())
    monkeypatch.setattr("autodev.config.shutil.which", lambda _n: None)
    cb = _fake_bin(tmp_path, "codebuddy")
    s = Settings(harness="auto", codex_bin="", codebuddy_bin=str(cb))
    assert detect_preferred_harness(s) == "codebuddy"
    assert s.harness_for("coder") == "codebuddy"
    h = _build_single_harness("codebuddy", s)
    assert h.name == "codebuddy"
    cmd = h.build_cmd("hi", role="coder")
    assert cmd[0] == str(cb) and "stream-json" in cmd and cmd[-1] == "hi"


def test_role_mix_routes_to_different_harnesses() -> None:
    s = Settings(harness="codex", coder_harness="dsh", qa_harness="codebuddy")
    assert s.harness_for("director") == "codex"
    assert s.harness_for("coder") == "dsh"
    assert s.harness_for("reviewer") == "codebuddy"


def test_stream_json_parser_handles_claude_and_dsh_shapes() -> None:
    usage = Usage()
    sid, texts, act = _parse_stream_json_line(
        {"type": "assistant", "session_id": "s1", "message": {"content": [{"type": "text", "text": "ok"}]}}, usage
    )
    assert sid == "s1" and texts == ["ok"]
    _, texts, act = _parse_stream_json_line({"type": "tool_use", "name": "Bash"}, usage)
    assert act and "Bash" in act[1]
    _, texts, _ = _parse_stream_json_line({"type": "result", "result": "{\"a\":1}", "usage": {"input_tokens": 5}}, usage)
    assert texts == ["{\"a\":1}"] and usage.input_tokens == 5


def test_builtin_skills_and_agents_reference_registered_ids() -> None:
    slugs = {s["slug"] for s in load_builtin_skills()}
    assert {"frontend-design", "webapp-testing", "test-driven-development", "systematic-debugging", "security-review"} <= slugs
    assert {m["id"] for m in builtin_mcp_templates()} >= {"vernal-playwright-mcp", "vernal-context7-mcp"}
    agents = {a["id"]: a for a in build_vernal_agents("p", Settings())}
    assert skill_asset_id("p", "frontend-design") in agents["p-vernal-coder-frontend"]["skills"]
    assert skill_asset_id("p", "security-review") in agents["p-vernal-reviewer"]["skills"]
    assert set(agents) >= {f"p-vernal-coder-{x}" for x in ("frontend", "contract", "bugfix")}


def test_lane_workflows_and_classification() -> None:
    wfs = build_lane_workflows("p")
    ids = {w["id"] for w in wfs.values()}
    assert "p-vernal-file-sop" in ids and "p-vernal-file-frontend-sop" in ids and "p-vernal-git-bugfix-sop" in ids
    fe = wfs[("files", "frontend")]
    assert next(n for n in fe["nodes"] if n["id"] == "implement")["agentId"] == "p-vernal-coder-frontend"
    assert lane_workflow_id("p", "files", "backend") == "p-vernal-file-sop"
    assert classify_lane("coder", ["web/index.html"]) == "frontend"
    assert classify_lane("contract", ["contracts/x.json"]) == "contract"
    assert classify_lane("coder", ["engine/game.mjs"]) == "backend"
    assert classify_lane("coder", ["engine/game.mjs"], "修复计分缺陷") == "bugfix"


def test_agent_config_api_persists_and_validates(tmp_path: Path) -> None:
    ws = init_workspace(tmp_path / "cfg", name="cfg", collaboration_mode="files")
    eng = Engine(ws, settings=ws.load_settings())
    app = create_app(eng)
    with TestClient(app) as client:
        bad = client.post("/vernal-api/agent/config", json={"coder_harness": "nope"})
        assert bad.status_code == 422
        ok = client.post(
            "/vernal-api/agent/config",
            json={"coder_harness": "deepseek", "qa_harness": "codebuddy", "max_concurrency": 3, "coder_model": "deepseek-chat"},
        )
        assert ok.status_code == 200, ok.text
        eff = ok.json()["effective"]
        assert eff["coder_harness"] == "dsh" and eff["qa_harness"] == "codebuddy" and eff["max_concurrency"] == 3
        reloaded = ws.load_settings()
        assert reloaded.coder_harness == "dsh" and reloaded.qa_harness == "codebuddy"
        assert reloaded.max_concurrency == 3 and reloaded.coder_model == "deepseek-chat"
        st = client.get("/vernal-api/state").json()["settings"]
        assert st["effective_coder_harness"] == "dsh"
        scan = client.post("/vernal-api/agent/rescan")
        assert scan.status_code == 200 and "dsh" in scan.json()["runtimes"]
