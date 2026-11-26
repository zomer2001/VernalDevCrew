"""变更行覆盖率门禁（WP-4：解析 Cobertura coverage.xml 或调用 diff-cover 核验增量代码覆盖率）。"""

from __future__ import annotations

import asyncio
import re
import shutil
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from autodev.quality.model import Finding, QualityResult


def _parse_cobertura_xml(xml_path: Path) -> dict[str, dict[int, int]]:
    """返回 {relative_path: {line_no: hits}}。"""
    tree = ET.parse(xml_path)
    root = tree.getroot()
    file_hits: dict[str, dict[int, int]] = {}
    for cls_el in root.findall(".//class"):
        fname = (cls_el.attrib.get("filename") or "").replace("\\", "/").lstrip("./")
        if not fname:
            continue
        lines_map = file_hits.setdefault(fname, {})
        for ln_el in cls_el.findall("./lines/line"):
            try:
                num = int(ln_el.attrib["number"])
                hits = int(ln_el.attrib.get("hits", "0"))
            except (KeyError, ValueError):
                continue
            lines_map[num] = max(lines_map.get(num, 0), hits)
    return file_hits


async def _git_added_lines(cwd: Path, base_ref: str, changed_files: list[str]) -> dict[str, set[int]]:
    if not base_ref or not (cwd / ".git").exists() or not shutil.which("git"):
        return {}
    proc = await asyncio.create_subprocess_exec(
        "git",
        "diff",
        "-U0",
        base_ref,
        "--",
        *changed_files,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
    if proc.returncode != 0:
        return {}
    added: dict[str, set[int]] = {}
    cur_file = ""
    hunk_re = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
    for raw in out_b.decode("utf-8", errors="replace").splitlines():
        if raw.startswith("+++ b/"):
            cur_file = raw[6:].strip()
        elif raw.startswith("@@ ") and cur_file:
            m = hunk_re.match(raw)
            if m:
                start = int(m.group(1))
                count = int(m.group(2)) if m.group(2) is not None else 1
                if count > 0:
                    added.setdefault(cur_file, set()).update(range(start, start + count))
    return added


async def run(
    cwd: Path,
    changed_files: list[str],
    base_ref: str = "",
    cfg: dict[str, Any] | None = None,
) -> QualityResult:
    t0 = time.monotonic()
    cfg = cfg or {}
    threshold = float(cfg.get("changed_line_coverage") or 0.0)
    cov_xml = cwd / "coverage.xml"

    if not cov_xml.is_file():
        if threshold <= 0:
            return QualityResult(
                gate="diff_coverage",
                status="SKIPPED",
                skipped_reason="未开启增量覆盖率阈值且未生成 coverage.xml",
                duration_sec=round(time.monotonic() - t0, 3),
            )
        return QualityResult(
            gate="diff_coverage",
            status="SKIPPED",
            skipped_reason="工具或产物缺失：未找到 coverage.xml（请在测试命令中启用 --cov-report=xml）",
            duration_sec=round(time.monotonic() - t0, 3),
        )

    try:
        file_hits = _parse_cobertura_xml(cov_xml)
    except Exception as exc:
        return QualityResult(
            gate="diff_coverage",
            status="SKIPPED",
            skipped_reason=f"解析 coverage.xml 失败：{exc}",
            duration_sec=round(time.monotonic() - t0, 3),
        )

    src_candidates = [
        f for f in (changed_files or list(file_hits.keys()))
        if not Path(f).name.startswith("test_") and not f.endswith("_test.py") and "tests/" not in f
    ]
    added_map = await _git_added_lines(cwd, base_ref, src_candidates) if src_candidates else {}

    total_measurable = 0
    covered_count = 0
    uncovered_findings: list[Finding] = []

    for rel in src_candidates:
        hits_by_line = file_hits.get(rel)
        if not hits_by_line:
            # 尝试后缀匹配
            matched_key = next((k for k in file_hits if k.endswith(rel) or rel.endswith(k)), "")
            hits_by_line = file_hits.get(matched_key)
        if not hits_by_line:
            continue
        target_lines = added_map.get(rel) or set(hits_by_line.keys())
        for ln in sorted(target_lines):
            if ln not in hits_by_line:
                continue
            total_measurable += 1
            if hits_by_line[ln] > 0:
                covered_count += 1
            else:
                uncovered_findings.append(
                    Finding(
                        tool="diff_coverage",
                        rule="uncovered-changed-line",
                        severity="medium",
                        file=rel,
                        line=ln,
                        message=f"新增或变更代码行第 {ln} 行未被自动化测试覆盖",
                    )
                )

    pct = 100.0 if total_measurable == 0 else round((covered_count / total_measurable) * 100.0, 2)
    failed = threshold > 0 and total_measurable > 0 and pct < threshold
    return QualityResult(
        gate="diff_coverage",
        status="FAILED" if failed else "PASSED",
        metrics={
            "changed_line_coverage": pct,
            "threshold": threshold,
            "covered_lines": float(covered_count),
            "measurable_lines": float(total_measurable),
        },
        findings=uncovered_findings[:50] if failed else [],
        duration_sec=round(time.monotonic() - t0, 3),
    )
