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
                 clarifications=[], findings=[], handoff_brief=BRIEF)

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
