"""质量门禁统一编排器与结果持久化（WP-4）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from autodev.quality.gates import diff_coverage, lint, sast, secrets, test_integrity
from autodev.quality.model import Finding, QualityResult

if TYPE_CHECKING:
    from autodev.db import Database


async def run_quality(
    stage: Literal["task", "milestone"],
    *,
    cwd: Path,
    changed_files: list[str] | None = None,
    base_ref: str = "",
    cfg: dict[str, Any] | None = None,
) -> list[QualityResult]:
    files = list(changed_files or [])
    quality_cfg = dict(cfg or {})
    plugins = [test_integrity, secrets, lint, diff_coverage]
    if stage == "milestone":
        plugins.append(sast)
    results: list[QualityResult] = []
    for plugin in plugins:
        res = await plugin.run(cwd, files, base_ref=base_ref, cfg=quality_cfg)
        results.append(res)
    return results


def save_quality_results(
    db: "Database",
    results: list[QualityResult],
    *,
    task_id: str | None = None,
    milestone_id: str | None = None,
    attempt: int = 1,
) -> None:
    from autodev.db import now_iso

    ts = now_iso()
    with db.transaction():
        for r in results:
            db._exec(
                "INSERT INTO quality_results(task_id, milestone_id, attempt, gate, status, skipped_reason, duration_sec, metrics_json, findings_json, created_at)"
                " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    milestone_id,
                    attempt,
                    r.gate,
                    r.status,
                    r.skipped_reason,
                    r.duration_sec,
                    json.dumps(r.metrics, ensure_ascii=False),
                    json.dumps([f.model_dump() for f in r.findings], ensure_ascii=False),
                    ts,
                ),
            )


def list_quality_results(
    db: "Database",
    *,
    task_id: str | None = None,
    milestone_id: str | None = None,
    latest_only: bool = False,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if task_id:
        clauses.append("task_id=?")
        params.append(task_id)
    if milestone_id:
        clauses.append("milestone_id=?")
        params.append(milestone_id)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = db._query(f"SELECT * FROM quality_results{where} ORDER BY id ASC", params)
    items: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        d["metrics"] = json.loads(d.pop("metrics_json", None) or "{}")
        d["findings"] = json.loads(d.pop("findings_json", None) or "[]")
        items.append(d)
    if not latest_only or not items:
        return items
    max_attempt = max(int(i.get("attempt") or 1) for i in items)
    return [i for i in items if int(i.get("attempt") or 1) == max_attempt]


def format_quality_feedback(results: list[QualityResult], max_findings: int = 30) -> str:
    failed = [r for r in results if r.status == "FAILED"]
    if not failed:
        return ""
    lines = ["代码质量门禁未通过："]
    shown = 0
    for r in failed:
        metric_note = ""
        if "changed_line_coverage" in r.metrics:
            metric_note = f"（变更行覆盖率 {r.metrics['changed_line_coverage']}% < 阈值 {r.metrics.get('threshold', 0)}%）"
        lines.append(f"- [{r.gate}] FAILED{metric_note}")
        for f in r.findings:
            if shown >= max_findings:
                break
            loc = f"{f.file}:{f.line}" if f.line else f.file
            lines.append(f"  * {loc} [{f.rule}] ({f.severity}) {f.message}")
            shown += 1
    return "\n".join(lines)
