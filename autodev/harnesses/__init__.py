"""可插拔 CLI 编码智能体执行器与多角色混合路由器（Codex / CodeBuddy / OpenCode / DeepSeek Harness / Claude Code）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from autodev.config import (
    Settings,
    detect_preferred_harness,
    normalize_harness_name,
    resolve_claude_bin,
    resolve_codebuddy_bin,
    resolve_codex_bin,
    resolve_dsh_bin,
    resolve_opencode_bin,
)
from autodev.harnesses.base import ActivityCallback, AgentResult, Harness, Usage, extract_json

__all__ = ["AgentResult", "Harness", "HarnessRouter", "Usage", "extract_json", "create_harness"]


def _build_single_harness(name: str, settings: Settings, activity_cb: ActivityCallback | None = None) -> Harness:
    norm = normalize_harness_name(name or "auto")
    if norm == "auto":
        norm = detect_preferred_harness(settings)
    if norm == "codex":
        from autodev.harnesses.codex_cli import CodexHarness

        codex_bin = resolve_codex_bin(settings)
        if not codex_bin:
            raise RuntimeError("找不到 codex 可执行文件，请在网页「编队与模型设置」切换已安装的执行器或安装 codex")
        return CodexHarness(settings, codex_bin, activity_cb=activity_cb)
    if norm == "opencode":
        from autodev.harnesses.opencode_cli import OpenCodeHarness

        opencode_bin = resolve_opencode_bin(settings)
        if not opencode_bin:
            raise RuntimeError("找不到 opencode 可执行文件，请先安装 opencode 或在「编队与模型设置」切换其他执行器")
        return OpenCodeHarness(settings, opencode_bin, activity_cb=activity_cb)
    if norm == "codebuddy":
        from autodev.harnesses.cli_generic import CodeBuddyHarness

        cb_bin = resolve_codebuddy_bin(settings)
        if not cb_bin:
            raise RuntimeError("找不到 codebuddy 可执行文件，请先安装 @tencent-ai/codebuddy-code 或切换其他执行器")
        return CodeBuddyHarness(settings, cb_bin, activity_cb=activity_cb)
    if norm == "dsh":
        from autodev.harnesses.cli_generic import DeepSeekHarness

        dsh_bin = resolve_dsh_bin(settings)
        if not dsh_bin:
            raise RuntimeError("找不到 dsh (DeepSeek Harness) 可执行文件，请先安装 @deepseek-ai/dsh 或切换其他执行器")
        return DeepSeekHarness(settings, dsh_bin, activity_cb=activity_cb)
    if norm == "claude-code":
        from autodev.harnesses.cli_generic import ClaudeCodeHarness

        cl_bin = resolve_claude_bin(settings)
        if not cl_bin:
            raise RuntimeError("找不到 claude 可执行文件，请先安装 @anthropic-ai/claude-code 或切换其他执行器")
        return ClaudeCodeHarness(settings, cl_bin, activity_cb=activity_cb)
    raise RuntimeError(f"不支持的 harness: {name}（可选: auto, codex, codebuddy, opencode, dsh, claude-code）")


class HarnessRouter(Harness):
    """按角色动态路由到对应的底层引擎（支持同一工作区内 Codex 与 OpenCode 混合编队）。"""

    name = "router"

    def __init__(self, settings: Settings, activity_cb: ActivityCallback | None = None):
        super().__init__(activity_cb)
        self.settings = settings
        self._cache: dict[str, Harness] = {}

    def for_role(self, role: str) -> Harness:
        hname = self.settings.harness_for(role)
        if hname not in self._cache:
            self._cache[hname] = _build_single_harness(hname, self.settings, activity_cb=self.activity_cb)
        return self._cache[hname]

    def badge_for(self, role: str) -> tuple[str, str]:
        hname = self.settings.harness_for(role)
        model = self.settings.model_for(role) or "default"
        return hname, model

    async def run_structured_readonly(
        self, prompt: str, *, schema_path: Path, cwd: Path, log_dir: Path, role: str
    ) -> AgentResult:
        return await self.for_role(role).run_structured_readonly(
            prompt, schema_path=schema_path, cwd=cwd, log_dir=log_dir, role=role
        )

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
    ) -> AgentResult:
        return await self.for_role(role).run_workspace_task(
            prompt,
            cwd=cwd,
            log_dir=log_dir,
            schema_path=schema_path,
            role=role,
            allow_network=allow_network,
            extra_env=extra_env,
        )

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
    ) -> AgentResult:
        return await self.for_role(role).resume_workspace_task(
            session_id, prompt, cwd=cwd, log_dir=log_dir, schema_path=schema_path, role=role, extra_env=extra_env
        )

    async def health_check(self) -> tuple[bool, str]:
        return await self.for_role("coder").health_check()


def create_harness(settings: Settings, activity_cb: ActivityCallback | None = None) -> Harness:
    distinct = {
        settings.harness_for("director"),
        settings.harness_for("pm"),
        settings.harness_for("coder"),
        settings.harness_for("reviewer"),
    }
    if len(distinct) == 1:
        return _build_single_harness(next(iter(distinct)), settings, activity_cb=activity_cb)
    return HarnessRouter(settings, activity_cb=activity_cb)
