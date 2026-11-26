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
from autodev.speckit import service
from autodev.models import ClarificationItem

root=Path.cwd()/"workspaces/p1-review"
engine=Engine(init_workspace(root,name="P1 功能规格验收",collaboration_mode="files"))
try:
    if not service.enabled(engine):
        service.enable(engine,"vernal-core")
        service.publish_revision(engine,"project","constitution","# 项目宪章\n\n采用中文交付；功能必须有可复跑的验收依据；保持需求和实现可追溯。\n",expected_rev_id=0,author="review-fixture")
        spec=(Path.cwd()/"docs/examples/P1_SAMPLE_SPEC.md").read_text("utf-8")
        revision=service.publish_revision(engine,"001-save","spec",spec,expected_rev_id=0,author="review-fixture")
        service.add_clarifications(engine,"001-save",revision["rev_id"],[ClarificationItem(
            question_key="Q-01",category="scope",severity="BLOCKING",question="本轮回答数据需要保存多久？",
            options=["保留至负责人主动删除","保存 30 天后删除"])],actor="review-fixture")
    service.ensure_specification_gate(engine)
    print(f"评审工作区：{root}。已有测试修改会保留。")
    print("本地验收夹具只准备宪章、规格和一个澄清问题，不调用模型，也不自动批准或启动研发。")
finally:
    engine.db.close()
PY

if [[ "${1:-}" == "--prepare-only" ]]; then exit 0; fi
exec "$review_python" -m autodev.cli serve --host 127.0.0.1 --port "${VERNAL_REVIEW_PORT:-8789}" --dir "$review_root/workspaces/p1-review"
