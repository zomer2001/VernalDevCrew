"""OpenCode CLI / `opencode serve` 适配器与原生 `.opencode/agents/*.md` 角色文件生成。"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
import urllib.request
from pathlib import Path
from typing import Any

from autodev.config import Settings, resolve_opencode_bin
from autodev.harnesses.base import ActivityCallback, AgentResult, Harness, Usage, extract_json

_STREAM_LIMIT = 32 * 1024 * 1024

_OPENCODE_PROFILES: dict[str, tuple[str, bool, str]] = {
    "pm": ("产品经理：只读补全产品规格、异常流与验收红线", False, "你是产品经理（PM），只读分析输入文档并输出结构化规格。"),
    "director": ("研发总监：只读负责研发组划分、里程碑滚动排期与异常仲裁", False, "你是研发总监（Tech Director），只读分析仓库与契约并做架构决策。"),
    "planner": ("架构规划师：只读生成研发计划", False, "你是软件架构师，只读分析资料并输出 JSON 计划。"),
    "bootstrap": ("基础设施组基线工程师：搭建工程骨架与依赖环境", True, "你是基础设施组基线工程师，负责搭建项目骨架并跑通基础检查。"),
    "contract": ("契约架构组工程师：编写并维护公共接口契约", True, "你是契约工程师，负责编写清晰稳定的接口契约与契约测试。"),
    "coder": ("业务组开发工程师：在所属模块领地内 TDD 编码", True, "你是业务研发组工程师，严格在授权目录内按 TDD 实现功能。"),
    "integrator": ("网关装配工程师：跨模块挂载路由、DI 与配置", True, "你是集成装配工程师，负责将各研发组模块挂载连通并通过集成测试。"),
    "tester": ("质量保障组测试工程师：编写独立契约、集成与端到端测试", True, "你是 QA 测试工程师，基于规格书与契约编写严密的自动化测试。"),
    "reviewer": ("独立代码审查员：只读核对验收标准与代码缺陷", False, "你是独立只读代码审查员，逐条核对验收标准并指出阻塞缺陷。"),
}


def write_opencode_agent_profiles(repo_root: Path) -> list[str]:
    """在目标仓库生成原生 `.opencode/agents/*.md`，直接复用 OpenCode 的角色与工具权限机制。"""
    agents_dir = repo_root / ".opencode" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for name, (desc, writable, body) in _OPENCODE_PROFILES.items():
        tools_block = (
            "tools:\n  write: true\n  edit: true\n  bash: true\n"
            if writable
            else "tools:\n  write: false\n  edit: false\n  patch: false\n  bash: true\n"
        )
        content = f"---\ndescription: {desc}\nmode: primary\n{tools_block}---\n\n{body}\n"
        target = agents_dir / f"{name}.md"
        target.write_text(content, "utf-8")
        written.append(f".opencode/agents/{name}.md")
    return written


class OpenCodeHarness(Harness):
    """支持直接 `opencode run --format json --agent <role>` 及挂载 `opencode serve`（--attach + /event SSE）。"""

    name = "opencode"

    def __init__(self, settings: Settings, opencode_bin: str, activity_cb: ActivityCallback | None = None):
        super().__init__(activity_cb)
        self.settings = settings
        self.opencode_bin = opencode_bin

    def _model_args(self, role: str) -> list[str]:
        model = self.settings.model_for(role)
        return ["-m", model] if model else []

    def _server_args(self) -> list[str]:
        url = self.settings.opencode_server_url.strip()
        return ["--server", url] if url else []

    def build_cmd(self, *, role: str, session_id: str | None = None, cwd: Path | None = None) -> list[str]:
        cmd = [
            self.opencode_bin,
            "run",
            "--format",
            "json",
            "--auto",
            "--thinking",
            *self._server_args(),
            *self._model_args(role),
        ]
        if cwd and (cwd / ".opencode" / "agents" / f"{role}.md").exists():
            cmd += ["--agent", role]
        if session_id:
            cmd += ["--session", session_id]
        return cmd

    async def run_structured_readonly(
        self, prompt: str, *, schema_path: Path, cwd: Path, log_dir: Path, role: str
    ) -> AgentResult:
        schema_text = schema_path.read_text("utf-8")
        full_prompt = (
            f"{prompt}\n\n## 严格输出格式（JSON Schema）\n\n"
            f"请只输出满足以下 JSON Schema 的单个 JSON 对象，不要附加额外解释：\n```json\n{schema_text}\n```\n"
        )
        return await self._run(
            self.build_cmd(role=role, cwd=cwd), full_prompt, cwd=cwd, log_dir=log_dir, want_json=True
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
        full_prompt = prompt
        if schema_path is not None:
            full_prompt += "\n\n## 最终汇报 JSON Schema\n```json\n" + schema_path.read_text("utf-8") + "\n```\n"
        return await self._run(
            self.build_cmd(role=role, cwd=cwd),
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
        return await self._run(
            self.build_cmd(role=role, session_id=session_id, cwd=cwd),
            prompt,
            cwd=cwd,
            log_dir=log_dir,
            want_json=schema_path is not None,
            extra_env=extra_env,
        )

    async def health_check(self) -> tuple[bool, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                self.opencode_bin, "--version", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
            out, _ = await asyncio.wait_for(proc.communicate(), 20)
        except (OSError, TimeoutError) as exc:
            return False, f"无法执行 {self.opencode_bin}: {exc}"
        return proc.returncode == 0, out.decode(errors="replace").strip()

    async def _watch_server_sse(self, url: str, stop: asyncio.Event) -> None:
        """若连接了 `opencode serve`，旁路监听 `/event` SSE 以捕获子智能体（Subagent）实时事件。"""
        sse_url = url.rstrip("/") + "/event"

        def _blocking_tail() -> None:
            try:
                req = urllib.request.Request(sse_url, headers={"Accept": "text/event-stream"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    for raw_bytes in resp:
                        if stop.is_set():
                            break
                        line = raw_bytes.decode("utf-8", errors="replace").strip()
                        if not line.startswith("data:"):
                            continue
                        try:
                            ev = json.loads(line[5:].strip())
                        except json.JSONDecodeError:
                            continue
                        props = ev.get("properties") or {}
                        part = props.get("part") or {}
                        if part.get("type") == "tool" and part.get("tool") == "task":
                            sub = (part.get("state") or {}).get("input") or {}
                            sub_agent = sub.get("subagent_type") or "subagent"
                            desc = sub.get("description") or ""
                            self._activity(
                                "command",
                                f"[OpenCode 子智能体 @{sub_agent}] {desc}",
                                {"phase": "started", "command": f"@{sub_agent}: {desc}"},
                            )
            except Exception:
                return

        await asyncio.to_thread(_blocking_tail)

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
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=True,
                limit=_STREAM_LIMIT,
            )
        except OSError as exc:
            return AgentResult(False, -1, None, "", error=f"启动 opencode 失败: {exc}", log_dir=log_dir)

        session_id: str | None = None
        usage = Usage()
        texts: list[str] = []
        stderr_chunks: list[bytes] = []
        events_file = (log_dir / "events.jsonl").open("w", encoding="utf-8")
        sse_stop = asyncio.Event()
        sse_task = (
            asyncio.create_task(self._watch_server_sse(self.settings.opencode_server_url, sse_stop))
            if self.settings.opencode_server_url.strip()
            else None
        )

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
            nonlocal session_id
            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                raw = line.decode("utf-8", errors="replace").rstrip("\n")
                events_file.write(raw + "\n")
                events_file.flush()
                try:
                    ev: dict[str, Any] = json.loads(raw)
                except json.JSONDecodeError:
                    texts.append(raw)
                    continue
                if isinstance(ev, dict):
                    sid = ev.get("sessionID") or ev.get("session_id")
                    if sid and not session_id:
                        session_id = str(sid)
                        self._activity("turn", f"OpenCode 会话已建立 ({session_id[:12]})", {"phase": "thread", "thread_id": session_id})
                    self._handle_opencode_event(ev, texts, usage)

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
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(proc.pid, sig)
                except (ProcessLookupError, PermissionError):
                    break
                if sig == signal.SIGTERM:
                    await asyncio.sleep(1.0)
            try:
                await asyncio.wait_for(proc.wait(), 10)
            except TimeoutError:
                pass
        finally:
            sse_stop.set()
            if sse_task is not None:
                sse_task.cancel()
            events_file.close()

        stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace")
        (log_dir / "stderr.log").write_text(stderr_text, "utf-8")
        final = "\n".join(texts).strip()
        (log_dir / "last_message.txt").write_text(final, "utf-8")
        exit_code = proc.returncode if proc.returncode is not None else -9
        structured = extract_json(final) if want_json else None
        ok = exit_code == 0 and not timed_out and (not want_json or structured is not None)
        err = ""
        if timed_out:
            err = f"超时（>{self.settings.agent_timeout_sec}s）被终止"
        elif exit_code != 0:
            err = f"opencode 退出码 {exit_code}: {(stderr_text or final)[-400:]}"
        elif want_json and structured is None:
            err = "最终输出不是合法 JSON"
        return AgentResult(
            ok=ok,
            exit_code=exit_code,
            session_id=session_id,
            final_message=final,
            structured=structured,
            usage=usage,
            error=err,
            log_dir=log_dir,
            duration_sec=time.monotonic() - started,
            timed_out=timed_out,
        )

    def _handle_opencode_event(self, ev: dict[str, Any], texts: list[str], usage: Usage) -> None:
        etype = ev.get("type", "")
        part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
        item_id = str(part.get("id") or ev.get("id") or "")
        if etype == "step_start":
            self._activity("turn", "OpenCode 步骤开始", {"phase": "started"})
        elif etype == "text" and part.get("text"):
            txt = str(part["text"])
            texts.append(txt)
            self._activity("message", txt[:220].replace("\n", " "), {"item_id": item_id, "phase": "completed", "text": txt[:12_000]})
        elif etype == "tool_use":
            tool = str(part.get("tool") or part.get("name") or "tool")
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            status = str(state.get("status") or "completed")
            inp = state.get("input") or {}
            out = str(state.get("output") or "")[:12_000]
            if tool == "task":
                sub = inp.get("subagent_type") or "subagent"
                desc = inp.get("description") or json.dumps(inp, ensure_ascii=False)[:140]
                label = f"子智能体 @{sub}: {desc}"
            elif tool == "bash":
                label = str(inp.get("command") or "")
            elif tool in ("edit", "write", "patch"):
                path = str(inp.get("filePath") or inp.get("path") or "")
                self._activity(
                    "file",
                    f"{tool}:{path}",
                    {"item_id": item_id, "phase": "completed", "changes": [{"kind": tool, "path": path}]},
                )
                return
            else:
                label = f"{tool}({json.dumps(inp, ensure_ascii=False)[:140]})"
            phase = "started" if status in ("pending", "running") else "completed"
            self._activity(
                "command",
                f"[OpenCode·{tool}] {label[:160]}",
                {
                    "item_id": item_id,
                    "phase": phase,
                    "command": f"[{tool}] {label}",
                    "exit_code": 0 if status == "completed" else None,
                    "output": out,
                },
            )
        elif etype == "step_finish":
            toks = part.get("tokens") or {}
            cache = toks.get("cache") or {}
            in_t = int(toks.get("input") or 0)
            ca_t = int(cache.get("read") or 0)
            ou_t = int(toks.get("output") or 0)
            re_t = int(toks.get("reasoning") or 0)
            usage.add(Usage(in_t, ca_t, ou_t, re_t))
            self._activity(
                "turn",
                f"步骤完成（in {in_t:,} / cache {ca_t:,} / out {ou_t:,}）",
                {"phase": "completed", "usage": {"input": in_t, "cached": ca_t, "output": ou_t, "reasoning": re_t}},
            )


__all__ = ["OpenCodeHarness", "resolve_opencode_bin", "write_opencode_agent_profiles"]
