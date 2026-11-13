#include "spectrometer/finespec.hh"

#include <cstdlib>
#include <string>

namespace Lft3 {

namespace {

__device__ inline float2 ToFloat2(const char2 v) {
    return make_float2(static_cast<float>(v.x), static_cast<float>(v.y));
}
__device__ inline float2 ToFloat2(const float2 v) {
    return v;
}

// One CUDA block per (coarse channel, beam); fftLength/2 threads, each owning one butterfly per stage
// and two output bins (tid and tid + fftLength/2). Shared memory holds X and Y (2 * fftLength float2).
//
// Memory traffic per input block is one coalesced read of the voltages (the samples of one channel and
// segment are contiguous: fftLength * 2 pols) plus one read-modify-write of the output row, which is
// what makes this real-time where separate FFT / detect / integrate passes are not.
template <typename InT>
__device__ inline void LoadPair(const InT* row, std::uint32_t s, float2& x, float2& y) {
    x = ToFloat2(row[2 * s + 0]);
    y = ToFloat2(row[2 * s + 1]);
}
// CI8: X and Y of one sample are 4 adjacent bytes; one 32-bit load instead of two 16-bit loads.
template <>
__device__ inline void LoadPair<char2>(const char2* row, std::uint32_t s, float2& x, float2& y) {
    const char4 v = reinterpret_cast<const char4*>(row)[s];
    x = make_float2(static_cast<float>(v.x), static_cast<float>(v.y));
    y = make_float2(static_cast<float>(v.z), static_cast<float>(v.w));
}

template <typename InT>
__global__ void FineSpecKernel(const InT* __restrict__ input, float* __restrict__ output, FineSpecGeometry g,
                               std::uint32_t logLength) {
    extern __shared__ float2 shared[];
    const std::uint32_t S = g.fftLength;
    const std::uint32_t half = S / 2;
    float2* X = shared;
    float2* Y = shared + S;

    const std::uint32_t channel = blockIdx.x;
    const std::uint32_t beam = blockIdx.y;
    const std::uint32_t tid = threadIdx.x;

    float acc0 = 0.0f;   // bin tid
    float acc1 = 0.0f;   // bin tid + S/2

    for (std::uint32_t m = 0; m < g.segments; m++) {
        const InT* row = input + ((static_cast<std::uint64_t>(beam) * g.channels + channel) * g.rowLength +
                                  static_cast<std::uint64_t>(m) * S) * 2;

        // Load samples tid and tid + S/2 into bit-reversed positions (decimation in time).
        for (std::uint32_t s = tid; s < S; s += half) {
            const std::uint32_t r = __brev(s) >> (32 - logLength);
            float2 x, y;
            LoadPair(row, s, x, y);
            X[r] = x;
            Y[r] = y;
        }
        __syncthreads();

        for (std::uint32_t len = 2; len <= S; len <<= 1) {
            const std::uint32_t h = len >> 1;
            const std::uint32_t pos = tid & (h - 1);
            const std::uint32_t i = (tid / h) * len + pos;
            const std::uint32_t j = i + h;
            float sn, cs;
            sincospif(-2.0f * static_cast<float>(pos) / static_cast<float>(len), &sn, &cs);   // e^{-2 pi i pos/len}

            const float2 xj = X[j], yj = Y[j];
            const float2 tx = make_float2(cs * xj.x - sn * xj.y, cs * xj.y + sn * xj.x);
            const float2 ty = make_float2(cs * yj.x - sn * yj.y, cs * yj.y + sn * yj.x);
            const float2 xi = X[i], yi = Y[i];
            X[i] = make_float2(xi.x + tx.x, xi.y + tx.y);
            X[j] = make_float2(xi.x - tx.x, xi.y - tx.y);
            Y[i] = make_float2(yi.x + ty.x, yi.y + ty.y);
            Y[j] = make_float2(yi.x - ty.x, yi.y - ty.y);
            __syncthreads();
        }

        const float2 x0 = X[tid], y0 = Y[tid], x1 = X[tid + half], y1 = Y[tid + half];
        acc0 += x0.x * x0.x + x0.y * x0.y + y0.x * y0.x + y0.y * y0.y;
        acc1 += x1.x * x1.x + x1.y * x1.y + y1.x * y1.x + y1.y * y1.y;
        __syncthreads();   // before the next segment overwrites shared memory
    }

    // fftshift: bin b goes to (b + S/2) mod S, so bin tid -> tid + S/2 and bin tid + S/2 -> tid.
    if (g.binsPerOutput == 1) {
        float* out = output + (static_cast<std::uint64_t>(beam) * g.channels + channel) * S;
        out[tid + half] += acc0;
        out[tid] += acc1;
    } else {
        const std::uint64_t perBeam = static_cast<std::uint64_t>(g.channels) * S / g.binsPerOutput;
        const std::uint64_t fine = static_cast<std::uint64_t>(channel) * S;
        float* out = output + beam * perBeam;
        atomicAdd(out + (fine + tid + half) / g.binsPerOutput, acc0);
        atomicAdd(out + (fine + tid) / g.binsPerOutput, acc1);
    }
}

// One CUDA block per (coarse channel, beam): threads stride over the samples of every segment,
// accumulate |X|^2 + |Y|^2, reduce in shared memory, and one atomic add per block.
template <typename InT>
__global__ void CoarseDetectKernel(const InT* __restrict__ input, float* __restrict__ output, FineSpecGeometry g) {
    __shared__ float partial[256];
    const std::uint32_t channel = blockIdx.x;
    const std::uint32_t beam = blockIdx.y;
    float acc = 0.0f;
    for (std::uint32_t m = 0; m < g.segments; m++) {
        const InT* row = input + ((static_cast<std::uint64_t>(beam) * g.channels + channel) * g.rowLength +
                                  static_cast<std::uint64_t>(m) * g.fftLength) * 2;
        for (std::uint32_t s = threadIdx.x; s < g.fftLength; s += blockDim.x) {
            const float2 x = ToFloat2(row[2 * s + 0]);
            const float2 y = ToFloat2(row[2 * s + 1]);
            acc += x.x * x.x + x.y * x.y + y.x * y.x + y.y * y.y;
        }
    }
    partial[threadIdx.x] = acc;
    __syncthreads();
    for (std::uint32_t stride = blockDim.x / 2; stride > 0; stride >>= 1) {
        if (threadIdx.x < stride) {
            partial[threadIdx.x] += partial[threadIdx.x + stride];
        }
        __syncthreads();
    }
    if (threadIdx.x == 0) {
        const std::uint64_t perBeam = g.channels / g.binsPerOutput;
        atomicAdd(output + beam * perBeam + channel / g.binsPerOutput, partial[0]);
    }
}


// ---------------------------------------------------------------------------------------------------------
// Warp FFT (fftLength = R * 32, R = 2, 4, 8): one warp per (coarse channel, beam), entirely in registers,
// no shared memory and no __syncthreads (the shared-memory kernel above is bound by its 56 barriers per block).
//
// Cooley-Tukey with N = R * 32, input n = j + 32 k (lane j, register k), output f = q + R m:
//   X[q + R m] = sum_j W_32^{j m} [ W_N^{j q} sum_k x[j + 32 k] W_R^{k q} ]
// 1. R-point DFT over the lane's own registers (k -> q), 2. twiddle W_N^{j q}, 3. for each q a 32-point DFT
// across the lanes (radix-2 decimation in frequency with __shfl_xor), which leaves lane l holding m = bitrev5(l).
// X and Y are transformed together; |X|^2 + |Y|^2 is accumulated over segments in registers.
// Loads: register k of all lanes = samples 32k .. 32k+31 = 128 contiguous bytes (X, Y interleaved).

__device__ inline float2 CMul(float2 a, float2 b) { return make_float2(a.x * b.x - a.y * b.y, a.x * b.y + a.y * b.x); }

template <int R>
__global__ void FineSpecWarpKernel(const char2* __restrict__ input, float* __restrict__ output, FineSpecGeometry g) {
    constexpr int N = R * 32;
    const std::uint32_t lane = threadIdx.x & 31;
    const std::uint32_t channel = blockIdx.x * (blockDim.x / 32) + threadIdx.x / 32;
    const std::uint32_t beam = blockIdx.y;
    if (channel >= g.channels) return;

    // Per-lane constants: W_R^{kq}, W_N^{lane q}, and the 5 butterfly twiddles of the lane.
    float2 wr[R][R];
#pragma unroll
    for (int k = 0; k < R; k++)
#pragma unroll
        for (int q = 0; q < R; q++) {
            float sn, cs;
            sincospif(-2.0f * static_cast<float>((k * q) % R) / static_cast<float>(R), &sn, &cs);
            wr[k][q] = make_float2(cs, sn);
        }
    float2 wn[R];
#pragma unroll
    for (int q = 0; q < R; q++) {
        float sn, cs;
        sincospif(-2.0f * static_cast<float>(lane * q) / static_cast<float>(N), &sn, &cs);
        wn[q] = make_float2(cs, sn);
    }
    float2 wb[5];
#pragma unroll
    for (int s = 0; s < 5; s++) {
        const std::uint32_t h = 16u >> s;
        float sn, cs;
        sincospif(-2.0f * static_cast<float>(lane & (h - 1)) / static_cast<float>(2 * h), &sn, &cs);
        wb[s] = make_float2(cs, sn);
    }

    float acc[R];
#pragma unroll
    for (int q = 0; q < R; q++) acc[q] = 0.0f;

    const char4* row = reinterpret_cast<const char4*>(input) +
                       (static_cast<std::uint64_t>(beam) * g.channels + channel) * g.rowLength;
    for (std::uint32_t m = 0; m < g.segments; m++) {
        float2 x[R], y[R];
#pragma unroll
        for (int k = 0; k < R; k++) {
            const char4 v = row[m * N + lane + 32 * k];
            x[k] = make_float2(v.x, v.y);
            y[k] = make_float2(v.z, v.w);
        }
        float2 X[R], Y[R];
#pragma unroll
        for (int q = 0; q < R; q++) {
            float2 sx = make_float2(0.f, 0.f), sy = make_float2(0.f, 0.f);
#pragma unroll
            for (int k = 0; k < R; k++) {
                const float2 a = CMul(x[k], wr[k][q]), b = CMul(y[k], wr[k][q]);
                sx.x += a.x; sx.y += a.y; sy.x += b.x; sy.y += b.y;
            }
            X[q] = CMul(sx, wn[q]);
            Y[q] = CMul(sy, wn[q]);
        }
#pragma unroll
        for (int s = 0; s < 5; s++) {
            const std::uint32_t h = 16u >> s;
            const bool upper = lane & h;
#pragma unroll
            for (int q = 0; q < R; q++) {
                const float2 ox = make_float2(__shfl_xor_sync(0xffffffffu, X[q].x, h), __shfl_xor_sync(0xffffffffu, X[q].y, h));
                const float2 oy = make_float2(__shfl_xor_sync(0xffffffffu, Y[q].x, h), __shfl_xor_sync(0xffffffffu, Y[q].y, h));
                if (upper) {
                    X[q] = CMul(make_float2(ox.x - X[q].x, ox.y - X[q].y), wb[s]);
                    Y[q] = CMul(make_float2(oy.x - Y[q].x, oy.y - Y[q].y), wb[s]);
                } else {
                    X[q] = make_float2(X[q].x + ox.x, X[q].y + ox.y);
                    Y[q] = make_float2(Y[q].x + oy.x, Y[q].y + oy.y);
                }
            }
        }
#pragma unroll
        for (int q = 0; q < R; q++) acc[q] += X[q].x * X[q].x + X[q].y * X[q].y + Y[q].x * Y[q].x + Y[q].y * Y[q].y;
    }

    const std::uint32_t mIdx = __brev(lane) >> 27;   // this lane's 32-point output index
    const std::uint64_t perBeam = static_cast<std::uint64_t>(g.channels) * N / g.binsPerOutput;
#pragma unroll
    for (int q = 0; q < R; q++) {
        const std::uint32_t bin = (q + R * mIdx + N / 2) % N;   // fftshift
        const std::uint64_t fine = static_cast<std::uint64_t>(channel) * N + bin;
        if (g.binsPerOutput == 1) {
            output[static_cast<std::uint64_t>(beam) * perBeam + fine] += acc[q];
        } else {
            atomicAdd(output + beam * perBeam + fine / g.binsPerOutput, acc[q]);
        }
    }
}

template <int R>
cudaError_t LaunchWarp(const void* input, float* output, const FineSpecGeometry& g, cudaStream_t stream) {
    constexpr std::uint32_t kWarpsPerBlock = 8;
    const dim3 grid((g.channels + kWarpsPerBlock - 1) / kWarpsPerBlock, g.beams);
    FineSpecWarpKernel<R><<<grid, kWarpsPerBlock * 32, 0, stream>>>(static_cast<const char2*>(input), output, g);
    return cudaGetLastError();
}

}  // namespace

cudaError_t LaunchCoarseDetect(const void* input, bool inputCF32, float* output,
                               const FineSpecGeometry& geometry, cudaStream_t stream) {
    FineSpecGeometry g = geometry;
    if (g.rowLength == 0) g.rowLength = g.segments * g.fftLength;
    if (g.rowLength < g.segments * g.fftLength || g.fftLength == 0 || g.binsPerOutput == 0 || g.channels % g.binsPerOutput != 0) {
        return cudaErrorInvalidValue;
    }
    std::uint32_t threads = 32;
    while (threads < g.fftLength && threads < 256) threads <<= 1;   // power of two for the reduction
    const dim3 grid(g.channels, g.beams);
    if (inputCF32) {
        CoarseDetectKernel<float2><<<grid, threads, 0, stream>>>(static_cast<const float2*>(input), output, g);
    } else {
        CoarseDetectKernel<char2><<<grid, threads, 0, stream>>>(static_cast<const char2*>(input), output, g);
    }
    return cudaGetLastError();
}

cudaError_t LaunchFineSpec(const void* input, bool inputCF32, float* output,
                           const FineSpecGeometry& geometry, cudaStream_t stream) {
    FineSpecGeometry g = geometry;
    if (g.rowLength == 0) g.rowLength = g.segments * g.fftLength;
    if (g.rowLength < g.segments * g.fftLength || g.fftLength < 2 || g.fftLength > kFineSpecMaxFftLength || (g.fftLength & (g.fftLength - 1)) != 0 ||
        g.binsPerOutput == 0 || (static_cast<std::uint64_t>(g.channels) * g.fftLength) % g.binsPerOutput != 0) {
        return cudaErrorInvalidValue;
    }
    // CI8 input, 64/128/256 points: the warp kernel (no shared memory, no barriers). Otherwise the shared-memory
    // kernel. LFT3_FINESPEC_KERNEL=shared forces the latter (comparisons, benchmarks).
    static const bool forceShared = [] {
        const char* e = std::getenv("LFT3_FINESPEC_KERNEL");
        return e && std::string(e) == "shared";
    }();
    if (!inputCF32 && !forceShared) {
        switch (g.fftLength) {
            case 64: return LaunchWarp<2>(input, output, g, stream);
            case 128: return LaunchWarp<4>(input, output, g, stream);
            case 256: return LaunchWarp<8>(input, output, g, stream);
            default: break;
        }
    }
    std::uint32_t logLength = 0;
    while ((1u << logLength) < g.fftLength) logLength++;

    const dim3 grid(g.channels, g.beams);
    const dim3 block(g.fftLength / 2);
    const std::size_t sharedBytes = 2 * g.fftLength * sizeof(float2);
    if (inputCF32) {
        FineSpecKernel<float2><<<grid, block, sharedBytes, stream>>>(static_cast<const float2*>(input), output, g, logLength);
    } else {
        FineSpecKernel<char2><<<grid, block, sharedBytes, stream>>>(static_cast<const char2*>(input), output, g, logLength);
    }
    return cudaGetLastError();
}

}  // namespace Lft3
