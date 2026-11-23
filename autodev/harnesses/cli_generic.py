"""腾讯 CodeBuddy、DeepSeek Harness (dsh) 与 Claude Code 无头 CLI 适配器。

使顶层多智能体圆桌（Architecture Council）、蓝图拆单（Director/Planner）及离线模式
均可直接驱动国产/跨平台 CLI，与底层 LoopForge 执行器保持一致。
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from pathlib import Path
from typing import Any

from autodev.config import Settings
from autodev.harnesses.base import ActivityCallback, AgentResult, Harness, Usage, extract_json

_STREAM_LIMIT = 32 * 1024 * 1024


def _parse_stream_json_line(ev: dict[str, Any], usage: Usage) -> tuple[str | None, list[str], tuple[str, str] | None]:
    """兼容 anthropic-stream-v1 (CodeBuddy / Claude Code) 与 DeepSeek Harness (dsh --json) 的单行事件提取。"""
    sid: str | None = None
    for k in ("session_id", "sessionId", "conversation_id"):
        if isinstance(ev.get(k), str) and ev[k]:
            sid = ev[k]
            break

    u = ev.get("usage")
    if isinstance(u, dict):
        usage.input_tokens = max(usage.input_tokens, int(u.get("input_tokens") or u.get("inputTokens") or 0))
        usage.cached_tokens = max(
            usage.cached_tokens,
            int(u.get("cache_read_input_tokens") or u.get("cacheReadTokens") or u.get("cached_tokens") or 0),
        )
        usage.output_tokens = max(usage.output_tokens, int(u.get("output_tokens") or u.get("outputTokens") or 0))
        usage.reasoning_tokens = max(
            usage.reasoning_tokens,
            int(u.get("reasoning_tokens") or u.get("reasoningTokens") or 0),
        )

    texts: list[str] = []
    activity: tuple[str, str] | None = None
    etype = str(ev.get("type") or ev.get("event") or "")

    if etype == "result":
        res = ev.get("result")
        if isinstance(res, str) and res.strip():
            texts.append(res)
    elif etype in ("assistant", "message"):
        msg = ev.get("message") if isinstance(ev.get("message"), dict) else ev
        content = msg.get("content")
        if isinstance(content, str) and content:
            texts.append(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text" and isinstance(block.get("text"), str):
                    texts.append(block["text"])
                elif btype in ("thinking", "reasoning") and isinstance(block.get("thinking"), str):
                    activity = ("reasoning", block["thinking"][:160])
                elif btype in ("tool_use", "tool_call"):
                    tname = str(block.get("name") or block.get("toolName") or "tool")
                    activity = ("command", f"调用工具 {tname}")
    elif etype in ("text", "text_delta", "output", "content_block_delta"):
        delta = ev.get("delta")
        if isinstance(delta, dict) and isinstance(delta.get("text"), str):
            texts.append(delta["text"])
        elif isinstance(ev.get("text"), str):
            texts.append(ev["text"])
        elif isinstance(ev.get("content"), str):
            texts.append(ev["content"])
    elif etype in ("tool_use", "tool_call", "tool.use"):
        tname = str(ev.get("name") or ev.get("toolName") or "tool")
        activity = ("command", f"调用工具 {tname}")

    return sid, texts, activity


class _StreamCLIHarness(Harness):
    """基于 JSONL / Stream-JSON 标准输出的通用无头 CLI 基类。"""

    def __init__(self, settings: Settings, bin_path: str, activity_cb: ActivityCallback | None = None):
        super().__init__(activity_cb)
        self.settings = settings
        self.bin_path = bin_path

    def build_cmd(
        self,
        prompt: str,
        *,
        role: str,
        readonly: bool = False,
        session_id: str | None = None,
    ) -> list[str]:
        raise NotImplementedError

    async def health_check(self) -> tuple[bool, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                self.bin_path, "--version", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
            out, _ = await asyncio.wait_for(proc.communicate(), 15)
        except (OSError, TimeoutError) as exc:
            return False, f"无法执行 {self.bin_path}: {exc}"
        return proc.returncode == 0, out.decode(errors="replace").strip()

    async def run_structured_readonly(
        self, prompt: str, *, schema_path: Path, cwd: Path, log_dir: Path, role: str
    ) -> AgentResult:
        schema_text = schema_path.read_text("utf-8")
        full_prompt = (
            f"{prompt}\n\n## 严格输出格式（JSON Schema）\n\n"
            f"请只输出满足以下 JSON Schema 的单个 JSON 对象，不要附加额外解释或代码块以外的文字：\n```json\n{schema_text}\n```\n"
        )
        cmd = self.build_cmd(full_prompt, role=role, readonly=True)
        return await self._run(cmd, full_prompt, cwd=cwd, log_dir=log_dir, want_json=True)

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
        full_prompt = prompt
        if schema_path is not None:
            full_prompt += "\n\n## 最终汇报 JSON Schema\n```json\n" + schema_path.read_text("utf-8") + "\n```\n"
        cmd = self.build_cmd(full_prompt, role=role, readonly=False)
        return await self._run(
            cmd,
            full_prompt,
            cwd=cwd,
            log_dir=log_dir,
            want_json=schema_path is not None,
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
        cmd = self.build_cmd(prompt, role=role, readonly=False, session_id=session_id)
        return await self._run(
            cmd,
            prompt,
            cwd=cwd,
            log_dir=log_dir,
            want_json=schema_path is not None,
            extra_env=extra_env,
        )

    async def _run(
        self,
        cmd: list[str],
        prompt: str,
        *,
        cwd: Path,
        log_dir: Path,
        want_json: bool,
        extra_env: dict[str, str] | None = None,
    ) -> AgentResult:
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "prompt.md").write_text(prompt, "utf-8")
        (log_dir / "command.json").write_text(json.dumps(cmd, ensure_ascii=False, indent=2), "utf-8")
        env = os.environ.copy()
        env.update(self.settings.env)
        if extra_env:
            env.update(extra_env)
        started = time.monotonic()

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(cwd),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=True,
                limit=_STREAM_LIMIT,
            )
        except OSError as exc:
            return AgentResult(False, -1, None, "", error=f"启动 {self.name} 失败: {exc}", log_dir=log_dir)

        session_id: str | None = None
        usage = Usage()
        texts: list[str] = []
        raw_lines: list[str] = []
        stderr_chunks: list[bytes] = []
        events_path = log_dir / "events.jsonl"

        async def _read_stdout() -> None:
            nonlocal session_id
            assert proc.stdout is not None
            with events_path.open("w", encoding="utf-8") as ef:
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    s = line.decode("utf-8", errors="replace")
                    ef.write(s)
                    ef.flush()
                    stripped = s.strip()
                    if not stripped:
                        continue
                    try:
                        ev = json.loads(stripped)
                    except json.JSONDecodeError:
                        raw_lines.append(s)
                        continue
                    if isinstance(ev, dict):
                        sid, segs, act = _parse_stream_json_line(ev, usage)
                        if sid:
                            session_id = sid
                        if segs:
                            texts.extend(segs)
                            self._activity("message", segs[-1][:180], None)
                        if act:
                            self._activity(act[0], act[1], None)

        async def _read_stderr() -> None:
            assert proc.stderr is not None
            while True:
                chunk = await proc.stderr.read(4096)
                if not chunk:
                    break
                stderr_chunks.append(chunk)

        timed_out = False
        try:
            await asyncio.wait_for(
                asyncio.gather(_read_stdout(), _read_stderr(), proc.wait()),
                timeout=self.settings.agent_timeout_sec,
            )
        except TimeoutError:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()

        duration = time.monotonic() - started
        stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace")
        (log_dir / "stderr.log").write_text(stderr_text, "utf-8")
        final_msg = ("\n".join(texts).strip() if texts else "".join(raw_lines).strip())
        (log_dir / "final_message.txt").write_text(final_msg, "utf-8")

        if timed_out:
            return AgentResult(
                False,
                -1,
                session_id,
                final_msg,
                usage=usage,
                error=f"{self.name} 执行超时 ({self.settings.agent_timeout_sec}s)",
                log_dir=log_dir,
                duration_sec=duration,
                timed_out=True,
            )

        code = proc.returncode if proc.returncode is not None else -1
        structured = extract_json(final_msg) if want_json else None
        if want_json and structured is None and code == 0:
            return AgentResult(
                False,
                code,
                session_id,
                final_msg,
                usage=usage,
                error=f"{self.name} 输出中未提取到有效 JSON 对象",
                log_dir=log_dir,
                duration_sec=duration,
            )
        ok = code == 0
        err = "" if ok else (stderr_text.strip()[-800:] or final_msg[-800:] or f"{self.name} 退出码 {code}")
        return AgentResult(
            ok=ok,
            exit_code=code,
            session_id=session_id,
            final_message=final_msg,
            structured=structured,
            usage=usage,
            error=err,
            log_dir=log_dir,
            duration_sec=duration,
        )


class CodeBuddyHarness(_StreamCLIHarness):
    """腾讯 CodeBuddy CLI (`@tencent-ai/codebuddy-code`) 适配器。"""

    name = "codebuddy"

    def build_cmd(
        self,
        prompt: str,
        *,
        role: str,
        readonly: bool = False,
        session_id: str | None = None,
    ) -> list[str]:
        cmd = [
            self.bin_path,
            "-p",
            "--output-format",
            "stream-json",
            "-y",
            "--permission-mode",
            "bypassPermissions",
        ]
        model = self.settings.model_for(role)
        if model:
            cmd += ["--model", model]
        if session_id:
            cmd += ["--resume", session_id]
        cmd += ["--", prompt]
        return cmd


class DeepSeekHarness(_StreamCLIHarness):
    """DeepSeek 官方 Harness (`@deepseek-ai/dsh`, 命令 `dsh`) 适配器。"""

    name = "dsh"

    def build_cmd(
        self,
        prompt: str,
        *,
        role: str,
        readonly: bool = False,
        session_id: str | None = None,
    ) -> list[str]:
        perm = "read-only" if readonly else "danger-full-access"
        cmd = [
            self.bin_path,
            "--profile",
            "headless",
            "--json",
            "--permission-mode",
            perm,
            "--approval-policy",
            "never",
        ]
        if session_id:
            cmd += ["--session-id", session_id]
        cmd.append(prompt)
        return cmd


class ClaudeCodeHarness(_StreamCLIHarness):
    """Anthropic Claude Code CLI (`@anthropic-ai/claude-code`) 适配器。"""

    name = "claude-code"

    def build_cmd(
        self,
        prompt: str,
        *,
        role: str,
        readonly: bool = False,
        session_id: str | None = None,
    ) -> list[str]:
        cmd = [
            self.bin_path,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
        ]
        model = self.settings.model_for(role)
        if model:
            cmd += ["--model", model]
        if session_id:
            cmd += ["--resume", session_id]
        cmd.append(prompt)
        return cmd
