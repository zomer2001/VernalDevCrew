#!/usr/bin/env bash
set -euo pipefail
umask 077
root="$(cd "$(dirname "$0")/../.." && pwd -P)"
compose=(docker compose -f "$root/deploy/loopforge/compose.yaml" --profile full --profile ci)
project="${COMPOSE_PROJECT_NAME:-autodev-runtime}"
image="${VERNAL_BACKUP_IMAGE:-autodev-runtime-vernal}"
mode="$1"
case "$mode" in
  --backup)
    out="${2:-$root/dist}"
    if [[ "$out" == *.tar.gz || "$out" == *.tgz ]] && [[ ! -d "$out" ]]; then
      mkdir -p "$(dirname "$out")"
      archive="$(cd "$(dirname "$out")" && pwd -P)/$(basename "$out")"
    else
      mkdir -p "$out"
      archive="$(cd "$out" && pwd -P)/vernal-backup-$(date +%Y%m%d-%H%M%S)-$$.tar.gz"
    fi ;;
  --restore)
    [[ $# == 2 && -f "$2" ]] || { echo "请指定完整备份归档" >&2; exit 2; }
    archive="$(cd "$(dirname "$2")" && pwd -P)/$(basename "$2")" ;;
  *) exit 2 ;;
esac
# Read the single Compose setting without executing .env.
native_dir="${LOOPFORGE_DATA_DIR:-}"
if [[ -z "$native_dir" && -f "$root/deploy/loopforge/.env" ]]; then
  native_dir="$(sed -n 's/^LOOPFORGE_DATA_DIR=//p' "$root/deploy/loopforge/.env" | tail -1)"
  native_dir="${native_dir#\'}"
  native_dir="${native_dir%\'}"
  native_dir="${native_dir#\"}"
  native_dir="${native_dir%\"}"
fi
native_dir="${native_dir:-$root/deploy/loopforge/.data}"
if [[ "$native_dir" == /* && -d "$native_dir" ]]; then
  native_dir="$(cd "$native_dir" && pwd -P)"
elif [[ "$mode" != --restore || "$native_dir" != "$root/deploy/loopforge/.data" ]]; then
  echo "找不到原生数据目录" >&2
  exit 2
fi
for source in "$root/workspaces" "$root/deploy/loopforge/.runner" "$native_dir"; do
  [[ "$archive" != "$source/"* ]] || { echo "归档须放在待备份目录之外" >&2; exit 2; }
done
echo "完整归档包含工作区、四个 Docker 卷、Runner、.env 和原生数据目录。单独备份 .data 无法恢复整个平台。"
# Quiesce the original services only after checking every volume and the offline helper image.
docker image inspect "$image" >/dev/null
mounts=()
for volume in mysql-data platform-data gitea-data; do
  full="${project}_$volume"
  docker volume inspect "$full" >/dev/null
  mounts+=(--mount "type=volume,src=$full,dst=/volumes/$volume")
done
ci_volume="${project}_gitea-runner-data"
if docker volume inspect "$ci_volume" >/dev/null 2>&1; then
  mounts+=(--mount "type=volume,src=$ci_volume,dst=/volumes/gitea-runner-data")
elif [[ "$mode" == --restore ]]; then
  docker volume create "$ci_volume" >/dev/null
  mounts+=(--mount "type=volume,src=$ci_volume,dst=/volumes/gitea-runner-data")
else
  echo "未检测到可选 CI Runner 卷 ${ci_volume}（尚未启用 --enable-ci），跳过该卷并继续完整备份。" >&2
fi
workdir_args=()
if [[ -f "$root/autodev/deployment_backup.py" ]]; then
  workdir_args=(--workdir /project)
fi
if [[ "$native_dir" != "$root/deploy/loopforge/.data" ]]; then
  if [[ "$native_dir" == / || "$root/" == "$native_dir/"* || "$root/deploy/loopforge/" == "$native_dir/"* ||
        "$native_dir/" == "$root/workspaces/"* || "$native_dir/" == "$root/deploy/loopforge/.runner/"* ]]; then
    echo "原生数据目录与其它备份目标重叠" >&2
    exit 2
  fi
  mounts+=(--mount "type=bind,src=$native_dir,dst=/native-data" --env VERNAL_BACKUP_NATIVE_MOUNT=/native-data)
fi
lock="$root/deploy/loopforge/.backup-lock"
mkdir "$lock" || { echo "另一个备份或恢复正在运行" >&2; exit 1; }
running=()
runner_running=0
restart() {
  code=$?
  if [[ -f "$root/deploy/loopforge/.restore-transaction.json" ]]; then
    echo "恢复中断，服务已保持停止。请再次运行 ./vernal.sh --restore 以恢复一致状态。" >&2
    rmdir "$lock"
    exit 1
  fi
  if [[ ${#running[@]} -gt 0 ]]; then "${compose[@]}" start "${running[@]}" || code=1; fi
  if [[ "$runner_running" == 1 ]]; then "$root/deploy/loopforge/start-runner.sh" || code=1; fi
  rmdir "$lock"
  exit "$code"
}
trap restart EXIT
if [[ -s "$root/deploy/loopforge/.runner/launcher.pid" ]]; then
  runner_pid="$(cat "$root/deploy/loopforge/.runner/launcher.pid")"
  if [[ "$runner_pid" =~ ^[0-9]+$ ]] && kill -0 "$runner_pid" 2>/dev/null; then
    "$root/deploy/loopforge/start-runner.sh" --stop
    runner_running=1
  fi
fi
services="$("${compose[@]}" ps --status running --services)"
while IFS= read -r service; do [[ -z "$service" ]] || running+=("$service"); done <<< "$services"
if [[ ${#running[@]} -gt 0 ]]; then "${compose[@]}" stop "${running[@]}"; fi
docker run --rm --pull never --user 0:0 --network none ${workdir_args[@]+"${workdir_args[@]}"} --entrypoint python3 "${mounts[@]}" \
  --mount "type=bind,src=$root,dst=/project" --mount "type=bind,src=$(dirname "$archive"),dst=/archive" \
  "$image" -m autodev.deployment_backup "$mode" "/archive/$(basename "$archive")"
echo "$archive"
