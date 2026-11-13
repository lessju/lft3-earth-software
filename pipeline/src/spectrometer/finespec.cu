#include "spectrometer/finespec.hh"

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
