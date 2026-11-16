#!/usr/bin/env bash
# Build lft3-view inside the lft3-runtime container (no root). Output: tools/viewer/build/
# lft3-runtime (pipeline/docker/Dockerfile) has g++ 13, meson, ninja, cmake (for the HDF5 subproject) and
# zlib. The binaries run on the host too (HDF5 is linked statically; libz.so.1, glibc and libstdc++ come
# from Ubuntu 24.04, which every host here has).
#
#   tools/viewer/scripts/build.sh          # configure (first time) + compile
#   tools/viewer/scripts/build.sh clean    # wipe tools/viewer/build first
set -euo pipefail
VIEWER_DIR=$(cd "$(dirname "$0")/.." && pwd)
REPO_DIR=$(cd "$VIEWER_DIR/../.." && pwd)
IMAGE=${LFT3_IMAGE:-lft3-runtime}
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    docker build -t "$IMAGE" "$REPO_DIR/pipeline/docker"
fi
if [[ "${1:-}" == "clean" ]]; then
    rm -rf "$VIEWER_DIR/build"
fi
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$REPO_DIR:$REPO_DIR" -w "$VIEWER_DIR" \
    "$IMAGE" bash -c "(meson setup build --reconfigure 2>/dev/null || meson setup build) >/dev/null && meson compile -C build"
