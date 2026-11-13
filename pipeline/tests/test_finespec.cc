#include <catch2/catch_test_macros.hpp>

#include <cmath>
#include <complex>
#include <random>
#include <vector>

#include <cuda_runtime.h>

#include "spectrometer/finespec.hh"

using namespace Lft3;

namespace {

// Reference: DFT of each segment, |X|^2 + |Y|^2, summed over segments, fftshifted.
std::vector<double> Reference(const std::vector<std::int8_t>& in, const FineSpecGeometry& g) {
    const std::uint32_t S = g.fftLength;
    std::vector<double> out(static_cast<std::size_t>(g.beams) * g.channels * S, 0.0);
    for (std::uint32_t b = 0; b < g.beams; b++)
        for (std::uint32_t c = 0; c < g.channels; c++)
            for (std::uint32_t m = 0; m < g.segments; m++) {
                // Ring row layout [beam][channel][segments * S][pol]: segment m = samples m*S .. m*S+S-1.
                const std::size_t base = ((static_cast<std::size_t>(b) * g.channels + c) * g.segments * S + m * S) * 2 * 2;
                for (std::uint32_t k = 0; k < S; k++) {
                    std::complex<double> X = 0, Y = 0;
                    for (std::uint32_t n = 0; n < S; n++) {
                        const std::complex<double> w = std::polar(1.0, -2.0 * M_PI * k * n / S);
                        X += w * std::complex<double>(in[base + n * 4 + 0], in[base + n * 4 + 1]);
                        Y += w * std::complex<double>(in[base + n * 4 + 2], in[base + n * 4 + 3]);
                    }
                    const std::uint32_t shifted = (k + S / 2) % S;
                    out[(static_cast<std::size_t>(b) * g.channels + c) * S + shifted] += std::norm(X) + std::norm(Y);
                }
            }
    return out;
}

}  // namespace

TEST_CASE("Fused fine spectrometer matches a reference DFT", "[lft3][finespec][cuda]") {
    int devices = 0;
    if (cudaGetDeviceCount(&devices) != cudaSuccess || devices == 0) {
        SKIP("No CUDA device.");
    }
    // 64, 128, 256: warp kernel; 2, 16, 1024: shared-memory kernel. 5 channels: not a multiple of the 8 warps per block.
    for (const std::uint32_t S : {2u, 16u, 64u, 128u, 256u, 1024u}) {
        const FineSpecGeometry g = {2, 3, 5, S};
        const std::size_t samples = static_cast<std::size_t>(g.beams) * g.segments * g.channels * S * 2;   // complex
        std::vector<std::int8_t> host(samples * 2);
        std::mt19937 rng(S);
        std::uniform_int_distribution<int> dist(-128, 127);
        for (auto& v : host) v = static_cast<std::int8_t>(dist(rng));

        std::int8_t* in = nullptr;
        float* out = nullptr;
        const std::size_t outCount = static_cast<std::size_t>(g.beams) * g.channels * S;
        REQUIRE(cudaMallocManaged(&in, host.size()) == cudaSuccess);
        REQUIRE(cudaMallocManaged(&out, outCount * sizeof(float)) == cudaSuccess);
        std::copy(host.begin(), host.end(), in);
        std::fill(out, out + outCount, 0.0f);

        // Two launches accumulate (integration over two blocks of identical data).
        REQUIRE(LaunchFineSpec(in, false, out, g, nullptr) == cudaSuccess);
        REQUIRE(LaunchFineSpec(in, false, out, g, nullptr) == cudaSuccess);
        REQUIRE(cudaDeviceSynchronize() == cudaSuccess);

        const auto ref = Reference(host, g);
        double worst = 0.0;
        for (std::size_t i = 0; i < outCount; i++) {
            const double want = 2.0 * ref[i];
            worst = std::max(worst, std::abs(out[i] - want) / std::max(1.0, want));
        }
        INFO("fftLength " << S << " worst relative error " << worst);
        REQUIRE(worst < 1e-4);

        cudaFree(in);
        cudaFree(out);
    }
}

TEST_CASE("Fused fine spectrometer puts a tone in the expected fine bin", "[lft3][finespec][cuda]") {
    int devices = 0;
    if (cudaGetDeviceCount(&devices) != cudaSuccess || devices == 0) {
        SKIP("No CUDA device.");
    }
    const std::uint32_t S = 128;
    const FineSpecGeometry g = {1, 1, 1, S};
    std::vector<float> host(S * 2 * 2, 0.0f);
    const double f = 13.0 / S;   // +13 fine bins above the centre
    for (std::uint32_t n = 0; n < S; n++) {
        host[n * 4 + 2] = static_cast<float>(100 * std::cos(2 * M_PI * f * n));   // Y pol only
        host[n * 4 + 3] = static_cast<float>(100 * std::sin(2 * M_PI * f * n));
    }
    float *in = nullptr, *out = nullptr;
    REQUIRE(cudaMallocManaged(&in, host.size() * sizeof(float)) == cudaSuccess);
    REQUIRE(cudaMallocManaged(&out, S * sizeof(float)) == cudaSuccess);
    std::copy(host.begin(), host.end(), in);
    std::fill(out, out + S, 0.0f);
    REQUIRE(LaunchFineSpec(in, true, out, g, nullptr) == cudaSuccess);
    REQUIRE(cudaDeviceSynchronize() == cudaSuccess);
    std::uint32_t peak = 0;
    for (std::uint32_t i = 1; i < S; i++) if (out[i] > out[peak]) peak = i;
    REQUIRE(peak == S / 2 + 13);
    cudaFree(in);
    cudaFree(out);
}

