#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

MODE="docker"
BUILD_FLAG=""
STOP_ONLY=0
PORT="${VERNAL_PORT:-8787}"
BIND_HOST="${VERNAL_BIND:-127.0.0.1}"
EXTRA_DEV_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dev)
      MODE="dev"
      shift
      ;;
    --docker)
      MODE="docker"
      shift
      ;;
    --build)
      BUILD_FLAG="--build"
      shift
      ;;
    --stop)
      STOP_ONLY=1
      shift
      ;;
    --host)
      BIND_HOST="$2"
      shift 2
      ;;
    --port|-p)
      PORT="$2"
      shift 2
      ;;
    *)
      EXTRA_DEV_ARGS+=("$1")
      shift
      ;;
  esac
done

COMPOSE_FILE="$ROOT/deploy/loopforge/compose.yaml"

if [[ "$STOP_ONLY" == "1" ]]; then
  echo "==> 正在停止 Vernal 与底层容器..."
  docker compose -f "$COMPOSE_FILE" --profile full stop || true
  pkill -f "deploy/loopforge/bin/agentflow-runner" >/dev/null 2>&1 || true
  echo "==> 已停止全部服务（数据均已保留）"
  exit 0
fi

if ! curl -fsS -m 2 http://127.0.0.1:8090/api/health >/dev/null 2>&1; then
  echo "==> LoopForge 执行底座未就绪，正在通过 ./deploy/loopforge/up.sh 启动..."
  ./deploy/loopforge/up.sh
fi

if [[ "$MODE" == "dev" ]]; then
  ./deploy/loopforge/start-runner.sh || true
  echo "==> [开发模式] 启动本地 Vernal: http://${BIND_HOST}:${PORT}"
  exec "$ROOT/.venv/bin/python" -m autodev.cli serve --host "$BIND_HOST" --port "$PORT" ${EXTRA_DEV_ARGS[@]+"${EXTRA_DEV_ARGS[@]}"}
fi

# 默认纯容器模式：同事 / 麒麟宿主机零 Python 依赖，只需 Docker + 自己的 Harness
if [[ -z "$BUILD_FLAG" ]] && ! docker image inspect autodev-runtime-vernal >/dev/null 2>&1; then
  BUILD_FLAG="--build"
fi

echo "==> 正在通过 Docker 启动 Vernal 统一平台（宿主机免装 Python）..."
(
  set -a
  source "$ROOT/deploy/loopforge/.env"
  set +a
  export VERNAL_PORT="$PORT"
  export VERNAL_BIND="$BIND_HOST"
  export VERNAL_PUBLIC_URL="${VERNAL_PUBLIC_URL:-http://127.0.0.1:${PORT}}"
  docker compose -f "$COMPOSE_FILE" --profile full up -d $BUILD_FLAG vernal
)

echo "==> 等待 Vernal 服务就绪..."
for _ in $(seq 1 40); do
  if curl -fsS -m 2 "http://127.0.0.1:${PORT}/vernal-api/state" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

VERNAL_PORT="$PORT" ./deploy/loopforge/start-runner.sh || true

echo ""
echo "=========================================================="
echo "  Vernal 平台已启动：http://127.0.0.1:${PORT}"
echo "  （宿主机免 Python · 规划与研发全量走 LoopForge + 本机 Runner）"
echo "  停止服务可执行：./vernal.sh --stop"
echo "=========================================================="
