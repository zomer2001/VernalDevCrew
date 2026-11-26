#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
image="${VERNAL_SANDBOX_IMAGE:-autodev-runtime-sandbox:latest}"
base="${LOOPFORGE_IMAGE:-ghcr.io/tencent/loopforge:0.4.0}"
mkdir -p .runner/pool
rm -f .runner/pool/image-ready

stage_ctx=""
tmp=""
cleanup() {
  [[ -z "$stage_ctx" ]] || rm -rf "$stage_ctx"
  [[ -z "$tmp" ]] || rm -f "$tmp"
}
trap cleanup EXIT

if [[ "${VERNAL_OFFLINE:-0}" != "1" ]]; then
  repo_root="$(cd ../.. && pwd)"
  quality_fp=""
  if [[ -d "$repo_root/autodev/quality" ]]; then
    find_roots=("$repo_root/autodev/quality")
    if [[ -d "$repo_root/autodev/assets/semgrep" ]]; then
      find_roots+=("$repo_root/autodev/assets/semgrep")
    fi
    quality_fp="$(
      find "${find_roots[@]}" -type f ! -path '*/__pycache__/*' ! -name '*.pyc' ! -name '.DS_Store' \
        | LC_ALL=C sort \
        | while IFS= read -r f; do cat "$f"; done \
        | cksum
    )"
  fi
  fingerprint="$(printf '%s\n%s\n' "$base" "$quality_fp" | cat Dockerfile.sandbox - | cksum)"
  if [[ "$(docker image inspect -f '{{index .Config.Labels "vernal.sandbox.source"}}' "$image" 2>/dev/null || true)" != "$fingerprint" ]]; then
    # Only sterile Dockerfile + quality CLI sources are streamed. Deployment credentials stay outside the build context.
    stage_ctx="$(mktemp -d .runner/pool/sbx-ctx.XXXXXX)"
    cp Dockerfile.sandbox "$stage_ctx/Dockerfile"
    if [[ -d "$repo_root/autodev/quality" ]]; then
      mkdir -p "$stage_ctx/autodev"
      if [[ -f "$repo_root/autodev/__init__.py" ]]; then
        cp "$repo_root/autodev/__init__.py" "$stage_ctx/autodev/"
      else
        printf '__version__ = "0.2.0"\n' > "$stage_ctx/autodev/__init__.py"
      fi
      cp -R "$repo_root/autodev/quality" "$stage_ctx/autodev/quality"
      find "$stage_ctx/autodev" \( -name '__pycache__' -o -name '*.pyc' -o -name '.DS_Store' \) -exec rm -rf {} +
      if [[ -d "$repo_root/autodev/assets/semgrep" ]]; then
        mkdir -p "$stage_ctx/autodev/assets"
        cp -R "$repo_root/autodev/assets/semgrep" "$stage_ctx/autodev/assets/semgrep"
      fi
    fi
    COPYFILE_DISABLE=1 tar --format ustar -C "$stage_ctx" -czf - . | \
      docker build --build-arg "LOOPFORGE_IMAGE=$base" --label "vernal.sandbox.source=$fingerprint" -t "$image" -
    rm -rf "$stage_ctx"
    stage_ctx=""
  fi
fi

tmp="$(mktemp .runner/pool/image.XXXXXX)"
docker run --pull=never --rm --network none "$image" sh -ec 'dsh --version && opencode --version && vernal-quality --help >/dev/null' > "$tmp"
mv "$tmp" .runner/pool/image-ready
tmp=""
