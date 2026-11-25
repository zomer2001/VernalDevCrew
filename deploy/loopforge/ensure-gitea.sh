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
  docker compose exec -T --user git gitea sh -ec 'password=$(cat); gitea admin user create --username vernal-local --email vernal-local@localhost.invalid --password "$password" --must-change-password=false --admin' < "$pwfile"
fi

if [[ "${VERNAL_ENABLE_CI:-0}" == "1" ]]; then
  tokfile=.runner/pool/act-runner-token
  if [[ ! -s "$tokfile" ]]; then
    runner_tok="$(docker compose exec -T --user git gitea gitea actions generate-runner-token 2>/dev/null | tr -d '\r\n ' || true)"
    if [[ -z "$runner_tok" && -s "$pwfile" ]]; then
      gitea_pw="$(cat "$pwfile")"
      runner_tok="$(curl -fsS -m 5 -u "vernal-local:${gitea_pw}" -X POST "http://127.0.0.1:${GITEA_HTTP_PORT:-3000}/api/v1/admin/runners/registration-token" 2>/dev/null | sed -n 's/.*"token"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' || true)"
    fi
    if [[ -z "$runner_tok" ]]; then
      echo "无法从 Gitea 生成 act_runner 注册令牌 (act-runner-token)" >&2
      exit 1
    fi
    tmp_tok="$(mktemp .runner/pool/act-tok.XXXXXX)"
    printf '%s\n' "$runner_tok" > "$tmp_tok"
    chmod 600 "$tmp_tok"
    ln "$tmp_tok" "$tokfile" 2>/dev/null || [[ -s "$tokfile" ]]
    rm -f "$tmp_tok"
  fi
  if [[ ${VERNAL_OFFLINE:-0} == 1 ]]; then
    docker compose --profile ci up -d --no-build --pull never gitea-runner
  else
    docker compose --profile ci up -d gitea-runner
  fi
fi
