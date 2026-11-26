#!/usr/bin/env bash
set -euo pipefail

review_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
review_python="${VERNAL_REVIEW_PYTHON:-$review_root/.venv/bin/python}"
if [[ ! -x "$review_python" && -x "$review_root/../../.venv/bin/python" ]]; then
  review_python="$review_root/../../.venv/bin/python"
fi
if [[ ! -x "$review_python" ]]; then
  echo '请通过 VERNAL_REVIEW_PYTHON 指定已安装项目依赖的 Python。' >&2
  exit 1
fi
cd "$review_root"
export PYTHONPATH="$review_root${PYTHONPATH:+:$PYTHONPATH}"
export VERNAL_DISABLE_AUTO_RESUME=1

"$review_python" - <<'PY'
from pathlib import Path
from autodev.engine import Engine, init_workspace
from autodev.harnesses.mock import MockHarness
from autodev.docs_center.service import save_doc_version
from autodev.process.runner import ensure_gate_record, load_builtin_template, save_custom_template

root = Path.cwd() / "workspaces/p0-review"
if not (root / ".autodev/state.db").exists():
    engine = Engine(init_workspace(root, name="P0 隔离评审", collaboration_mode="files"), harness=MockHarness())
    try:
        save_custom_template(engine.ws, load_builtin_template("standard").model_dump_json())
        save_doc_version(engine, "SRS.md", "# P0 评审夹具\n\n| 编号 | 优先级 | 标题 | 验收 |\n| --- | --- | --- | --- |\n| REQ-001 | P0 | 保存回答 | 重启后可读取 |\n| NFR-001 | P1 | 响应时间 | 本地接口 P95 小于 200ms |\n")
        save_doc_version(engine, "ARCHITECTURE.md", "# 评审夹具架构\n\n回答通过本地接口写入 SQLite。")
        for scope in ("M1", "M2"):
            save_doc_version(engine, f"design/{scope}/core.md", f"# {scope} 评审夹具详设\n\nREQ-001：保存后返回确认。")
        for stage, gate, scope_type, scope_id in [
            ("requirements", "srs_approval", "project", "project"),
            ("architecture", "architecture_approval", "project", "project"),
            ("detailed_design", "detailed_design_approval", "milestone", "M1"),
            ("detailed_design", "detailed_design_approval", "milestone", "M2"),
        ]:
            ensure_gate_record(engine.db, stage_id=stage, gate_id=gate, kind="human_approval", scope_type=scope_type, scope_id=scope_id)
    finally:
        engine.db.close()
print(f"评审工作区：{root}。再次启动会保留你的测试修改。")
PY

exec "$review_python" -m autodev.cli serve --host 127.0.0.1 --port "${VERNAL_REVIEW_PORT:-8788}" --dir "$review_root/workspaces/p0-review"
