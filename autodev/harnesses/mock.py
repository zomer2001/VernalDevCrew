"""可编程的模拟执行器：用于平台自身的端到端测试，不调用任何模型。"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from autodev.harnesses.base import AgentResult, Harness, Usage


@dataclass
class MockCall:
    kind: str           # readonly | workspace | resume
    role: str
    prompt: str
    cwd: Path
    session_id: str | None


# 编码回调：(prompt, cwd, call) -> worker_report dict
CoderFn = Callable[[str, Path, MockCall], dict[str, Any]]
# 只读回调：(prompt, cwd, call) -> 结构化 dict
ReadonlyFn = Callable[[str, Path, MockCall], dict[str, Any]]


@dataclass
class MockHarness(Harness):
    planner: ReadonlyFn | None = None
    coder: CoderFn | None = None
    reviewer: ReadonlyFn | None = None
    role_handlers: dict[str, ReadonlyFn] = field(default_factory=dict)
    calls: list[MockCall] = field(default_factory=list)
    name: str = "mock"

    def __post_init__(self) -> None:
        super().__init__(None)
        self._ids = itertools.count(1)

    async def run_structured_readonly(self, prompt, *, schema_path, cwd, log_dir, role) -> AgentResult:
        call = MockCall("readonly", role, prompt, Path(cwd), None)
        self.calls.append(call)
        fn = self.role_handlers.get(role) or (self.planner if role in ("planner", "director", "pm") else self.reviewer)
        if fn is None:
            data = {"verdict": "PASS", "summary": "mock", "criteria_check": [], "blocking_issues": []}
        else:
            data = fn(prompt, Path(cwd), call)
        return AgentResult(True, 0, f"mock-ro-{next(self._ids)}", "", structured=data, usage=Usage(100, 0, 20, 10))

    async def run_workspace_task(
        self, prompt, *, cwd, log_dir, schema_path, role, allow_network: bool = False, extra_env=None
    ) -> AgentResult:
        session = f"mock-session-{next(self._ids)}"
        call = MockCall("workspace", role, prompt, Path(cwd), session)
        return self._code(call)

    async def resume_workspace_task(
        self, session_id, prompt, *, cwd, log_dir, schema_path, role, extra_env=None
    ) -> AgentResult:
        call = MockCall("resume", role, prompt, Path(cwd), session_id)
        return self._code(call)

    def _code(self, call: MockCall) -> AgentResult:
        self.calls.append(call)
        if self.coder is None:
            raise RuntimeError("MockHarness.coder 未设置")
        report = self.coder(call.prompt, call.cwd, call)
        return AgentResult(True, 0, call.session_id, "", structured=report, usage=Usage(1000, 200, 300))

    async def health_check(self) -> tuple[bool, str]:
        return True, "mock"
