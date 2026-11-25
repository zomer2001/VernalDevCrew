"""CI Provider 协议定义（WP-3）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

CIStateLiteral = Literal["QUEUED", "RUNNING", "SUCCESS", "FAILURE", "CANCELLED", "TIMEOUT"]


@dataclass
class CIHandle:
    provider: str
    project: str
    ref: str
    commit_sha: str
    external_id: str = ""
    url: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class CIStatus:
    status: CIStateLiteral
    url: str = ""
    summary: dict[str, Any] = field(default_factory=dict)


class CIProvider(Protocol):
    name: str

    async def trigger(self, project: str, ref: str, sha: str, ctx: dict[str, Any]) -> CIHandle: ...

    async def poll(self, handle: CIHandle) -> CIStatus: ...

    async def fetch_log_tail(self, handle: CIHandle, lines: int = 150) -> str: ...
