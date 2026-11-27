#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$DIR/bin"
RUNNER_DIR="$DIR/.runner"
VERNAL_URL="${VERNAL_URL:-http://127.0.0.1:${VERNAL_PORT:-8787}}"
LOOPFORGE_URL="${LOOPFORGE_URL:-http://127.0.0.1:${AGENTFLOW_HTTP_PORT:-8090}}"
PIDFILE="$RUNNER_DIR/launcher.pid"
umask 077
mkdir -p "$BIN_DIR" "$RUNNER_DIR/workspaces"

owns_pid() {
  local p="$1" cmd
  [[ "$p" =~ ^[0-9]+$ ]] || return 1
  if ! cmd="$(ps -p "$p" -o command= 2>/dev/null)"; then
    # 无权查询进程信息时允许去重，停止操作仍需核对进程身份。
    [[ "${2:-}" == check ]] && kill -0 "$p" 2>/dev/null
    return
  fi
  [[ "$cmd" == *"$DIR/start-runner.sh --foreground"* || "$cmd" == "$BIN_DIR/agentflow-runner "* ]]
}

case "${1:-}" in
  --stop)
    if [[ -s "$PIDFILE" ]]; then
      read -r old_pid < "$PIDFILE"
      if owns_pid "$old_pid"; then
        kill "$old_pid" || true
        for _ in $(seq 1 40); do
          owns_pid "$old_pid" || break
          sleep 0.1
        done
        if owns_pid "$old_pid"; then echo "旧 Runner 尚未退出" >&2; exit 1; fi
      elif owns_pid "$old_pid" check; then
        echo "无法验证本机 Runner 的进程身份，保留进程记录" >&2
        exit 1
      fi
      [[ "$(cat "$RUNNER_DIR/binder.pid" 2>/dev/null || true)" != "$old_pid" ]] || rm -f "$RUNNER_DIR/binder.pid"
      rm -f "$PIDFILE"
    else
      for p in $(pgrep -f agentflow-runner 2>/dev/null || true); do
        if owns_pid "$p"; then kill "$p" || true; fi
      done
    fi
    exit 0 ;;
  --foreground|--probe) ;;
  "")
    if [[ -s "$PIDFILE" ]] && owns_pid "$(cat "$PIDFILE")" check; then
      echo "==> 本机 Runner 已启动或正在等待登录"
      exit 0
    fi
    # 兼容已由旧启动脚本拉起的同一 Runner。
    if pgrep -f "^$BIN_DIR/agentflow-runner " >/dev/null 2>&1; then
      echo "==> 本机 Runner 已运行"
      exit 0
    fi
    nohup "$DIR/start-runner.sh" --foreground >>"$RUNNER_DIR/runner.log" 2>&1 </dev/null &
    echo "==> 已启动本机 Runner 登录监听，日志位于 $RUNNER_DIR/runner.log"
    exit 0 ;;
  *) echo "未知 Runner 参数 $1" >&2; exit 2 ;;
esac

desktop_codex="/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"
if [[ -x "$desktop_codex" && -x "$DIR/runner-codex.sh" ]]; then
  if [[ ! -e "$BIN_DIR/codex" || ( -L "$BIN_DIR/codex" && "$(readlink "$BIN_DIR/codex")" == "$desktop_codex" ) ]]; then
    ln -sfn "../runner-codex.sh" "$BIN_DIR/codex"
  fi
fi
paths=("$HOME/.local/bin" "$HOME/.npm-global/bin" "$HOME/.opencode/bin" "$HOME/.codebuddy/bin"
       "$HOME/.dsh/bin" "$HOME/.claude/local" "$HOME/.volta/bin" "$HOME/.asdf/shims"
       "$HOME/.local/share/mise/shims" "$HOME/.local/share/fnm/aliases/default/bin"
       "$HOME/Library/Application Support/fnm/aliases/default/bin" "$HOME/.fnm/aliases/default/bin"
       "$HOME/Library/pnpm" "$HOME/.local/share/pnpm" "$HOME/.bun/bin" "$HOME/.cargo/bin" "/usr/local/go/bin" "$HOME/go/bin")
nvm_versions="${NVM_DIR:-$HOME/.nvm}/versions/node"
while IFS= read -r version; do
  paths+=("$nvm_versions/v$version/bin")
done < <(
  for p in "$nvm_versions"/v*/bin; do
    [[ -d "$p" ]] || continue
    v="${p%/bin}"
    printf '%s\n' "${v##*/v}"
  done | LC_ALL=C sort -t. -k1,1nr -k2,2nr -k3,3nr
)
for p in "${paths[@]}" "/opt/homebrew/bin" "/usr/local/bin"; do
  [[ ! -d "$p" ]] || PATH="$PATH:$p"
done
# Prefer the scoped Codex launcher when it is installed by this deployment.
if [[ -L "$BIN_DIR/codex" && "$(readlink "$BIN_DIR/codex")" == "../runner-codex.sh" ]]; then
  export PATH="$BIN_DIR:$PATH"
