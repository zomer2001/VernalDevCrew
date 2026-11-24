#!/usr/bin/env bash
set -u
cd "$(dirname "$0")"
port="${1:-8787}"
[[ ! -f .env ]] || source .env
bad=0
if docker info >/dev/null 2>&1; then
  echo "Docker 已连接"
  for svc in mysql loopforge gitea vernal; do
    id="$(docker compose --profile full ps -q "$svc")"
    status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$id" 2>/dev/null)"
    echo "$svc ${status:-未运行}"
    [[ "$status" == healthy || "$status" == running ]] || bad=1
  done
  docker run --rm --network none --entrypoint sh "${VERNAL_SANDBOX_IMAGE:-autodev-runtime-sandbox:latest}" -ec 'dsh --version && opencode --version' || bad=1
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
