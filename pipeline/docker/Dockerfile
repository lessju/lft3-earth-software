# CyberEther CUDA image + the RDMA userspace libraries DAQIRI's ibverbs engine needs, at build time
# (headers, libibverbs, libmlx5) and at run time (plus the mlx5 provider plugin for libibverbs).
# Same Ubuntu 24.04 rdma-core as the DGX Spark host.
#   docker build -t lft3-cyberether pipeline/docker      (pipeline/scripts/build.sh does this when needed)
ARG BASE=ghcr.io/luigifcruz/cyberether:ubuntu24-aarch64-cuda
FROM ${BASE}
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        libibverbs-dev libibverbs1 ibverbs-providers rdma-core ibverbs-utils vulkan-tools \
 && rm -rf /var/lib/apt/lists/*
