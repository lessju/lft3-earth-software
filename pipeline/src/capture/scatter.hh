#ifndef LFT3_CAPTURE_SCATTER_HH
#define LFT3_CAPTURE_SCATTER_HH

#include <cstdint>

#include <cuda_runtime.h>

namespace Lft3 {

// One unit of work for the scatter kernel: one packet (1024 channels x 4 inputs) into one ring block.
// A null `source` means "missing packet": the same region is zero-filled.
// Job arrays live in pinned host memory and are read by the GPU in place.
struct ScatterJob {
    const std::uint8_t* source;   // packet payload (DAQIRI data segment), or nullptr
    std::uint8_t* destination;    // the ring block
    std::uint32_t spectrum;       // t within the block (0 .. spectraPerBlock-1)
    std::uint32_t channelOffset;  // first channel of this packet within the selection (0, 1024, ...)
};

struct ScatterGeometry {
    std::uint32_t channelCount;     // C (channels per block)
    std::uint32_t spectraPerBlock;  // T
    std::uint8_t inputBeam[4];
    std::uint8_t inputPol[4];
};

// Writes block[beam][channel][t][pol] (complex int8) for every job. Each CUDA block transposes 32 jobs x 32
// channels through shared memory, so a warp writes 32 jobs of one channel row: when those jobs are
// consecutive in t (sort jobs by channelOffset, then t — SortJobsForScatter), that is one contiguous run.
cudaError_t LaunchScatter(const ScatterJob* jobs, std::uint32_t jobCount, const ScatterGeometry& geometry,
                          cudaStream_t stream);

// Stable counting sort of `in` by channelOffset into `out` (CPU). Arrival order is t-major, so this
// groups each packet column with ascending t.
void SortJobsForScatter(const ScatterJob* in, std::uint32_t n, ScatterJob* out);

}  // namespace Lft3

#endif  // LFT3_CAPTURE_SCATTER_HH
