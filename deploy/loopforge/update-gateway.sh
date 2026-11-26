# Sourced from vernal.sh in a source checkout.
GATEWAY_IMAGE=autodev-runtime-vernal
GATEWAY_BASE=autodev-runtime-vernal-base

compatible_gateway_base() (
  local image base recipe saved probe container=""
  image="$(docker image inspect -f '{{.Id}}' "$GATEWAY_IMAGE")" || return 1
  recipe="$(_fingerprint Dockerfile pyproject.toml)" || return 1
  saved="$(docker image inspect -f '{{index .Config.Labels "vernal.gateway.recipe"}}' "$image")" || return 1
  if [[ -n "$saved" && "$saved" != "<no value>" ]]; then
    [[ "$saved" == "$recipe" ]] || return 1
    base="$(docker image inspect -f '{{index .Config.Labels "vernal.gateway.base"}}' "$image")" || return 1
    [[ -n "$base" && "$(docker image inspect -f '{{.Id}}' "$GATEWAY_BASE" 2>/dev/null)" == "$base" ]] || return 1
    echo "$base"
    return
  fi

  # Older gateways have no build labels. Reproduce the last successful stamp using
  # the image's own files and today's Dockerfile before reusing its dependencies.
  probe="$(mktemp -d)" || return 1
  trap 'if [[ -n "$container" ]]; then docker rm "$container" >/dev/null; fi; rm -rf "$probe"' EXIT
  container="$(docker create --pull=never "$image")" || return 1
  mkdir -p "$probe/deploy" || return 1
  docker cp "$container:/app/autodev" "$probe/autodev" || return 1
  docker cp "$container:/app/deploy/loopforge" "$probe/deploy/loopforge" || return 1
  docker cp "$container:/app/pyproject.toml" "$probe/pyproject.toml" || return 1
  cmp -s pyproject.toml "$probe/pyproject.toml" || return 1
  cp Dockerfile "$probe/Dockerfile" || return 1
  [[ "$(cd "$probe" && _source_fingerprint)" == "$PREV_FP" ]] || return 1
  echo "$image"
)

update_gateway_code() (
  set -euo pipefail
  local base="$1" container cmd recipe
  cmd="$(docker image inspect -f '{{json .Config.Cmd}}' "$base")"
  recipe="$(_fingerprint Dockerfile pyproject.toml)"
  # Use a pristine image container, with no service volumes or credentials.
  container="$(docker create -i --pull=never --network none "$base" sh -ec '
    rm -rf /app/autodev
    tar --no-same-owner -xf - -C /app
    PYTHONDONTWRITEBYTECODE=1 python3 -m autodev.cli --help >/dev/null
  ')"
  trap 'docker rm "$container" >/dev/null' EXIT
  COPYFILE_DISABLE=1 tar --exclude __pycache__ --exclude '*.pyc' --exclude .DS_Store \
    -cf - autodev deploy/loopforge/start-runner.sh deploy/loopforge/wait-and-bind-runner.sh |
    docker start -ai "$container"
  [[ "$(docker inspect -f '{{.State.ExitCode}}' "$container")" == 0 ]]
  # Keep the original dependency layers for subsequent updates and offline exports.
  docker tag "$base" "$GATEWAY_BASE"
  docker commit --change "CMD $cmd" --change "LABEL vernal.gateway.base=$base" \
    --change "LABEL vernal.gateway.recipe=$recipe" "$container" "$GATEWAY_IMAGE" >/dev/null
)
