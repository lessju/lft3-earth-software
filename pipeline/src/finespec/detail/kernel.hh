#ifndef LFT3_FINESPEC_DETAIL_KERNEL_HH
#define LFT3_FINESPEC_DETAIL_KERNEL_HH

#include <cstdint>

#include <cuda_runtime.h>

namespace Lft3 {

// Fused fine spectrometer: for every (beam, coarse channel) and each of `segments` time segments,
//   load fftLength dual-pol samples -> radix-2 FFT in shared memory (X and Y) -> |X|^2 + |Y|^2
//   -> sum over segments -> add into output[beam][channel * fftLength + fftshift(bin)].
// Input  [beams * segments, channels, fftLength, 2] CI8 (int8 I/Q) or CF32, as produced by lft3_receiver.
// Output [beams, channels * fftLength / binsPerOutput] float, accumulated (the caller zeroes it at
// integration start). binsPerOutput > 1 sums groups of consecutive fine channels (e.g. fftLength for one
// value per coarse channel, for quick-look displays); it must divide channels * fftLength.
// fftLength must be a power of two, 2..2048 (one thread per butterfly, at most 1024 threads).
struct FineSpecGeometry {
    std::uint32_t beams;
    std::uint32_t segments;
    std::uint32_t channels;
    std::uint32_t fftLength;
    std::uint32_t binsPerOutput = 1;   // sum this many consecutive fine channels per output value
};

constexpr std::uint32_t kFineSpecMaxFftLength = 2048;

cudaError_t LaunchFineSpec(const void* input, bool inputCF32, float* output,
                           const FineSpecGeometry& geometry, cudaStream_t stream);

// Coarse mode (no FFT): output[beam][channel / binsPerOutput] += sum over segments and samples of
// |X|^2 + |Y|^2. binsPerOutput here groups *coarse* channels and must divide `channels`; fftLength can be
// any size. Equals the fine-mode output summed over each channel's fine bins divided by fftLength
// (Parseval), at a fraction of the arithmetic. Used for quick-look displays of the board's channels.
cudaError_t LaunchCoarseDetect(const void* input, bool inputCF32, float* output,
                               const FineSpecGeometry& geometry, cudaStream_t stream);

}  // namespace Lft3

#endif  // LFT3_FINESPEC_DETAIL_KERNEL_HH
