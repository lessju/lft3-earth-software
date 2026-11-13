#include "kernel.hh"

namespace Lft3 {

namespace {

constexpr std::uint32_t kChannelsPerPacket = 1024;
constexpr std::uint32_t kThreads = 256;

// One thread per (job, channel). Each thread reads 8 bytes (4 inputs x I,Q) and writes 4 complex
// samples: for each input, element ((aspect*C + c)*S + s)*2 + pol, aspect = beam*M + segment.
// Pol is the innermost axis, so X and Y of the same beam are adjacent in memory and each pair of
// writes shares a cache sector. Consecutive channels are fftLength*2 elements apart, so writes are
// not coalesced across threads; this is the receiver's main memory-bandwidth cost (see docs).
__global__ void ScatterKernel(const ScatterJob* jobs, std::uint32_t jobCount, ScatterGeometry g) {
    const std::uint64_t tid = static_cast<std::uint64_t>(blockIdx.x) * blockDim.x + threadIdx.x;
    const std::uint32_t job = static_cast<std::uint32_t>(tid / kChannelsPerPacket);
    const std::uint32_t lane = static_cast<std::uint32_t>(tid % kChannelsPerPacket);
    if (job >= jobCount) {
        return;
    }

    const ScatterJob j = jobs[job];
    const std::uint32_t segment = j.spectrum / g.fftLength;
    const std::uint32_t sample = j.spectrum % g.fftLength;
    const std::uint64_t channel = j.channelOffset + lane;

    char2 iq[4] = {};
    if (j.source) {
        // 8 bytes, 8-byte aligned (payload buffers are aligned and lane*8 keeps alignment).
        const uint2 raw = *reinterpret_cast<const uint2*>(j.source + static_cast<std::uint64_t>(lane) * 8);
        const char* bytes = reinterpret_cast<const char*>(&raw);
        for (int input = 0; input < 4; input++) {
            iq[input] = make_char2(bytes[2 * input], bytes[2 * input + 1]);
        }
    }

    const float sign = (g.fftShiftSign && (sample & 1u)) ? -1.0f : 1.0f;

    for (int input = 0; input < 4; input++) {
        const std::uint64_t aspect = static_cast<std::uint64_t>(g.inputBeam[input]) * g.segments + segment;
        const std::uint64_t element =
            ((aspect * g.channelCount + channel) * g.fftLength + sample) * 2 + g.inputPol[input];
        if (g.outputCF32) {
            float2* dst = static_cast<float2*>(j.destination);
            dst[element] = make_float2(sign * static_cast<float>(iq[input].x),
                                       sign * static_cast<float>(iq[input].y));
        } else {
            char2* dst = static_cast<char2*>(j.destination);
            dst[element] = iq[input];
        }
    }
}

}  // namespace

cudaError_t LaunchScatter(const ScatterJob* jobs,
                          std::uint32_t jobCount,
                          const ScatterGeometry& geometry,
                          cudaStream_t stream) {
    if (jobCount == 0) {
        return cudaSuccess;
    }
    const std::uint64_t threads = static_cast<std::uint64_t>(jobCount) * kChannelsPerPacket;
    const dim3 grid(static_cast<unsigned>((threads + kThreads - 1) / kThreads));
    ScatterKernel<<<grid, kThreads, 0, stream>>>(jobs, jobCount, geometry);
    return cudaGetLastError();
}

}  // namespace Lft3
