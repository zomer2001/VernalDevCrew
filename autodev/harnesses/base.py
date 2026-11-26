"""执行器抽象接口与通用结果结构。"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 活动回调：(kind, text, payload)，例如 ("command", "pytest -q (exit 0)", {...})
ActivityCallback = Callable[[str, str, dict[str, Any] | None], None]


@dataclass
class Usage:
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    telemetry_known: bool = True
    source: str = ""

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.cached_tokens += other.cached_tokens
        self.output_tokens += other.output_tokens
        self.reasoning_tokens += other.reasoning_tokens
        if not other.telemetry_known:
            self.telemetry_known = False


@dataclass
class AgentResult:
    ok: bool
    exit_code: int
    session_id: str | None
    final_message: str
    structured: dict[str, Any] | None = None
    usage: Usage = field(default_factory=Usage)
    error: str = ""
    log_dir: Path | None = None
    duration_sec: float = 0.0
    timed_out: bool = False
    timing: dict[str, Any] = field(default_factory=dict)


def extract_json(text: str) -> dict[str, Any] | None:
    """从模型输出中提取 JSON 对象：直接解析 → ```json 代码块 → 首尾花括号。"""
    text = (text or "").strip()
    if not text:
        return None
    candidates = [text]
    candidates += re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])
    for c in candidates:
        try:
            value = json.loads(c)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


class Harness(ABC):
    """统一的 CLI 智能体执行器接口。

    - run_structured_readonly: 只读沙箱 + 结构化输出（规划 / 审查）
    - run_workspace_task: 在隔离工作树中首次读写运行（编码）
    - resume_workspace_task: 恢复同一会话继续修复（返工）
    """

    name: str = "base"

    def __init__(self, activity_cb: ActivityCallback | None = None):
        self.activity_cb = activity_cb

    def _activity(self, kind: str, text: str, payload: dict[str, Any] | None = None) -> None:
        if self.activity_cb:
            self.activity_cb(kind, text, payload)

    @abstractmethod
    async def run_structured_readonly(
        self, prompt: str, *, schema_path: Path, cwd: Path, log_dir: Path, role: str
    ) -> AgentResult: ...

    @abstractmethod
    async def run_workspace_task(
        self,
        prompt: str,
        *,
        cwd: Path,
        log_dir: Path,
        schema_path: Path | None,
        role: str,
        allow_network: bool = False,
        extra_env: dict[str, str] | None = None,
    ) -> AgentResult: ...

    @abstractmethod
    async def resume_workspace_task(
        self,
        session_id: str,
        prompt: str,
        *,
        cwd: Path,
        log_dir: Path,
        schema_path: Path | None,
        role: str,
        extra_env: dict[str, str] | None = None,
    ) -> AgentResult: ...

    @abstractmethod
    async def health_check(self) -> tuple[bool, str]: ...
