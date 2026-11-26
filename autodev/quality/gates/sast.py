"""静态安全分析门禁（WP-4：基于自维护离线规则集运行 semgrep --sarif；缺失时显式标记 SKIPPED）。"""

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path
from typing import Any

from autodev.quality.model import QualityResult
from autodev.quality.sarif import parse_sarif

RULES_PATH = Path(__file__).resolve().parents[2] / "assets" / "semgrep" / "vernal-security.yaml"


async def run(
    cwd: Path,
    changed_files: list[str],
    base_ref: str = "",
    cfg: dict[str, Any] | None = None,
) -> QualityResult:
    t0 = time.monotonic()
    semgrep_bin = shutil.which("semgrep")
    if not semgrep_bin:
        return QualityResult(
            gate="sast",
            status="SKIPPED",
            skipped_reason="工具缺失：未安装 semgrep SAST 静态安全扫描引擎",
            duration_sec=round(time.monotonic() - t0, 3),
        )
    if not RULES_PATH.is_file():
        return QualityResult(
            gate="sast",
            status="SKIPPED",
            skipped_reason=f"规则文件缺失：{RULES_PATH}",
            duration_sec=round(time.monotonic() - t0, 3),
        )

    targets = [f for f in changed_files if (cwd / f).is_file()] if changed_files else ["."]
    if changed_files and not targets:
        return QualityResult(gate="sast", status="PASSED", duration_sec=round(time.monotonic() - t0, 3))

    proc = await asyncio.create_subprocess_exec(
        semgrep_bin,
        "scan",
        "--config",
        str(RULES_PATH),
        "--sarif",
        "--metrics=off",
        "--quiet",
        *targets,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
    findings = parse_sarif(out_b.decode("utf-8", errors="replace"), default_tool="semgrep")
    blocking = [f for f in findings if f.severity in ("blocker", "high", "medium")]
    return QualityResult(
        gate="sast",
        status="FAILED" if blocking else "PASSED",
        metrics={"findings": float(len(findings)), "blocking_findings": float(len(blocking))},
        findings=findings,
        duration_sec=round(time.monotonic() - t0, 3),
    )
