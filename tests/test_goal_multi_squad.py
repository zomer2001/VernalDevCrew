"""端到端测试：Goal 多角色多研发组滚动排期 + 总监自动契约升版仲裁 + OpenCode v2 适配器 + Git 实名提交。"""

from __future__ import annotations

import asyncio
import json
import stat
import sys
from pathlib import Path
from typing import Any

from autodev import gitops
from autodev.config import Settings
from autodev.engine import Engine, init_workspace
from autodev.harnesses.mock import MockCall, MockHarness
from autodev.harnesses.opencode_cli import OpenCodeHarness, write_opencode_agent_profiles
from autodev.models import ProjectStatus, TaskStatus
from autodev.scheduler import Scheduler


def test_goal_multi_squad_rolling_wave_and_director_contract_patch(tmp_path: Path) -> None:
    ws = init_workspace(tmp_path / "order-hub", name="order-hub")
    (ws.inputs_dir / "arch.md").write_text("# Order Hub\n用户域 + 订单域 + 网关装配。\n", "utf-8")

    # 1. 产品经理输出
    def pm_fn(_prompt: str, _cwd: Path, _call: MockCall) -> dict[str, Any]:
        return {
            "product_vision": "轻量多域订单后端",
            "core_workflows": ["创建用户", "创建订单并关联用户"],
            "edge_cases": ["空用户名报错"],
            "error_conventions": ["统一使用 DomainError"],
            "global_acceptance_redlines": ["契约只读，测试全通过"],
            "spec_markdown": "# PRD_SPEC\n\n- 统一使用 contracts/schema.py 定义模型。\n",
        }

    # 2. 研发总监：按提示词区分 立项蓝图 / 滚动拆单 / 异常仲裁
    def director_fn(prompt: str, _cwd: Path, _call: MockCall) -> dict[str, Any]:
        if "立项蓝图设计规则" in prompt:
            return {
                "summary": "多域订单后端立项蓝图",
                "tech_stack": "Python 3 标准库 + unittest",
                "agents_md": "# Order Hub AGENTS\n各研发组严格在各自目录内工作。",
                "squads": [
                    {"squad_id": "squad-contract", "name": "契约架构组", "mission": "维护公共契约", "owned_paths": ["contracts/", "tests/test_contracts.py"], "roles": ["contract"]},
                    {"squad_id": "squad-user", "name": "用户域研发组", "mission": "用户模块", "owned_paths": ["modules/user/", "tests/test_user.py"], "roles": ["coder"]},
                    {"squad_id": "squad-order", "name": "订单域研发组", "mission": "订单模块", "owned_paths": ["modules/order/", "tests/test_order.py"], "roles": ["coder"]},
                    {"squad_id": "squad-gateway", "name": "网关装配组", "mission": "统一入口装配", "owned_paths": ["gateway/", "tests/test_gateway.py"], "roles": ["integrator"]},
                ],
                "milestones": [
                    {
                        "milestone_id": "M1-contracts",
                        "title": "全局契约冻结",
                        "goal": "冻结用户与订单基础数据结构",
                        "target_squads": ["squad-contract"],
                        "key_deliverables": ["contracts/schema.py"],
                        "integration_cmd": "python3 -m unittest tests/test_contracts.py",
                    },
                    {
                        "milestone_id": "M2-domains",
                        "title": "多域并行研发与网关装配",
                        "goal": "用户组与订单组并行实现，网关组收口装配",
                        "target_squads": ["squad-user", "squad-order", "squad-gateway"],
                        "key_deliverables": ["modules/user", "modules/order", "gateway/app.py"],
                        "integration_cmd": "python3 -m unittest discover -s tests -t .",
                    },
                ],
                "initial_tasks": [
                    {
                        "task_id": "T-001",
                        "milestone_id": "M1-contracts",
                        "squad": "squad-contract",
                        "module": "contracts",
                        "role": "contract",
                        "title": "定义基础契约",
                        "description": "创建 contracts/schema.py",
                        "acceptance_criteria": ["定义 User 与 Order"],
                        "depends_on": [],
                        "doc_slice_ids": ["DOC01-S01"],
                        "allowed_paths": ["contracts/", "modules/__init__.py", "tests/__init__.py", "tests/test_contracts.py"],
                        "verify_cmds": ["python3 -m unittest tests/test_contracts.py"],
                        "priority": 10,
                    }
                ],
            }

        if "滚动拆解" in prompt or "当前待展开里程碑：M2-domains" in prompt:
            return {
                "milestone_id": "M2-domains",
                "wave_notes": "基于 M1 契约并行派发用户组、订单组，并由网关组收口。",
                "tasks": [
                    {
                        "task_id": "T-002",
                        "milestone_id": "M2-domains",
                        "squad": "squad-user",
                        "module": "user",
                        "role": "coder",
                        "title": "实现用户服务",
                        "description": "实现 modules/user/svc.py",
                        "acceptance_criteria": ["支持创建与查询用户"],
                        "depends_on": ["T-001"],
                        "doc_slice_ids": ["PRD-S01"],
                        "allowed_paths": ["modules/user/", "tests/test_user.py"],
                        "verify_cmds": ["python3 -m unittest tests/test_user.py"],
                        "priority": 10,
                    },
                    {
                        "task_id": "T-003",
                        "milestone_id": "M2-domains",
                        "squad": "squad-order",
                        "module": "order",
                        "role": "coder",
                        "title": "实现订单服务（需要契约 currency 字段）",
                        "description": "实现 modules/order/svc.py",
                        "acceptance_criteria": ["支持带 currency 的订单创建"],
                        "depends_on": ["T-001"],
                        "doc_slice_ids": ["PRD-S01"],
                        "allowed_paths": ["modules/order/", "tests/test_order.py"],
                        "verify_cmds": ["python3 -m unittest tests/test_order.py"],
                        "priority": 10,
                    },
                    {
                        "task_id": "T-004",
                        "milestone_id": "M2-domains",
                        "squad": "squad-gateway",
                        "module": "gateway",
                        "role": "integrator",
                        "title": "网关装配用户与订单模块",
                        "description": "实现 gateway/app.py 并做跨模块测试",
                        "acceptance_criteria": ["通过网关创建用户并下单"],
                        "depends_on": ["T-002", "T-003"],
                        "doc_slice_ids": ["PRD-S01"],
                        "allowed_paths": ["gateway/", "tests/test_gateway.py"],
                        "verify_cmds": ["python3 -m unittest tests/test_gateway.py"],
                        "priority": 20,
                    },
                ],
            }

        # 异常仲裁：为 T-003 自动派发契约补丁任务！
        return {
            "action": "PATCH_CONTRACT",
            "diagnosis": "contracts/schema.py 的 Order 缺少 currency 字段，需要升版契约。",
            "steer_note": "契约已补齐 currency 字段，请直接引用 Order.currency 完成订单服务。",
            "contract_patch": {
                "title": "为 Order 契约补充 currency 字段",
                "description": "在 contracts/schema.py 的 Order 中新增 currency: str = 'CNY'",
                "allowed_paths": ["contracts/", "tests/test_contracts.py"],
                "verify_cmds": ["python3 -m unittest tests/test_contracts.py"],
                "acceptance_criteria": ["Order 支持 currency 字段"],
            },
        }

    # 3. 各研发组工位编码逻辑
    def mock_coder(_prompt: str, cwd: Path, _call: MockCall) -> dict[str, Any]:
        tid = cwd.name
        if tid == "T-001":
            (cwd / "contracts").mkdir(exist_ok=True)
            (cwd / "contracts" / "__init__.py").write_text("", "utf-8")
            (cwd / "contracts" / "schema.py").write_text(
                "from dataclasses import dataclass\n\n"
                "@dataclass\nclass User:\n    uid: str\n    name: str\n\n"
                "@dataclass\nclass Order:\n    oid: str\n    uid: str\n    amount: int\n",
                "utf-8",
            )
            (cwd / "modules").mkdir(exist_ok=True)
            (cwd / "modules" / "__init__.py").write_text("", "utf-8")
            (cwd / "tests").mkdir(exist_ok=True)
            (cwd / "tests" / "__init__.py").write_text("", "utf-8")
            (cwd / "tests" / "test_contracts.py").write_text(
                "import unittest\nfrom contracts.schema import User, Order\n"
                "class T(unittest.TestCase):\n    def test_ok(self): self.assertEqual(User('u1','a').uid, 'u1')\n",
                "utf-8",
            )
            return {"status": "DONE", "summary": "M1 contracts done", "files_changed": ["contracts/schema.py"], "tests_added": ["tests/test_contracts.py"], "notes": ""}

        if tid == "T-003-P1":
            # 总监派发的契约补丁任务：补齐 currency 字段
            (cwd / "contracts" / "schema.py").write_text(
                "from dataclasses import dataclass\n\n"
                "@dataclass\nclass User:\n    uid: str\n    name: str\n\n"
                "@dataclass\nclass Order:\n    oid: str\n    uid: str\n    amount: int\n    currency: str = 'CNY'\n",
                "utf-8",
            )
            (cwd / "tests" / "test_contracts.py").write_text(
                "import unittest\nfrom contracts.schema import User, Order\n"
                "class T(unittest.TestCase):\n    def test_currency(self): self.assertEqual(Order('o1','u1',10).currency, 'CNY')\n",
                "utf-8",
            )
            return {"status": "DONE", "summary": "contract patched", "files_changed": ["contracts/schema.py"], "tests_added": ["tests/test_contracts.py"], "notes": ""}

        if tid == "T-002":
            (cwd / "modules" / "user").mkdir(parents=True, exist_ok=True)
            (cwd / "modules" / "user" / "__init__.py").write_text("", "utf-8")
            (cwd / "modules" / "user" / "svc.py").write_text(
                "from contracts.schema import User\n"
                "class UserService:\n"
                "    def __init__(self): self._u = {}\n"
                "    def create(self, uid: str, name: str) -> User:\n"
                "        u = User(uid, name); self._u[uid] = u; return u\n"
                "    def get(self, uid: str) -> User: return self._u[uid]\n",
                "utf-8",
            )
            (cwd / "tests" / "test_user.py").write_text(
                "import unittest\nfrom modules.user.svc import UserService\n"
                "class T(unittest.TestCase):\n    def test_user(self): s=UserService(); self.assertEqual(s.create('u1','Tom').name,'Tom')\n",
                "utf-8",
            )
            return {"status": "DONE", "summary": "user squad done", "files_changed": ["modules/user/svc.py"], "tests_added": ["tests/test_user.py"], "notes": ""}

        if tid == "T-003":
            schema_txt = (cwd / "contracts" / "schema.py").read_text("utf-8")
            if "currency" not in schema_txt:
                # 首次执行发现契约缺 currency 字段，主动发起契约变更升级！
                return {
                    "status": "NEED_CONTRACT_UPDATE",
                    "summary": "Order 缺少 currency 字段",
                    "files_changed": [],
                    "tests_added": [],
                    "notes": "需要契约组在 contracts/schema.py 的 Order 类添加 currency 字段",
                }
            (cwd / "modules" / "order").mkdir(parents=True, exist_ok=True)
            (cwd / "modules" / "order" / "__init__.py").write_text("", "utf-8")
            (cwd / "modules" / "order" / "svc.py").write_text(
                "from contracts.schema import Order\n"
                "class OrderService:\n"
                "    def __init__(self): self._o = {}\n"
                "    def create(self, oid: str, uid: str, amount: int, currency: str = 'CNY') -> Order:\n"
                "        o = Order(oid, uid, amount, currency); self._o[oid] = o; return o\n",
                "utf-8",
            )
            (cwd / "tests" / "test_order.py").write_text(
                "import unittest\nfrom modules.order.svc import OrderService\n"
                "class T(unittest.TestCase):\n    def test_order(self): self.assertEqual(OrderService().create('o1','u1',99,'USD').currency,'USD')\n",
                "utf-8",
            )
            return {"status": "DONE", "summary": "order squad done", "files_changed": ["modules/order/svc.py"], "tests_added": ["tests/test_order.py"], "notes": ""}

        if tid == "T-004":
            (cwd / "gateway").mkdir(exist_ok=True)
            (cwd / "gateway" / "__init__.py").write_text("", "utf-8")
            (cwd / "gateway" / "app.py").write_text(
                "from modules.user.svc import UserService\nfrom modules.order.svc import OrderService\n"
                "class Gateway:\n"
                "    def __init__(self): self.users = UserService(); self.orders = OrderService()\n"
                "    def place(self, uid: str, name: str, oid: str, amount: int):\n"
                "        u = self.users.create(uid, name)\n"
                "        return self.orders.create(oid, u.uid, amount)\n",
                "utf-8",
            )
            (cwd / "tests" / "test_gateway.py").write_text(
                "import unittest\nfrom gateway.app import Gateway\n"
                "class T(unittest.TestCase):\n    def test_place(self): self.assertEqual(Gateway().place('u1','A','o1',50).currency,'CNY')\n",
                "utf-8",
            )
            return {"status": "DONE", "summary": "gateway integrated", "files_changed": ["gateway/app.py"], "tests_added": ["tests/test_gateway.py"], "notes": ""}

        raise AssertionError(f"unexpected task {tid}")

    harness = MockHarness(
        coder=mock_coder,
        role_handlers={"pm": pm_fn, "director": director_fn},
    )
    settings = Settings(
        planning_mode="goal",
        enable_pm_spec=True,
        require_plan_approval=True,
        require_merge_approval=False,
        max_concurrency=2,
        director_max_arbitrations=2,
    )
    eng = Engine(ws, settings=settings, harness=harness)
    try:
        eng.ingest()
        asyncio.run(eng.plan())
        assert (ws.root / "docs" / "PRD_SPEC.md").exists()
        assert eng.status == ProjectStatus.PLANNED
        assert len(eng.db.list_squads()) == 4
        # M2 尚未展开（滚动排期）
        assert eng.db.get_milestone("M2-domains").planned is False

        eng.approve_plan(actor="zomer")
        final = asyncio.run(Scheduler(eng).run(watch=False))
        assert final == ProjectStatus.COMPLETED
        assert eng.db.get_milestone("M2-domains").planned is True
        # 验证总监自动创建的契约补丁任务已跑通并合入
        patch = eng.db.get_task("T-003-P1")
        assert patch is not None and patch.status == TaskStatus.MERGED
        assert (ws.root / "gateway" / "app.py").exists()

        # 验证 Git 提交历史包含具体研发组与角色实名署名！
        authors = gitops.git(["log", "--format=%an"], ws.root).stdout
        assert "用户域研发组" in authors
        assert "订单域研发组" in authors
        assert "网关装配组" in authors
    finally:
        eng.db.close()


