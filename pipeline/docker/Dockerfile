# lft3-runtime: build and run image for the LFT3 real-time runtime (pipeline/) and tools/viewer.
#   CUDA 13 toolkit (same major as the DGX Spark host driver), Ubuntu 24.04 rdma-core (DAQIRI's ibverbs engine
#   needs the headers, libibverbs, libmlx5 and the mlx5 provider at run time), meson/ninja/cmake for the build
#   (HDF5 is built from source as a CMake subproject), zlib.
#   docker build -t lft3-runtime pipeline/docker      (pipeline/scripts/build.sh does this when needed)
ARG BASE=nvidia/cuda:13.0.2-devel-ubuntu24.04
FROM ${BASE}
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        libibverbs-dev libibverbs1 ibverbs-providers rdma-core ibverbs-utils \
        python3 python3-pip git pkg-config cmake ninja-build zlib1g-dev ca-certificates \
 && pip3 install --no-cache-dir --break-system-packages 'meson==1.6.1' \
 && rm -rf /var/lib/apt/lists/*
