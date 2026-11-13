#!/usr/bin/env bash
# Build (and optionally test) the LFT3 runtime inside the lft3-runtime image. No root needed.
# Output: pipeline/build/{lft3-capture,lft3-spectrometer,lft3-dump,tests/lft3-tests}
#
#   pipeline/scripts/build.sh          # configure + compile
#   pipeline/scripts/build.sh test     # ... and run the unit tests on the GPU
set -euo pipefail
PIPELINE_DIR=$(cd "$(dirname "$0")/.." && pwd)
REPO_DIR=$(dirname "$PIPELINE_DIR")
IMAGE=${LFT3_IMAGE:-lft3-runtime}
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    docker build -t "$IMAGE" "$PIPELINE_DIR/docker"
fi
CMD="meson setup build --reconfigure >/dev/null 2>&1 || meson setup build; meson compile -C build"
GPU_ARGS=()
if [[ "${1:-}" == "test" ]]; then
    GPU_ARGS=(--gpus all --ipc=host)
    CMD="$CMD && meson test -C build --print-errorlogs"
fi
docker run --rm "${GPU_ARGS[@]}" --user "$(id -u):$(id -g)" -e HOME=/tmp \
    -v "$REPO_DIR:$REPO_DIR" -w "$PIPELINE_DIR" "$IMAGE" bash -c "$CMD"
