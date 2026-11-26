#!/usr/bin/env bash
set -u
cd "$(dirname "$0")"
port="${1:-8787}"
caller_ci="${VERNAL_ENABLE_CI:-}"
[[ ! -f .env ]] || source .env
[[ -z "$caller_ci" ]] || VERNAL_ENABLE_CI="$caller_ci"
bad=0
if docker info >/dev/null 2>&1; then
  echo "Docker 已连接"
  for svc in mysql loopforge gitea vernal; do
    id="$(docker compose --profile full ps -q "$svc")"
    status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$id" 2>/dev/null)"
    echo "$svc ${status:-未运行}"
    [[ "$status" == healthy || "$status" == running ]] || bad=1
  done
  mysql_id="$(docker compose --profile full ps -q mysql)"
  if [[ -n "$mysql_id" ]]; then
    adapter_col="$(docker exec "$mysql_id" sh -c 'MYSQL_PWD="$MYSQL_PASSWORD" mysql -N -B -u "$MYSQL_USER" "$MYSQL_DATABASE" -e "SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='\''run_telemetry'\'' AND COLUMN_NAME='\''adapter'\'';"' 2>/dev/null || true)"
    if [[ "$adapter_col" == "1" ]]; then
      echo "loopforge-schema run_telemetry.adapter 就绪"
    elif [[ "$adapter_col" == "0" ]]; then
      echo "loopforge-schema 不兼容：服务存活但 run_telemetry 缺少 adapter 列（上游详情页 telemetry?adapter= 筛选会触发 MySQL 1054）"
    fi
  fi
  if [[ "${VERNAL_ENABLE_CI:-0}" == "1" ]]; then
    if [[ -s .runner/pool/act-runner-token ]]; then
      echo "act-runner-token 已就绪"
    else
      echo "act-runner-token 缺失"
      bad=1
    fi
    ci_id="$(docker compose --profile ci ps -q gitea-runner)"
    ci_st="$(docker inspect -f '{{.State.Status}}' "$ci_id" 2>/dev/null)"
    echo "gitea-runner ${ci_st:-未运行}"
    [[ "$ci_st" == running ]] || bad=1
  fi
  docker run --rm --network none "${VERNAL_SANDBOX_IMAGE:-autodev-runtime-sandbox:latest}" sh -ec 'dsh --version && opencode --version && vernal-quality --help >/dev/null' || bad=1
else echo "Docker 守护进程无法访问"; bad=1; fi
url="http://127.0.0.1:$port/vernal-api"
curl -fsS -m 4 "$url/runtime/public-pool?format=doctor" || bad=1
echo
header=()
[[ ! -s .runner/host.header ]] || header=(-H "@.runner/host.header")
health="$(curl -fsS -m 4 ${header[@]+"${header[@]}"} "$url/runner/health?format=raw" 2>/dev/null || true)"
echo "Runner 心跳 ${health:-无法读取}"
if [[ -s .runner/launcher.pid ]] && kill -0 "$(cat .runner/launcher.pid)" 2>/dev/null; then echo "Runner 进程已启动"; else echo "Runner 进程未运行"; fi
./start-runner.sh --probe
exit "$bad"
