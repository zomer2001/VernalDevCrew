"""Native validation must reject output that the current Spec Kit step cannot publish."""
import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from autodev.models import ClarificationItem
from autodev.speckit import adapter, service
from test_speckit import BRIEF, eng, prepared


@pytest.mark.parametrize("runtime", ["python", "node"])
@pytest.mark.parametrize("step", ["constitution", "clarify"])
def test_dispatched_step_contract_rejects_cross_step_output(eng, tmp_path, monkeypatch, runtime, step):
    executable = sys.executable if runtime == "python" else shutil.which("node")
    if not executable:
        pytest.skip("Node is unavailable")
    if step == "clarify":
        prepared(eng)
    validator = Path(__file__).parents[1] / "autodev/speckit" / ("validate_output.py" if runtime == "python" else "validate_output.cjs")
    workspace = tmp_path / "native"
    workspace.mkdir()
    value = dict(step=step, content="" if step == "clarify" else "# 项目宪章\n交付须保留验收依据。\n",
                 clarifications=[], findings=[], handoff_brief=BRIEF, work_packages=[])

    async def execute(prompt, *, schema_path, **kwargs):
        (workspace / ".vernal-schema.json").write_text(schema_path.read_text())

        def validate(payload):
            (workspace / "vernal-result.json").write_text(json.dumps(payload, ensure_ascii=False))
            return subprocess.run([executable, str(validator)], cwd=workspace, capture_output=True, text=True, timeout=10)

        count = 4 if step == "clarify" else 1
        questions = [ClarificationItem(question_key=f"Q-{i:02d}", question="需要确认的业务边界？").model_dump()
                     for i in range(1, count + 1)]
        rejected = validate({**value, "clarifications": questions})
        assert rejected.returncode != 0 and "数组长度无效" in rejected.stderr
        rejected = validate({**value, "content": "改写正文" if step == "clarify" else "   "})
        assert rejected.returncode != 0 and "文本格式无效" in rejected.stderr
        accepted = validate(value)
        assert accepted.returncode == 0, accepted.stderr
        return SimpleNamespace(ok=True, structured=value, session_id="native-step-test",
                               usage=SimpleNamespace(input_tokens=1, cached_tokens=0, output_tokens=1, reasoning_tokens=0))

    monkeypatch.setattr(eng.harness, "run_structured_readonly", execute)
    asyncio.run(adapter.run_step(eng, "001-save", step))
    assert not service.list_clarifications(eng, "001-save")
    if step == "constitution":
        assert service.latest(eng, "project", "constitution")["content_text"] == value["content"]


@pytest.mark.parametrize("runtime", ["python", "node"])
def test_plan_validator_requires_findings_handling_table_when_checklist_has_findings(tmp_path, runtime):
    executable = sys.executable if runtime == "python" else shutil.which("node")
    if not executable:
        pytest.skip("Node is unavailable")
    validator = Path(__file__).parents[1] / "autodev/speckit" / ("validate_output.py" if runtime == "python" else "validate_output.cjs")
    workspace = tmp_path / f"plan-findings-{runtime}"
    workspace.mkdir()
    from autodev.models import SpecStepResult, strict_json_schema
    schema = strict_json_schema(SpecStepResult)
    schema["properties"]["step"]["enum"] = ["plan"]
    schema["properties"]["clarifications"]["maxItems"] = 0
    schema["x-vernal-step"] = "plan"
    schema["x-vernal-artifact-path"] = "specs/001-save/plan.md"
    schema["x-vernal-require-findings-ids"] = ["CHK-5-01", "CHK-5-02"]
    (workspace / ".vernal-schema.json").write_text(json.dumps(schema, ensure_ascii=False), "utf-8")

    base_plan = "# 技术方案\n\n## 需求映射\n- REQ-001\n\n## 接口与数据\n- save\n\n## 实施边界\n- src/app.py\n\n## 验证\n- pytest\n"
    payload = dict(step="plan", content=base_plan, clarifications=[], findings=[], handoff_brief=BRIEF, work_packages=[])
    (workspace / "vernal-result.json").write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
    res_missing_sec = subprocess.run([executable, str(validator)], cwd=workspace, capture_output=True, text=True, timeout=10)
    assert res_missing_sec.returncode != 0 and "检查发现处理" in res_missing_sec.stderr

    partial_plan = base_plan + "\n## 检查发现处理\n| 发现标识 | 处理位置 | 验证方式 | 是否需规格变更 |\n| --- | --- | --- | --- |\n| CHK-5-01 | 接口与数据 | pytest | 否 |\n"
    (workspace / "vernal-result.json").write_text(json.dumps({**payload, "content": partial_plan}, ensure_ascii=False), "utf-8")
    res_missing_id = subprocess.run([executable, str(validator)], cwd=workspace, capture_output=True, text=True, timeout=10)
    assert res_missing_id.returncode != 0 and "CHK-5-02" in res_missing_id.stderr

    full_plan = partial_plan + "| CHK-5-02 | 验证 | pytest | 否 |\n"
    (workspace / "vernal-result.json").write_text(json.dumps({**payload, "content": full_plan}, ensure_ascii=False), "utf-8")
    res_ok = subprocess.run([executable, str(validator)], cwd=workspace, capture_output=True, text=True, timeout=10)
    assert res_ok.returncode == 0, res_ok.stderr
