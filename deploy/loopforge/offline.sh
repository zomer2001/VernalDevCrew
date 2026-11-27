# Sourced by vernal.sh. The archive contains deployment scripts and tagged images.
package_images() (
  set -euo pipefail
  cd "$ROOT/deploy/loopforge"
  local caller_ci="${VERNAL_ENABLE_CI:-}"
  set -a
  source .env
  set +a
  [[ -z "$caller_ci" ]] || export VERNAL_ENABLE_CI="$caller_ci"
  local platform arch stage release image actual id service
  platform="$(docker version -f '{{.Server.Os}}/{{.Server.Arch}}')"
  case "$platform" in linux/arm64|linux/amd64) ;; *) echo "不支持的 Docker 平台 $platform" >&2; return 1 ;; esac
  arch="${platform#linux/}"
  mkdir -p "$ROOT/dist"
  stage="$(mktemp -d "$ROOT/dist/.pack.XXXXXXXX")"
  trap 'rm -rf "$stage"' EXIT
  release="$stage/vernal-offline"
  mkdir -p "$release/deploy/loopforge"
  cp "$ROOT/vernal.sh" "$release/"
  cp compose.yaml Dockerfile.sandbox offline.sh up.sh doctor.sh ensure-gitea.sh build-sandbox.sh \
    start-runner.sh runner-codex.sh wait-and-bind-runner.sh "$release/deploy/loopforge/"
  if [[ -f backup.sh ]]; then cp backup.sh "$release/deploy/loopforge/"; fi
  local lock="$release/deploy/loopforge/images.lock"
  local -a images
  images=()
  echo "$platform" > "$lock"
  # Compose may include dependencies with config --images SERVICE. List the entire stack once.
  local stack expected_cnt=5
  local runner_img="${GITEA_RUNNER_IMAGE:-gitea/act_runner:0.2.11}"
  if [[ "${VERNAL_ENABLE_CI:-0}" == "1" ]]; then
    stack="$(docker compose --profile full --profile ci config --images)"
    expected_cnt=6
  else
    stack="$(docker compose --profile full config --images)"
  fi
  for image in $stack "${VERNAL_SANDBOX_IMAGE:-autodev-runtime-sandbox:latest}"; do
    service=service
    if [[ "$image" == "${LOOPFORGE_IMAGE:-ghcr.io/tencent/loopforge:0.4.0}" ]]; then service=loopforge; fi
    if [[ "$image" == "${VERNAL_SANDBOX_IMAGE:-autodev-runtime-sandbox:latest}" ]]; then service=sandbox; fi
    if [[ "${VERNAL_ENABLE_CI:-0}" == "1" && "$image" == "$runner_img" ]]; then service=ci_runner; fi
    # An untagged repository argument to docker save includes all locally tagged versions.
    if [[ "${image##*/}" != *:* && "$image" != *@* ]]; then image="$image:latest"; fi
    actual="$(docker image inspect -f '{{.Os}}/{{.Architecture}} {{.Id}}' "$image")"
    if [[ "${actual% *}" != "$platform" ]]; then
      echo "镜像 $image 的平台与 $platform 不一致" >&2
      return 1
    fi
    id="${actual##* }"
    printf '%s %s %s\n' "$service" "$image" "$id" >> "$lock"
    images+=("$image")
  done
  if [[ ${#images[@]} != "$expected_cnt" ]]; then echo "未能解析完整镜像清单" >&2; return 1; fi
  echo "正在导出 ${#images[@]} 个本机镜像，镜像较大时可能需要几分钟"
  docker image save -o "$release/images.tar" "${images[@]}"
  COPYFILE_DISABLE=1 tar -C "$stage" -czf "$stage/release.tar.gz" vernal-offline
  mv -f "$stage/release.tar.gz" "$ROOT/dist/vernal-offline-$arch.tar.gz"
  echo "离线包已生成：$ROOT/dist/vernal-offline-$arch.tar.gz"
)

restore_images() {
  local expected platform service image id current missing=0 has_ci_runner=0
  local lock="$ROOT/deploy/loopforge/images.lock"
  if ! docker info >/dev/null 2>&1 && command -v colima >/dev/null 2>&1; then colima start; fi
  platform="$(docker version -f '{{.Server.Os}}/{{.Server.Arch}}')"
  IFS= read -r expected < "$lock"
  if [[ "$platform" != "$expected" ]]; then
    echo "本包适用于 ${expected}，当前 Docker 平台为 $platform" >&2
    return 1
  fi
  local -a images ids
  images=()
  ids=()
  while read -r service image id; do
    if [[ -z "$image" || "$image" == -* || ! "$id" =~ ^sha256:[0-9a-f]{64}$ ]]; then
      echo "离线镜像清单格式错误" >&2
      return 1
    fi
    case "$service" in
      loopforge) export LOOPFORGE_IMAGE="$image" ;;
      sandbox) export VERNAL_SANDBOX_IMAGE="$image" ;;
      ci_runner)
        export GITEA_RUNNER_IMAGE="$image"
        export VERNAL_ENABLE_CI="${VERNAL_ENABLE_CI:-1}"
        has_ci_runner=1
        ;;
      service) ;;
      *) echo "离线镜像清单包含未知条目" >&2; return 1 ;;
    esac
    images+=("$image")
    ids+=("$id")
    current="$(docker image inspect -f '{{.Id}}' "$image" 2>/dev/null || true)"
    [[ "$current" == "$id" ]] || missing=1
  done < <(tail -n +2 "$lock")
  local expected_cnt=5
  [[ "$has_ci_runner" == "1" ]] && expected_cnt=6
  if [[ ${#images[@]} != "$expected_cnt" ]]; then echo "离线镜像清单不完整" >&2; return 1; fi
  if [[ "$missing" == 1 ]]; then
    echo "正在从安装包导入镜像"
    docker image load -i "$ROOT/images.tar"
    local i
    for i in "${!images[@]}"; do
      if [[ "$(docker image inspect -f '{{.Id}}' "${images[i]}")" != "${ids[i]}" ]]; then
        echo "镜像校验失败：${images[i]}" >&2
        return 1
      fi
    done
  fi
  export VERNAL_OFFLINE=1
}
