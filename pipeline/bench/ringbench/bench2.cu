// Follow-up: block-major ring (ring = S blocks of [beam][ch][1024 t] u32), large rings.
//  a) scatter into block-major ring (12.9 / 25.8 GB) on cudaMalloc and shm+register
//  b) reader gather for a long FFT: Nf samples per (beam, ch) spanning Nf/1024 blocks -> contiguous buffer
//  c) cross-process sharing support: CUDA IPC, VMM POSIX-fd export, and their granularity
#include <cuda.h>
#include <cuda_runtime.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <unistd.h>

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>

#define CK(x)                                                                                 \
    do {                                                                                      \
        cudaError_t e = (x);                                                                  \
        if (e != cudaSuccess) {                                                               \
            fprintf(stderr, "%s:%d %s: %s\n", __FILE__, __LINE__, #x, cudaGetErrorString(e)); \
            exit(1);                                                                          \
        }                                                                                     \
    } while (0)

constexpr int kCh = 1024, C = 6144, BT = 1024;  // channels/packet, channels, spectra per block
constexpr size_t kBlockWords = (size_t)2 * C * BT;  // u32 per block (50.3 MB)

static float timeIt(cudaStream_t s, int reps, auto&& launch) {
    cudaEvent_t a, b;
    cudaEventCreate(&a);
    cudaEventCreate(&b);
    launch();
    CK(cudaStreamSynchronize(s));
    cudaEventRecord(a, s);
    for (int r = 0; r < reps; ++r) launch();
    cudaEventRecord(b, s);
    CK(cudaEventSynchronize(b));
    float ms;
    cudaEventElapsedTime(&ms, a, b);
    return ms / reps;
}

// tiled scatter: 32 packets (consecutive t) x 32 channels per CUDA block; writes 128 B runs
__global__ void kScatter(const uint2* __restrict__ pkts, int nPkts, uint64_t t0, uint32_t* __restrict__ ring,
                         uint64_t S, int chBase) {
    __shared__ uint2 tile[32][33];
    int pk0 = blockIdx.y * 32, ch0 = blockIdx.x * 32, tx = threadIdx.x, ty = threadIdx.y;
    for (int r = ty; r < 32; r += 8) {
        int pk = pk0 + r;
        tile[r][tx] = pk < nPkts ? pkts[(size_t)pk * kCh + ch0 + tx] : make_uint2(0, 0);
    }
    __syncthreads();
    for (int r = ty; r < 32; r += 8) {
        int pk = pk0 + tx;
        if (pk >= nPkts) continue;
        uint64_t t = t0 + pk, blk = (t / BT) % S, tt = t % BT;
        uint32_t* base = ring + blk * kBlockWords;
        int ch = chBase + ch0 + r;
        uint2 v = tile[tx][r];
        base[((size_t)0 * C + ch) * BT + tt] = v.x;
        base[((size_t)1 * C + ch) * BT + tt] = v.y;
    }
}

// gather: out[row][0..Nf) = ring rows across Nf/BT consecutive blocks; row = beam*C + ch.
// One CUDA block per (row, chunk); 256 threads copy 1024 u32 (4 KB) contiguous.
__global__ void kGather(const uint32_t* __restrict__ ring, uint64_t S, uint64_t blk0, int rows, int chunks,
                        uint32_t* __restrict__ out) {
    int row = blockIdx.x, ck = blockIdx.y;
    const uint4* src = (const uint4*)(ring + ((blk0 + ck) % S) * kBlockWords + (size_t)row * BT);
    uint4* dst = (uint4*)(out + ((size_t)row * chunks + ck) * BT);
    for (int i = threadIdx.x; i < BT / 4; i += blockDim.x) dst[i] = src[i];
}

struct Ring {
    uint32_t* host = nullptr;
    uint32_t* dev = nullptr;
    size_t bytes;
    bool shm;
    int fd = -1;
};
static Ring makeRing(size_t bytes, bool shm) {
    Ring r;
    r.bytes = bytes;
    r.shm = shm;
    if (!shm) {
        CK(cudaMalloc(&r.dev, bytes));
        CK(cudaMemset(r.dev, 0, bytes));
        return r;
    }
    shm_unlink("/lft3-ringbench2");
    r.fd = shm_open("/lft3-ringbench2", O_CREAT | O_RDWR, 0600);
    if (r.fd < 0 || ftruncate(r.fd, bytes)) { perror("shm"); exit(1); }
    r.host = (uint32_t*)mmap(nullptr, bytes, PROT_READ | PROT_WRITE, MAP_SHARED, r.fd, 0);
    memset(r.host, 0, bytes);
    CK(cudaHostRegister(r.host, bytes, cudaHostRegisterDefault));
    CK(cudaHostGetDevicePointer((void**)&r.dev, r.host, 0));
    return r;
}
static void freeRing(Ring& r) {
    if (!r.shm) { cudaFree(r.dev); return; }
    cudaHostUnregister(r.host);
    munmap(r.host, r.bytes);
    close(r.fd);
    shm_unlink("/lft3-ringbench2");
}

int main() {
    cudaStream_t s;
    CK(cudaStreamCreate(&s));
    // payload pool (pinned), 8192 packets
    const int poolPkts = 8192, burst = 1024, groups = C / kCh, tPerBurst = burst / groups;
    uint2* pool;
    CK(cudaMallocHost(&pool, (size_t)poolPkts * kCh * 8));
    for (size_t i = 0; i < (size_t)poolPkts * kCh * 2; ++i) ((uint32_t*)pool)[i] = i * 2654435761u;

    printf("== a) scatter into block-major ring (block = [2][6144][1024] u32 = %.1f MB) ==\n", kBlockWords * 4 / 1e6);
    printf("%-14s %10s %12s %8s\n", "memory", "ring GB", "payload GB/s", "x need");
    for (bool shm : {false, true})
        for (size_t S : {256ull, 512ull}) {
            Ring r = makeRing(S * kBlockWords * 4, shm);
            uint64_t t = 0;
            int nb = 256;  // bursts per timed run (moves through ~43 blocks)
            auto run = [&] {
                for (int b = 0; b < nb; ++b) {
                    for (int g = 0; g < groups; ++g) {
                        const uint2* src = pool + (size_t)((b * burst + g * tPerBurst) % poolPkts) * kCh;
                        dim3 grid(kCh / 32, (tPerBurst + 31) / 32), blk(32, 8);
                        kScatter<<<grid, blk, 0, s>>>(src, tPerBurst, t, r.dev, S, g * kCh);
                    }
                    t += tPerBurst;
                }
            };
            double payload = (double)nb * burst * kCh * 8;
            float ms = timeIt(s, 3, run);
            printf("%-14s %10.2f %12.1f %8.1f\n", shm ? "shm+register" : "cudaMalloc", r.bytes / 1e9,
                   payload / ms / 1e6, payload / ms / 1e6 / 7.5);
            freeRing(r);
        }

    printf("\n== b) reader gather of Nf samples per row across blocks (ring 12.9 GB), all 12288 rows ==\n");
    printf("%-14s %8s %10s %12s %22s\n", "memory", "Nf", "blocks", "GB/s (r+w)", "x real-time (7.5 GB/s)");
    for (bool shm : {false, true}) {
        size_t S = 256;
        Ring r = makeRing(S * kBlockWords * 4, shm);
        for (int Nf : {1 << 13, 1 << 16, 1 << 17}) {
            int chunks = Nf / BT, rows = 2 * C;
            // process the rows in batches so the output buffer stays bounded (~1.6 GB per 2^17 batch of 3072 rows)
            int rowBatch = 3072;
            uint32_t* out;
            CK(cudaMalloc(&out, (size_t)rowBatch * Nf * 4));
            auto run = [&] {
                for (int r0 = 0; r0 < rows; r0 += rowBatch) {
                    dim3 grid(rowBatch, chunks);
                    kGather<<<grid, 256, 0, s>>>(r.dev + (size_t)r0 * BT, S, 7, rowBatch, chunks, out);
                }
            };
            float ms = timeIt(s, 3, run);
            double bytes = (double)rows * Nf * 4;  // read; write the same
            printf("%-14s %8d %10d %12.1f %22.1f\n", shm ? "shm+register" : "cudaMalloc", Nf, chunks,
                   2 * bytes / ms / 1e6, bytes / ms / 1e6 / 7.5);
            cudaFree(out);
        }
        freeRing(r);
    }

    printf("\n== c) cross-process sharing support ==\n");
    CUdevice d;
    cuInit(0);
    cuDeviceGet(&d, 0);
    int v;
    cuDeviceGetAttribute(&v, CU_DEVICE_ATTRIBUTE_HANDLE_TYPE_POSIX_FILE_DESCRIPTOR_SUPPORTED, d);
    printf("VMM POSIX fd handles supported: %d\n", v);
    cuDeviceGetAttribute(&v, CU_DEVICE_ATTRIBUTE_VIRTUAL_MEMORY_MANAGEMENT_SUPPORTED, d);
    printf("VMM supported: %d\n", v);
    cudaDeviceGetAttribute(&v, cudaDevAttrIpcEventSupport, 0);
    printf("IPC event support: %d\n", v);
    void* p;
    CK(cudaMalloc(&p, 1 << 21));
    cudaIpcMemHandle_t h;
    cudaError_t e = cudaIpcGetMemHandle(&h, p);
    printf("cudaIpcGetMemHandle: %s\n", cudaGetErrorString(e));
    cudaGetLastError();
    CUmemAllocationProp prop = {};
    prop.type = CU_MEM_ALLOCATION_TYPE_PINNED;
    prop.location.type = CU_MEM_LOCATION_TYPE_DEVICE;
    prop.location.id = 0;
    prop.requestedHandleTypes = CU_MEM_HANDLE_TYPE_POSIX_FILE_DESCRIPTOR;
    size_t gmin = 0, grec = 0;
    cuMemGetAllocationGranularity(&gmin, &prop, CU_MEM_ALLOC_GRANULARITY_MINIMUM);
    cuMemGetAllocationGranularity(&grec, &prop, CU_MEM_ALLOC_GRANULARITY_RECOMMENDED);
    printf("VMM granularity: min %zu KB, recommended %zu KB\n", gmin >> 10, grec >> 10);
    CUmemGenericAllocationHandle mh;
    CUresult cr = cuMemCreate(&mh, 1 << 30, &prop, 0);
    int fd = -1;
    if (cr == CUDA_SUCCESS) cr = cuMemExportToShareableHandle(&fd, mh, CU_MEM_HANDLE_TYPE_POSIX_FILE_DESCRIPTOR, 0);
    const char* es;
    cuGetErrorString(cr, &es);
    printf("cuMemCreate(1 GB) + export POSIX fd: %s (fd %d)\n", es, fd);
    return 0;
}