TEST_CASE("Fused fine spectrometer can sum fine channels per output", "[lft3][finespec][cuda]") {
    int devices = 0;
    if (cudaGetDeviceCount(&devices) != cudaSuccess || devices == 0) {
        SKIP("No CUDA device.");
    }
    const std::uint32_t S = 64;
    FineSpecGeometry full = {2, 2, 6, S};
    FineSpecGeometry coarse = full;
    coarse.binsPerOutput = S;
    const std::size_t bytes = static_cast<std::size_t>(full.beams) * full.segments * full.channels * S * 2 * 2;
    std::int8_t* in = nullptr;
    float *a = nullptr, *b = nullptr;
    REQUIRE(cudaMallocManaged(&in, bytes) == cudaSuccess);
    REQUIRE(cudaMallocManaged(&a, full.beams * full.channels * S * sizeof(float)) == cudaSuccess);
    REQUIRE(cudaMallocManaged(&b, full.beams * full.channels * sizeof(float)) == cudaSuccess);
    std::mt19937 rng(7);
    std::uniform_int_distribution<int> dist(-128, 127);
    for (std::size_t i = 0; i < bytes; i++) in[i] = static_cast<std::int8_t>(dist(rng));
    std::fill(a, a + full.beams * full.channels * S, 0.0f);
    std::fill(b, b + full.beams * full.channels, 0.0f);
    REQUIRE(LaunchFineSpec(in, false, a, full, nullptr) == cudaSuccess);
    REQUIRE(LaunchFineSpec(in, false, b, coarse, nullptr) == cudaSuccess);
    REQUIRE(cudaDeviceSynchronize() == cudaSuccess);
    for (std::uint32_t i = 0; i < full.beams * full.channels; i++) {
        double sum = 0;
        for (std::uint32_t k = 0; k < S; k++) sum += a[i * S + k];
        REQUIRE(std::abs(b[i] - sum) / sum < 1e-5);
    }
    cudaFree(in);
    cudaFree(a);
    cudaFree(b);
}

TEST_CASE("Coarse mode equals the fine spectrum summed per channel / fftLength (Parseval)", "[lft3][finespec][cuda]") {
    int devices = 0;
    if (cudaGetDeviceCount(&devices) != cudaSuccess || devices == 0) {
        SKIP("No CUDA device.");
    }
    const std::uint32_t S = 128;
    const FineSpecGeometry fine = {2, 3, 12, S};
    FineSpecGeometry coarse = fine;
    coarse.binsPerOutput = 3;   // group 3 coarse channels per output
    const std::size_t bytes = static_cast<std::size_t>(fine.beams) * fine.segments * fine.channels * S * 2 * 2;
    std::int8_t* in = nullptr;
    float *a = nullptr, *b = nullptr;
    const std::size_t nFine = static_cast<std::size_t>(fine.beams) * fine.channels * S;
    const std::size_t nCoarse = static_cast<std::size_t>(fine.beams) * fine.channels / 3;
    REQUIRE(cudaMallocManaged(&in, bytes) == cudaSuccess);
    REQUIRE(cudaMallocManaged(&a, nFine * sizeof(float)) == cudaSuccess);
    REQUIRE(cudaMallocManaged(&b, nCoarse * sizeof(float)) == cudaSuccess);
    std::mt19937 rng(11);
    std::uniform_int_distribution<int> dist(-128, 127);
    for (std::size_t i = 0; i < bytes; i++) in[i] = static_cast<std::int8_t>(dist(rng));
    std::fill(a, a + nFine, 0.0f);
    std::fill(b, b + nCoarse, 0.0f);
    REQUIRE(LaunchFineSpec(in, false, a, fine, nullptr) == cudaSuccess);
    REQUIRE(LaunchCoarseDetect(in, false, b, coarse, nullptr) == cudaSuccess);
    REQUIRE(cudaDeviceSynchronize() == cudaSuccess);
    for (std::size_t o = 0; o < nCoarse; o++) {
        double sum = 0;
        for (std::size_t k = 0; k < 3 * S; k++) sum += a[o * 3 * S + k];
        REQUIRE(std::abs(b[o] - sum / S) / (sum / S) < 1e-4);
    }
    cudaFree(in);
    cudaFree(a);
    cudaFree(b);
}
