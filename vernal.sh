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
STAMP_FILE="$ROOT/deploy/loopforge/.vernal-build.stamp"

_source_fingerprint() {
  # 零 Python 源码指纹：检测 Dockerfile / pyproject.toml / autodev 下文件变化，改完代码自动触发重编
  if command -v shasum >/dev/null 2>&1; then
    find "$ROOT/autodev" "$ROOT/Dockerfile" "$ROOT/pyproject.toml" -type f \
      ! -path "*/__pycache__/*" ! -name "*.pyc" ! -name ".DS_Store" \
      | LC_ALL=C sort \
      | xargs shasum 2>/dev/null \
      | shasum | awk '{print $1}'
  elif command -v sha256sum >/dev/null 2>&1; then
    find "$ROOT/autodev" "$ROOT/Dockerfile" "$ROOT/pyproject.toml" -type f \
      ! -path "*/__pycache__/*" ! -name "*.pyc" ! -name ".DS_Store" \
      | LC_ALL=C sort \
      | xargs sha256sum 2>/dev/null \
      | sha256sum | awk '{print $1}'
  else
    find "$ROOT/autodev" "$ROOT/Dockerfile" "$ROOT/pyproject.toml" -type f \
      ! -path "*/__pycache__/*" ! -name "*.pyc" ! -name ".DS_Store" \
      | LC_ALL=C sort \
      | xargs cksum 2>/dev/null \
      | cksum | awk '{print $1}'
  fi
}

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

# 默认纯容器模式：零 Python 宿主机启动；若源码相比上次镜像构建有变动，自动重编避免跑旧版代码
CUR_FP="$(_source_fingerprint || true)"
PREV_FP=""
[[ -f "$STAMP_FILE" ]] && PREV_FP="$(cat "$STAMP_FILE" 2>/dev/null || true)"

if [[ -z "$BUILD_FLAG" ]]; then
  if ! docker image inspect autodev-runtime-vernal >/dev/null 2>&1; then
    BUILD_FLAG="--build"
  elif [[ -n "$CUR_FP" && "$CUR_FP" != "$PREV_FP" ]]; then
    echo "==> 检测到源码或 Dockerfile 有更新，自动重建 Vernal 镜像..."
    BUILD_FLAG="--build"
  fi
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
if [[ -n "$BUILD_FLAG" && -n "$CUR_FP" ]]; then
  printf '%s\n' "$CUR_FP" >"$STAMP_FILE"
fi

echo "==> 等待 Vernal 服务就绪..."
GATEWAY_OK=0
for _ in $(seq 1 40); do
  if curl -fsS -m 2 "http://127.0.0.1:${PORT}/vernal-api/state" >/dev/null 2>&1; then
    GATEWAY_OK=1
    break
  fi
  sleep 1
done

if [[ "$GATEWAY_OK" != "1" ]]; then
  echo "" >&2
  echo "==========================================================" >&2
  echo "  [❌] 网关启动失败：http://127.0.0.1:${PORT} 未在 40 秒内响应" >&2
  echo "  请查看容器日志排查：" >&2
  echo "    docker compose -f deploy/loopforge/compose.yaml --profile full logs --tail 50 vernal" >&2
  echo "==========================================================" >&2
  exit 1
fi

RUNNER_EXIT=0
VERNAL_PORT="$PORT" ./deploy/loopforge/start-runner.sh || RUNNER_EXIT=$?

# 核对 LoopForge 控制面是否真正收到 Runner 在线心跳（不只看本机进程）
HEALTH_RAW=""
for _ in $(seq 1 5); do
  HEALTH_RAW="$(curl -fsS -m 3 "http://127.0.0.1:${PORT}/vernal-api/runner/health?format=raw" 2>/dev/null || true)"
  [[ "$HEALTH_RAW" == online:* ]] && break
  [[ "$HEALTH_RAW" == unbound:* ]] && break
  sleep 1
done

HEALTH_STATE="${HEALTH_RAW%%:*}"
HEALTH_REST="${HEALTH_RAW#*:}"
HEALTH_CNT="${HEALTH_REST%%:*}"
HEALTH_NAMES="${HEALTH_REST#*:}"

echo ""
echo "=========================================================="
echo "  [✅] 网关可访问：http://127.0.0.1:${PORT}"
if [[ "$RUNNER_EXIT" != "0" ]]; then
  echo "  [❌] 本机执行机：启动脚本异常退出（退出码 ${RUNNER_EXIT}），请查看 deploy/loopforge/.runner/runner.log"
elif [[ "$HEALTH_STATE" == "online" ]]; then
  echo "  [✅] 本机执行机：LoopForge 已收到真实心跳（${HEALTH_CNT} 台在线：${HEALTH_NAMES}）"
elif [[ "$HEALTH_STATE" == "unbound" ]]; then
  echo "  [⚠️] 本机执行机：尚未完成首次网页登录绑定（打开上方网址登录后，再执行一次 ./vernal.sh 自动挂载）"
else
  echo "  [⚠️] 本机执行机：LoopForge 暂未收到在线心跳（请检查 deploy/loopforge/.runner/runner.log 或网页「设置 → 执行机器」）"
fi
echo "  停止服务可执行：./vernal.sh --stop"
echo "=========================================================="

if [[ "$RUNNER_EXIT" != "0" ]]; then
  exit "$RUNNER_EXIT"
fi
