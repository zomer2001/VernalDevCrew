"""基于官方 `codex exec` 非交互模式的执行器实现。

调用形态：
  只读结构化:  codex exec --sandbox read-only --json --output-schema S -o OUT -C DIR -
  首次读写:    codex exec --sandbox workspace-write --json [--output-schema S] -o OUT -C DIR -
  断点续跑:    codex exec resume <SESSION_ID> -c sandbox_mode="workspace-write" --json -o OUT -
提示词一律通过 stdin 传入。
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


class CodexHarness(Harness):
    name = "codex"

    def __init__(self, settings: Settings, codex_bin: str, activity_cb: ActivityCallback | None = None):
        super().__init__(activity_cb)
        self.settings = settings
        self.codex_bin = codex_bin

    # ---------- 参数组装 ----------
    def _common_args(self, role: str) -> list[str]:
        args: list[str] = []
        model = self.settings.model_for(role)
        if model:
            args += ["-m", model]
        args += list(self.settings.codex_extra_args)
        return args

    def _profile_args(self) -> list[str]:
        return ["-p", self.settings.codex_profile] if self.settings.codex_profile else []

    def _write_args(self, allow_network: bool = False) -> list[str]:
        if self.settings.agent_network_access or allow_network:
            return ["-c", "sandbox_workspace_write.network_access=true"]
        return []

    def build_readonly_cmd(self, *, schema_path: Path, out_path: Path, cwd: Path, role: str) -> list[str]:
        return [
            self.codex_bin, "exec", *self._profile_args(),
            "--sandbox", "read-only", "--skip-git-repo-check", "--json",
            "--output-schema", str(schema_path), "-o", str(out_path), "-C", str(cwd),
            *self._common_args(role), "-",
        ]

    def build_workspace_cmd(
        self, *, schema_path: Path | None, out_path: Path, cwd: Path, role: str, allow_network: bool = False
    ) -> list[str]:
        schema = ["--output-schema", str(schema_path)] if schema_path else []
        return [
            self.codex_bin, "exec", *self._profile_args(),
            "--sandbox", "workspace-write", "--skip-git-repo-check", "--json",
            *schema, "-o", str(out_path), "-C", str(cwd),
            *self._write_args(allow_network), *self._common_args(role), "-",
        ]

    def build_resume_cmd(self, session_id: str, *, schema_path: Path | None, out_path: Path, role: str) -> list[str]:
        schema = ["--output-schema", str(schema_path)] if schema_path else []
        return [
            self.codex_bin, "exec", *self._profile_args(), "resume", session_id,
            "-c", 'sandbox_mode="workspace-write"', "--skip-git-repo-check", "--json",
            *schema, "-o", str(out_path),
            *self._write_args(), *self._common_args(role), "-",
        ]

    # ---------- 公共接口 ----------
    async def run_structured_readonly(
        self, prompt: str, *, schema_path: Path, cwd: Path, log_dir: Path, role: str
    ) -> AgentResult:
        out = log_dir / "last_message.txt"
        cmd = self.build_readonly_cmd(schema_path=schema_path, out_path=out, cwd=cwd, role=role)
        return await self._run(cmd, prompt, cwd=cwd, log_dir=log_dir, out_path=out, want_json=True)

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
        out = log_dir / "last_message.txt"
        cmd = self.build_workspace_cmd(
            schema_path=schema_path, out_path=out, cwd=cwd, role=role, allow_network=allow_network
        )
        return await self._run(
            cmd, prompt, cwd=cwd, log_dir=log_dir, out_path=out, want_json=schema_path is not None, extra_env=extra_env
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
        out = log_dir / "last_message.txt"
        cmd = self.build_resume_cmd(session_id, schema_path=schema_path, out_path=out, role=role)
        return await self._run(
            cmd, prompt, cwd=cwd, log_dir=log_dir, out_path=out, want_json=schema_path is not None, extra_env=extra_env
        )

    async def health_check(self) -> tuple[bool, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                self.codex_bin, "--version", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
            out, _ = await asyncio.wait_for(proc.communicate(), 20)
        except (OSError, TimeoutError) as exc:
            return False, f"无法执行 {self.codex_bin}: {exc}"
        text = out.decode(errors="replace").strip()
        return proc.returncode == 0, text

    # ---------- 子进程与事件流 ----------
    async def _run(
        self,
        cmd: list[str],
        prompt: str,
        *,
        cwd: Path,
        log_dir: Path,
        out_path: Path,
        want_json: bool,
        extra_env: dict[str, str] | None = None,
    ) -> AgentResult:
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "prompt.md").write_text(prompt, "utf-8")
        (log_dir / "command.json").write_text(json.dumps(cmd, ensure_ascii=False, indent=2), "utf-8")
        if out_path.exists():
            out_path.unlink()
        env = os.environ.copy()
        env.update(self.settings.env)
        if extra_env:
            env.update(extra_env)
        started = time.monotonic()
        state = _StreamState()

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(cwd),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=True,
                limit=_STREAM_LIMIT,
            )
        except OSError as exc:
            return AgentResult(False, -1, None, "", error=f"启动 codex 失败: {exc}", log_dir=log_dir)

        events_file = (log_dir / "events.jsonl").open("w", encoding="utf-8")
        stderr_chunks: list[bytes] = []

        async def feed_stdin() -> None:
            assert proc.stdin is not None
            try:
                proc.stdin.write(prompt.encode("utf-8"))
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                proc.stdin.close()

        async def read_stdout() -> None:
            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip("\n")
                events_file.write(text + "\n")
                events_file.flush()
                self._handle_line(text, state)

        async def read_stderr() -> None:
            assert proc.stderr is not None
            while True:
                chunk = await proc.stderr.read(65536)
                if not chunk:
                    break
                stderr_chunks.append(chunk)

        timed_out = False
        try:
            await asyncio.wait_for(
                asyncio.gather(feed_stdin(), read_stdout(), read_stderr(), proc.wait()),
                timeout=self.settings.agent_timeout_sec,
            )
        except TimeoutError:
            timed_out = True
            _kill_group(proc)
            try:
                await asyncio.wait_for(proc.wait(), 10)
            except TimeoutError:
                pass
        finally:
            events_file.close()

        stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace")
        (log_dir / "stderr.log").write_text(stderr_text, "utf-8")
        final = out_path.read_text("utf-8") if out_path.exists() else state.last_agent_message
        exit_code = proc.returncode if proc.returncode is not None else -9

        errors = list(state.errors)
        if timed_out:
            errors.append(f"超时（>{self.settings.agent_timeout_sec}s）被终止")
        if exit_code != 0 and not errors:
            tail = "\n".join(stderr_text.strip().splitlines()[-20:])
            errors.append(f"codex 退出码 {exit_code}: {tail}")

        structured = extract_json(final) if want_json else None
        ok = exit_code == 0 and not timed_out and not state.turn_failed
        if ok and want_json and structured is None:
            ok = False
            errors.append("最终输出不是合法 JSON")
        return AgentResult(
            ok=ok,
            exit_code=exit_code,
            session_id=state.thread_id,
            final_message=final,
            structured=structured,
            usage=state.usage,
            error="; ".join(errors),
            log_dir=log_dir,
            duration_sec=time.monotonic() - started,
            timed_out=timed_out,
        )

    def _handle_line(self, line: str, state: "_StreamState") -> None:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict):
            return
        etype = event.get("type", "")
        if etype == "thread.started":
            state.thread_id = event.get("thread_id") or state.thread_id
            self._activity("turn", f"会话已建立 ({(state.thread_id or '')[:12]})", {"phase": "thread", "thread_id": state.thread_id})
        elif etype == "turn.started":
            self._activity("turn", "模型开始思考与执行", {"phase": "started"})
        elif etype == "turn.completed":
            u = event.get("usage") or {}
            in_tok = int(u.get("input_tokens") or 0)
            cache_tok = int(u.get("cached_input_tokens") or 0)
            out_tok = int(u.get("output_tokens") or 0)
            reason_tok = int(u.get("reasoning_output_tokens") or 0)
            state.usage.add(Usage(in_tok, cache_tok, out_tok, reason_tok))
            self._activity(
                "turn",
                f"本轮完成（in {in_tok:,} / cache {cache_tok:,} / out {out_tok:,}"
                + (f" / think {reason_tok:,}" if reason_tok else "")
                + "）",
                {
                    "phase": "completed",
                    "usage": {"input": in_tok, "cached": cache_tok, "output": out_tok, "reasoning": reason_tok},
                },
            )
        elif etype == "turn.failed":
            state.turn_failed = True
            msg = str((event.get("error") or {}).get("message") or "turn.failed")
            state.errors.append(msg)
            self._activity("warning", f"本轮失败：{msg}", {"phase": "failed", "text": msg})
        elif etype == "error":
            msg = str(event.get("message") or "error")
            state.errors.append(msg)
            self._activity("warning", msg, {"phase": "warning", "text": msg})
        elif etype == "item.started":
            self._handle_item_started(event.get("item") or {})
        elif etype == "item.completed":
            self._handle_item_completed(event.get("item") or {}, state)

    def _handle_item_started(self, item: dict[str, Any]) -> None:
        itype = item.get("type") or item.get("item_type") or ""
        item_id = item.get("id") or ""
        if itype == "command_execution":
            cmd = str(item.get("command", ""))
            short = cmd.replace("\n", " ")[:180]
            self._activity(
                "command",
                f"正在执行：{short}",
                {"item_id": item_id, "phase": "started", "command": cmd},
            )
        elif itype == "file_change":
            self._activity("file", "正在修改文件…", {"item_id": item_id, "phase": "started"})
        elif itype == "reasoning":
            self._activity("reasoning", "正在推理…", {"item_id": item_id, "phase": "started"})

    def _handle_item_completed(self, item: dict[str, Any], state: "_StreamState") -> None:
        itype = item.get("type") or item.get("item_type") or ""
        item_id = item.get("id") or ""
        if itype == "command_execution":
            cmd = str(item.get("command", ""))
            short = cmd.replace("\n", " ")[:180]
            exit_code = item.get("exit_code")
            output = str(item.get("aggregated_output") or "")
            if len(output) > 12_000:
                output = output[:6_000] + f"\n…（中间省略 {len(output) - 12_000} 字符）…\n" + output[-6_000:]
            self._activity(
                "command",
                f"{short} (exit {exit_code})",
                {
                    "item_id": item_id,
                    "phase": "completed",
                    "command": cmd,
                    "exit_code": exit_code,
                    "output": output,
                },
            )
        elif itype == "file_change":
            changes = item.get("changes") or []
            files = ", ".join(f"{c.get('kind', '?')}:{c.get('path', '?')}" for c in changes[:12])
            self._activity(
                "file",
                files or "文件已更新",
                {"item_id": item_id, "phase": "completed", "changes": changes},
            )
        elif itype == "agent_message":
            text = str(item.get("text", ""))
            state.last_agent_message = text
            self._activity(
                "message",
                text[:220].replace("\n", " "),
                {"item_id": item_id, "phase": "completed", "text": text[:12_000]},
            )
        elif itype == "reasoning":
            text = str(item.get("text", ""))
            self._activity(
                "reasoning",
                text[:220].replace("\n", " "),
                {"item_id": item_id, "phase": "completed", "text": text[:12_000]},
            )
        elif itype == "error":
            msg = str(item.get("message", ""))
            state.errors.append(msg)
            self._activity("warning", msg, {"item_id": item_id, "phase": "warning", "text": msg})


def parse_events_jsonl(path: Path, max_output_chars: int = 12_000) -> list[dict[str, Any]]:
    """把落盘的 events.jsonl 解析为结构化步骤列表（合并同一 item_id 的 started/completed）。"""
    if not path.exists():
        return []
    steps: list[dict[str, Any]] = []
    by_item: dict[str, int] = {}
    for raw in path.read_text("utf-8", errors="replace").splitlines():
        if not raw.strip():
            continue
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        etype = ev.get("type", "")
        if etype == "turn.started":
            steps.append({"kind": "turn", "phase": "started", "summary": "模型开始思考与执行"})
        elif etype == "turn.completed":
            u = ev.get("usage") or {}
            steps.append(
                {
                    "kind": "turn",
                    "phase": "completed",
                    "summary": "本轮完成",
                    "usage": {
                        "input": int(u.get("input_tokens") or 0),
                        "cached": int(u.get("cached_input_tokens") or 0),
                        "output": int(u.get("output_tokens") or 0),
                        "reasoning": int(u.get("reasoning_output_tokens") or 0),
                    },
                }
            )
        elif etype in ("error", "turn.failed"):
            msg = str(ev.get("message") or (ev.get("error") or {}).get("message") or etype)
            steps.append({"kind": "warning", "phase": "warning", "text": msg})
        elif etype in ("item.started", "item.completed"):
            item = ev.get("item") or {}
            itype = item.get("type") or item.get("item_type") or ""
            item_id = str(item.get("id") or "")
            phase = "started" if etype == "item.started" else "completed"
            step: dict[str, Any] | None = None
            if itype == "command_execution":
                out = str(item.get("aggregated_output") or "")
                if len(out) > max_output_chars:
                    half = max_output_chars // 2
                    out = out[:half] + f"\n…（省略 {len(out) - max_output_chars} 字符）…\n" + out[-half:]
                step = {
                    "item_id": item_id,
                    "kind": "command",
                    "phase": phase,
                    "command": str(item.get("command") or ""),
                    "exit_code": item.get("exit_code"),
                    "output": out,
                }
            elif itype == "file_change":
                step = {
                    "item_id": item_id,
                    "kind": "file",
                    "phase": phase,
                    "changes": item.get("changes") or [],
                }
            elif itype == "agent_message":
                step = {
                    "item_id": item_id,
                    "kind": "message",
                    "phase": phase,
                    "text": str(item.get("text") or "")[:max_output_chars],
                }
            elif itype == "reasoning":
                step = {
                    "item_id": item_id,
                    "kind": "reasoning",
                    "phase": phase,
                    "text": str(item.get("text") or "")[:max_output_chars],
                }
            elif itype == "error":
                step = {"item_id": item_id, "kind": "warning", "phase": "warning", "text": str(item.get("message") or "")}
            if step is not None:
                if item_id and item_id in by_item:
                    steps[by_item[item_id]] = step
                else:
                    if item_id:
                        by_item[item_id] = len(steps)
                    steps.append(step)
    return steps


class _StreamState:
    def __init__(self) -> None:
        self.thread_id: str | None = None
        self.usage = Usage()
        self.turn_failed = False
        self.errors: list[str] = []
        self.last_agent_message = ""


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        if sig == signal.SIGTERM:
            time.sleep(1.0)
