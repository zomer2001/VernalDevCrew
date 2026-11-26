#!/usr/bin/env bash
set -euo pipefail

review_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
review_python="${VERNAL_REVIEW_PYTHON:-$review_root/.venv/bin/python}"
if [[ ! -x "$review_python" && -x "$review_root/../../.venv/bin/python" ]]; then
  review_python="$review_root/../../.venv/bin/python"
fi
if [[ ! -x "$review_python" ]]; then
  echo '请用 VERNAL_REVIEW_PYTHON 指定已安装项目依赖的 Python。' >&2
  exit 1
fi
cd "$review_root"
export PYTHONPATH="$review_root${PYTHONPATH:+:$PYTHONPATH}"
export VERNAL_DISABLE_AUTO_RESUME=1

"$review_python" - <<'PY'
from pathlib import Path
from autodev.engine import Engine, init_workspace
from autodev.models import ClarificationItem
from autodev.speckit import missions, service, triage

root=Path.cwd()/"workspaces/p2-review"
engine=Engine(init_workspace(root,name="P2 需求澄清验收",collaboration_mode="files"))
try:
    if not missions.get(engine,"001-save"):
        engine.db.meta_set("source_mode","scratch")
        mission=missions.save(engine,"001-save","保存需求澄清回答，刷新后仍能读取，并明确数据保留范围。","feature",0,actor="review-fixture")
        service.enable(engine,"vernal-lite")
        service.publish_revision(engine,"project","constitution","# 项目宪章\n\n负责人确认范围后才允许开工；交付需有可复跑验收依据。\n",expected_rev_id=0,author="review-fixture")
        text=(Path.cwd()/"docs/examples/P1_SAMPLE_SPEC.md").read_text("utf-8")
        rev=service.publish_revision(engine,"001-save","spec",text,expected_rev_id=0,author="review-fixture")
        question=ClarificationItem(question_key="Q-01",category="scope",severity="BLOCKING",question="本轮回答数据需要保存多久？",options=["保留至负责人主动删除","保存 30 天后删除"])
        service.add_clarifications(engine,"001-save",rev["rev_id"],[question],actor="review-fixture")
        brief=dict(delivered="已准备人工构造的页面验收夹具。",integration="回答后可检查执行环境并重新评估。",boundaries="本夹具没有调用模型，也没有批准开工。",verification="真实执行需在线 Runner 和后台身份。")
        assessment=triage.Assessment(goal_clear=True,scope_clear=False,acceptance_clear=True,local_impact=True,change_type="feature",risk="low",rationale="数据保留范围需要负责人明确。",questions=[question],handoff_brief=brief)
        mission.update(assessment=assessment.model_dump(),route=triage.recommendation(assessment,"greenfield"),actual_preset="vernal-lite",actual_process="legacy",prerequisites=[],assessed_input=triage.input_key(engine,mission))
        missions._store(engine,mission)
    print(f"P2 验收工作区：{root}")
    print("已准备独立测试夹具，保留已有回答。不调用模型，不批准门禁，不启动研发任务。")
finally:
    engine.db.close()
PY

if [[ "${1:-}" == "--prepare-only" ]]; then exit 0; fi
exec "$review_python" -m autodev.cli serve --host 127.0.0.1 --port "${VERNAL_REVIEW_PORT:-8790}" --dir "$review_root/workspaces/p2-review"
