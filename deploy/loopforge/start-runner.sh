#!/usr/bin/env bash
set -euo pipefail

FOREGROUND=0
[[ "${1:-}" == "--foreground" ]] && FOREGROUND=1

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$DIR/bin"
RUNNER_DIR="$DIR/.runner"
VERNAL_URL="${VERNAL_URL:-http://127.0.0.1:${VERNAL_PORT:-8787}}"
LOOPFORGE_URL="${LOOPFORGE_URL:-http://127.0.0.1:8090}"
RUNNER_CONCURRENCY="${VERNAL_RUNNER_CONCURRENCY:-3}"
mkdir -p "$BIN_DIR" "$RUNNER_DIR/workspaces"

if [[ -x "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex" && ! -e "$BIN_DIR/codex" ]]; then
  ln -sf "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex" "$BIN_DIR/codex"
fi

EXTRA_PATHS=(
  "$HOME/.local/bin"
  "$HOME/.npm-global/bin"
  "$HOME/.opencode/bin"
  "$HOME/.codebuddy/bin"
  "$HOME/.dsh/bin"
  "$HOME/.claude/local"
  "/opt/homebrew/bin"
  "/usr/local/bin"
)
for p in "${EXTRA_PATHS[@]}"; do
  [[ -d "$p" ]] && export PATH="$p:$PATH"
done
export PATH="$BIN_DIR:$PATH"

DETECTED_CLIS=()
for cli in codex codebuddy opencode dsh claude; do
  if command -v "$cli" >/dev/null 2>&1; then
    DETECTED_CLIS+=("$cli")
  fi
done
if [[ "${#DETECTED_CLIS[@]}" -gt 0 ]]; then
  echo "==> 已扫描到本机可用 Harness: ${DETECTED_CLIS[*]}"
else
  echo "==> 提示: 本机暂未扫描到 codex/codebuddy/opencode/dsh/claude，可在宿主机安装或在网页配 API Key 走容器沙箱"
fi

OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
ARCH="$(uname -m)"
[[ "$ARCH" == "aarch64" ]] && ARCH="arm64"
[[ "$ARCH" == "x86_64" ]] && ARCH="amd64"

if [[ ! -x "$BIN_DIR/agentflow-runner" ]]; then
  curl -fsSL "${LOOPFORGE_URL}/runners/agentflow-runner?os=${OS}&arch=${ARCH}" -o "$BIN_DIR/agentflow-runner"
  chmod +x "$BIN_DIR/agentflow-runner"
fi

GATE_BIN="$BIN_DIR/agentflow-gate-mcp-${OS}-${ARCH}"
if [[ ! -x "$GATE_BIN" ]]; then
  curl -fsSL "${LOOPFORGE_URL}/runners/agentflow-gate-mcp?os=${OS}&arch=${ARCH}" -o "$GATE_BIN"
  chmod +x "$GATE_BIN"
fi

if [[ "$FOREGROUND" == "0" ]] && pgrep -f "$BIN_DIR/agentflow-runner" >/dev/null 2>&1; then
  echo "==> 本机执行器 agentflow-runner 已在运行"
  exit 0
fi

BOOTSTRAP_TOKEN="session-reused"
if [[ ! -s "$RUNNER_DIR/state.json" ]]; then
  BOOTSTRAP_TOKEN="$(curl -fsS -m 5 -X POST "${VERNAL_URL}/vernal-api/runner/bind-code?format=raw" 2>/dev/null || true)"
  if [[ -z "$BOOTSTRAP_TOKEN" && -n "${AGENTFLOW_TOKEN:-}" ]]; then
    RAW_JSON="$(curl -fsS -m 5 -X POST "${LOOPFORGE_URL}/api/runners/bind-codes" \
      -H "Authorization: Bearer ${AGENTFLOW_TOKEN}" \
      -H "Origin: ${LOOPFORGE_URL}" \
      -H "Content-Type: application/json" \
      -d '{}' 2>/dev/null || true)"
    BOOTSTRAP_TOKEN="$(printf '%s' "$RAW_JSON" | sed -n 's/.*"code"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
  fi

  if [[ -z "$BOOTSTRAP_TOKEN" ]]; then
    echo "==> 尚未完成首次登录绑定：请先打开 ${VERNAL_URL} 注册/登录账号，然后重新运行 ./vernal.sh 自动挂载本机执行器"
    exit 0
  fi
fi

export AGENTFLOW_GATE_MCP_BINARY="$GATE_BIN"
HOST_SHORT="$(hostname -s 2>/dev/null || echo Local)"

CMD=(
  "$BIN_DIR/agentflow-runner"
  -server "$LOOPFORGE_URL"
  -token "$BOOTSTRAP_TOKEN"
  -state "$RUNNER_DIR/state.json"
  -workspace "$RUNNER_DIR/workspaces"
  -name "Vernal-${HOST_SHORT}-Runner"
  -concurrency "$RUNNER_CONCURRENCY"
  -auto-update=false
)

if [[ "$FOREGROUND" == "1" ]]; then
  exec "${CMD[@]}"
fi

nohup "${CMD[@]}" >"$RUNNER_DIR/runner.log" 2>&1 &
echo "==> 已拉起本机多引擎执行器 Vernal-${HOST_SHORT}-Runner (PID $!，并发 ${RUNNER_CONCURRENCY}，日志: $RUNNER_DIR/runner.log)"
