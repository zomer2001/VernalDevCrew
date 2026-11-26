"""增量静态代码规范检查门禁（WP-4：仅检查变更文件；工具缺失时显式标记 SKIPPED）。"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path
from typing import Any

from autodev.quality.model import Finding, QualityResult


def _has_eslint_config(cwd: Path) -> bool:
    for name in (
        "eslint.config.js",
        "eslint.config.mjs",
        "eslint.config.cjs",
        ".eslintrc",
        ".eslintrc.js",
        ".eslintrc.cjs",
        ".eslintrc.json",
        ".eslintrc.yml",
        ".eslintrc.yaml",
    ):
        if (cwd / name).exists():
            return True
    return False


async def run(
    cwd: Path,
    changed_files: list[str],
    base_ref: str = "",
    cfg: dict[str, Any] | None = None,
) -> QualityResult:
    t0 = time.monotonic()
    py_files = [f for f in changed_files if f.endswith(".py") and (cwd / f).is_file()]
    js_files = [f for f in changed_files if f.endswith((".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx")) and (cwd / f).is_file()]

    if not py_files and not js_files:
        return QualityResult(
            gate="lint",
            status="PASSED",
            metrics={"checked_files": 0.0},
            duration_sec=round(time.monotonic() - t0, 3),
        )

    findings: list[Finding] = []
    skipped_reasons: list[str] = []
    ran_any = False

    if py_files:
        ruff_bin = shutil.which("ruff")
        if not ruff_bin:
            skipped_reasons.append("工具缺失：未安装 ruff（Python 增量 lint 已跳过）")
        else:
            ran_any = True
            proc = await asyncio.create_subprocess_exec(
                ruff_bin,
                "check",
                "--output-format",
                "json",
                "--no-cache",
                *py_files,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=45)
            try:
                items = json.loads(out_b.decode("utf-8", errors="replace") or "[]")
            except json.JSONDecodeError:
                items = []
            for item in items if isinstance(items, list) else []:
                fn = str(item.get("filename") or "")
                try:
                    rel = str(Path(fn).resolve().relative_to(cwd.resolve())).replace("\\", "/")
                except Exception:
                    rel = fn
                loc = item.get("location") or {}
                code = str(item.get("code") or "RUFF")
                findings.append(
                    Finding(
                        tool="ruff",
                        rule=code,
                        severity="high" if code.startswith(("F", "E9")) else "medium",
                        file=rel,
                        line=int(loc.get("row") or 1),
                        message=str(item.get("message") or code),
                    )
                )

    if js_files and _has_eslint_config(cwd):
        eslint_bin = shutil.which("eslint")
        if not eslint_bin:
            skipped_reasons.append("工具缺失：未安装 eslint（JS/TS 增量 lint 已跳过）")
        else:
            ran_any = True
            proc = await asyncio.create_subprocess_exec(
                eslint_bin,
                "-f",
                "json",
                *js_files,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
            try:
                reports = json.loads(out_b.decode("utf-8", errors="replace") or "[]")
            except json.JSONDecodeError:
                reports = []
            for rep in reports if isinstance(reports, list) else []:
                fn = str(rep.get("filePath") or "")
                try:
                    rel = str(Path(fn).resolve().relative_to(cwd.resolve())).replace("\\", "/")
                except Exception:
                    rel = fn
                for m in rep.get("messages") or []:
                    sev_num = int(m.get("severity") or 1)
                    if sev_num < 2:
                        continue
                    findings.append(
                        Finding(
                            tool="eslint",
                            rule=str(m.get("ruleId") or "eslint"),
                            severity="high",
                            file=rel,
                            line=int(m.get("line") or 1),
                            message=str(m.get("message") or "ESLint error"),
                        )
                    )

    if not ran_any and skipped_reasons:
        return QualityResult(
            gate="lint",
            status="SKIPPED",
            skipped_reason="；".join(skipped_reasons),
            duration_sec=round(time.monotonic() - t0, 3),
        )

    return QualityResult(
        gate="lint",
        status="FAILED" if findings else "PASSED",
        metrics={"checked_files": float(len(py_files) + len(js_files)), "findings": float(len(findings))},
        findings=findings,
        skipped_reason="；".join(skipped_reasons),
        duration_sec=round(time.monotonic() - t0, 3),
    )
