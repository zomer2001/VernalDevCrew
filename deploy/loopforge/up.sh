#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

python3 - <<'PY'
import os, pathlib, secrets, shlex

env_path = pathlib.Path(".env")
data_dir = pathlib.Path(".data").resolve()
for sub in ("workspaces", "conversations", "home", "cli"):
    (data_dir / sub).mkdir(parents=True, exist_ok=True)

if not env_path.exists():
    db_pass = secrets.token_hex(20)
    root_pass = secrets.token_hex(20)
    sess = secrets.token_hex(32)
    values = {
        "LOOPFORGE_DB_PASSWORD": db_pass,
        "LOOPFORGE_DB_ROOT_PASSWORD": root_pass,
        "LOOPFORGE_MYSQL_PORT": "33315",
        "AGENTFLOW_HTTP_PORT": "8090",
        "AGENTFLOW_SESSION_SECRET": sess,
        "LOOPFORGE_DATA_DIR": str(data_dir),
        "LOOPFORGE_IMAGE": os.environ.get("LOOPFORGE_IMAGE", "ghcr.io/tencent/loopforge:0.4.0"),
        "LOOPFORGE_BIND": "127.0.0.1",
        "GITEA_HTTP_PORT": "3000",
        "GITEA_SSH_PORT": "2222",
    }
    env_path.write_text("".join(f"{k}={shlex.quote(v)}\n" for k, v in values.items()), "utf-8")
    os.chmod(env_path, 0o600)
    print("Generated deploy/loopforge/.env")
else:
    print("Using existing deploy/loopforge/.env")
PY

if ! docker info >/dev/null 2>&1; then
  if command -v colima >/dev/null 2>&1; then
    echo "检测到 Docker 守护进程未启动，正在自动拉起 Colima..."
    colima start
  else
    echo "错误：Docker 未启动，请先启动 Docker Desktop 或 Colima。" >&2
    exit 1
  fi
fi

docker compose up -d
echo "Waiting for LoopForge health..."
for i in $(seq 1 60); do
  if curl -sf http://127.0.0.1:8090/api/health >/dev/null 2>&1; then
    echo "LoopForge is UP at http://127.0.0.1:8090"
    echo "Gitea is UP at http://127.0.0.1:3000"
    exit 0
  fi
  sleep 2
done
echo "Timed out waiting for LoopForge. Check: docker compose logs loopforge" >&2
exit 1
