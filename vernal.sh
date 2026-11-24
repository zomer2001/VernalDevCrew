#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

MODE="docker"
BUILD_FLAG=""
STOP_ONLY=0
PACK=0
VERNAL_OFFLINE=0
PORT="${VERNAL_PORT:-8787}"
BIND_HOST="${VERNAL_BIND:-127.0.0.1}"
EXTRA_DEV_ARGS=()
PROJECTS_DIR=""
LAUNCHER_DIR="$ROOT/deploy/loopforge/.runner"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mount)
      [[ $# -ge 2 && -d "$2" ]] || { echo "--mount 需要一个已存在的目录" >&2; exit 2; }
      PROJECTS_DIR="$(cd "$2" && pwd -P)"
      shift 2
      ;;
    --doctor|--restart-runner)
      RUNNER_ACTION="$1"; shift ;;
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
    --pack)
      PACK=1
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

_fingerprint() {
  local -a checksum
  if command -v shasum >/dev/null 2>&1; then
    checksum=(shasum)
  elif command -v sha256sum >/dev/null 2>&1; then
    checksum=(sha256sum)
  else
    checksum=(cksum)
  fi
  find "$@" \
    -type f ! -path '*/__pycache__/*' ! -name '*.pyc' ! -name '.DS_Store' \
    -exec "${checksum[@]}" {} + | LC_ALL=C sort | "${checksum[@]}" | awk '{print $1}'
}

_source_fingerprint() {
  _fingerprint autodev Dockerfile pyproject.toml deploy/loopforge/start-runner.sh deploy/loopforge/wait-and-bind-runner.sh
}

if [[ "${RUNNER_ACTION:-}" == "--doctor" ]]; then
  exec "$ROOT/deploy/loopforge/doctor.sh" "$PORT"
elif [[ "${RUNNER_ACTION:-}" == "--restart-runner" ]]; then
  "$ROOT/deploy/loopforge/start-runner.sh" --stop
  VERNAL_PORT="$PORT" exec "$ROOT/deploy/loopforge/start-runner.sh"
fi

if [[ "$STOP_ONLY" == "1" ]]; then
  echo "==> 正在停止 Vernal 与底层容器..."
  "$ROOT/deploy/loopforge/start-runner.sh" --stop
  docker compose -f "$COMPOSE_FILE" --profile full stop || true
  echo "==> 已停止全部服务（数据均已保留）"
  exit 0
fi

if [[ -f deploy/loopforge/images.lock || $PACK == 1 ]]; then
  source "$ROOT/deploy/loopforge/offline.sh"
  if [[ $PACK == 1 ]]; then
    if [[ -f deploy/loopforge/images.lock ]]; then
      echo "此目录已是离线发行包，直接运行 ./vernal.sh 即可" >&2
      exit 2
    fi
    if [[ ! -f deploy/loopforge/.env || ! -s "$STAMP_FILE" || "$(cat "$STAMP_FILE")" != "$(_source_fingerprint)" ]] ||
       ! docker image inspect autodev-runtime-vernal >/dev/null 2>&1; then
      "$ROOT/vernal.sh"
    fi
    package_images
    exit 0
  fi
  if [[ "$MODE" == dev || -n "$BUILD_FLAG" ]]; then
    echo "从源码目录执行开发或重新构建。离线发行包直接运行 ./vernal.sh" >&2
    exit 2
  fi
  restore_images
fi

mkdir -p "$LAUNCHER_DIR"
if [[ -n "$PROJECTS_DIR" ]]; then
  printf '%s\n' "$PROJECTS_DIR" > "$LAUNCHER_DIR/projects-dir"
elif [[ -s "$LAUNCHER_DIR/projects-dir" ]]; then
  IFS= read -r PROJECTS_DIR < "$LAUNCHER_DIR/projects-dir"
fi
if [[ -n "$PROJECTS_DIR" ]]; then
  [[ -d "$PROJECTS_DIR" ]] || { echo "上次挂载的目录已经移动，请用 --mount 指定目录" >&2; exit 2; }
  export VERNAL_HOST_PROJECTS_DIR="$PROJECTS_DIR"
fi

