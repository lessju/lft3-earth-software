#include "capture/synth.hh"

#include <cmath>

namespace Lft3 {
namespace {

__host__ __device__ inline std::uint64_t SplitMix64(std::uint64_t x) {
    x += 0x9E3779B97F4A7C15ULL;
    x = (x ^ (x >> 30)) * 0xBF58476D1CE4E5B9ULL;
    x = (x ^ (x >> 27)) * 0x94D049BB133111EBULL;
    return x ^ (x >> 31);
}

// Irwin-Hall of the 4 bytes of a 32-bit word: mean 510, standard deviation sqrt(4 * (256^2 - 1) / 12).
__host__ __device__ inline float IrwinHall4(std::uint32_t w) {
    constexpr float kInvStd = 1.0f / 147.80054f;
    const int s = static_cast<int>(w & 0xFF) + static_cast<int>((w >> 8) & 0xFF) + static_cast<int>((w >> 16) & 0xFF) +
                  static_cast<int>(w >> 24);
    return (static_cast<float>(s) - 510.0f) * kInvStd;
}

__host__ __device__ inline std::int8_t Quantise(float v) {
    const float r = rintf(fminf(127.0f, fmaxf(-127.0f, v)));
    return static_cast<std::int8_t>(r);
}

// Tone phase in cycles, reduced to [0, 1): f0 n + chirp n^2 / 2, each term reduced separately (doubles).
__host__ __device__ inline double TonePhase(double f0, double chirp, double n) {
    const double a = f0 * n;
    const double b = 0.5 * chirp * n * n;
    double ph = (a - floor(a)) + (b - floor(b));
    return ph - floor(ph);
}

// The 4 noise values (X re, X im, Y re, Y im) of one (beam, channel, t).
__host__ __device__ inline void Noise4(std::uint64_t seed, std::uint64_t g, float sigma, float v[4]) {
    const std::uint64_t h = SplitMix64(seed ^ SplitMix64(2 * g));
    const std::uint64_t k = SplitMix64(seed ^ SplitMix64(2 * g + 1));
    v[0] = sigma * IrwinHall4(static_cast<std::uint32_t>(h));
    v[1] = sigma * IrwinHall4(static_cast<std::uint32_t>(h >> 32));
    v[2] = sigma * IrwinHall4(static_cast<std::uint32_t>(k));
    v[3] = sigma * IrwinHall4(static_cast<std::uint32_t>(k >> 32));
}

__global__ void SynthesizeKernel(std::uint32_t* block, std::uint64_t blockIndex, SynthParams p, std::uint32_t toneBeam,
                                 std::uint32_t tonePol) {
    const std::uint64_t C = p.channelCount, T = p.spectraPerBlock;
    const std::uint64_t idx = static_cast<std::uint64_t>(blockIdx.x) * blockDim.x + threadIdx.x;   // (beam, c, t)
    if (idx >= 2 * C * T) return;
    const std::uint64_t t = idx % T;
    const std::uint64_t c = (idx / T) % C;
    const std::uint64_t beam = idx / (T * C);
    float v[4];
    Noise4(p.seed, blockIndex * 2 * C * T + idx, p.noiseSigma, v);
    if (p.toneChannel >= 0 && beam == toneBeam && static_cast<std::int64_t>(p.channelStart + c) == p.toneChannel) {
        const double n = static_cast<double>(blockIndex * T + t);
        const double ph = TonePhase(p.toneFrequency, p.toneChirp, n);
        double s, co;
        sincospi(2.0 * ph, &s, &co);
        v[2 * tonePol] += static_cast<float>(p.toneAmplitude * co);
        v[2 * tonePol + 1] += static_cast<float>(p.toneAmplitude * s);
    }
    const std::uint32_t packed = static_cast<std::uint8_t>(Quantise(v[0])) |
                                 (static_cast<std::uint32_t>(static_cast<std::uint8_t>(Quantise(v[1]))) << 8) |
                                 (static_cast<std::uint32_t>(static_cast<std::uint8_t>(Quantise(v[2]))) << 16) |
                                 (static_cast<std::uint32_t>(static_cast<std::uint8_t>(Quantise(v[3]))) << 24);
    // Element (beam, c, t, pol 0) starts at byte 4 * idx: [pol][re, im] int8 = one 32-bit word.
    block[idx] = packed;
}

}  // namespace

cudaError_t LaunchSynthesizeBlock(std::uint8_t* block, std::uint64_t blockIndex, const SynthParams& p,
                                  cudaStream_t stream) {
    const std::uint64_t n = 2ULL * p.channelCount * p.spectraPerBlock;
    constexpr unsigned kThreads = 256;
    const std::uint32_t toneInput = p.toneInput & 3;
    SynthesizeKernel<<<static_cast<unsigned>((n + kThreads - 1) / kThreads), kThreads, 0, stream>>>(
        reinterpret_cast<std::uint32_t*>(block), blockIndex, p, p.inputBeam[toneInput], p.inputPol[toneInput]);
    return cudaGetLastError();
}

void SynthesizeSampleHost(std::uint64_t blockIndex, std::uint32_t beam, std::uint32_t channel, std::uint32_t t,
                          std::uint32_t pol, const SynthParams& p, std::int8_t out[2]) {
    const std::uint64_t C = p.channelCount, T = p.spectraPerBlock;
    const std::uint64_t idx = (static_cast<std::uint64_t>(beam) * C + channel) * T + t;
    float v[4];
    Noise4(p.seed, blockIndex * 2 * C * T + idx, p.noiseSigma, v);
    const std::uint32_t ti = p.toneInput & 3;
    if (p.toneChannel >= 0 && beam == p.inputBeam[ti] && static_cast<std::int64_t>(p.channelStart + channel) == p.toneChannel) {
        const double ph = TonePhase(p.toneFrequency, p.toneChirp, static_cast<double>(blockIndex * T + t));
        v[2 * p.inputPol[ti]] += static_cast<float>(p.toneAmplitude * std::cos(2.0 * M_PI * ph));
        v[2 * p.inputPol[ti] + 1] += static_cast<float>(p.toneAmplitude * std::sin(2.0 * M_PI * ph));
    }
    out[0] = Quantise(v[2 * pol]);
    out[1] = Quantise(v[2 * pol + 1]);
}

}  // namespace Lft3
