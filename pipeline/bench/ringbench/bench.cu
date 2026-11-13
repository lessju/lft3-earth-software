// Ring-buffer design measurements on GB10 (docs/runtime.md §7).
//  1. GPU read/write/copy bandwidth on: cudaMalloc, cudaMallocHost, malloc+cudaHostRegister,
//     POSIX shm + cudaHostRegister, THP (madvise) anon + cudaHostRegister.
//  2. Scatter cost: packets (t, 1024 ch, 4 inputs x IQ int8) -> ring [beam][ch][t mod T][pol], for a range
//     of T; naive (thread per packet-channel) vs smem-tiled (32 t per thread block -> 128 B runs).
#include <cuda_runtime.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <unistd.h>

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#define CK(x)                                                                                 \
    do {                                                                                      \
        cudaError_t e = (x);                                                                  \
        if (e != cudaSuccess) {                                                               \
            fprintf(stderr, "%s:%d %s: %s\n", __FILE__, __LINE__, #x, cudaGetErrorString(e)); \
            exit(1);                                                                          \
        }                                                                                     \
    } while (0)

// ---------- bandwidth kernels (uint4 = 16 B per access, grid-stride) ----------
__global__ void kRead(const uint4* __restrict__ p, size_t n, unsigned* out) {
    unsigned acc = 0;
    for (size_t i = blockIdx.x * (size_t)blockDim.x + threadIdx.x; i < n; i += (size_t)gridDim.x * blockDim.x) {
        uint4 v = p[i];
        acc ^= v.x ^ v.y ^ v.z ^ v.w;
    }
    if (acc == 0x12345678) *out = acc;
}
__global__ void kWrite(uint4* __restrict__ p, size_t n) {
    for (size_t i = blockIdx.x * (size_t)blockDim.x + threadIdx.x; i < n; i += (size_t)gridDim.x * blockDim.x)
        p[i] = make_uint4(i, i, i, i);
}
__global__ void kCopy(const uint4* __restrict__ a, uint4* __restrict__ b, size_t n) {
    for (size_t i = blockIdx.x * (size_t)blockDim.x + threadIdx.x; i < n; i += (size_t)gridDim.x * blockDim.x)
        b[i] = a[i];
}

static float timeIt(cudaStream_t s, int reps, auto&& launch) {
    cudaEvent_t a, b;
    CK(cudaEventCreate(&a));
    CK(cudaEventCreate(&b));
    launch();  // warm-up (page faults / TLB)
    CK(cudaStreamSynchronize(s));
    CK(cudaEventRecord(a, s));
    for (int r = 0; r < reps; ++r) launch();
    CK(cudaEventRecord(b, s));
    CK(cudaEventSynchronize(b));
    float ms;
    CK(cudaEventElapsedTime(&ms, a, b));
    cudaEventDestroy(a);
    cudaEventDestroy(b);
    return ms / reps;
}

enum Kind { DEV, PINNED, REG_MALLOC, REG_SHM, REG_THP };
static const char* kindName[] = {"cudaMalloc", "cudaMallocHost", "malloc+register", "shm+register",
                                 "THP-anon+register"};

struct Buf {
    void* p = nullptr;
    size_t n = 0;
    Kind k;
    int fd = -1;
    std::string shmName;
};

static Buf alloc(Kind k, size_t bytes, const char* tag) {
    Buf b;
    b.n = bytes;
    b.k = k;
    switch (k) {
        case DEV: CK(cudaMalloc(&b.p, bytes)); break;
        case PINNED: CK(cudaMallocHost(&b.p, bytes)); break;
        case REG_MALLOC:
            b.p = aligned_alloc(4096, bytes);
            memset(b.p, 0, bytes);
            CK(cudaHostRegister(b.p, bytes, cudaHostRegisterDefault));
            break;
        case REG_SHM: {
            b.shmName = std::string("/lft3-ringbench-") + tag;
            shm_unlink(b.shmName.c_str());
            b.fd = shm_open(b.shmName.c_str(), O_CREAT | O_RDWR, 0600);
            if (b.fd < 0 || ftruncate(b.fd, bytes) != 0) { perror("shm"); exit(1); }
            b.p = mmap(nullptr, bytes, PROT_READ | PROT_WRITE, MAP_SHARED, b.fd, 0);
            if (b.p == MAP_FAILED) { perror("mmap"); exit(1); }
            memset(b.p, 0, bytes);
            CK(cudaHostRegister(b.p, bytes, cudaHostRegisterDefault));
            break;
        }
        case REG_THP: {
            b.p = mmap(nullptr, bytes, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
            if (b.p == MAP_FAILED) { perror("mmap"); exit(1); }
            madvise(b.p, bytes, MADV_HUGEPAGE);
            memset(b.p, 0, bytes);
            CK(cudaHostRegister(b.p, bytes, cudaHostRegisterDefault));
            break;
        }
    }
    if (k == DEV) CK(cudaMemset(b.p, 0, bytes));
    return b;
}
static void freeBuf(Buf& b) {
    switch (b.k) {
        case DEV: cudaFree(b.p); break;
        case PINNED: cudaFreeHost(b.p); break;
        case REG_MALLOC: cudaHostUnregister(b.p); free(b.p); break;
        case REG_SHM: cudaHostUnregister(b.p); munmap(b.p, b.n); close(b.fd); shm_unlink(b.shmName.c_str()); break;
        case REG_THP: cudaHostUnregister(b.p); munmap(b.p, b.n); break;
    }
}
static void* devPtr(const Buf& b) {
    if (b.k == DEV) return b.p;
    void* d;
    CK(cudaHostGetDevicePointer(&d, b.p, 0));
    return d;
}

static void bandwidth(size_t bytes, cudaStream_t s, int sms, unsigned* dOut) {
    printf("\n== 1. GPU bandwidth, %.1f GB buffers (GB/s; copy counts read+write) ==\n", bytes / 1e9);
    printf("%-20s %8s %8s %8s\n", "memory", "read", "write", "copy");
    for (int k = DEV; k <= REG_THP; ++k) {
        Buf a = alloc((Kind)k, bytes, "a"), b = alloc((Kind)k, bytes, "b");
        auto* pa = (uint4*)devPtr(a);
        auto* pb = (uint4*)devPtr(b);
        size_t n = bytes / 16;
        int grid = sms * 8, blk = 512;
        float r = timeIt(s, 5, [&] { kRead<<<grid, blk, 0, s>>>(pa, n, dOut); });
        float w = timeIt(s, 5, [&] { kWrite<<<grid, blk, 0, s>>>(pb, n); });
        float c = timeIt(s, 5, [&] { kCopy<<<grid, blk, 0, s>>>(pa, pb, n); });
        printf("%-20s %8.1f %8.1f %8.1f\n", kindName[k], bytes / r / 1e6, bytes / w / 1e6, 2 * bytes / c / 1e6);
        freeBuf(a);
        freeBuf(b);
    }
}

// ---------- scatter ----------
// Packet payload: 1024 channels x 4 inputs x (I,Q) int8 = 8 bytes per channel, 8192 B per packet.
// Inputs 0,1 = beam 0 X,Y; 2,3 = beam 1 X,Y. Ring: [beam(2)][ch(C)][T][pol(2) x IQ] -> 4 B per (beam,ch,t).
constexpr int kCh = 1024;
constexpr int kPktBytes = kCh * 8;

// naive: one thread per (packet, channel): read 8 B, write 2 x 4 B.
__global__ void kScatterNaive(const uint2* __restrict__ pkts, int nPkts, uint64_t t0, uint32_t* __restrict__ ring,
                              uint64_t T, int C) {
    size_t i = blockIdx.x * (size_t)blockDim.x + threadIdx.x;
    if (i >= (size_t)nPkts * kCh) return;
    int pk = i / kCh, ch = i % kCh;
    uint2 v = pkts[i];
    uint64_t tt = (t0 + pk) % T;
    ring[((size_t)0 * C + ch) * T + tt] = v.x;
    ring[((size_t)1 * C + ch) * T + tt] = v.y;
}

// tiled: block = 32 consecutive packets x 32 channels; smem transpose; each warp writes 32 consecutive t
// of one (beam, ch) = 128 B coalesced.
__global__ void kScatterTiled(const uint2* __restrict__ pkts, int nPkts, uint64_t t0, uint32_t* __restrict__ ring,
                              uint64_t T, int C) {
    __shared__ uint2 tile[32][33];
    int pk0 = blockIdx.y * 32, ch0 = blockIdx.x * 32;
    int tx = threadIdx.x, ty = threadIdx.y;  // 32 x 8
    for (int r = ty; r < 32; r += 8) {
        int pk = pk0 + r;
        tile[r][tx] = pk < nPkts ? pkts[(size_t)pk * kCh + ch0 + tx] : make_uint2(0, 0);
    }
    __syncthreads();
    for (int r = ty; r < 32; r += 8) {  // r = channel within tile, tx = packet within tile
        int pk = pk0 + tx;
        if (pk >= nPkts) continue;
        uint64_t tt = (t0 + pk) % T;
        uint2 v = tile[tx][r];
        int ch = ch0 + r;
        ring[((size_t)0 * C + ch) * T + tt] = v.x;
        ring[((size_t)1 * C + ch) * T + tt] = v.y;
    }
}

static void scatter(cudaStream_t s, Kind ringKind, int C) {
    // C = channels kept in ring (multiple of 1024). One "bindex" worth of packets per t per 1024 channels.
    printf("\n== 2. Scatter into ring (%s), %d channels, payload in pinned host memory ==\n", kindName[ringKind], C);
    printf("required: 7.50 GB/s of payload (full band, 6144 ch). Rate below = payload GB/s scattered.\n");
    printf("%-10s %10s %12s %12s %10s %10s\n", "T", "ring GB", "naive GB/s", "tiled GB/s", "naive x", "tiled x");
    const int burst = 1024;                // packets per DAQIRI burst
    const int poolPkts = 8192;             // 64 MB of payload, reused
    Buf pool = alloc(PINNED, (size_t)poolPkts * kPktBytes, "pool");
    for (size_t i = 0; i < pool.n / 4; ++i) ((uint32_t*)pool.p)[i] = (uint32_t)(i * 2654435761u);
    auto* dpool = (const uint2*)devPtr(pool);
    int packetsPerT = C / kCh;
    for (uint64_t T : {128ull, 1024ull, 8192ull, 65536ull, 131072ull, 262144ull, 524288ull}) {
        size_t ringBytes = (size_t)2 * C * T * 4;
        if (ringBytes > 30e9) continue;
        Buf ring = alloc(ringKind, ringBytes, "ring");
        auto* dring = (uint32_t*)devPtr(ring);
        // Each burst: `burst` packets = burst/packetsPerT time steps x packetsPerT channel groups. We model it as
        // packetsPerT independent channel groups each with burst/packetsPerT consecutive t (same as real data).
        int tPerBurst = burst / packetsPerT;
        uint64_t t = 0;
        int nb = 64;
        auto run = [&](bool tiled) {
            for (int b = 0; b < nb; ++b) {
                for (int g = 0; g < packetsPerT; ++g) {
                    const uint2* src = dpool + (size_t)(((b * burst + g * tPerBurst) % poolPkts)) * kCh;
                    uint32_t* dst = dring + (size_t)g * kCh * T;  // channel group g: rows g*1024.. (beam 0);
                    // beam-1 rows are at + C*T inside the kernel; offsetting by g*1024*T keeps both beams right.
                    if (tiled) {
                        dim3 grid(kCh / 32, (tPerBurst + 31) / 32), blk(32, 8);
                        kScatterTiled<<<grid, blk, 0, s>>>(src, tPerBurst, t, dst, T, C);
                    } else {
                        size_t n = (size_t)tPerBurst * kCh;
                        kScatterNaive<<<(n + 255) / 256, 256, 0, s>>>(src, tPerBurst, t, dst, T, C);
                    }
                }
                t += tPerBurst;
            }
        };
        double payload = (double)nb * burst * kPktBytes;
        float mn = timeIt(s, 5, [&] { run(false); });
        float mt = timeIt(s, 5, [&] { run(true); });
        double gn = payload / mn / 1e6, gt = payload / mt / 1e6;
        printf("%-10llu %10.2f %12.1f %12.1f %10.1f %10.1f\n", (unsigned long long)T, ringBytes / 1e9, gn, gt,
               gn / 7.5, gt / 7.5);
        freeBuf(ring);
    }
    freeBuf(pool);
}

int main(int argc, char** argv) {
    size_t bwBytes = (argc > 1 ? atof(argv[1]) : 4.0) * 1e9;
    cudaDeviceProp p;
    CK(cudaGetDeviceProperties(&p, 0));
    int pageable = 0, ats = 0;
    cudaDeviceGetAttribute(&pageable, cudaDevAttrPageableMemoryAccess, 0);
    cudaDeviceGetAttribute(&ats, cudaDevAttrPageableMemoryAccessUsesHostPageTables, 0);
    printf("%s, %d SMs, L2 %d MB, pageableMemoryAccess=%d usesHostPageTables=%d\n", p.name, p.multiProcessorCount,
           p.l2CacheSize >> 20, pageable, ats);
    cudaStream_t s;
    CK(cudaStreamCreate(&s));
    unsigned* dOut;
    CK(cudaMalloc(&dOut, 4));
    bandwidth(bwBytes, s, p.multiProcessorCount, dOut);
    scatter(s, DEV, 6144);
    scatter(s, REG_SHM, 6144);
    return 0;
}
