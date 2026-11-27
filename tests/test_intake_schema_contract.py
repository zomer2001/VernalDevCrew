"""The model-facing validators and application must agree on clarification identifiers."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from autodev.models import ClarificationItem, strict_json_schema
from autodev.speckit import missions, service, triage
from test_missions import advance, assessed, project, responses


@pytest.mark.parametrize("runtime", ["python", "node"])
def test_native_validator_rejects_invalid_question_identifiers_before_application(tmp_path, runtime):
    executable = sys.executable if runtime == "python" else shutil.which("node")
    if not executable:
        pytest.skip("Node is unavailable")
    script = Path(__file__).parents[1] / "autodev/speckit" / ("validate_output.py" if runtime == "python" else "validate_output.cjs")
    schema = strict_json_schema(triage.Assessment)
    (tmp_path / ".vernal-schema.json").write_text(json.dumps(schema))
    question = ClarificationItem(question_key="Q-01", question="仅填写说明能否保存？", options=["必须选中选项", "可以只填说明"])
    data = assessed()
    data.update(scope_clear=False, questions=[question.model_dump()])

    def run(value):
        (tmp_path / "vernal-result.json").write_text(json.dumps(value, ensure_ascii=False))
        return subprocess.run([executable, str(script)], cwd=tmp_path, capture_output=True, text=True, timeout=10)

    for invalid in ({"question_key": "Q1"}, {"question_key": "Q-1"}, {"question": "   "}):
        broken = {**data, "questions": [{**question.model_dump(), **invalid}]}
        result = run(broken)
        assert result.returncode != 0 and "文本格式无效" in result.stderr
        with pytest.raises(ValueError):
            triage.Assessment.model_validate(broken)

    result = run(data)
    assert result.returncode == 0, result.stderr
    assert triage.Assessment.model_validate(data).questions[0].question_key == "Q-01"


def test_failed_intake_keeps_validation_details_and_does_not_publish(project, monkeypatch):
    value = assessed()
    value["questions"] = [dict(question_key="Q1", question="问题", severity="BLOCKING", category="scope", options=[], default_option="", rationale="")]
    responses(project, monkeypatch, [value])
    missions.save(project, "001-save", "保存回答并刷新读取", "feature", 0, actor="owner")
    with pytest.raises(ValueError, match="question_key"):
        advance(project)
    assert not service.latest(project, "001-save")
    assert "input_value" not in missions.get(project, "001-save")["last_error"]
    run = project.db.list_runs("_speckit_001-save_std_spec")[-1]
    assert "question_key" in run["summary_log"]
    assert run["verdict"] == "ERROR"
