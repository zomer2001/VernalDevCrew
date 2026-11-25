#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

DATA_DIR="$(pwd)/.data"
mkdir -p "$DATA_DIR/workspaces" "$DATA_DIR/conversations" "$DATA_DIR/home" "$DATA_DIR/cli"

_rand_hex() {
  local bytes="$1"
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex "$bytes"
  else
    LC_ALL=C od -An -tx1 -N "$bytes" /dev/urandom | tr -d ' \n'
  fi
}

ENV_PATH=".env"
if [[ ! -f "$ENV_PATH" ]]; then
  DB_PASS="$(_rand_hex 20)"
  ROOT_PASS="$(_rand_hex 20)"
  SESS_SECRET="$(_rand_hex 32)"
  IMG="${LOOPFORGE_IMAGE:-ghcr.io/tencent/loopforge:0.4.0}"
  umask 077
  cat >"$ENV_PATH" <<EOF
LOOPFORGE_DB_PASSWORD='${DB_PASS}'
LOOPFORGE_DB_ROOT_PASSWORD='${ROOT_PASS}'
LOOPFORGE_MYSQL_PORT='33315'
AGENTFLOW_HTTP_PORT='8090'
AGENTFLOW_SESSION_SECRET='${SESS_SECRET}'
LOOPFORGE_DATA_DIR='${DATA_DIR}'
LOOPFORGE_IMAGE='${IMG}'
LOOPFORGE_BIND='127.0.0.1'
GITEA_HTTP_PORT='3000'
GITEA_SSH_PORT='2222'
VERNAL_ENABLE_CI='${VERNAL_ENABLE_CI:-0}'
EOF
  chmod 600 "$ENV_PATH"
  echo "Generated deploy/loopforge/.env"
else
  echo "Using existing deploy/loopforge/.env"
fi

if ! docker info >/dev/null 2>&1; then
  if command -v colima >/dev/null 2>&1; then
    echo "检测到 Docker 守护进程未启动，正在自动拉起 Colima..."
    colima start
  else
    echo "错误：Docker 未启动，请先启动 Docker Desktop、Colima 或系统 Docker 服务。" >&2
    exit 1
  fi
fi

caller_ci="${VERNAL_ENABLE_CI:-}"
set -a
source .env
set +a
[[ -z "$caller_ci" ]] || export VERNAL_ENABLE_CI="$caller_ci"
./build-sandbox.sh || echo "公用池镜像未就绪，本机 Runner 可继续启动" >&2
if [[ ${VERNAL_OFFLINE:-0} == 1 ]]; then
  docker compose up -d --no-build --pull never
else
  docker compose up -d
fi
echo "Waiting for LoopForge health..."
for _ in $(seq 1 60); do
  if curl -sf http://127.0.0.1:8090/api/health >/dev/null 2>&1; then
    echo "LoopForge is UP at http://127.0.0.1:8090"
    echo "Gitea is UP at http://127.0.0.1:3000"
    exit 0
  fi
  sleep 2
done
echo "Timed out waiting for LoopForge. Check: docker compose logs loopforge" >&2
exit 1
