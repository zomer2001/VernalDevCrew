"""私有化模型调用网关策略与敏感上下文脱敏（WP-7）。"""

from __future__ import annotations

import os
from pathlib import Path
import re
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from autodev.db import now_iso
from autodev.governance.audit import _AUDIT_LOCK, _connect_platform_db
from autodev.loopforge.client import use_environment_proxy

if TYPE_CHECKING:
    from autodev.engine import Engine

_SECRET_SCRUB_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{16,}"),
)


def _normalize_origin(url: str) -> str:
    parts = urlsplit((url or "").strip())
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


def enforce_model_gateway_policy(
    eng: "Engine",
    provider: str,
    upstream_url: str,
    *,
    model_name: str = "",
    platform_db_path: Path | None = None,
) -> None:
    """当项目密级为 internal 时，仅放行内网/回环或显式加入 VERNAL_INTERNAL_MODEL_ALLOWLIST 的模型端点。"""
    classification = (eng.settings.data_classification or "public").strip().lower()
    pid = eng.settings.loopforge_project_id or eng.ws.root.name

    if classification == "internal":
        allowlist = {
            _normalize_origin(u)
            for u in os.environ.get("VERNAL_INTERNAL_MODEL_ALLOWLIST", "").split(",")
            if _normalize_origin(u)
        }
        target_origin = _normalize_origin(upstream_url)
        import ipaddress
        host = urlsplit(upstream_url).hostname or ""
        try:
            address = ipaddress.ip_address(host)
            local = address.is_loopback or any(address in net for net in (
                ipaddress.ip_network("10.0.0.0/8"),ipaddress.ip_network("192.168.0.0/16"),
                ipaddress.ip_network("172.16.0.0/12"),ipaddress.ip_network("fc00::/7")))
        except ValueError:
            local = host.lower() == "localhost"
        is_external = not local and target_origin not in allowlist
        if is_external:
            if platform_db_path is not None:
                record_model_gateway_call(
                    platform_db_path,
                    project=pid,
                    provider=provider,
                    model=model_name or "-",
                    status="BLOCKED_INTERNAL_CLASSIFICATION",
                )
            raise PermissionError(
                f"当前项目 ({pid}) 数据密级为 internal，安全网关已阻断向外部公网模型端点 ({target_origin or upstream_url}) 发起调用；"
                "请配置内网私有化模型端点（如 vLLM / Ollama）或 VERNAL_INTERNAL_MODEL_ALLOWLIST"
            )


def record_model_gateway_call(
    db_path: Path,
    *,
    project: str,
    provider: str,
    model: str,
    status: str,
    duration_ms: int = 0,
) -> None:
    """记录模型网关调用元数据（严禁记录 Prompt 或补全正文）。"""
    with _AUDIT_LOCK:
        conn = _connect_platform_db(db_path)
        try:
            conn.execute(
                "INSERT INTO model_gateway_calls(ts, project, provider, model, status, duration_ms) VALUES(?, ?, ?, ?, ?, ?)",
                (now_iso(), project, provider, model, status, int(duration_ms)),
            )
            conn.commit()
        finally:
            conn.close()


def scrub_sensitive_text(text: str) -> str:
    """对外发上下文执行轻量密钥掩码脱敏。"""
    out = text
    for pat in _SECRET_SCRUB_PATTERNS:
        out = pat.sub("[REDACTED_SECRET]", out)
    return out


def metered_response(response, db, reservation):
    """Keep the reservation through streaming. Charge the cap when the provider omits usage."""
    import json
    source = response.body_iterator
    async def stream():
        tail = b""
        actual = None
        def usage(data):
            try:
                message = json.loads(data)
            except (ValueError, UnicodeError):
                return None
            value = (message.get("usage") or {}).get("total_tokens") if isinstance(message,dict) else None
            return value if type(value) is int and value > 0 else None
        try:
            async for chunk in source:
                tail += chunk
                while b"\n" in tail:
                    line, tail = tail.split(b"\n",1)
                    value = usage(line.removeprefix(b"data:").strip())
                    if value is not None:
                        actual = value
                tail = tail[-1048576:]
                yield chunk
            actual = usage(tail.removeprefix(b"data:").strip()) or actual
        finally:
            db._exec("UPDATE model_budget_reservations SET finalized=1,tokens=COALESCE(?,tokens) WHERE request_id=?",
                     (actual,reservation))
    response.body_iterator = stream()
