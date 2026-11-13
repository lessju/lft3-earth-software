#ifndef LFT3_RECEIVER_DETAIL_KERNEL_HH
#define LFT3_RECEIVER_DETAIL_KERNEL_HH

#include <cstdint>

#include <cuda_runtime.h>

namespace Lft3 {

// One unit of work for the scatter kernel: one packet (1024 channels x 4 inputs) into one block.
// A null `source` means "missing packet": the same destination region is zero-filled.
// Job arrays live in pinned host memory and are read by the GPU directly (zero-copy).
struct ScatterJob {
    const std::uint8_t* source;   // packet payload (DAQIRI data segment), or nullptr
    void* destination;            // base pointer of the block's output tensor
    std::uint32_t spectrum;       // spectrum index within the block (0 .. spectraPerBlock-1)
    std::uint32_t channelOffset;  // first output channel of this packet (0, 1024, ...)
};

struct ScatterGeometry {
    std::uint32_t channelCount;   // C
    std::uint32_t fftLength;      // S
    std::uint32_t segments;       // M
    std::uint8_t inputBeam[4];
    std::uint8_t inputPol[4];
    bool outputCF32;              // false: CI8
    bool fftShiftSign;            // multiply sample s by (-1)^s (centres a plain FFT, CF32 only)
};

cudaError_t LaunchScatter(const ScatterJob* jobs,
                          std::uint32_t jobCount,
                          const ScatterGeometry& geometry,
                          cudaStream_t stream);

}  // namespace Lft3

#endif  // LFT3_RECEIVER_DETAIL_KERNEL_HH
