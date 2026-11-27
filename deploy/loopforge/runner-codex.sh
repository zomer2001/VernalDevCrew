#!/usr/bin/env bash
# Invoked as deploy/loopforge/bin/codex by the personal macOS Runner.
set -eu
real_codex=/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex
bin_dir="$(cd "$(dirname "$0")" && pwd -P)"
runs_dir="$(dirname "$bin_dir")/.runner/workspaces/runs"
workdir="$(pwd -P)"
gate="$bin_dir/agentflow-gate-mcp-darwin-$(uname -m)"
[[ "$(uname -m)" != x86_64 ]] || gate="$bin_dir/agentflow-gate-mcp-darwin-amd64"

args=("$@")
if [[ ${1:-} == exec && "$workdir" == "$runs_dir"/run-*/workspace && -f "$gate" && ! -L "$gate" && -x "$gate" ]]; then
  # Only grant the scoped tool exception for a single unambiguous native helper binding.
  bindings=0
  for arg in "${args[@]}"; do
    case "$arg" in
      mcp_servers.agentflow-gate.command=*) bindings=$((bindings+1)) ;;
      mcp_servers=*|mcp_servers.agentflow-gate=*) exec "$real_codex" "$@" ;;
    esac
  done
  [[ "$bindings" == 1 ]] || exec "$real_codex" "$@"
  # Bind the exception to the installed helper, even if the task edits its staged copy later.
  for ((i=1; i<${#args[@]}; i++)); do
    [[ ${args[i-1]} == -c && ${args[i]} == mcp_servers.agentflow-gate.command=* ]] || continue
    staged="${args[i]#*=}"
    staged="${staged#\"}"; staged="${staged%\"}"
    [[ "$staged" == /* ]] || staged="$workdir/$staged"
    [[ "$staged" == "$workdir"/.agentflow/config/*/gate-mcp && -f "$staged" && ! -L "$staged" ]] || break
    cmp -s "$staged" "$gate" || break
    toml_gate="${gate//\\/\\\\}"
    toml_gate="${toml_gate//\"/\\\"}"
    args[i]="mcp_servers.agentflow-gate.command=\"$toml_gate\""
    exec "$real_codex" -c 'mcp_servers.agentflow-gate.tools.submit_gate_result.approval_mode="approve"' "${args[@]}"
  done
fi
exec "$real_codex" "$@"
