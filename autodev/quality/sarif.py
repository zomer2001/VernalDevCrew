"""SARIF 2.1.0 静态扫描结果解析转换器（WP-4）。"""

from __future__ import annotations

import json
from typing import Any

from autodev.quality.model import Finding, Severity

_LEVEL_MAP: dict[str, Severity] = {
    "error": "high",
    "warning": "medium",
    "note": "low",
    "none": "info",
}


def parse_sarif(raw: str | dict[str, Any], default_tool: str = "sarif") -> list[Finding]:
    data = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(data, dict):
        return []
    findings: list[Finding] = []
    for run in data.get("runs") or []:
        if not isinstance(run, dict):
            continue
        driver = ((run.get("tool") or {}).get("driver") or {}) if isinstance(run.get("tool"), dict) else {}
        tool_name = str(driver.get("name") or default_tool)
        for res in run.get("results") or []:
            if not isinstance(res, dict):
                continue
            rule_id = str(res.get("ruleId") or "rule")
            level = str(res.get("level") or "warning").lower()
            sev: Severity = _LEVEL_MAP.get(level, "medium")
            props = res.get("properties") or {}
            if isinstance(props, dict) and str(props.get("security-severity", "")) >= "8":
                sev = "blocker"
            msg_obj = res.get("message") or {}
            message = str(msg_obj.get("text") if isinstance(msg_obj, dict) else msg_obj or "")
            file_path = ""
            line_no = 0
            locs = res.get("locations") or []
            if locs and isinstance(locs[0], dict):
                phys = locs[0].get("physicalLocation") or {}
                if isinstance(phys, dict):
                    art = phys.get("artifactLocation") or {}
                    if isinstance(art, dict):
                        file_path = str(art.get("uri") or "")
                    reg = phys.get("region") or {}
                    if isinstance(reg, dict):
                        line_no = int(reg.get("startLine") or 0)
            findings.append(
                Finding(
                    tool=tool_name,
                    rule=rule_id,
                    severity=sev,
                    file=file_path,
                    line=line_no,
                    message=message or rule_id,
                )
            )
    return findings