personal=0
case "$BIND_HOST" in 127.0.0.1|localhost|::1) personal=1 ;; esac
if [[ "$personal" == 1 ]]; then
  header_file="$LAUNCHER_DIR/host.header"
  if [[ ! -s "$header_file" ]]; then
    key="$(LC_ALL=C od -An -tx1 -N32 /dev/urandom | tr -d ' \n')"
    tmp_header="$(mktemp "$LAUNCHER_DIR/header.XXXXXX")"
    printf 'X-Vernal-Runner-Secret: %s\n' "$key" > "$tmp_header"
    ln "$tmp_header" "$header_file" 2>/dev/null || [[ -s "$header_file" ]]
    rm -f "$tmp_header"
  fi
  IFS= read -r header < "$header_file"
  export VERNAL_RUNNER_BOOTSTRAP_SECRET="${header#*: }"
else
  export VERNAL_RUNNER_BOOTSTRAP_SECRET=""
fi

if [[ ! -f deploy/loopforge/.env ]] || ! curl -fsS -m 2 http://127.0.0.1:8090/api/health >/dev/null 2>&1; then
  echo "==> LoopForge 执行底座未就绪，正在通过 ./deploy/loopforge/up.sh 启动..."
  ./deploy/loopforge/up.sh
fi

if [[ "$MODE" == "dev" ]]; then
  if [[ "$personal" == 1 ]]; then VERNAL_PORT="$PORT" ./deploy/loopforge/start-runner.sh; fi
  echo "==> [开发模式] 启动本地 Vernal: http://${BIND_HOST}:${PORT}"
  exec "$ROOT/.venv/bin/python" -m autodev.cli serve --host "$BIND_HOST" --port "$PORT" ${EXTRA_DEV_ARGS[@]+"${EXTRA_DEV_ARGS[@]}"}
fi

# 默认纯容器模式：零 Python 宿主机启动；若源码相比上次镜像构建有变动，自动重编避免跑旧版代码
CUR_FP=""
if [[ ${VERNAL_OFFLINE:-0} != 1 ]]; then CUR_FP="$(_source_fingerprint)"; fi
PREV_FP=""
[[ -f "$STAMP_FILE" ]] && PREV_FP="$(cat "$STAMP_FILE" 2>/dev/null || true)"

if [[ ${VERNAL_OFFLINE:-0} != 1 && -z "$BUILD_FLAG" ]]; then
  if ! docker image inspect autodev-runtime-vernal >/dev/null 2>&1; then
    BUILD_FLAG="--build"
  elif [[ -n "$CUR_FP" && "$CUR_FP" != "$PREV_FP" ]]; then
    base=""
    if [[ -n "$PREV_FP" && -z "${VERNAL_PYTHON_IMAGE:-}" ]]; then
      source "$ROOT/deploy/loopforge/update-gateway.sh"
      base="$(compatible_gateway_base)" || base=""
    fi
    if [[ -n "$base" ]]; then
      echo "==> 使用本机已有运行环境，离线更新 Vernal 网关代码..."
      update_gateway_code "$base"
    else
      echo "==> 需要构建 Vernal 网关运行环境..."
      BUILD_FLAG="--build"
    fi
  fi
fi

