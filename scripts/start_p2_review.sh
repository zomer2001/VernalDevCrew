#!/usr/bin/env bash
set -euo pipefail

review_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
review_workspace="${VERNAL_REVIEW_WORKSPACE:-$review_root/workspaces/p2-review}"
review_prepare_only=0
review_fixture=0
for review_arg in "$@"; do
  case "$review_arg" in
    --prepare-only) review_prepare_only=1 ;;
    --fixture) review_fixture=1 ;;
    *) echo "未知参数：$review_arg" >&2; exit 2 ;;
  esac
done
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

"$review_python" - "$review_workspace" "$review_fixture" <<'PY'
import sys
from pathlib import Path
from autodev.config import Workspace
from autodev.engine import Engine, init_workspace
from autodev.models import ClarificationItem
from autodev.process.runner import load_template
from autodev.speckit import missions, service, triage

root=Path(sys.argv[1]).resolve()
fixture=sys.argv[2]=="1"
ws=Workspace(root)
if not ws.config_path.exists():
    if fixture:
        raise SystemExit("请先在网页新建独立验收项目，再用 VERNAL_REVIEW_WORKSPACE 指向其工作区。")
    ws=init_workspace(root,name="P2 需求澄清验收",collaboration_mode="files")
engine=Engine(ws)
try:
    if fixture and not engine.settings.loopforge_project_id:
        raise SystemExit("此工作区尚未绑定原生项目。请在网页新建独立验收项目后再准备夹具。")
    feature="001-initial"
    if fixture and not service.latest(engine,feature):
        missions.assert_idle(engine)
        if engine.db.list_tasks() or service.current_specs(engine) or service.enabled(engine):
            raise SystemExit("验收夹具只能写入尚未启用规格、没有任务的独立项目。")
        mission=missions.get(engine,feature)
        if not mission:
            mission=missions.save(engine,feature,"保存需求澄清回答，刷新后仍能读取，并明确数据保留范围。","feature",0,actor="review-fixture")
        service.enable(engine,"vernal-lite")
        service.publish_revision(engine,"project","constitution","# 项目宪章\n\n负责人确认范围后才允许开工；交付需有可复跑验收依据。\n",expected_rev_id=0,author="review-fixture")
        text=(Path.cwd()/"docs/examples/P1_SAMPLE_SPEC.md").read_text("utf-8")
        rev=service.publish_revision(engine,feature,"spec",text,expected_rev_id=0,author="review-fixture")
        question=ClarificationItem(question_key="Q-01",category="scope",severity="BLOCKING",question="本轮回答数据需要保存多久？",options=["保留至负责人主动删除","保存 30 天后删除"])
        service.add_clarifications(engine,feature,rev["rev_id"],[question],actor="review-fixture")
        brief=dict(delivered="已准备人工构造的页面验收夹具。",integration="回答后可检查执行环境并重新评估。",boundaries="本夹具没有调用模型，也没有批准开工。",verification="真实执行需在线 Runner 和后台身份。")
        assessment=triage.Assessment(goal_clear=True,scope_clear=False,acceptance_clear=True,local_impact=True,change_type="feature",risk="low",rationale="数据保留范围需要负责人明确。",questions=[question],handoff_brief=brief)
        mission.update(assessment=assessment.model_dump(),route=triage.recommendation(assessment,triage.source_origin(engine)),actual_preset="vernal-lite",actual_process=load_template(engine.ws,engine.settings).id,prerequisites=[],assessed_input=triage.input_key(engine,mission))
        missions._store(engine,mission)
    print(f"P2 验收工作区：{root}")
    if fixture:
        print(f"页面选择项目 {engine.settings.loopforge_project_id}，进入“继续需求交付”即可查看夹具。")
        print("已有规格和回答保持原样。不调用模型，不批准门禁，不启动研发任务。")
    elif not engine.settings.loopforge_project_id:
        print("此入口工作区尚未绑定。登录网页后请先“新建 / 接入项目”，再进入“继续需求交付”。")
    if not engine.settings.service_token():
        print("尚未配置后台身份。已有部署可通过 VERNAL_SERVICE_TOKEN_FILE 指定同一执行服务的部署身份文件。")
    print("本脚本仅启动网页服务。真实模型验收还需要在线 Runner。")
finally:
    engine.db.close()
PY

if [[ "$review_prepare_only" == 1 ]]; then exit 0; fi
exec "$review_python" -m autodev.cli serve --host 127.0.0.1 --port "${VERNAL_REVIEW_PORT:-8790}" --dir "$review_workspace"
