"""确定性门禁：路径白名单 / 敏感文件拦截 + 独立执行验证命令。"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path

from autodev import gitops
from autodev.dag import path_allowed

MAX_FILE_BYTES = 5 * 1024 * 1024
TAIL_LINES = 150

_SECRET_PATTERNS = [
    re.compile(r"(^|/)\.env(\.(?!example$|sample$|template$)[^/]+)?$"),
    re.compile(r"\.(pem|key|p12|pfx|keystore)$"),
    re.compile(r"(^|/)id_(rsa|dsa|ecdsa|ed25519)$"),
]


@dataclass
class GuardResult:
    changed: list[str] = field(default_factory=list)
    out_of_scope: list[str] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)
    oversized: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.out_of_scope or self.secrets or self.oversized)

    def describe(self) -> str:
        lines = []
        if self.out_of_scope:
            lines.append("以下文件超出允许修改的路径白名单，已被自动回滚：\n" + "\n".join(f"- {p}" for p in self.out_of_scope))
        if self.secrets:
            lines.append("以下疑似密钥/敏感文件不允许提交，已被删除：\n" + "\n".join(f"- {p}" for p in self.secrets))
        if self.oversized:
            lines.append(f"以下文件超过 {MAX_FILE_BYTES // 1024 // 1024}MB，已被删除：\n" + "\n".join(f"- {p}" for p in self.oversized))
        return "\n\n".join(lines)


def guard_worktree(cwd: Path, allowed_paths: list[str], protected_paths: list[str]) -> GuardResult:
    """检查工作区全部改动；越界、敏感、超大文件自动回滚。protected_paths 为该任务额外禁止的目录。"""
    result = GuardResult(changed=gitops.changed_files(cwd))
    for p in result.changed:
        if any(rx.search(p) for rx in _SECRET_PATTERNS):
            result.secrets.append(p)
        elif not path_allowed(p, allowed_paths) or path_allowed(p, protected_paths):
            result.out_of_scope.append(p)
        else:
            f = cwd / p
            if f.is_file() and f.stat().st_size > MAX_FILE_BYTES:
                result.oversized.append(p)
    offenders = result.out_of_scope + result.secrets + result.oversized
    if offenders:
        gitops.revert_paths(cwd, offenders)
    return result


@dataclass
class CmdResult:
    cmd: str
    exit_code: int
    output: str
    duration_sec: float
    timed_out: bool = False


def tail(text: str, n: int = TAIL_LINES) -> str:
    lines = text.splitlines()
    if len(lines) <= n:
        return text
    return f"…（省略前 {len(lines) - n} 行）\n" + "\n".join(lines[-n:])


async def run_command(cmd: str, cwd: Path, timeout: int, env: dict[str, str] | None = None) -> CmdResult:
    full_env = os.environ.copy()
    full_env.update(env or {})
    full_env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    started = time.monotonic()
    proc = await asyncio.create_subprocess_shell(
        cmd,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        stdin=asyncio.subprocess.DEVNULL,
        env=full_env,
        start_new_session=True,
    )
    timed_out = False
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        out, _ = await proc.communicate()
    text = out.decode("utf-8", errors="replace")
    if timed_out:
        text += f"\n[超时 {timeout}s 被终止]"
    code = proc.returncode if not timed_out else 124
    return CmdResult(cmd, code if code is not None else -1, tail(text), time.monotonic() - started, timed_out)


async def run_commands(
    cmds: list[str], cwd: Path, timeout: int, env: dict[str, str] | None = None, log_path: Path | None = None
) -> list[CmdResult]:
    """按顺序执行，遇到第一个失败即停止。"""
    results: list[CmdResult] = []
    for cmd in cmds:
        r = await run_command(cmd, cwd, timeout, env)
        results.append(r)
        if r.exit_code != 0:
            break
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            "\n\n".join(f"$ {r.cmd}\n[exit {r.exit_code}, {r.duration_sec:.1f}s]\n{r.output}" for r in results), "utf-8"
        )
    return results