echo "==> 正在通过 Docker 启动 Vernal 统一平台（宿主机免装 Python）..."
(
  set -a
  source "$ROOT/deploy/loopforge/.env"
  set +a
  export VERNAL_PORT="$PORT"
  export VERNAL_BIND="$BIND_HOST"
  public_host="$BIND_HOST"
  if [[ "$public_host" == "0.0.0.0" || "$public_host" == "::" ]]; then
    public_host=""
    if command -v ip >/dev/null 2>&1; then
      public_host="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<NF;i++) if($i=="src"){print $(i+1);exit}}' || true)"
    elif command -v ipconfig >/dev/null 2>&1; then
      iface="$(route -n get default 2>/dev/null | awk '/interface:/{print $2; exit}' || true)"
      [[ -z "$iface" ]] || public_host="$(ipconfig getifaddr "$iface" 2>/dev/null || true)"
    fi
    public_host="${public_host:-$(hostname)}"
  fi
  [[ "$public_host" != *:* ]] || public_host="[$public_host]"
  export VERNAL_PUBLIC_URL="${VERNAL_PUBLIC_URL:-http://${public_host}:${PORT}}"
  # 默认仅挂载项目工作区目录，不暴露整个用户主目录；需原地接管外部目录时显式传 VERNAL_HOST_PROJECTS_DIR=~/你的代码目录
  export VERNAL_HOST_PROJECTS_DIR="${PROJECTS_DIR:-${VERNAL_HOST_PROJECTS_DIR:-$ROOT/workspaces}}"
  "$ROOT/deploy/loopforge/build-sandbox.sh" || echo "公用沙箱构建失败，个人 Runner 仍可使用" >&2
  "$ROOT/deploy/loopforge/ensure-gitea.sh" || echo "Gitea 尚未就绪，请在体检中查看服务状态" >&2
  if [[ -z "$BUILD_FLAG" ]]; then
    docker compose -f "$COMPOSE_FILE" --profile full up -d --no-build --pull never vernal
  elif [[ -n "$BUILD_FLAG" && -z "${VERNAL_PYTHON_IMAGE:-}" ]]; then
    # Remember the registry that successfully built this checkout on this machine.
    registry_file="$LAUNCHER_DIR/python-image"
    if [[ -s "$registry_file" ]]; then
      IFS= read -r VERNAL_PYTHON_IMAGE < "$registry_file"
      export VERNAL_PYTHON_IMAGE
    fi
    build_log="$(mktemp "$LAUNCHER_DIR/build.XXXXXXXX")"
    trap 'rm -f "$build_log"' EXIT
    if ! docker compose -f "$COMPOSE_FILE" --profile full up -d --build vernal 2>&1 | tee "$build_log"; then
      if [[ "${VERNAL_PYTHON_IMAGE:-python:3.12-slim}" != python:3.12-slim ]] ||
         ! grep -Eq '(failed to resolve source metadata|failed to fetch anonymous token|failed to do request).*(docker.io|python:3.12-slim)' "$build_log"; then
        exit 1
      fi
      echo "Docker Hub 无法提供 Python 基础镜像，改用 Docker 官方 ECR 镜像"
      export VERNAL_PYTHON_IMAGE=public.ecr.aws/docker/library/python:3.12-slim
      docker compose -f "$COMPOSE_FILE" --profile full up -d --build vernal
    fi
  else
    docker compose -f "$COMPOSE_FILE" --profile full up -d $BUILD_FLAG vernal
  fi
  if [[ -n "$BUILD_FLAG" && -n "${VERNAL_PYTHON_IMAGE:-}" ]]; then
    printf '%s\n' "$VERNAL_PYTHON_IMAGE" > "$LAUNCHER_DIR/python-image"
  fi
)
if [[ -n "$CUR_FP" && "$CUR_FP" != "$PREV_FP" ]]; then
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

if [[ "$personal" != 1 ]]; then
  echo "==> 团队网关已启动，本机执行器可在登录后由成员绑定"
  exit 0
fi
RUNNER_EXIT=0
VERNAL_PORT="$PORT" ./deploy/loopforge/start-runner.sh || RUNNER_EXIT=$?

# 核对 LoopForge 控制面是否真正收到 Runner 在线心跳（不只看本机进程）
HEALTH_RAW=""
for _ in $(seq 1 5); do
  HEALTH_RAW="$(curl -fsS -m 3 -H "@$header_file" "http://127.0.0.1:${PORT}/vernal-api/runner/health?format=raw" 2>/dev/null || true)"
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
  echo "  [⚠️] 本机执行机：尚未完成首次网页登录绑定（打开上方网址登录，后台将自动绑定）"
else
  echo "  [⚠️] 本机执行机：LoopForge 暂未收到在线心跳（请检查 deploy/loopforge/.runner/runner.log 或网页「设置 → 执行机器」）"
fi
echo "  停止服务可执行：./vernal.sh --stop"
echo "=========================================================="

if [[ "$RUNNER_EXIT" != "0" ]]; then
  exit "$RUNNER_EXIT"
fi
