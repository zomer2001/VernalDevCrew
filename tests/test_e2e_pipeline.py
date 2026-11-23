"""端到端闭环测试（MockHarness + 真实 Git worktree + 真实 unittest 门禁）。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from autodev import gitops
from autodev.config import Settings
from autodev.engine import Engine, init_workspace
from autodev.harnesses.mock import MockCall, MockHarness
from autodev.models import MilestoneStatus, ProjectStatus, TaskStatus
from autodev.scheduler import Scheduler


def _build_plan_dict() -> dict[str, Any]:
    return {
        "summary": "微型内存 KV 引擎（契约 + 存储 + 服务）",
        "tech_stack": "Python 3 标准库 + unittest",
        "agents_md": "# Mini KV\n契约目录 contracts/ 只读，所有测试用 unittest。",
        "modules": [
            {"name": "contracts", "path": "contracts/", "description": "共享接口"},
            {"name": "storage", "path": "storage/", "description": "内存实现"},
            {"name": "service", "path": "service/", "description": "业务封装"},
        ],
        "milestones": [
            {
                "milestone_id": "M1-contracts",
                "title": "接口契约",
                "goal": "冻结 Store 接口",
                "integration_cmd": "python3 -m unittest tests/test_contracts.py",
            },
            {
                "milestone_id": "M2-core",
                "title": "核心实现",
                "goal": "实现存储与服务并跑通集成测试",
                "integration_cmd": "python3 -m unittest discover -s tests -t .",
            },
        ],
        "tasks": [
            {
                "task_id": "T-001",
                "milestone_id": "M1-contracts",
                "module": "contracts",
                "role": "contract",
                "title": "定义 Store 协议",
                "description": "创建 contracts/kv.py 与测试",
                "acceptance_criteria": ["定义 StoreProtocol 和 KeyMissingError"],
                "depends_on": [],
                "doc_slice_ids": ["DOC01-S01"],
                "allowed_paths": ["contracts/", "tests/test_contracts.py", "tests/__init__.py"],
                "verify_cmds": ["python3 -m unittest tests/test_contracts.py"],
                "priority": 10,
            },
            {
                "task_id": "T-002",
                "milestone_id": "M2-core",
                "module": "storage",
                "role": "coder",
                "title": "实现内存 KV",
                "description": "实现 storage/mem.py",
                "acceptance_criteria": ["实现 put/get，缺失 key 抛 KeyMissingError"],
                "depends_on": ["T-001"],
                "doc_slice_ids": ["DOC01-S01"],
                "allowed_paths": ["storage/", "tests/test_storage.py"],
                "verify_cmds": ["python3 -m unittest tests/test_storage.py"],
                "priority": 20,
            },
            {
                "task_id": "T-003",
                "milestone_id": "M2-core",
                "module": "service",
                "role": "coder",
                "title": "实现上层 KVService",
                "description": "基于 storage/mem.py 实现 service/kv_service.py",
                "acceptance_criteria": ["支持 batch_put 和 safe_get"],
                "depends_on": ["T-001", "T-002"],
                "doc_slice_ids": ["DOC01-S01"],
                "allowed_paths": ["service/", "tests/test_service.py"],
                "verify_cmds": ["python3 -m unittest tests/test_service.py"],
                "priority": 30,
            },
        ],
    }


def test_full_e2e_pipeline_with_retry_resume_and_integration(tmp_path: Path) -> None:
    ws = init_workspace(tmp_path / "demo-kv", name="demo-kv")
    (ws.inputs_dir / "spec.md").write_text("# Mini KV\n\n支持 put / get / batch_put。\n", "utf-8")

    t2_attempts = {"count": 0}
    t2_sessions: list[str | None] = []

    def mock_coder(prompt: str, cwd: Path, call: MockCall) -> dict[str, Any]:
        # 根据工作树目录名识别当前任务
        tid = cwd.name
        if tid == "T-001":
            (cwd / "contracts").mkdir(exist_ok=True)
            (cwd / "contracts" / "__init__.py").write_text("", "utf-8")
            (cwd / "contracts" / "kv.py").write_text(
                "class KeyMissingError(KeyError):\n    pass\n\n"
                "class StoreProtocol:\n"
                "    def put(self, k: str, v: str) -> None: raise NotImplementedError\n"
                "    def get(self, k: str) -> str: raise NotImplementedError\n",
                "utf-8",
            )
            (cwd / "tests").mkdir(exist_ok=True)
            (cwd / "tests" / "__init__.py").write_text("", "utf-8")
            (cwd / "tests" / "test_contracts.py").write_text(
                "import unittest\nfrom contracts.kv import KeyMissingError\n"
                "class T(unittest.TestCase):\n"
                "    def test_err(self): self.assertTrue(issubclass(KeyMissingError, KeyError))\n",
                "utf-8",
            )
            return {"status": "DONE", "summary": "contract done", "files_changed": ["contracts/kv.py"], "tests_added": ["tests/test_contracts.py"], "notes": ""}

        if tid == "T-002":
            t2_attempts["count"] += 1
            t2_sessions.append(call.session_id)
            (cwd / "storage").mkdir(exist_ok=True)
            (cwd / "storage" / "__init__.py").write_text("", "utf-8")
            # 第 1 轮故意写错 get()，验证失败后由 resume 修复
            buggy = t2_attempts["count"] == 1
            body = (
                "from contracts.kv import KeyMissingError, StoreProtocol\n"
                "class MemStore(StoreProtocol):\n"
                "    def __init__(self): self._d = {}\n"
                "    def put(self, k: str, v: str) -> None: self._d[k] = v\n"
                "    def get(self, k: str) -> str:\n"
                + ("        return 'WRONG'\n" if buggy else "        if k not in self._d: raise KeyMissingError(k)\n        return self._d[k]\n")
            )
            (cwd / "storage" / "mem.py").write_text(body, "utf-8")
            (cwd / "tests").mkdir(exist_ok=True)
            (cwd / "tests" / "test_storage.py").write_text(
                "import unittest\nfrom contracts.kv import KeyMissingError\nfrom storage.mem import MemStore\n"
                "class T(unittest.TestCase):\n"
                "    def test_put_get(self):\n"
                "        s = MemStore(); s.put('a', '1'); self.assertEqual(s.get('a'), '1')\n"
                "        with self.assertRaises(KeyMissingError): s.get('missing')\n",
                "utf-8",
            )
            return {"status": "DONE", "summary": f"attempt {t2_attempts['count']}", "files_changed": ["storage/mem.py"], "tests_added": ["tests/test_storage.py"], "notes": ""}

        if tid == "T-003":
            # 验证上游 T-002 的代码已自动合入 T-003 的 worktree 基线！
            assert (cwd / "storage" / "mem.py").exists()
            (cwd / "service").mkdir(exist_ok=True)
            (cwd / "service" / "__init__.py").write_text("", "utf-8")
            (cwd / "service" / "kv_service.py").write_text(
                "from contracts.kv import KeyMissingError\nfrom storage.mem import MemStore\n"
                "class KVService:\n"
                "    def __init__(self, store=None): self.store = store or MemStore()\n"
                "    def batch_put(self, items: dict[str, str]) -> None:\n"
                "        for k, v in items.items(): self.store.put(k, v)\n"
                "    def safe_get(self, k: str, default: str = '') -> str:\n"
                "        try: return self.store.get(k)\n"
                "        except KeyMissingError: return default\n",
                "utf-8",
            )
            (cwd / "tests" / "test_service.py").write_text(
                "import unittest\nfrom service.kv_service import KVService\n"
                "class T(unittest.TestCase):\n"
                "    def test_svc(self):\n"
                "        svc = KVService(); svc.batch_put({'x': '10'})\n"
                "        self.assertEqual(svc.safe_get('x'), '10')\n"
                "        self.assertEqual(svc.safe_get('y', 'none'), 'none')\n",
                "utf-8",
            )
            return {"status": "DONE", "summary": "service done", "files_changed": ["service/kv_service.py"], "tests_added": ["tests/test_service.py"], "notes": ""}

        raise AssertionError(f"unexpected task {tid}")

    harness = MockHarness(
        planner=lambda _p, _c, _call: _build_plan_dict(),
        coder=mock_coder,
        reviewer=lambda _p, _c, _call: {
            "verdict": "PASS",
            "summary": "LGTM",
            "criteria_check": [{"criterion": "ok", "met": True, "evidence": "tests passed"}],
            "blocking_issues": [],
        },
    )

    settings = Settings(require_plan_approval=True, require_merge_approval=True, max_concurrency=2, max_retries=3)
    eng = Engine(ws, settings=settings, harness=harness)
    try:
        assert eng.ingest() == 1
        plan = asyncio.run(eng.plan())
        assert len(plan.tasks) == 3
        assert eng.status == ProjectStatus.PLANNED
        assert (ws.root / "AGENTS.md").exists()

        eng.approve_plan(actor="zomer")
        sched = Scheduler(eng)

        # 第一轮运行：完成 M1 集成，因 require_merge_approval=True 停在 AWAITING_MERGE
        asyncio.run(sched.run(watch=False))
        m1 = eng.db.get_milestone("M1-contracts")
        assert m1 is not None and m1.status == MilestoneStatus.AWAITING_MERGE
        assert m1.report_path and Path(m1.report_path).exists()

        # 人工签字合入 M1 -> 自动解锁 M2
        eng.merge_milestone("M1-contracts", actor="zomer")
        assert (ws.root / "contracts" / "kv.py").exists()

        # 第二轮运行：执行 T-002（失败1次后 resume 修复）→ T-003 → 集成 M2
        asyncio.run(sched.run(watch=False))
        assert t2_attempts["count"] == 2
        assert t2_sessions[0] == t2_sessions[1]  # 确认使用了同一个 session_id 续跑
        assert any(c.kind == "resume" for c in harness.calls)

        m2 = eng.db.get_milestone("M2-core")
        assert m2 is not None and m2.status == MilestoneStatus.AWAITING_MERGE
        matrix = json.loads(m2.commit_matrix_json or "{}")
        assert set(matrix.keys()) == {"T-002", "T-003"}

        # 合入 M2 -> 全项目 COMPLETED
        eng.merge_milestone("M2-core", actor="zomer")
        assert eng.status == ProjectStatus.COMPLETED
        assert (ws.root / "service" / "kv_service.py").exists()
        assert gitops.is_clean(ws.root)
    finally:
        eng.db.close()


def test_fuse_after_max_retries_and_human_steer_recovery(tmp_path: Path) -> None:
    ws = init_workspace(tmp_path / "fuse-demo")
    (ws.inputs_dir / "spec.md").write_text("# Fuse Demo\n\n测试熔断与人工点拨恢复。\n", "utf-8")

    plan_dict = {
        "summary": "单任务熔断测试",
        "tech_stack": "Python 3",
        "agents_md": "# Fuse",
        "modules": [{"name": "contracts", "path": "contracts/", "description": "契约"}],
        "milestones": [{"milestone_id": "M1", "title": "M1", "goal": "g", "integration_cmd": ""}],
        "tasks": [
            {
                "task_id": "T-001",
                "milestone_id": "M1",
                "module": "contracts",
                "role": "contract",
                "title": "写函数",
                "description": "实现 contracts/math_util.py",
                "acceptance_criteria": ["add(1,2)==3"],
                "depends_on": [],
                "doc_slice_ids": ["DOC01-S01"],
                "allowed_paths": ["contracts/", "tests/"],
                "verify_cmds": ["python3 -m unittest tests/test_math.py"],
                "priority": 10,
            }
        ],
    }

    def coder_needs_steer(prompt: str, cwd: Path, _call: MockCall) -> dict[str, Any]:
        (cwd / "contracts").mkdir(exist_ok=True)
        (cwd / "tests").mkdir(exist_ok=True)
        fixed = "用加法" in prompt
        (cwd / "contracts" / "math_util.py").write_text(
            "def add(a, b): return a + b\n" if fixed else "def add(a, b): return a - b\n", "utf-8"
        )
        (cwd / "tests" / "test_math.py").write_text(
            "import unittest\nfrom contracts.math_util import add\n"
            "class T(unittest.TestCase):\n    def test_add(self): self.assertEqual(add(1, 2), 3)\n",
            "utf-8",
        )
        return {"status": "DONE", "summary": "done", "files_changed": ["contracts/math_util.py"], "tests_added": ["tests/test_math.py"], "notes": ""}

    harness = MockHarness(planner=lambda *_: plan_dict, coder=coder_needs_steer)
    settings = Settings(require_plan_approval=False, require_merge_approval=False, max_retries=2)
    eng = Engine(ws, settings=settings, harness=harness)
    try:
        eng.ingest()
        asyncio.run(eng.plan())
        sched = Scheduler(eng)

        # 连续失败 2 次自动熔断
        asyncio.run(sched.run(watch=False))
        t = eng.db.get_task("T-001")
        assert t is not None and t.status == TaskStatus.FUSED
        assert t.retry_count == 2

        # 人工点拨并继续运行
        eng.steer("T-001", note="把减法改成用加法", actor="alice")
        asyncio.run(sched.run(watch=False))
        assert eng.status == ProjectStatus.COMPLETED
        assert eng.db.get_task("T-001").status == TaskStatus.MERGED
    finally:
        eng.db.close()
