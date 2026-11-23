#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PORT="${VERNAL_PORT:-8787}"

if ! curl -fsS -m 2 http://127.0.0.1:8090/api/health >/dev/null 2>&1; then
  echo "==> LoopForge 执行底座未就绪，正在通过 ./deploy/loopforge/up.sh 启动..."
  ./deploy/loopforge/up.sh
fi

./deploy/loopforge/start-runner.sh || true

echo "==> 启动统一平台 Vernal: http://127.0.0.1:${PORT}"
exec "$ROOT/.venv/bin/python" -m autodev.cli serve --port "$PORT" "$@"
