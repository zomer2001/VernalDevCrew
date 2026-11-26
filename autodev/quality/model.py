"""代码质量门禁统一结果模型（WP-4：优先 Pydantic，无第三方依赖沙箱内置 dataclass 等价实现）。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field as dc_field
from typing import Any, Literal

Severity = Literal["blocker", "high", "medium", "low", "info"]
GateStatus = Literal["PASSED", "FAILED", "SKIPPED"]

try:
    from pydantic import BaseModel, Field

    class Finding(BaseModel):
        tool: str
        rule: str
        severity: Severity = "medium"
        file: str
        line: int = 0
        message: str

    class QualityResult(BaseModel):
        gate: str
        status: GateStatus
        metrics: dict[str, float] = Field(default_factory=dict)
        findings: list[Finding] = Field(default_factory=list)
        skipped_reason: str = ""
        duration_sec: float = 0.0

except ImportError:

    @dataclass
    class Finding:  # type: ignore[no-redef]
        tool: str
        rule: str
        file: str
        message: str
        severity: Severity = "medium"
        line: int = 0

        def model_dump(self) -> dict[str, Any]:
            return asdict(self)

    @dataclass
    class QualityResult:  # type: ignore[no-redef]
        gate: str
        status: GateStatus
        metrics: dict[str, float] = dc_field(default_factory=dict)
        findings: list[Finding] = dc_field(default_factory=list)
        skipped_reason: str = ""
        duration_sec: float = 0.0

        def model_dump(self) -> dict[str, Any]:
            return {
                "gate": self.gate,
                "status": self.status,
                "metrics": dict(self.metrics),
                "findings": [f.model_dump() for f in self.findings],
                "skipped_reason": self.skipped_reason,
                "duration_sec": self.duration_sec,
            }