def test_opencode_v2_adapter_command_and_stream(tmp_path: Path) -> None:
    written = write_opencode_agent_profiles(tmp_path)
    assert ".opencode/agents/pm.md" in written
    assert "edit: false" in (tmp_path / ".opencode/agents/director.md").read_text("utf-8")

    fake_oc = tmp_path / "fake-opencode"
    fake_oc.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "_ = sys.stdin.read()\n"
        "print(json.dumps({'type': 'step_start', 'sessionID': 'ses_oc_999'}), flush=True)\n"
        "print(json.dumps({'type': 'tool_use', 'part': {'id': 'p1', 'tool': 'task', 'state': {'status': 'completed', 'input': {'subagent_type': 'explorer', 'description': 'scan contracts'}, 'output': 'found'}}}), flush=True)\n"
        "print(json.dumps({'type': 'text', 'part': {'id': 'p2', 'text': json.dumps({'status': 'DONE', 'summary': 'ok', 'files_changed': [], 'tests_added': [], 'notes': ''})}}), flush=True)\n"
        "print(json.dumps({'type': 'step_finish', 'part': {'tokens': {'input': 500, 'output': 80, 'cache': {'read': 200}}}}), flush=True)\n",
        "utf-8",
    )
    fake_oc.chmod(fake_oc.stat().st_mode | stat.S_IEXEC)

    events: list[tuple[str, str, dict | None]] = []
    settings = Settings(harness="opencode", opencode_server_url="", model="deepseek/deepseek-chat")
    h = OpenCodeHarness(settings, str(fake_oc), activity_cb=lambda k, t, p=None: events.append((k, t, p)))
    cmd = h.build_cmd(role="coder", cwd=tmp_path)
    assert "--auto" in cmd and "--thinking" in cmd
    assert "--agent" in cmd and "coder" in cmd

    res = asyncio.run(
        h.run_workspace_task("hi", cwd=tmp_path, log_dir=tmp_path / "log", schema_path=None, role="coder")
    )
    assert res.ok is True
    assert res.session_id == "ses_oc_999"
    assert res.usage.input_tokens == 500 and res.usage.cached_tokens == 200
    assert any("explorer" in text for _, text, _ in events)
