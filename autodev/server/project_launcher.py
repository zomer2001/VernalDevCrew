"""项目启动台与 Runner 能力聚合助手：
1. 宿主机安全目录浏览与原地接管路径登记（.vernal-projects.json）
2. 已有代码库结构摘要生成（供摄入与圆桌快速理解现有目录/分支）
3. LoopForge 在线 Runner 执行器清单与 CodeBuddy 个人凭据状态融合
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from autodev.artifacts import MAX_TOTAL as FILE_EXCHANGE_MAX_TOTAL
from autodev.config import HARNESS_LABELS, SUPPORTED_HARNESSES, Settings, detect_preferred_harness

_SKIP_BROWSE_DIRS = frozenset(
    {
        ".git",
        ".ssh",
        ".aws",
        ".gnupg",
        ".kube",
        ".config",
        ".cache",
        ".npm",
        ".pnpm-store",
        ".yarn",
        ".venv",
        ".Trash",
        "Library",
        "Applications",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "dist",
        "build",
    }
)

_SKIP_SCAN_DIRS = frozenset(
    {
        ".git",
        ".autodev",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".pytest_cache",
        "dist",
        "build",
        "target",
        ".next",
        ".nuxt",
        "coverage",
    }
)

# LoopForge 0.4.0 上游对各 CLI 的认证模式：codex / claude-code 支持复用宿主机登录态，codebuddy 强制个人 API Key
LOOPFORGE_LOCAL_LOGIN_PROVIDERS = frozenset({"codex", "claude-code", "agy"})
LOOPFORGE_KEY_REQUIRED_PROVIDERS = frozenset({"codebuddy"})

_SLUG_RE = re.compile(r"[^a-zA-Z0-9_-]+")


def slugify_project_id(name: str, fallback: str = "project") -> str:
    raw = _SLUG_RE.sub("-", (name or "").strip().lower()).strip("-_")
    if not raw or not raw[0].isalnum():
        raw = f"{fallback}-{abs(hash(name or fallback)) % 100000:05d}"
    return raw[:48]


def host_projects_root() -> Path:
    """宿主机可浏览根目录：仅开放用户显式配置的 VERNAL_HOST_PROJECTS_DIR（Docker 默认仅挂载工作区目录，不暴露整个用户主目录）。"""
    env_dir = os.environ.get("VERNAL_HOST_PROJECTS_DIR", "").strip()
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    docker_default = Path("/host-projects")
    if docker_default.is_dir():
        return docker_default.resolve()
    return (Path.cwd() / "workspaces").resolve() if (Path.cwd() / "workspaces").is_dir() else Path.cwd().resolve()


def host_projects_display_prefix() -> str:
    return os.environ.get("VERNAL_HOST_PROJECTS_DISPLAY", "").strip()


def resolve_safe_host_dir(raw_path: str) -> Path:
    """把前端传入的宿主机目录路径安全解析到允许根目录之内，防止路径穿越。"""
    root = host_projects_root()
    text = (raw_path or "").strip()
    if not text:
        return root
    # 允许用户直接贴宿主机真实路径（例如 Docker 下贴 /Users/alice/proj，自动映射到 /host-projects/proj）
    display_prefix = host_projects_display_prefix().rstrip("/")
    if display_prefix and (text == display_prefix or text.startswith(display_prefix + "/")):
        rel = text[len(display_prefix) :].lstrip("/")
        candidate = (root / rel).resolve() if rel else root
    else:
        p = Path(text).expanduser()
        candidate = p.resolve() if p.is_absolute() else (root / p).resolve()
    if candidate != root and not candidate.is_relative_to(root):
        raise ValueError(f"目录超出允许浏览的宿主机范围（根目录：{root}）")
    if not candidate.exists() or not candidate.is_dir():
        raise ValueError(f"目录不存在：{text}")
    return candidate


def _git_branch_of(path: Path) -> str:
    if not (path / ".git").exists():
        return ""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    except Exception:
        pass
    return ""


def _git_remote_url(path: Path) -> str:
    if not (path / ".git").exists():
        return ""
    try:
        proc = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    except Exception:
        pass
    return ""


def list_host_directories(raw_path: str = "", limit: int = 120) -> dict[str, Any]:
    root = host_projects_root()
    if not root.exists() or not root.is_dir():
        return {
            "available": False,
            "root": str(root),
            "display_root": host_projects_display_prefix() or str(root),
            "current": str(root),
            "parent": "",
            "entries": [],
            "note": "宿主机目录尚未挂载，可使用「浏览器导入文件夹」作为兜底。",
        }
    current = resolve_safe_host_dir(raw_path)
    entries: list[dict[str, Any]] = []
    try:
        children = sorted(current.iterdir(), key=lambda p: (not (p / ".git").exists(), p.name.lower()))
    except PermissionError:
        children = []

    for child in children:
        if len(entries) >= limit:
            break
        name = child.name
        if name.startswith(".") or name in _SKIP_BROWSE_DIRS:
            continue
        try:
            if not child.is_dir() or child.is_symlink():
                continue
        except OSError:
            continue
        is_git = (child / ".git").exists()
        entries.append(
            {
                "name": name,
                "path": str(child),
                "is_git": is_git,
                "git_branch": _git_branch_of(child) if is_git else "",
                "has_autodev": (child / ".autodev" / "state.db").is_file(),
                "has_readme": any((child / rn).is_file() for rn in ("README.md", "readme.md", "README.txt")),
            }
        )

    parent = str(current.parent) if current != root and current.parent.is_relative_to(root) else ""
    cur_is_git = (current / ".git").exists()
    return {
        "available": True,
        "root": str(root),
        "display_root": host_projects_display_prefix() or str(root),
        "current": str(current),
        "current_is_git": cur_is_git,
        "current_git_branch": _git_branch_of(current) if cur_is_git else "",
        "current_git_remote": _git_remote_url(current) if cur_is_git else "",
        "parent": parent,
        "entries": entries,
    }


def _registry_path(workspaces_parent: Path) -> Path:
    workspaces_parent.mkdir(parents=True, exist_ok=True)
    return workspaces_parent / ".vernal-projects.json"


def load_project_registry(workspaces_parent: Path) -> dict[str, dict[str, Any]]:
    rp = _registry_path(workspaces_parent)
    if not rp.is_file():
        return {}
    try:
        data = json.loads(rp.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def register_project_mapping(
    workspaces_parent: Path,
    project_id: str,
    root: Path,
    *,
    source_mode: str,
    git_url: str = "",
    branch: str = "",
) -> None:
    reg = load_project_registry(workspaces_parent)
    reg[project_id] = {
        "root": str(root.resolve()),
        "source_mode": source_mode,
        "git_url": git_url,
        "branch": branch,
    }
    rp = _registry_path(workspaces_parent)
    rp.write_text(json.dumps(reg, ensure_ascii=False, indent=2), "utf-8")


def scan_workspace_source_files_detailed(
    ws_root: Path,
    *,
    max_total_bytes: int = FILE_EXCHANGE_MAX_TOTAL,
) -> tuple[dict[str, str], list[str], int]:
    """扫描已导入/接管目录中的 UTF-8 源码文件（自动跳过 inputs/.autodev/.git/依赖与二进制文件）。

    返回 `(included_files, overflow_paths, total_bytes)`，调用方可据此显式拦截超限而非静默丢文件。
    """
    files: dict[str, str] = {}
    overflow: list[str] = []
    total = 0
    skip_top = _SKIP_SCAN_DIRS | {"inputs"}
    for root_dir, dirnames, filenames in os.walk(ws_root):
        rel_dir = Path(root_dir).relative_to(ws_root)
        dirnames[:] = sorted(
            d for d in dirnames
            if not d.startswith(".") and d not in (skip_top if rel_dir == Path(".") else _SKIP_SCAN_DIRS)
        )
        for fn in sorted(filenames):
            if fn.startswith("."):
                continue
            fp = Path(root_dir) / fn
            if fp.is_symlink() or not fp.is_file():
                continue
            try:
                raw = fp.read_bytes()
            except OSError:
                continue
            if b"\x00" in raw[:4096]:
                continue  # 跳过二进制文件
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            rel = (rel_dir / fn).as_posix() if rel_dir != Path(".") else fn
            if total + len(raw) > max_total_bytes:
                overflow.append(rel)
                total += len(raw)
                continue
            files[rel] = text
            total += len(raw)
    return files, overflow, total


def scan_workspace_source_files(ws_root: Path, *, max_total_bytes: int = FILE_EXCHANGE_MAX_TOTAL) -> dict[str, str]:
    files, _overflow, _total = scan_workspace_source_files_detailed(ws_root, max_total_bytes=max_total_bytes)
    return files


def write_codebase_overview_doc(ws_root: Path, *, source_mode: str, git_url: str = "", branch: str = "") -> Path:
    """为接管的本地目录或克隆的 Git 仓库生成代码结构与源码摘录，写入 inputs/ 供规划器与圆桌直接摄入。"""
    inputs_dir = ws_root / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    target = inputs_dir / "00-existing-codebase.md"

    source_files = scan_workspace_source_files(ws_root)
    tree_lines = [f"- `{rel}`" for rel in sorted(source_files)[:150]]
    file_count = len(source_files)

    readme_excerpt = ""
    for cand in ("README.md", "readme.md", "README.rst", "README.txt"):
        if cand in source_files:
            readme_excerpt = source_files[cand][:3000].strip()
            break

    manifests: list[str] = []
    for mf in ("pyproject.toml", "package.json", "go.mod", "Cargo.toml", "pom.xml", "Makefile"):
        if mf in source_files:
            manifests.append(f"#### `{mf}`\n```\n{source_files[mf][:1200].strip()}\n```")

    # 附带关键源码文件摘录（总预算约 24KB），确保规划器与圆桌直接看到现有核心实现
    code_excerpts: list[str] = []
    excerpt_budget = 24_000
    used_budget = 0
    skip_excerpt_names = {"README.md", "readme.md", "README.rst", "README.txt", "pyproject.toml", "package.json", "go.mod", "Cargo.toml", "pom.xml", "Makefile"}
    for rel, content in sorted(source_files.items()):
        if rel in skip_excerpt_names or used_budget >= excerpt_budget:
            continue
        snippet = content[:3000].strip()
        if not snippet:
            continue
        code_excerpts.append(f"#### `{rel}`\n```\n{snippet}\n```")
        used_budget += len(snippet)

    lines = [
        f"# 已有代码库概览（{ws_root.name}）",
        "",
        f"- **接入方式**：{source_mode}",
        f"- **工作目录**：`{ws_root}`",
    ]
    if git_url:
        lines.append(f"- **Git 远端**：`{git_url}`")
    if branch:
        lines.append(f"- **当前分支**：`{branch}`")
    lines.append(f"- **扫描到源码文件数**：{file_count}")
    lines.extend(["", "## 目录结构摘要（前 150 项）", *(tree_lines or ["- （当前目录为空）"])])
    if manifests:
        lines.extend(["", "## 关键工程配置文件", *manifests])
    if readme_excerpt:
        lines.extend(["", "## 原有 README 摘录", readme_excerpt])
    if code_excerpts:
        lines.extend(["", "## 现有核心源码摘录", *code_excerpts])

    target.write_text("\n".join(lines).strip() + "\n", "utf-8")
    return target


def merge_runner_runtimes(
    local_runtimes: dict[str, dict[str, Any]],
    settings: Settings,
    *,
    runners: list[dict[str, Any]] | None = None,
    user_info: dict[str, Any] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """把 LoopForge 在线 Runner 上报的真实执行器能力与当前用户凭据状态融合进 Vernal 执行器视图。"""
    merged: dict[str, dict[str, Any]] = {k: dict(v) for k, v in local_runtimes.items()}
    online_runners = [r for r in (runners or []) if isinstance(r, dict) and r.get("status") == "online"]
    user_obj = (user_info or {}).get("user") if isinstance((user_info or {}).get("user"), dict) else (user_info or {})
    cb_key_configured = bool((user_obj or {}).get("codeBuddyApiKeyConfigured"))
    cb_env = str((user_obj or {}).get("codeBuddyInternetEnvironment") or "")

    runner_summary: dict[str, Any] = {
        "source": "loopforge_runner" if settings.runtime_backend == "loopforge" and online_runners else "local",
        "online_count": len(online_runners),
        "runner_names": [str(r.get("name") or r.get("id") or "") for r in online_runners],
        "codebuddy_api_key_configured": cb_key_configured,
        "codebuddy_internet_environment": cb_env,
        "user_id": (user_obj or {}).get("id"),
    }

    if settings.runtime_backend != "loopforge":
        return merged, runner_summary

    # 从所有在线 Runner 中汇总每个 provider 的真实可用状态
    provider_map: dict[str, list[dict[str, Any]]] = {}
    for runner in online_runners:
        rname = str(runner.get("name") or runner.get("id") or "Runner")
        for rt in runner.get("runtimes") or []:
            if not isinstance(rt, dict):
                continue
            prov = str(rt.get("provider") or "").strip().lower()
            if not prov:
                continue
            provider_map.setdefault(prov, []).append({**rt, "_runner": rname})

    for hname in SUPPORTED_HARNESSES:
        base = merged.get(
            hname,
            {"label": HARNESS_LABELS.get(hname, hname), "installed": False, "ready": False, "version": "", "bin": "", "note": "未安装"},
        )
        if not online_runners:
            # 没有任何在线 Runner：无论本机是否装了 CLI，LoopForge 底座都无法派单，绝不可返回 ready=True
            merged[hname] = {
                **base,
                "ready": False,
                "source": "loopforge_runner",
                "runner_online": False,
                "runner_names": [],
                "needs_api_key": hname in LOOPFORGE_KEY_REQUIRED_PROVIDERS,
                "api_key_configured": cb_key_configured if hname == "codebuddy" else False,
                "note": "暂无在线执行机心跳（请运行 ./vernal.sh 启动本机 Runner）",
            }
            continue

        matched = [item for item in provider_map.get(hname, []) if str(item.get("status") or "").lower() != "disabled"]
        if matched:
            first = matched[0]
            rnames = sorted({str(m["_runner"]) for m in matched})
            ver = str(first.get("version") or base.get("version") or "")
            bpath = str(first.get("binaryPath") or base.get("bin") or "")
            auth_mode = str(first.get("authMode") or ("personal" if hname in LOOPFORGE_KEY_REQUIRED_PROVIDERS else "local")).lower()
            if hname == "codebuddy":
                ready = cb_key_configured
                note = (
                    f"Runner 在线（{', '.join(rnames)}）· CodeBuddy API Key 已配置"
                    if ready
                    else f"Runner 已装 CLI（{', '.join(rnames)}），但 LoopForge 要求配置 CodeBuddy API Key 才能执行"
                )
                merged[hname] = {
                    "label": HARNESS_LABELS.get(hname, hname),
                    "installed": True,
                    "ready": ready,
                    "version": ver,
                    "bin": bpath,
                    "source": "loopforge_runner",
                    "runner_online": True,
                    "runner_names": rnames,
                    "auth_mode": "personal",
                    "needs_api_key": True,
                    "api_key_configured": cb_key_configured,
                    "note": note,
                }
            elif hname in LOOPFORGE_LOCAL_LOGIN_PROVIDERS:
                merged[hname] = {
                    "label": HARNESS_LABELS.get(hname, hname),
                    "installed": True,
                    "ready": True,
                    "version": ver,
                    "bin": bpath,
                    "source": "loopforge_runner",
                    "runner_online": True,
                    "runner_names": rnames,
                    "auth_mode": auth_mode,
                    "needs_api_key": False,
                    "api_key_configured": True,
                    "note": f"Runner 在线（{', '.join(rnames)}）· 复用本机 CLI 登录态（免 Key）",
                }
            else:
                merged[hname] = {
                    "label": HARNESS_LABELS.get(hname, hname),
                    "installed": True,
                    "ready": True,
                    "version": ver,
                    "bin": bpath,
                    "source": "loopforge_runner",
                    "runner_online": True,
                    "runner_names": rnames,
                    "auth_mode": auth_mode,
                    "needs_api_key": auth_mode == "personal",
                    "api_key_configured": True,
                    "note": f"Runner 在线（{', '.join(rnames)}）",
                }
        else:
            merged[hname] = {
                "label": HARNESS_LABELS.get(hname, hname),
                "installed": False,
                "ready": False,
                "version": "",
                "bin": "",
                "source": "loopforge_runner",
                "runner_online": True,
                "runner_names": [],
                "needs_api_key": hname in LOOPFORGE_KEY_REQUIRED_PROVIDERS,
                "api_key_configured": cb_key_configured if hname == "codebuddy" else False,
                "note": f"在线 Runner（{', '.join(runner_summary['runner_names'])}）未检测到 {hname}",
            }

    return merged, runner_summary


def choose_preferred_harness_from_runtimes(runtimes: dict[str, dict[str, Any]], settings: Settings) -> str:
    for name in ("codex", "claude-code", "codebuddy", "opencode", "dsh"):
        info = runtimes.get(name) or {}
        if info.get("installed") and info.get("ready"):
            return name
    return detect_preferred_harness(settings)