else
  export PATH="$PATH:$BIN_DIR"
fi
for cli in codex codebuddy opencode dsh claude; do
  if cli_path="$(command -v "$cli")"; then
    version="$("$cli_path" --version </dev/null 2>/dev/null | head -n 1 || true)"
    if [[ -z "$version" && "$cli" == opencode ]]; then
      # Some installations migrate their state even for --version. Probe with a disposable XDG home.
      probe="$(mktemp -d)"
      version="$(HOME="$probe" XDG_DATA_HOME="$probe/data" XDG_CACHE_HOME="$probe/cache" XDG_CONFIG_HOME="$probe/config" XDG_STATE_HOME="$probe/state" "$cli_path" --version 2>/dev/null | head -n 1 || true)"
      rm -rf "$probe"
    fi
    cred="登录状态待 CLI 确认"
    case "$cli" in
      codex) [[ -z "${OPENAI_API_KEY:-}" && ! -s "$HOME/.codex/auth.json" ]] || cred="检测到个人凭据" ;;
      claude) [[ -z "${ANTHROPIC_API_KEY:-}" && ! -s "$HOME/.claude/.credentials.json" ]] || cred="检测到个人凭据" ;;
      *) cred="个人 Key 由 LoopForge 确认，见网页执行池设置" ;;
    esac
    echo "==> $cli: $cli_path ${version:-版本未知} ($cred)"
  else echo "==> $cli: 未检测到"; fi
done

if [[ "${1:-}" == --probe ]]; then
  os="$(uname -s | tr '[:upper:]' '[:lower:]')"
  arch="$(uname -m)"
  [[ "$arch" != aarch64 ]] || arch=arm64
  [[ "$arch" != x86_64 ]] || arch=amd64
  for bin_item in "$BIN_DIR/agentflow-runner" "$BIN_DIR/agentflow-gate-mcp-${os}-${arch}"; do
    if [[ -x "$bin_item" ]]; then
      digest="$(shasum -a 256 "$bin_item" 2>/dev/null | awk '{print $1}' || true)"
      echo "==> binary: $(basename "$bin_item") sha256=${digest:-unknown}"
    else
      echo "==> binary: $(basename "$bin_item") 未下载"
    fi
  done
  exit 0
fi

# 硬链接原子发布完整 PID，避免并行启动重复签发绑定码。
pid_tmp="$(mktemp "$RUNNER_DIR/launcher.XXXXXX")"
echo "$$" > "$pid_tmp"
if ! ln "$pid_tmp" "$PIDFILE" 2>/dev/null; then
  if owns_pid "$(cat "$PIDFILE" 2>/dev/null || true)" check; then rm -f "$pid_tmp"; exit 0; fi
  rm -f "$PIDFILE"
  ln "$pid_tmp" "$PIDFILE" || { rm -f "$pid_tmp"; exit 0; }
fi
rm -f "$pid_tmp"
clear_pidfiles() {
  local file
  for file in "$PIDFILE" "$RUNNER_DIR/binder.pid"; do
    [[ "$(cat "$file" 2>/dev/null || true)" != "$$" ]] || rm -f "$file"
  done
}
trap clear_pidfiles EXIT

os="$(uname -s | tr '[:upper:]' '[:lower:]')"
arch="$(uname -m)"
[[ "$arch" != aarch64 ]] || arch=arm64
[[ "$arch" != x86_64 ]] || arch=amd64
gate="$BIN_DIR/agentflow-gate-mcp-${os}-${arch}"
for component in agentflow-runner agentflow-gate-mcp; do
  target="$BIN_DIR/agentflow-runner"
  [[ "$component" != agentflow-gate-mcp ]] || target="$gate"
  if [[ ! -x "$target" ]]; then
    curl -fsS --retry 3 "$LOOPFORGE_URL/runners/$component?os=$os&arch=$arch" -o "$target.download"
    chmod 700 "$target.download"
    mv "$target.download" "$target"
  fi
done

export AGENTFLOW_GATE_MCP_BINARY="$gate"
token="session-reused"
if [[ ! -s "$RUNNER_DIR/state.json" ]]; then
  if [[ -n "${VERNAL_BIND_CODE:-}" ]]; then
    token="$VERNAL_BIND_CODE"
  else
    source "$DIR/wait-and-bind-runner.sh"
    wait_for_runner_binding
    rm -f "$RUNNER_DIR/binder.pid"
  fi
fi
unset VERNAL_BIND_CODE
exec "$BIN_DIR/agentflow-runner" -server "$LOOPFORGE_URL" -token "$token" -state "$RUNNER_DIR/state.json" \
  -workspace "$RUNNER_DIR/workspaces" -name "Vernal-$(hostname -s)-Runner" \
  -concurrency "${VERNAL_RUNNER_CONCURRENCY:-3}" -auto-update=false
