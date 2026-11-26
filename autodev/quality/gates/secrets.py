"""敏感凭据与硬编码密钥扫描门禁（WP-4：优先 gitleaks，缺失时回退确定性正则扫描）。"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from autodev.quality.model import Finding, QualityResult

_SECRET_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "检测到疑似 AWS Access Key ID"),
    ("github-pat", re.compile(r"\b(?:ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b"), "检测到疑似 GitHub Personal Access Token"),
    ("api-sk-token", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), "检测到疑似硬编码大模型 / 平台 API Secret Key (sk-...)"),
    ("private-key-pem", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"), "检测到硬编码私钥 PEM 块"),
    (
        "hardcoded-credential",
        re.compile(r"""(?i)\b(?:api[_-]?key|secret[_-]?key|access[_-]?token|db[_-]?password)\s*[:=]\s*["'][A-Za-z0-9/+_=.-]{16,}["']"""),
        "检测到疑似硬编码密钥或口令字面量",
    ),
]

_IGNORE_PARTS = {".git", ".venv", "node_modules", "__pycache__", ".autodev"}


def _iter_scan_files(cwd: Path, changed_files: list[str]) -> list[str]:
    if changed_files:
        return [f for f in changed_files if (cwd / f).is_file()]
    out: list[str] = []
    for p in cwd.rglob("*"):
        if not p.is_file() or any(part in _IGNORE_PARTS for part in p.parts):
            continue
        if p.stat().st_size > 512_000:
            continue
        out.append(str(p.relative_to(cwd)).replace("\\", "/"))
    return out


async def run(
    cwd: Path,
    changed_files: list[str],
    base_ref: str = "",
    cfg: dict[str, Any] | None = None,
) -> QualityResult:
    t0 = time.monotonic()
    gitleaks_bin = shutil.which("gitleaks")
    if gitleaks_bin and not changed_files:
        with NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            rep_path = Path(tmp.name)
        try:
            proc = await asyncio.create_subprocess_exec(
                gitleaks_bin,
                "detect",
                "--no-git",
                "--source",
                str(cwd),
                "--report-format",
                "json",
                "--report-path",
                str(rep_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=60)
            findings: list[Finding] = []
            if rep_path.exists() and rep_path.stat().st_size > 0:
                raw = json.loads(rep_path.read_text("utf-8", errors="replace"))
                if isinstance(raw, list):
                    for item in raw:
                        rel_f = str(item.get("File") or "")
                        if any(part in _IGNORE_PARTS for part in Path(rel_f).parts):
                            continue
                        findings.append(
                            Finding(
                                tool="gitleaks",
                                rule=str(item.get("RuleID") or "secret"),
                                severity="blocker",
                                file=rel_f,
                                line=int(item.get("StartLine") or 1),
                                message=str(item.get("Description") or "发现疑似硬编码凭据"),
                            )
                        )
            return QualityResult(
                gate="secrets",
                status="FAILED" if findings else "PASSED",
                metrics={"findings": float(len(findings)), "engine": 1.0},
                findings=findings,
                duration_sec=round(time.monotonic() - t0, 3),
            )
        finally:
            rep_path.unlink(missing_ok=True)

    # 正则确定性扫描（离线无二进制环境或按变更文件增量扫描）
    targets = _iter_scan_files(cwd, changed_files)
    findings = []
    for rel in targets:
        if any(part in _IGNORE_PARTS for part in Path(rel).parts):
            continue
        text = (cwd / rel).read_text("utf-8", errors="replace")
        for line_no, line in enumerate(text.splitlines(), 1):
            if "pragma: allowlist secret" in line or "gitleaks:allow" in line:
                continue
            for rule_id, pattern, desc in _SECRET_PATTERNS:
                if pattern.search(line):
                    findings.append(
                        Finding(
                            tool="regex-secrets",
                            rule=rule_id,
                            severity="blocker",
                            file=rel,
                            line=line_no,
                            message=desc,
                        )
                    )
                    break

    return QualityResult(
        gate="secrets",
        status="FAILED" if findings else "PASSED",
        metrics={"scanned_files": float(len(targets)), "findings": float(len(findings))},
        findings=findings,
        duration_sec=round(time.monotonic() - t0, 3),
    )
