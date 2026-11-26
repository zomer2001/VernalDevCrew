"""工作区目录布局、多角色模型/执行器混搭配置与运行环境探测。"""

from __future__ import annotations

import os
import shutil
import subprocess
import tomllib
from contextvars import ContextVar
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

STATE_DIR = ".autodev"
EXECUTION_IDENTITY: ContextVar[tuple[str, str] | None] = ContextVar("execution_identity", default=None)

CODEX_FALLBACK_PATHS = (
    "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex",
    "/Applications/Codex.app/Contents/Resources/codex",
    str(Path.home() / ".local/bin/codex"),
    str(Path.home() / ".npm-global/bin/codex"),
    "/opt/homebrew/bin/codex",
    "/usr/local/bin/codex",
    "/usr/bin/codex",
)

OPENCODE_FALLBACK_PATHS = (
    "/opt/homebrew/bin/opencode",
    "/usr/local/bin/opencode",
    "/usr/bin/opencode",
    str(Path.home() / ".opencode/bin/opencode"),
    str(Path.home() / ".npm-global/bin/opencode"),
    str(Path.home() / ".local/bin/opencode"),
)

CODEBUDDY_FALLBACK_PATHS = (
    str(Path.home() / ".codebuddy/bin/codebuddy"),
    str(Path.home() / ".npm-global/bin/codebuddy"),
    str(Path.home() / ".local/bin/codebuddy"),
    "/opt/homebrew/bin/codebuddy",
    "/usr/local/bin/codebuddy",
    "/usr/bin/codebuddy",
)

DSH_FALLBACK_PATHS = (
    str(Path.home() / ".dsh/bin/dsh"),
    str(Path.home() / ".npm-global/bin/dsh"),
    str(Path.home() / ".local/bin/dsh"),
    "/opt/homebrew/bin/dsh",
    "/usr/local/bin/dsh",
    "/usr/bin/dsh",
)

CLAUDE_FALLBACK_PATHS = (
    str(Path.home() / ".claude/local/claude"),
    str(Path.home() / ".npm-global/bin/claude"),
    str(Path.home() / ".local/bin/claude"),
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
    "/usr/bin/claude",
)

SUPPORTED_HARNESSES = ("codex", "codebuddy", "opencode", "dsh", "claude-code")

HARNESS_LABELS: dict[str, str] = {
    "auto": "自动探测 (Auto)",
    "codex": "Codex CLI",
    "codebuddy": "CodeBuddy",
    "opencode": "OpenCode",
    "dsh": "DeepSeek Harness (dsh)",
    "claude-code": "Claude Code",
}


def normalize_harness_name(name: str) -> str:
    raw = (name or "").strip().lower()
    aliases = {
        "deepseek": "dsh",
        "deepseek-harness": "dsh",
        "deepseek_harness": "dsh",
        "opencode-cli": "opencode",
        "claude": "claude-code",
        "claude_code": "claude-code",
    }
    return aliases.get(raw, raw)


