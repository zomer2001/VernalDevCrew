"""测试 Codex CLI 执行器命令组装、JSONL 事件流解析、Token 统计与超时终止。"""

from __future__ import annotations

import asyncio
import json
import stat
import sys
from pathlib import Path

from autodev.config import Settings
from autodev.harnesses.codex_cli import CodexHarness


def _make_fake_codex(path: Path, script_body: str) -> str:
    path.write_text(f"#!{sys.executable}\n{script_body}\n", "utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def test_codex_command_building(tmp_path: Path) -> None:
    # 默认不强塞私有模型名，沿用 CLI 自身默认模型 + high 推理强度
    default_h = CodexHarness(Settings(), "/usr/local/bin/codex")
    default_cmd = default_h.build_readonly_cmd(
        schema_path=tmp_path / "s.json", out_path=tmp_path / "o.txt", cwd=tmp_path, role="planner"
    )
    assert "-m" not in default_cmd
    assert 'model_reasoning_effort="high"' in default_cmd

    settings = Settings(
        codex_profile="deepseek",
        planner_model="deepseek-reasoner",
        coder_model="deepseek-chat",
        agent_network_access=True,
    )
    h = CodexHarness(settings, "/usr/local/bin/codex")
    schema = tmp_path / "s.json"
    out = tmp_path / "out.txt"

    ro = h.build_readonly_cmd(schema_path=schema, out_path=out, cwd=tmp_path, role="planner")
    assert "--sandbox" in ro and "read-only" in ro
    assert "--output-schema" in ro
    assert "-p" in ro and "deepseek" in ro
    assert "-m" in ro and "deepseek-reasoner" in ro
    assert ro[-1] == "-"

    rw = h.build_workspace_cmd(schema_path=schema, out_path=out, cwd=tmp_path, role="coder")
    assert "--sandbox" in rw and "workspace-write" in rw
    assert "sandbox_workspace_write.network_access=true" in rw

    res = h.build_resume_cmd("thread-123", schema_path=schema, out_path=out, role="coder")
    assert "resume" in res and "thread-123" in res
    assert 'sandbox_mode="workspace-write"' in res
    assert "--sandbox" not in res  # codex exec resume 不接受 --sandbox，需用 -c sandbox_mode=...


def test_codex_jsonl_stream_and_usage_parsing(tmp_path: Path) -> None:
    fake = _make_fake_codex(
        tmp_path / "fake-codex",
        """
import json, sys, pathlib
args = sys.argv[1:]
prompt = sys.stdin.read()
out_path = pathlib.Path(args[args.index("-o") + 1])
print(json.dumps({"type": "thread.started", "thread_id": "thr_abc123"}), flush=True)
print(json.dumps({"type": "item.started", "item": {"id": "item_1", "type": "command_execution", "command": "pytest -q"}}), flush=True)
print(json.dumps({"type": "item.completed", "item": {"id": "item_1", "type": "command_execution", "command": "pytest -q", "aggregated_output": "1 passed", "exit_code": 0}}), flush=True)
print(json.dumps({"type": "item.completed", "item": {"id": "item_2", "type": "file_change", "changes": [{"kind": "add", "path": "a.py"}]}}), flush=True)
payload = {"status": "DONE", "summary": f"len={len(prompt)}", "files_changed": ["a.py"], "tests_added": ["t.py"], "notes": ""}
out_path.write_text(json.dumps(payload), "utf-8")
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1200, "cached_input_tokens": 800, "output_tokens": 150, "reasoning_output_tokens": 90}}), flush=True)
""",
    )
    activities: list[tuple[str, str, dict | None]] = []
    h = CodexHarness(Settings(), fake, activity_cb=lambda k, t, p=None: activities.append((k, t, p)))
    schema = tmp_path / "schema.json"
    schema.write_text("{}", "utf-8")

    res = asyncio.run(
        h.run_workspace_task("implement foo", cwd=tmp_path, log_dir=tmp_path / "log", schema_path=schema, role="coder")
    )
    assert res.ok is True
    assert res.session_id == "thr_abc123"
    assert res.usage.input_tokens == 1200
    assert res.usage.cached_tokens == 800
    assert res.usage.output_tokens == 150
    assert res.usage.reasoning_tokens == 90
    assert res.structured is not None and res.structured["status"] == "DONE"
    assert any(k == "command" and p and p.get("phase") == "started" for k, _, p in activities)
    assert any(k == "command" and p and p.get("output") == "1 passed" for k, _, p in activities)
    assert any(k == "file" for k, _, _ in activities)


def test_codex_timeout_kills_process_group(tmp_path: Path) -> None:
    fake = _make_fake_codex(
        tmp_path / "slow-codex",
        """
import time
time.sleep(30)
""",
    )
    h = CodexHarness(Settings(agent_timeout_sec=1), fake)
    res = asyncio.run(
        h.run_workspace_task("hang", cwd=tmp_path, log_dir=tmp_path / "log", schema_path=None, role="coder")
    )
    assert res.ok is False
    assert res.timed_out is True
    assert "超时" in res.error
