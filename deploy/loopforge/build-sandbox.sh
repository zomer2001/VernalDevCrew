#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
image="${VERNAL_SANDBOX_IMAGE:-autodev-runtime-sandbox:latest}"
base="${LOOPFORGE_IMAGE:-ghcr.io/tencent/loopforge:0.4.0}"
mkdir -p .runner/pool
rm -f .runner/pool/image-ready
fingerprint="$( (cat Dockerfile.sandbox; echo "$base") | cksum)"
if [[ ${VERNAL_OFFLINE:-0} != 1 && "$(docker image inspect -f '{{index .Config.Labels "vernal.sandbox.source"}}' "$image" 2>/dev/null || true)" != "$fingerprint" ]]; then
  # Only the Dockerfile is streamed. Deployment credentials stay outside the build context.
  docker build --build-arg "LOOPFORGE_IMAGE=$base" --label "vernal.sandbox.source=$fingerprint" -t "$image" - < Dockerfile.sandbox
fi
tmp="$(mktemp .runner/pool/image.XXXXXX)"
trap 'rm -f "$tmp"' EXIT
docker run --pull=never --rm --network none --entrypoint sh "$image" -ec 'dsh --version && opencode --version' > "$tmp"
mv "$tmp" .runner/pool/image-ready
