#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
umask 077
mkdir -p .runner/pool
pwfile=.runner/pool/gitea-password
if [[ ! -s "$pwfile" ]]; then
  tmp="$(mktemp .runner/pool/gitea.XXXXXX)"
  od -An -tx1 -N32 /dev/urandom | tr -d ' \n' > "$tmp"
  ln "$tmp" "$pwfile" 2>/dev/null || [[ -s "$pwfile" ]]
  rm -f "$tmp"
fi
if [[ ${VERNAL_OFFLINE:-0} == 1 ]]; then
  docker compose up -d --no-build --pull never gitea
else
  docker compose up -d gitea
fi
for _ in $(seq 1 30); do
  if curl -fsS -m 2 "http://127.0.0.1:${GITEA_HTTP_PORT:-3000}/api/v1/version" >/dev/null 2>&1; then break; fi
  sleep 1
done
if ! docker compose exec -T --user git gitea gitea admin user list | awk '$2=="vernal-local"{ok=1} END{exit !ok}'; then
  docker compose exec -T --user git gitea sh -ec 'password=$(cat); gitea admin user create --username vernal-local --email vernal-local@localhost.invalid --password "$password" --must-change-password=false' < "$pwfile"
fi
