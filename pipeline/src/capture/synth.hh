#ifndef LFT3_CAPTURE_SYNTH_HH
#define LFT3_CAPTURE_SYNTH_HH

// Synthetic source with non-repeating noise (lft3-capture --source synthetic --noise random).
//
// The default synthetic generator replays 1024 spectra of noise from host memory, so every block is the same
// and integrated spectra do not fluctuate: fine for parity tests, useless for testing a search (the fixed
// pattern adds up coherently at zero drift). This kernel overwrites a whole ring block with noise from a
// counter-based hash of (seed, block, beam, channel, t), so no two blocks repeat and any block can be
// regenerated exactly, plus an optional tone with a linear frequency drift in one coarse channel and input.
//
// Noise: each I/Q component is an Irwin-Hall sum of 4 uniform bytes, scaled to `noiseSigma` (close to Gaussian
// for detection statistics), rounded and clipped to int8.
// Tone: amplitude * exp(2 pi i phi(n)), phi(n) = f0 n + chirp n^2 / 2 cycles, n = absolute spectrum count;
// chirp = drift [Hz/s] / (152587.890625 Hz)^2. The frequency wraps inside the coarse channel (+-0.5 cycles).

#include <cstdint>

#include <cuda_runtime.h>

namespace Lft3 {

struct SynthParams {
    std::uint32_t channelCount = 0;      // C (channels per block)
    std::uint32_t spectraPerBlock = 0;   // T
    std::uint32_t channelStart = 0;      // absolute coarse channel of channel 0
    std::uint8_t inputBeam[4] = {0, 0, 1, 1};
    std::uint8_t inputPol[4] = {0, 1, 0, 1};
    std::uint64_t seed = 1;
    float noiseSigma = 8.0f;             // per I or Q component, in int8 units
    std::int64_t toneChannel = -1;       // absolute coarse channel, -1: no tone
    std::uint32_t toneInput = 0;         // ADC input 0..3
    double toneAmplitude = 0.0;          // per component peak, int8 units
    double toneFrequency = 0.0;          // cycles/sample at n = 0
    double toneChirp = 0.0;              // cycles/sample^2
};

// Fills block[beam][channel][t][pol] (complex int8) of block `blockIndex` (absolute: t0 = blockIndex * T).
cudaError_t LaunchSynthesizeBlock(std::uint8_t* block, std::uint64_t blockIndex, const SynthParams& p,
                                  cudaStream_t stream);

// Host reference of one complex sample (beam, channel, t, pol) for tests: {re, im}.
void SynthesizeSampleHost(std::uint64_t blockIndex, std::uint32_t beam, std::uint32_t channel, std::uint32_t t,
                          std::uint32_t pol, const SynthParams& p, std::int8_t out[2]);

}  // namespace Lft3

#endif  // LFT3_CAPTURE_SYNTH_HH
