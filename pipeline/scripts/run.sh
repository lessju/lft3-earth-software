#!/usr/bin/env bash
# Run an LFT3 flowgraph headless in the CyberEther container with the LFT3 plugin (and Stelline's, if
# built, for flowgraphs that use its blocks).
# Network flowgraphs need root: DAQIRI opens the NIC with raw ibverbs and pins memory (--privileged gives
# the container the RDMA devices; memlock unlimited lets it pin the receive buffers). The synthetic
# flowgraph runs without root too (scripts/run.sh as a normal user also works if you are in the docker group).
#
#   pipeline/scripts/run.sh [--remote] [--mount DIR]... [flowgraph.yml]   (default: pipeline/examples/lft3-spectrometer.yml)
#   (usually started by `lft3-pipeline run`; docker-group membership is enough, the container itself runs as root)
#   --mount DIR: also make DIR visible in the container (e.g. where a pcap to replay lives)
#
# --remote: stream the CyberEther UI (flowgraph, metrics, lineplots, waterfalls) to a browser. The terminal
# prints a join link http://<host>:8686/remote#<token>; open it (use any address of this host your browser
# can reach), compare the 6-character code shown in the browser with the one in the terminal, answer y.
# The NVIDIA Vulkan driver does not initialise inside this container (vkCreateInstance fails, though it
# works on the host), so the UI renders with Mesa llvmpipe on the CPU and is encoded in software at
# 10 fps; LP_NUM_THREADS=4 caps the renderer's threads to limit its CPU use.
#
# Output files go wherever the writer's outputDirectory points (it is bind-mounted below).
set -euo pipefail
PLUGIN_DIR=$(cd "$(dirname "$0")/.." && pwd)
REPO_DIR=$(dirname "$PLUGIN_DIR")
# CyberEther CUDA image + RDMA libraries (pipeline/docker/Dockerfile); built on first use.
IMAGE=${LFT3_IMAGE:-lft3-cyberether}
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    docker build -t "$IMAGE" "$PLUGIN_DIR/docker"
fi
STELLINE_CEP=${STELLINE_CEP:-/opt/lft3arth/pipeline/stelline/build-docker/stelline.cep}
LFT3_CEP=${LFT3_CEP:-$PLUGIN_DIR/build/lft3.cep}
DATA_DIR=${DATA_DIR:-/opt/lft3arth/data}
REMOTE_ARGS=()
EXTRA_MOUNTS=()
MOUNT_REQUESTS=()
while [[ "${1:-}" == --* ]]; do
    case "$1" in
        --remote) REMOTE_ARGS=(--remote --remote-encoder software --remote-framerate 10); shift ;;
        --mount) MOUNT_REQUESTS+=("$(realpath "$2")"); shift 2 ;;
        *) echo "unknown option $1" >&2; exit 1 ;;
    esac
done
FLOWGRAPH=$(realpath "${1:-$PLUGIN_DIR/examples/lft3-spectrometer.yml}")

PLUGINS=(--plugin "$LFT3_CEP")
STELLINE_MOUNT=()
if [[ -f "$STELLINE_CEP" ]]; then
    PLUGINS=(--plugin "$STELLINE_CEP" "${PLUGINS[@]}")
    STELLINE_MOUNT=(-v "$(dirname "$STELLINE_CEP"):$(dirname "$STELLINE_CEP"):ro")
fi
for f in "$LFT3_CEP" "$FLOWGRAPH"; do
    [[ -f "$f" ]] || { echo "missing: $f" >&2; exit 1; }
done
mkdir -p "$DATA_DIR"
# Extra mounts, unless already inside a mounted directory (docker rejects duplicate mount points).
for d in "${MOUNT_REQUESTS[@]}"; do
    covered=0
    for m in "$REPO_DIR" "$(realpath "$DATA_DIR")" "$(dirname "$FLOWGRAPH")"; do
        [[ "$d" == "$m" || "$d" == "$m"/* ]] && covered=1
    done
    [[ $covered == 0 ]] && EXTRA_MOUNTS+=(-v "$d:$d:ro")
done
# -i only with a terminal on stdin (the --remote approval prompt reads it); -t only when stdout is a terminal.
# Never -t when the output is piped (lft3-pipeline reads it): with a TTY, docker does not proxy signals, so
# Ctrl-C / --duration would stop only the docker client and leave the container (and the NIC) running.
# Interactive only when run directly from a terminal in the foreground; otherwise docker must not touch
# stdin (a background job reading the tty is stopped by SIGTTIN).
TTY_ARGS=()
[[ -t 0 && -t 1 ]] && TTY_ARGS+=(-it)

# One receiver per host NIC: a fixed name makes a second concurrent run fail instead of fighting over the NIC.
CONTAINER_NAME=${LFT3_CONTAINER:-lft3-pipeline}
if docker ps -q --filter "name=^${CONTAINER_NAME}$" | grep -q .; then
    echo "A pipeline container '$CONTAINER_NAME' is already running (docker stop $CONTAINER_NAME to end it)." >&2
    exit 1
fi

# CyberEther runs as PID 1 and only handles SIGINT (graceful: closes files). `docker stop` therefore sends
# SIGINT and waits 30 s before SIGKILL. lft3-pipeline stops runs this way; do the same by hand.
exec docker run --rm --name "$CONTAINER_NAME" "${TTY_ARGS[@]}" --stop-signal SIGINT --stop-timeout 30 \
    --privileged --net host --gpus all \
    --ulimit memlock=-1 --ipc=host \
    -v "$REPO_DIR:$REPO_DIR" "${STELLINE_MOUNT[@]}" \
    -v "$DATA_DIR:$DATA_DIR" -v "$(dirname "$FLOWGRAPH"):$(dirname "$FLOWGRAPH"):ro" "${EXTRA_MOUNTS[@]}" \
    -e HOME=/tmp -e LP_NUM_THREADS=4 \
    "$IMAGE" \
    cyberether --headless "${REMOTE_ARGS[@]}" "${PLUGINS[@]}" "$FLOWGRAPH"
