"""安全边界、SSRF 防护、同源 CSRF 校验、0600 凭据存储与结构化 TOML 序列化。"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import tomllib
from collections.abc import Iterable
from datetime import date, datetime, time as dtime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from fastapi import Request
    from autodev.config import Settings, Workspace

_INTERNAL_SERVICE_HOSTS = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "mysql",
        "loopforge",
        "vernal",
        "redis",
        "postgres",
        "metadata.google.internal",
    }
)


def _normalize_origin(url: str) -> str:
    if not url:
        return ""
    parts = urlsplit(url.strip())
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


def _is_blocked_ip(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return bool(
        ip.is_loopback
        or ip.is_link_local
        or ip.is_private
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_external_git_url(
    git_url: str,
    *,
    allow_local_path: bool = False,
    allowed_internal_urls: Iterable[str] = (),
) -> None:
    """校验用户提交的 Git 仓库地址，拦截云元数据与未授权内网 SSRF。"""
    raw = (git_url or "").strip()
    if not raw or any(c in raw for c in "\r\n\x00"):
        raise ValueError("请输入有效的仓库地址")

    if Path(raw).is_absolute():
        if not allow_local_path:
            raise ValueError("仅本机回环调用可使用本地绝对路径")
        resolved = Path(raw).resolve()
        blocked_prefixes = (Path("/etc"), Path("/proc"), Path("/sys"), Path("/var/run"), Path("/root"))
        if any(resolved == p or resolved.is_relative_to(p) for p in blocked_prefixes):
            raise ValueError("禁止访问受保护的系统目录")
        return

    ssh_scp = re.fullmatch(r"([A-Za-z0-9._-]+)@([A-Za-z0-9.-]+):([A-Za-z0-9._/-]+)", raw)
    if ssh_scp:
        ssh_host = ssh_scp.group(2).lower()
        if ssh_host in _INTERNAL_SERVICE_HOSTS or _is_blocked_ip(ssh_host):
            raise ValueError(f"不允许连接内网或受保护主机：{ssh_host}")
        return

    parts = urlsplit(raw)
    if (
        parts.scheme not in ("https", "http", "ssh")
        or not parts.hostname
        or parts.password
        or (parts.scheme in ("https", "http") and parts.username)
        or parts.fragment
        or parts.query
    ):
        raise ValueError("请输入有效的无内嵌凭据仓库地址，访问令牌请填到单独凭据栏")

    allow_origins = {
        _normalize_origin(u)
        for u in (
            *allowed_internal_urls,
            os.environ.get("VERNAL_GITEA_URL", ""),
            os.environ.get("VERNAL_GITEA_HOST_URL", ""),
            *(os.environ.get("VERNAL_GIT_ALLOWLIST", "").split(",")),
        )
        if u and _normalize_origin(u)
    }
    cur_origin = _normalize_origin(raw)
    if cur_origin and cur_origin in allow_origins:
        return

    host = parts.hostname.lower()
    if (
        host in _INTERNAL_SERVICE_HOSTS
        or "." not in host
        or host.endswith((".internal", ".local"))
        or _is_blocked_ip(host)
    ):
        raise ValueError(f"仓库地址指向受保护的内网或本机端点（{host}），已被安全策略拦截；如需访问内网 Git 服务请配置 VERNAL_GIT_ALLOWLIST")


def verify_same_origin(request: "Request") -> None:
    """对携带 Cookie 的状态修改请求执行同源 CSRF 校验。"""
    from fastapi import HTTPException

    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return
    if not request.headers.get("cookie", "").strip():
        return
    sec_site = (request.headers.get("sec-fetch-site") or "").strip().lower()
    if sec_site == "cross-site":
        raise HTTPException(status_code=403, detail="跨站请求已被安全策略拦截（Sec-Fetch-Site）")

    expected_host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip().lower()
    if not expected_host:
        return

    origin = (request.headers.get("origin") or "").strip()
    if origin:
        if origin == "null":
            raise HTTPException(status_code=403, detail="跨站空来源请求已被拦截")
        op = urlsplit(origin)
        if op.scheme not in ("http", "https") or op.netloc.lower() != expected_host:
            raise HTTPException(status_code=403, detail="请求来源（Origin）与当前站点不匹配")
        return

    referer = (request.headers.get("referer") or "").strip()
    if referer:
        rp = urlsplit(referer)
        if rp.scheme not in ("http", "https") or rp.netloc.lower() != expected_host:
            raise HTTPException(status_code=403, detail="请求来源（Referer）与当前站点不匹配")


def credentials_path(ws: "Workspace") -> Path:
    return ws.state_dir / "credentials.json"


def load_workspace_credentials(state_dir: Path) -> dict[str, str]:
    path = state_dir / "credentials.json"
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text("utf-8"))
        if isinstance(raw, dict):
            return {str(k): str(v) for k, v in raw.items() if isinstance(v, str)}
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def save_workspace_credentials(ws: "Workspace", **secrets_map: str | None) -> None:
    ws.ensure_dirs()
    path = credentials_path(ws)
    existing = load_workspace_credentials(ws.state_dir)
    for k, v in secrets_map.items():
        if v is None:
            continue
        cleaned = v.strip()
        if cleaned:
            existing[k] = cleaned
        else:
            existing.pop(k, None)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=ws.state_dir, delete=False) as tmp:
        os.fchmod(tmp.fileno(), 0o600)
        json.dump(existing, tmp, ensure_ascii=False, indent=2)
        tmp_name = tmp.name
    Path(tmp_name).replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def initialize_project_signing(ws: "Workspace", settings: "Settings") -> None:
    import secrets
    import time

    credentials = load_workspace_credentials(ws.state_dir)
    if not credentials.get("signing_secret"):
        credentials["signing_secret"] = secrets.token_hex(32)
        import hashlib
        old_key = settings.legacy_verification_token or settings.service_token()
        credentials["legacy_signing_fingerprint"] = hashlib.sha256(old_key.encode()).hexdigest() if old_key else ""
        credentials["legacy_signing_until"] = str(int(time.time()) + 86400) if old_key or credentials.get("ci_webhook_secret") else "0"
        save_workspace_credentials(ws, signing_secret=credentials["signing_secret"],
                                   legacy_signing_until=credentials["legacy_signing_until"],
                                   legacy_signing_fingerprint=credentials["legacy_signing_fingerprint"],
                                   loopforge_token=settings.legacy_verification_token or None)
    settings.signing_secret = credentials["signing_secret"]
    settings.legacy_signing_fingerprint = credentials.get("legacy_signing_fingerprint", "")
    settings.legacy_signing_until = float(credentials.get("legacy_signing_until", "0"))
    if settings.legacy_signing_until <= time.time() and credentials.get("loopforge_token"):
        save_workspace_credentials(ws, loopforge_token="")
        settings.legacy_verification_token = ""


def get_ci_signing_key(ws: "Workspace", settings: "Settings") -> str:
    """读取当前项目的 CI 回调签名密钥（禁止任何硬编码默认兜底）。"""
    explicit = settings.project_signing_key()
    if explicit:
        return explicit
    return load_workspace_credentials(ws.state_dir).get("ci_webhook_secret", "").strip()


def ensure_ci_signing_key(ws: "Workspace", settings: "Settings") -> str:
    """获取或为当前工作区生成独立的 256-bit 随机 CI 回调密钥，存入 0600 credentials.json。"""
    import secrets

    existing = get_ci_signing_key(ws, settings)
    if existing:
        return existing
    generated = secrets.token_hex(32)
    save_workspace_credentials(ws, ci_webhook_secret=generated)
    return generated


def _toml_key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else json.dumps(key, ensure_ascii=False)


def _toml_val(val: object) -> str:
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, (datetime, date, dtime)):
        return val.isoformat()
    if isinstance(val, str):
        return json.dumps(val, ensure_ascii=False)
    if isinstance(val, (list, tuple)):
        return "[" + ", ".join(_toml_val(x) for x in val) + "]"
    if isinstance(val, dict):
        inner = ", ".join(f"{_toml_key(str(k))} = {_toml_val(v)}" for k, v in val.items())
        return "{ " + inner + " }"
    return json.dumps(str(val), ensure_ascii=False)


def _render_custom_table(path: list[str], table: dict[str, Any], out_blocks: list[str]) -> None:
    scalars: list[str] = []
    subtables: list[tuple[str, dict[str, Any]]] = []
    aot_items: list[tuple[str, list[dict[str, Any]]]] = []
    for k, v in table.items():
        if isinstance(v, dict):
            subtables.append((k, v))
        elif isinstance(v, list) and v and all(isinstance(item, dict) for item in v):
            aot_items.append((k, v))
        else:
            scalars.append(f"{_toml_key(str(k))} = {_toml_val(v)}")
    header = ".".join(_toml_key(p) for p in path)
    if scalars or (not subtables and not aot_items):
        out_blocks.append("\n".join([f"[{header}]", *scalars]) + "\n")
    for sub_k, sub_v in subtables:
        _render_custom_table([*path, str(sub_k)], sub_v, out_blocks)
    for arr_k, arr_list in aot_items:
        arr_header = ".".join(_toml_key(p) for p in [*path, str(arr_k)])
        for elem in arr_list:
            elem_lines = [f"[[{arr_header}]]"]
            for ek, ev in elem.items():
                elem_lines.append(f"{_toml_key(str(ek))} = {_toml_val(ev)}")
            out_blocks.append("\n".join(elem_lines) + "\n")


def serialize_config_toml(settings: "Settings", existing_toml: str = "") -> str:
    """结构化生成完整工作区 config.toml：不含明文 Token，同时完整保留用户自定义节与未知字段。"""
    existing_doc: dict[str, Any] = {}
    if existing_toml.strip():
        try:
            parsed = tomllib.loads(existing_toml)
            if isinstance(parsed, dict):
                existing_doc = parsed
        except tomllib.TOMLDecodeError:
            existing_doc = {}

    sections: list[tuple[str, tuple[str, ...]]] = [
        (
            "agent",
            (
                "harness",
                "pm_harness",
                "director_harness",
                "coder_harness",
                "qa_harness",
                "codex_bin",
                "codex_profile",
                "opencode_bin",
                "opencode_server_url",
                "codebuddy_bin",
                "dsh_bin",
                "claude_bin",
                "model",
                "pm_model",
                "director_model",
                "planner_model",
                "coder_model",
                "qa_model",
                "reviewer_model",
                "codex_extra_args",
                "agent_network_access",
                "bootstrap_network_access",
                "agent_timeout_sec",
            ),
        ),
        (
            "scheduler",
            (
                "planning_mode",
                "enable_council",
                "enable_pm_spec",
                "director_max_arbitrations",
                "max_concurrency",
                "max_retries",
                "verify_timeout_sec",
                "require_plan_approval",
                "require_merge_approval",
                "process_template",
                "review_policy",
                "review_risk_lines",
                "review_required_paths",
                "doc_profile",
                "ci_provider",
                "ci_url",
                "ci_git_remote",
                "ci_timeout_sec",
                "ci_max_fix_rounds",
                "deploy_cmd",
                "data_classification",
                "token_budget_limit",
            ),
        ),
        (
            "runtime",
            (
                "execution_pool", "native_executor_user_id", "native_approver_user_id",
                "collaboration_mode",
                "runtime_backend",
                "loopforge_url",
                "loopforge_project_id",
                "loopforge_workflow_id",
                "loopforge_repository_id",
                "gitea_url",
            ),
        ),
        ("git", ("base_branch", "protected_paths")),
        ("ingest", ("slice_max_chars", "slice_min_chars")),
    ]

    managed_section_names = {name for name, _ in sections} | {"env"}
    all_managed_keys = {k for _, keys in sections for k in keys} | {"loopforge_token"}

    blocks: list[str] = ["# Vernal 工作区配置（自动结构化生成；敏感凭据独立存储于 credentials.json）\n"]

    # 保留顶层未知标量/数组字段（剔除敏感 loopforge_token 与已纳管字段）
    top_unknown: list[str] = []
    for k, v in existing_doc.items():
        if isinstance(v, dict) or (isinstance(v, list) and v and all(isinstance(i, dict) for i in v)):
            continue
        if k in all_managed_keys:
            continue
        top_unknown.append(f"{_toml_key(str(k))} = {_toml_val(v)}")
    if top_unknown:
        blocks.append("\n".join(top_unknown) + "\n")

    for sec_name, keys in sections:
        lines = [f"[{sec_name}]"]
        for k in keys:
            if hasattr(settings, k):
                lines.append(f"{k} = {_toml_val(getattr(settings, k))}")
        if sec_name == "runtime":
            lines.append('loopforge_token = ""')
        existing_sec = existing_doc.get(sec_name)
        extra_subtables: list[tuple[str, dict[str, Any]]] = []
        if isinstance(existing_sec, dict):
            for uk, uv in existing_sec.items():
                if uk in keys or uk in all_managed_keys:
                    continue
                if isinstance(uv, dict):
                    extra_subtables.append((uk, uv))
                else:
                    lines.append(f"{_toml_key(str(uk))} = {_toml_val(uv)}")
        blocks.append("\n".join(lines) + "\n")
        for sub_k, sub_v in extra_subtables:
            _render_custom_table([sec_name, str(sub_k)], sub_v, blocks)

    env_lines = ["[env]"]
    for ek, ev in sorted((settings.env or {}).items()):
        env_lines.append(f"{_toml_key(str(ek))} = {_toml_val(ev)}")
    blocks.append("\n".join(env_lines) + "\n")

    # 完整保留用户自定义节与 [[array_of_tables]]
    for sec_k, sec_v in existing_doc.items():
        if sec_k in managed_section_names:
            continue
        if isinstance(sec_v, dict):
            _render_custom_table([str(sec_k)], sec_v, blocks)
        elif isinstance(sec_v, list) and sec_v and all(isinstance(i, dict) for i in sec_v):
            for elem in sec_v:
                elem_lines = [f"[[{_toml_key(str(sec_k))}]]"]
                for ek, ev in elem.items():
                    elem_lines.append(f"{_toml_key(str(ek))} = {_toml_val(ev)}")
                blocks.append("\n".join(elem_lines) + "\n")

    return "\n".join(blocks)


def persist_workspace_settings(ws: "Workspace", settings: "Settings") -> None:
    """原子落盘 config.toml 与 0600 credentials.json，并保留用户自定义节。"""
    ws.ensure_dirs()
    save_workspace_credentials(ws, loopforge_service_token=settings.loopforge_token)
    existing_toml = ""
    if ws.config_path.exists():
        try:
            existing_toml = ws.config_path.read_text("utf-8")
        except OSError:
            existing_toml = ""
    content = serialize_config_toml(settings, existing_toml=existing_toml)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=ws.state_dir, delete=False) as tmp:
        tmp.write(content)
        tmp_name = tmp.name
    Path(tmp_name).replace(ws.config_path)