DEFAULT_CONFIG_TOML = """\
# Vernal 工作区配置（基于 LoopForge 执行底座；所有键均可省略，省略即使用默认值）

[agent]
harness = "auto"               # 默认执行器：auto（自动扫描本机）| codex | codebuddy | opencode | dsh | claude-code
pm_harness = ""                # 产品经理执行器（留空沿用 harness）
director_harness = ""          # 研发总监执行器（留空沿用 harness）
coder_harness = ""             # 研发/基线/契约/装配组执行器（可混编如 "codebuddy" / "opencode" / "dsh"）
qa_harness = ""                # 测试与独立审查组执行器（留空沿用 harness）

codex_bin = ""                 # 留空自动探测（PATH → ChatGPT.app 内置 codex）
codex_profile = ""             # 传给 `codex exec -p`，留空用 ~/.codex/config.toml 默认
opencode_bin = ""              # 留空自动探测
opencode_server_url = ""       # 若已运行 `opencode serve`（如 http://127.0.0.1:4096），填入可走实时事件总线
codebuddy_bin = ""             # 留空自动探测腾讯 CodeBuddy CLI
dsh_bin = ""                   # 留空自动探测 DeepSeek Harness (dsh) CLI
claude_bin = ""                # 留空自动探测 Claude Code CLI

model = ""                     # 全局默认模型（留空自动跟随所选 CLI / 执行池默认模型）
pm_model = ""                  # 各角色独立模型覆盖（留空沿用 model）
director_model = ""
planner_model = ""
coder_model = ""
qa_model = ""
reviewer_model = ""

codex_extra_args = ["-c", 'model_reasoning_effort="high"']
agent_network_access = false   # M1~Mn 业务编码沙箱默认断网
bootstrap_network_access = true # M0 基线任务（role=bootstrap）允许联网初始化共享依赖环境
agent_timeout_sec = 1800

[scheduler]
planning_mode = "goal"         # goal（多智能体评审圆桌 + 总监分组蓝图 + 滚动拆单）| classic（单次全量拆单）
enable_council = true          # goal 模式下启动 2 轮结构化多智能体架构评审圆桌（PM / 架构师 / QA / 总监）
enable_pm_spec = true          # 未启用圆桌时是否单独由 PM 补全 PRD
director_max_arbitrations = 2  # 单任务遇到契约缺字段或失败超限时，总监最多自动仲裁几次再叫人
max_concurrency = 2            # Mac M4 16GB 建议 2~3；麒麟服务器可调至 6~8
max_retries = 3                # 单轮连续失败多少次触发总监仲裁 / 熔断
verify_timeout_sec = 300
require_plan_approval = true   # 仅在立项蓝图出来后人工审批一次，批准后全程自动滚动推进
require_merge_approval = false

[runtime]
execution_pool = "auto"
native_executor_user_id = 0
native_approver_user_id = 0
collaboration_mode = "git"     # files（文件产物）或 git（版本库协作）
runtime_backend = "loopforge"  # loopforge（基于 LoopForge SOP/沙箱运行，默认）| local（离线 worktree 调试）
loopforge_url = "http://127.0.0.1:8090"
loopforge_token = ""           # 留空自动读取环境变量 AGENTFLOW_TOKEN
loopforge_project_id = ""
loopforge_workflow_id = ""
loopforge_repository_id = ""
gitea_url = "http://127.0.0.1:3000"

[git]
base_branch = "main"
protected_paths = ["contracts/"]

[ingest]
slice_max_chars = 6000
slice_min_chars = 1500

[env]
# 透传给编码会话与验证命令的环境变量
# CARGO_BUILD_JOBS = "4"
"""


class ConfigError(ValueError):
    pass


class WorkspaceNotFound(FileNotFoundError):
    pass


