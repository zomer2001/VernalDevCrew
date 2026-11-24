#!/usr/bin/env bash
# The launcher keeps this PID through binding and exec, so stopping it also stops the wait.
wait_for_runner_binding() {
  echo "$$" > "$RUNNER_DIR/binder.pid"
  echo "等待首次网页登录" >> "$RUNNER_DIR/binder.log"
  local deadline=$((SECONDS + 1800)) response
  local -a header
  while true; do
    if (( SECONDS >= deadline )); then echo "等待首次登录超时，请用 ./vernal.sh --restart-runner 重新监听" >> "$RUNNER_DIR/binder.log"; return 1; fi
    if [[ -n "${VERNAL_BIND_CODE:-}" ]]; then token="$VERNAL_BIND_CODE"; break; fi
    # 允许端口映射下的宿主机证明来源，不把任意网桥地址视为本机。
    header=()
    case "$VERNAL_URL" in
      http://127.0.0.1:*|http://localhost:*)
        [[ ! -s "$RUNNER_DIR/host.header" ]] || header=(-H "@$RUNNER_DIR/host.header") ;;
    esac
    if token="$(curl -fsS -m 5 ${header[@]+"${header[@]}"} -X POST "$VERNAL_URL/vernal-api/runner/bind-code?format=raw" 2>/dev/null)" && [[ -n "$token" ]]; then break; fi
    if [[ -n "${AGENTFLOW_TOKEN:-}" ]]; then
      response="$(curl -fsS -m 5 -X POST "$LOOPFORGE_URL/api/runners/bind-codes" \
        -H "Authorization: Bearer $AGENTFLOW_TOKEN" -H "Origin: $LOOPFORGE_URL" -H 'Content-Type: application/json' -d '{}' 2>/dev/null || true)"
      token="$(printf '%s' "$response" | sed -n 's/.*"code"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
      [[ -z "$token" ]] || break
    fi
    sleep 3
  done
  echo "已取得一次性绑定码，正在启动 Runner" >> "$RUNNER_DIR/binder.log"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  exec "$(dirname "$0")/start-runner.sh"
fi
