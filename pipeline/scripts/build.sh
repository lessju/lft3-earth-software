#!/usr/bin/env bash
# Build (and optionally test) the LFT3 CyberEther plugin inside the CyberEther CUDA container.
# No root needed. Output: pipeline/build/lft3.cep
#
#   pipeline/scripts/build.sh          # configure + compile
#   pipeline/scripts/build.sh test     # ... and run the unit tests on the GPU
#
# The container provides CyberEther/Jetstream (headers + libjetstream), CUDA, meson, ninja and
# libibverbs/libmlx5 (without them DAQIRI silently builds without its ibverbs engine).
# CMake (needed for the HDF5 subproject) is taken from $PIPELINE_VENV.
set -euo pipefail
PLUGIN_DIR=$(cd "$(dirname "$0")/.." && pwd)
REPO_DIR=$(dirname "$PLUGIN_DIR")
# CyberEther CUDA image + RDMA libraries (pipeline/docker/Dockerfile); built on first use.
IMAGE=${LFT3_IMAGE:-lft3-cyberether}
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    docker build -t "$IMAGE" "$PLUGIN_DIR/docker"
fi
PIPELINE_VENV=${PIPELINE_VENV:-/opt/lft3arth/pipeline/.venv}

GPU_ARGS=()
CMD="meson setup build --reconfigure 2>/dev/null || meson setup build; meson compile -C build"
if [[ "${1:-}" == "test" ]]; then
    GPU_ARGS=(--gpus all)
    CMD="$CMD && meson test -C build --print-errorlogs"
fi

docker run --rm "${GPU_ARGS[@]}" --user "$(id -u):$(id -g)" -e HOME=/tmp \
    -v "$REPO_DIR:$REPO_DIR" -v "$PIPELINE_VENV:$PIPELINE_VENV:ro" -w "$PLUGIN_DIR" \
    "$IMAGE" bash -c "export PATH=\$PATH:$PIPELINE_VENV/bin; $CMD"