@dataclass
class Settings:
    # agent
    harness: str = "codex"
    runner_harness: str = field(default="", init=False, repr=False)
    pm_harness: str = ""
    director_harness: str = ""
    coder_harness: str = ""
    qa_harness: str = ""
    codex_bin: str = ""
    codex_profile: str = ""
    opencode_bin: str = ""
    opencode_server_url: str = ""
    codebuddy_bin: str = ""
    dsh_bin: str = ""
    claude_bin: str = ""
    model: str = ""
    pm_model: str = ""
    director_model: str = ""
    planner_model: str = ""
    coder_model: str = ""
    qa_model: str = ""
    reviewer_model: str = ""
    codex_extra_args: list[str] = field(default_factory=lambda: ["-c", 'model_reasoning_effort="high"'])
    agent_network_access: bool = False
    bootstrap_network_access: bool = True
    agent_timeout_sec: int = 1800
    # scheduler & standardized pipeline
    planning_mode: str = "classic"
    enable_council: bool = False
    enable_pm_spec: bool = True
    director_max_arbitrations: int = 2
    max_concurrency: int = 2
    max_retries: int = 3
    verify_timeout_sec: int = 300
    require_plan_approval: bool = True
    require_merge_approval: bool = False
    process_template: str = "legacy"
    review_policy: str = "off"
    review_risk_lines: int = 400
    review_required_paths: list[str] = field(default_factory=list)
    doc_profile: str = "default"
    ci_provider: str = "none"
    ci_url: str = ""
    ci_git_remote: str = ""
    ci_timeout_sec: int = 1800
    ci_max_fix_rounds: int = 3
    deploy_cmd: str = ""
    data_classification: str = "public"
    native_executor_user_id: int = 0
    native_approver_user_id: int = 0
    token_budget_limit: int = 0
    # runtime (LoopForge + Gitea)
    collaboration_mode: str = "git"
    selected_pool: str = field(default="", init=False, repr=False)
    execution_pool: str = "auto"
    runtime_backend: str = "local"
    loopforge_url: str = "http://127.0.0.1:8090"
    loopforge_token: str = ""
    loopforge_project_id: str = ""
    loopforge_workflow_id: str = ""
    loopforge_repository_id: str = ""
    gitea_url: str = "http://127.0.0.1:3000"
    vernal_public_url: str = "http://127.0.0.1:8787"
    # git
    base_branch: str = "main"
    protected_paths: list[str] = field(default_factory=lambda: ["contracts/"])
    # ingest
    slice_max_chars: int = 6000
    slice_min_chars: int = 1500
    # env
    env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "Settings":
        settings = cls()
        if not path.exists():
            return settings
        try:
            data = tomllib.loads(path.read_text("utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"配置文件解析失败 {path}: {exc}") from exc
        known = {f.name for f in fields(cls) if f.init}
        managed_sections = {"agent", "scheduler", "runtime", "git", "ingest", "review", "docs", "ci"}
        flat: dict[str, object] = {}
        for key, value in data.items():
            if key == "env" and isinstance(value, dict):
                flat["env"] = {str(k): str(v) for k, v in value.items()}
            elif key in managed_sections and isinstance(value, dict):
                for sk, sv in value.items():
                    if sk in known and not isinstance(sv, dict):
                        flat[sk] = sv
            elif not isinstance(value, dict) and key in known:
                flat[key] = value
        for key, value in flat.items():
            setattr(settings, key, value)
        from autodev.security import load_workspace_credentials
        credentials = load_workspace_credentials(path.parent)
        # Older releases persisted browser tokens under this name. Their origin
        # cannot be established, so only the old HMAC verifier may read them.
        settings.legacy_verification_token = settings.loopforge_token.strip() or credentials.get("loopforge_token", "")
        settings.loopforge_token = credentials.get("loopforge_service_token", "")
        settings.signing_secret = credentials.get("signing_secret", "")
        settings.legacy_signing_fingerprint = credentials.get("legacy_signing_fingerprint", "")
        settings.legacy_signing_until = float(credentials.get("legacy_signing_until", "0"))
        if os.environ.get("VERNAL_LOOPFORGE_URL", "").strip():
            settings.loopforge_url = os.environ["VERNAL_LOOPFORGE_URL"].strip()
        elif not Path("/.dockerenv").exists() and settings.loopforge_url.rstrip("/") == "http://loopforge:8080":
            settings.loopforge_url = "http://127.0.0.1:8090"
        if os.environ.get("VERNAL_GITEA_URL", "").strip():
            settings.gitea_url = os.environ["VERNAL_GITEA_URL"].strip()
        elif not Path("/.dockerenv").exists() and settings.gitea_url.rstrip("/") == "http://gitea:3000":
            settings.gitea_url = "http://127.0.0.1:3000"
        if os.environ.get("VERNAL_PUBLIC_URL", "").strip():
            settings.vernal_public_url = os.environ["VERNAL_PUBLIC_URL"].strip()
        if settings.collaboration_mode not in ("files", "git") or settings.runtime_backend not in ("local", "loopforge"):
            raise ConfigError("无效的协作方式或执行底座")
        if settings.execution_pool not in ("auto", "public_docker", "local_runner"):
            raise ConfigError("无效的执行池")
        if settings.max_concurrency < 1 or settings.max_retries < 1:
            raise ConfigError("max_concurrency 与 max_retries 必须 >= 1")
        return settings

    def harness_for(self, role: str) -> str:
        specific = normalize_harness_name(
            {
                "pm": self.pm_harness,
                "architect": self.director_harness,
                "director": self.director_harness,
                "planner": self.director_harness,
                "bootstrap": self.coder_harness,
                "contract": self.coder_harness,
                "coder": self.coder_harness,
                "integrator": self.coder_harness,
                "qa_lead": self.qa_harness,
                "tester": self.qa_harness,
                "reviewer": self.qa_harness,
            }.get(role, "")
        )
        if specific and specific != "auto":
            return specific
        base = normalize_harness_name(self.harness or "auto")
        if base == "auto":
            if self.runtime_backend == "loopforge" and self.runner_harness:
                return self.runner_harness
            return detect_preferred_harness(self)
        if base == "codex" and self.runtime_backend != "loopforge" and not resolve_codex_bin(self):
            detected = detect_preferred_harness(self)
            if detected != "codex":
                return detected
        return base or "codex"

    def model_for(self, role: str) -> str:
        specific = {
            "pm": self.pm_model,
            "architect": self.director_model or self.planner_model,
            "director": self.director_model or self.planner_model,
            "planner": self.planner_model or self.director_model,
            "bootstrap": self.coder_model,
            "contract": self.coder_model,
            "coder": self.coder_model,
            "integrator": self.coder_model,
            "qa_lead": self.qa_model or self.reviewer_model,
            "tester": self.qa_model or self.coder_model,
            "reviewer": self.reviewer_model or self.qa_model,
        }.get(role, "")
        h = self.harness_for(role)
        if not specific and h in ("dsh", "opencode") and (not self.model or self.model.startswith(("gpt-6", "gpt-5"))):
            from autodev import public_pool
            if self.execution_pool == "public_docker" or getattr(self, "selected_pool", "") == "public_docker":
                pool = public_pool.status()
                reasoning = role in ("pm", "director", "architect", "planner", "qa_lead")
                name = (pool["reasoning_model"] if reasoning else "") or pool["default_model"]
                return name
            if h == "dsh":
                return "deepseek-reasoner" if role in ("pm", "director", "architect", "planner") else "deepseek-chat"
            return "deepseek/deepseek-chat" if os.getenv("DEEPSEEK_API_KEY") else ""
        if h != "codex" and not specific and self.model.startswith(("gpt-6", "gpt-5")):
            return ""
        return specific or self.model

    def effective_loopforge_token(self) -> str:
        session = EXECUTION_IDENTITY.get()
        if session and session[0] == self.loopforge_url.rstrip("/"):
            return session[1]
        return self.service_token()

    signing_secret: str = field(default="", init=False, repr=False)
    legacy_signing_until: float = field(default=0, init=False, repr=False)
    legacy_signing_fingerprint: str = field(default="", init=False, repr=False)
    legacy_verification_token: str = field(default="", init=False, repr=False)

    def project_signing_key(self) -> str:
        return self.signing_secret

    def verification_keys(self) -> tuple[str, ...]:
        import time
        keys = [self.signing_secret]
        if self.legacy_signing_fingerprint and time.time() < self.legacy_signing_until:
            import hashlib
            previous = self.legacy_verification_token or self.service_token()
            if hashlib.sha256(previous.encode()).hexdigest() == self.legacy_signing_fingerprint:
                keys.append(previous)
        return tuple(key for key in keys if key)

    def service_token(self) -> str:
        token = self.loopforge_token.strip() or os.environ.get("AGENTFLOW_TOKEN", "").strip()
        if token:
            return token
        key = Path(os.environ.get("VERNAL_SERVICE_TOKEN_FILE") or
                   Path(__file__).resolve().parents[1] / "deploy/loopforge/.runner/deployment.token")
        if key.is_file() and not key.is_symlink() and key.stat().st_mode & 0o077 == 0:
            return key.read_text().strip()
        return ""


@dataclass(frozen=True)
class Workspace:
    """一个工作区 = 一个目标研发仓库 + 其 .autodev 状态目录。"""

    root: Path

    @property
    def state_dir(self) -> Path:
        return self.root / STATE_DIR

    @property
    def config_path(self) -> Path:
        return self.state_dir / "config.toml"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "state.db"

    @property
    def inputs_dir(self) -> Path:
        return self.root / "inputs"

    @property
    def worktrees_dir(self) -> Path:
        return self.state_dir / "worktrees"

    @property
    def integration_dir(self) -> Path:
        return self.state_dir / "integration"

    @property
    def runs_dir(self) -> Path:
        return self.state_dir / "runs"

    @property
    def schemas_dir(self) -> Path:
        return self.state_dir / "schemas"

    @property
    def reports_dir(self) -> Path:
        return self.state_dir / "reports"

    @property
    def catalog_path(self) -> Path:
        return self.state_dir / "doc_catalog.json"

    @property
    def prd_json_path(self) -> Path:
        return self.state_dir / "prd_spec.json"

    @property
    def prd_md_path(self) -> Path:
        return self.root / "docs" / "PRD_SPEC.md"

    @property
    def blueprint_path(self) -> Path:
        return self.state_dir / "blueprint.json"

    @property
    def plan_json_path(self) -> Path:
        return self.state_dir / "plan.json"

    @property
    def plan_md_path(self) -> Path:
        return self.state_dir / "plan.md"

    @property
    def shared_venv_dir(self) -> Path:
        return self.state_dir / "shared_venv"

    @property
    def lock_path(self) -> Path:
        return self.state_dir / "scheduler.lock"

    def ensure_dirs(self) -> None:
        for d in (
            self.state_dir,
            self.inputs_dir,
            self.worktrees_dir,
            self.integration_dir,
            self.runs_dir,
            self.schemas_dir,
            self.reports_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

    def load_settings(self) -> Settings:
        return Settings.load(self.config_path)

    @classmethod
    def find(cls, start: Path | None = None) -> "Workspace":
        cur = (start or Path.cwd()).resolve()
        for candidate in (cur, *cur.parents):
            if (candidate / STATE_DIR / "config.toml").exists():
                return cls(candidate)
        raise WorkspaceNotFound(f"在 {cur} 及其上级目录中没有找到 AutoDev 工作区，请先运行 `autodev init`")


def _resolve_cli_bin(explicit: str, cmd_name: str, fallbacks: tuple[str, ...]) -> str | None:
    if explicit:
        p = Path(explicit).expanduser()
        return str(p) if p.exists() else None
    found = shutil.which(cmd_name)
    if found:
        return found
    for candidate in fallbacks:
        if Path(candidate).exists():
            return candidate
    return None


def resolve_codex_bin(settings: Settings | None = None) -> str | None:
    return _resolve_cli_bin(settings.codex_bin if settings else "", "codex", CODEX_FALLBACK_PATHS)


def resolve_opencode_bin(settings: Settings | None = None) -> str | None:
    return _resolve_cli_bin(settings.opencode_bin if settings else "", "opencode", OPENCODE_FALLBACK_PATHS)


def resolve_codebuddy_bin(settings: Settings | None = None) -> str | None:
    return _resolve_cli_bin(settings.codebuddy_bin if settings else "", "codebuddy", CODEBUDDY_FALLBACK_PATHS)


def resolve_dsh_bin(settings: Settings | None = None) -> str | None:
    return _resolve_cli_bin(settings.dsh_bin if settings else "", "dsh", DSH_FALLBACK_PATHS)


def resolve_claude_bin(settings: Settings | None = None) -> str | None:
    return _resolve_cli_bin(settings.claude_bin if settings else "", "claude", CLAUDE_FALLBACK_PATHS)


def resolve_harness_bin(name: str, settings: Settings | None = None) -> str | None:
    norm = normalize_harness_name(name)
    if norm == "codex":
        return resolve_codex_bin(settings)
    if norm == "opencode":
        return resolve_opencode_bin(settings)
    if norm == "codebuddy":
        return resolve_codebuddy_bin(settings)
    if norm == "dsh":
        return resolve_dsh_bin(settings)
    if norm == "claude-code":
        return resolve_claude_bin(settings)
    return None


def _probe_version(bin_path: str) -> str:
    try:
        proc = subprocess.run([bin_path, "--version"], capture_output=True, text=True, timeout=4)
        txt = (proc.stdout or proc.stderr or "").strip()
        if proc.returncode == 0 and txt:
            return txt.splitlines()[0][:64]
    except Exception:
        pass
    return "installed"


def _dir_nonempty(path: Path) -> bool:
    try:
        return path.is_dir() and any(path.iterdir())
    except OSError:
        return False


def probe_agent_runtimes(settings: Settings | None = None) -> dict[str, dict[str, Any]]:
    """自动扫描宿主机上的 Codex / CodeBuddy / OpenCode / DeepSeek Harness / Claude Code 安装与凭据就绪度。"""
    s = settings or Settings()
    out: dict[str, dict[str, Any]] = {}

    codex_bin = resolve_codex_bin(s)
    if codex_bin:
        ver = _probe_version(codex_bin)
        has_codex_auth = bool(os.environ.get("OPENAI_API_KEY")) or (Path.home() / ".codex" / "auth.json").exists() or True
        out["codex"] = {
            "label": "Codex",
            "installed": True,
            "ready": has_codex_auth,
            "version": ver,
            "bin": codex_bin,
            "note": f"已就绪 ({ver})",
        }
    else:
        out["codex"] = {"label": "Codex", "installed": False, "ready": False, "version": "", "bin": "", "note": "未安装"}

    cb_bin = resolve_codebuddy_bin(s)
    if cb_bin:
        ver = _probe_version(cb_bin)
        has_cb_auth = bool(os.environ.get("CODEBUDDY_API_KEY")) or _dir_nonempty(Path.home() / ".codebuddy")
        out["codebuddy"] = {
            "label": "CodeBuddy",
            "installed": True,
            "ready": True if has_cb_auth else True,
            "version": ver,
            "bin": cb_bin,
            "note": "已就绪" if has_cb_auth else "已安装（首次使用需登录或配 CODEBUDDY_API_KEY）",
        }
    else:
        out["codebuddy"] = {"label": "CodeBuddy", "installed": False, "ready": False, "version": "", "bin": "", "note": "未安装"}

    oc_bin = resolve_opencode_bin(s)
    if oc_bin:
        ver = _probe_version(oc_bin)
        auth_file = Path.home() / ".local" / "share" / "opencode" / "auth.json"
        has_env_key = any(os.environ.get(k) for k in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"))
        try:
            has_auth = has_env_key or (auth_file.exists() and auth_file.stat().st_size > 5)
        except OSError:
            has_auth = has_env_key
        out["opencode"] = {
            "label": "OpenCode",
            "installed": True,
            "ready": has_auth,
            "version": ver,
            "bin": oc_bin,
            "note": "已就绪" if has_auth else "已安装 · 待配 DeepSeek/API Key",
        }
    else:
        out["opencode"] = {"label": "OpenCode", "installed": False, "ready": False, "version": "", "bin": "", "note": "未安装"}

    dsh_bin = resolve_dsh_bin(s)
    if dsh_bin:
        ver = _probe_version(dsh_bin)
        has_dsh_auth = bool(os.environ.get("DEEPSEEK_API_KEY")) or _dir_nonempty(Path.home() / ".dsh")
        out["dsh"] = {
            "label": "DeepSeek Harness",
            "installed": True,
            "ready": has_dsh_auth,
            "version": ver,
            "bin": dsh_bin,
            "note": "已就绪" if has_dsh_auth else "已安装 · 待配 DEEPSEEK_API_KEY",
        }
    else:
        out["dsh"] = {"label": "DeepSeek Harness", "installed": False, "ready": False, "version": "", "bin": "", "note": "未安装"}

    cl_bin = resolve_claude_bin(s)
    if cl_bin:
        ver = _probe_version(cl_bin)
        has_cl_auth = bool(os.environ.get("ANTHROPIC_API_KEY")) or (Path.home() / ".claude" / ".credentials.json").exists() or _dir_nonempty(Path.home() / ".claude")
        out["claude-code"] = {
            "label": "Claude Code",
            "installed": True,
            "ready": has_cl_auth,
            "version": ver,
            "bin": cl_bin,
            "note": "已就绪" if has_cl_auth else "已安装 · 待登录或配 ANTHROPIC_API_KEY",
        }
    else:
        out["claude-code"] = {"label": "Claude Code", "installed": False, "ready": False, "version": "", "bin": "", "note": "未安装"}

    return out


def detect_preferred_harness(settings: Settings | None = None) -> str:
    """按优先级从本机已安装的执行器中自动选择最佳默认 Harness（无需手动写死 codex）。"""
    s = settings or Settings()
    order = ("codex", "codebuddy", "opencode", "dsh", "claude-code")
    installed: list[str] = []
    for name in order:
        if resolve_harness_bin(name, s):
            installed.append(name)
    if not installed:
        return "codex"
    # 优先选已具备凭据/登录目录的 CLI；若都未配凭据则选第一个已安装的
    for name in installed:
        if name == "codex":
            return "codex"
        if name == "codebuddy" and (os.environ.get("CODEBUDDY_API_KEY") or _dir_nonempty(Path.home() / ".codebuddy")):
            return "codebuddy"
        if name == "opencode" and (
            any(os.environ.get(k) for k in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"))
            or (Path.home() / ".local" / "share" / "opencode" / "auth.json").exists()
        ):
            return "opencode"
        if name == "dsh" and (os.environ.get("DEEPSEEK_API_KEY") or _dir_nonempty(Path.home() / ".dsh")):
            return "dsh"
        if name == "claude-code" and (os.environ.get("ANTHROPIC_API_KEY") or _dir_nonempty(Path.home() / ".claude")):
            return "claude-code"
    return installed[0]


_TOOLCHAINS = {
    "python3": ["python3", "--version"],
    "node": ["node", "--version"],
    "go": ["go", "version"],
    "cargo": ["cargo", "--version"],
    "java": ["java", "-version"],
    "gcc": ["gcc", "--version"],
    "make": ["make", "--version"],
}


def detect_toolchains() -> dict[str, str]:
    found: dict[str, str] = {}
    for name, cmd in _TOOLCHAINS.items():
        if not shutil.which(cmd[0]):
            continue
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode != 0:
            continue
        text = (proc.stdout or proc.stderr).strip().splitlines()
        if text:
            found[name] = text[0].strip()
    return found
