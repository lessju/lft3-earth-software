#include "capture/scatter.hh"

#include <cstring>

namespace Lft3 {

namespace {

constexpr std::uint32_t kChannelsPerPacket = 1024;
constexpr std::uint32_t kTile = 32;      // jobs and channels per CUDA block
constexpr std::uint32_t kRows = 8;       // threadIdx.y

// Block (32 x 8 threads) = 32 jobs x 32 channels.
// Load: warp y reads jobs y, y+8, ...: 32 consecutive channels x 8 bytes = 256 contiguous bytes per job.
// Store: warp y handles channels y, y+8, ...; lane = job, so for consecutive-t jobs the warp writes one
// contiguous run of the (beam, channel) row per input.
__global__ void ScatterKernel(const ScatterJob* __restrict__ jobs, std::uint32_t jobCount, ScatterGeometry g) {
    __shared__ uint2 tile[kTile][kTile + 1];
    __shared__ ScatterJob job[kTile];
    const std::uint32_t j0 = blockIdx.y * kTile;
    const std::uint32_t c0 = blockIdx.x * kTile;
    const std::uint32_t tx = threadIdx.x, ty = threadIdx.y;

    if (ty == 0 && j0 + tx < jobCount) job[tx] = jobs[j0 + tx];
    __syncthreads();

    for (std::uint32_t r = ty; r < kTile; r += kRows) {
        uint2 v = make_uint2(0, 0);
        if (j0 + r < jobCount && job[r].source) {
            v = reinterpret_cast<const uint2*>(job[r].source)[c0 + tx];
        }
        tile[r][tx] = v;
    }
    __syncthreads();

    if (j0 + tx >= jobCount) return;
    const ScatterJob& j = job[tx];
    const std::uint64_t T = g.spectraPerBlock;
    for (std::uint32_t r = ty; r < kTile; r += kRows) {
        const uint2 v = tile[tx][r];
        const std::uint64_t channel = j.channelOffset + c0 + r;
        const std::uint32_t words[2] = {v.x, v.y};
        char2* dst = reinterpret_cast<char2*>(j.destination);
#pragma unroll
        for (int input = 0; input < 4; input++) {
            const std::uint32_t w = words[input >> 1] >> ((input & 1) * 16);
            const char2 iq = make_char2(static_cast<char>(w & 0xFF), static_cast<char>((w >> 8) & 0xFF));
            const std::uint64_t e = ((g.inputBeam[input] * static_cast<std::uint64_t>(g.channelCount) + channel) * T +
                                     j.spectrum) * 2 + g.inputPol[input];
            dst[e] = iq;
        }
    }
}

}  // namespace

cudaError_t LaunchScatter(const ScatterJob* jobs, std::uint32_t jobCount, const ScatterGeometry& geometry,
                          cudaStream_t stream) {
    if (jobCount == 0) return cudaSuccess;
    const dim3 grid(kChannelsPerPacket / kTile, (jobCount + kTile - 1) / kTile);
    const dim3 block(kTile, kRows);
    ScatterKernel<<<grid, block, 0, stream>>>(jobs, jobCount, geometry);
    return cudaGetLastError();
}

void SortJobsForScatter(const ScatterJob* in, std::uint32_t n, ScatterJob* out) {
    constexpr std::uint32_t kMaxGroups = 64;   // channelOffset / 1024 < 6 in practice
    std::uint32_t count[kMaxGroups + 1] = {};
    for (std::uint32_t i = 0; i < n; i++) {
        const std::uint32_t g = in[i].channelOffset / kChannelsPerPacket;
        count[(g < kMaxGroups ? g : kMaxGroups - 1) + 1]++;
    }
    for (std::uint32_t g = 0; g < kMaxGroups; g++) count[g + 1] += count[g];
    for (std::uint32_t i = 0; i < n; i++) {
        const std::uint32_t g = in[i].channelOffset / kChannelsPerPacket;
        out[count[g < kMaxGroups ? g : kMaxGroups - 1]++] = in[i];
    }
}

}  // namespace Lft3
