"""WP-4 测试：代码质量门禁（5 类假测试 AST 拦截、密钥扫描、工具缺失 SKIPPED、增量覆盖率打回闭环）。"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from autodev.engine import Engine, init_workspace
from autodev.harnesses.mock import MockHarness
from autodev.models import PlanMilestone, PlanModule, PlanTask, ProjectPlan, ProjectStatus, TaskStatus
from autodev.process.model import GateDef
from autodev.process.runner import load_builtin_template, save_custom_template
from autodev.quality.gates import diff_coverage, lint, sast, secrets, test_integrity
from autodev.scheduler import Scheduler
from autodev.server.app import create_app


def test_test_integrity_detects_all_five_fake_test_patterns(tmp_path: Path) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()

    # 1~4：空函数、恒真断言、无断言、吞异常
    (tests_dir / "test_fake_cases.py").write_text(
        "def test_empty():\n"
        "    pass\n\n"
        "def test_trivial():\n"
        "    assert True\n\n"
        "def test_no_assert():\n"
        "    x = 1 + 2\n\n"
        "def test_swallowed():\n"
        "    try:\n"
        "        raise RuntimeError('boom')\n"
        "    except Exception:\n"
        "        pass\n"
        "    assert 2 == 1 + 1\n",
        "utf-8",
    )
    # 5：整文件级别 pytestmark skip
    (tests_dir / "test_skipped_file.py").write_text(
        "import pytest\n"
        "pytestmark = pytest.mark.skip(reason='skip all')\n\n"
        "def test_real():\n"
        "    assert 1 + 1 == 2\n",
        "utf-8",
    )

    res = asyncio.run(
        test_integrity.run(
            tmp_path,
            ["tests/test_fake_cases.py", "tests/test_skipped_file.py"],
        )
    )
    assert res.status == "FAILED"
    rules = {f.rule for f in res.findings}
    assert {
        "empty-test",
        "trivial-assert",
        "missing-assertion",
        "swallowed-exception",
        "whole-file-skip",
    } <= rules


def test_secrets_lint_and_sast_missing_tool_skipped_never_silent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "app.py").write_text(
        'OPENAI_KEY = "sk-' + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4" + '"\n',
        "utf-8",
    )
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    sec_res = asyncio.run(secrets.run(tmp_path, ["app.py"]))
    assert sec_res.status == "FAILED"
    assert any(f.rule == "api-sk-token" for f in sec_res.findings)

    lint_res = asyncio.run(lint.run(tmp_path, ["app.py"]))
    assert lint_res.status == "SKIPPED"
    assert "未安装 ruff" in lint_res.skipped_reason

    sast_res = asyncio.run(sast.run(tmp_path, ["app.py"]))
    assert sast_res.status == "SKIPPED"
    assert "未安装 semgrep" in sast_res.skipped_reason


def test_diff_coverage_below_threshold_triggers_scheduler_rework_with_findings(tmp_path: Path) -> None:
    attempts = 0

    def coder_fn(prompt: str, cwd: Path, _call: object) -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        (cwd / "pkg").mkdir(parents=True, exist_ok=True)
        (cwd / "pkg" / "calc.py").write_text("def add(a, b):\n    return a + b\n", "utf-8")
        # 第 1 轮覆盖率仅 25%（低于 80% 阈值），第 2 轮根据质量反馈补齐测试使覆盖率达 100%
        if attempts == 1:
            cov_xml = (
                '<?xml version="1.0" ?>'
                '<coverage><packages><package name="pkg"><classes>'
                '<class name="calc.py" filename="pkg/calc.py"><lines>'
                '<line number="1" hits="1"/><line number="2" hits="0"/><line number="3" hits="0"/><line number="4" hits="0"/>'
                "</lines></class></classes></package></packages></coverage>"
            )
        else:
            assert "diff_coverage" in prompt and "pkg/calc.py:2" in prompt
            cov_xml = (
                '<?xml version="1.0" ?>'
                '<coverage><packages><package name="pkg"><classes>'
                '<class name="calc.py" filename="pkg/calc.py"><lines>'
                '<line number="1" hits="1"/><line number="2" hits="1"/>'
                "</lines></class></classes></package></packages></coverage>"
            )
        (cwd / "coverage.xml").write_text(cov_xml, "utf-8")
        return {"status": "DONE", "summary": "implemented", "files_changed": ["pkg/calc.py"], "tests_added": [], "notes": ""}

    ws = init_workspace(tmp_path / "cov-rework", name="CovRework", collaboration_mode="files")
    eng = Engine(ws, harness=MockHarness(coder=coder_fn))
    try:
        eng.settings.runtime_backend = "local"
        tpl = load_builtin_template("legacy")
        tpl.quality = {"changed_line_coverage": 80.0}
        for s in tpl.stages:
            if s.id == "testing":
                s.gates.append(GateDef(id="quality_gate", name="质量门禁", kind="quality", scope="task", blocking=True))
        save_custom_template(ws, json.dumps(tpl.model_dump(), ensure_ascii=False))

        plan = ProjectPlan(
            summary="覆盖率门禁返工测试",
            tech_stack="python",
            agents_md="rule",
            modules=[PlanModule(name="pkg", path="pkg/", description="calc")],
            milestones=[PlanMilestone(milestone_id="M1", title="M1", goal="calc", integration_cmd="true")],
            tasks=[
                PlanTask(
                    task_id="T-001",
                    milestone_id="M1",
                    module="pkg",
                    role="coder",
                    title="实现加法模块",
                    description="编写 calc.py",
                    acceptance_criteria=["覆盖率达标"],
                    depends_on=[],
                    doc_slice_ids=[],
                    allowed_paths=["pkg/", "coverage.xml"],
                    verify_cmds=["true"],
                    priority=10,
                )
            ],
        )
        eng.db.save_plan(plan, max_retries=3)
        eng.set_status(ProjectStatus.APPROVED)

        sched = Scheduler(eng)
        asyncio.run(sched.run())

        assert attempts == 2
        t = eng.db.get_task("T-001")
        assert t is not None and t.status in (TaskStatus.APPROVED, TaskStatus.MERGED)

        client = TestClient(create_app(eng, proxy_transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))))
        q_resp = client.get("/vernal-api/tasks/T-001/quality?latest_only=true").json()
        assert q_resp["passed"] is True
        cov_entry = next(r for r in q_resp["results"] if r["gate"] == "diff_coverage")
        assert cov_entry["metrics"]["changed_line_coverage"] == 100.0
    finally:
        eng.db.close()
