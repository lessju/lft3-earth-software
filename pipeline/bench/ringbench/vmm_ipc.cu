// Cross-process sharing via CUDA VMM: owner cuMemCreate(4 GB, 2 MB pages) -> POSIX fd -> unix socket (SCM_RIGHTS)
// -> reader cuMemImportFromShareableHandle + cuMemMap. Measures read/write bandwidth in owner and reader.
// usage: vmm_ipc owner | vmm_ipc reader
#include <cuda.h>
#include <cuda_runtime.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>

#define CU(x)                                                          \
    do {                                                               \
        CUresult r = (x);                                              \
        if (r != CUDA_SUCCESS) {                                       \
            const char* s;                                             \
            cuGetErrorString(r, &s);                                   \
            fprintf(stderr, "%s:%d %s: %s\n", __FILE__, __LINE__, #x, s); \
            exit(1);                                                   \
        }                                                              \
    } while (0)

static const size_t kBytes = 4ull << 30;
static const char* kSock = "/tmp/lft3-vmm-bench.sock";

__global__ void kRead(const uint4* p, size_t n, unsigned* out) {
    unsigned acc = 0;
    for (size_t i = blockIdx.x * (size_t)blockDim.x + threadIdx.x; i < n; i += (size_t)gridDim.x * blockDim.x) {
        uint4 v = p[i];
        acc ^= v.x ^ v.y ^ v.z ^ v.w;
    }
    if (acc == 0x12345678) *out = acc;
}
__global__ void kWrite(uint4* p, size_t n) {
    for (size_t i = blockIdx.x * (size_t)blockDim.x + threadIdx.x; i < n; i += (size_t)gridDim.x * blockDim.x)
        p[i] = make_uint4(i, i, i, i);
}

static void bench(const char* who, CUdeviceptr d) {
    unsigned* out;
    cudaMalloc(&out, 4);
    size_t n = kBytes / 16;
    cudaEvent_t a, b;
    cudaEventCreate(&a);
    cudaEventCreate(&b);
    float ms;
    kRead<<<384, 512>>>((uint4*)d, n, out);
    cudaEventRecord(a);
    for (int i = 0; i < 5; ++i) kRead<<<384, 512>>>((uint4*)d, n, out);
    cudaEventRecord(b);
    cudaEventSynchronize(b);
    cudaEventElapsedTime(&ms, a, b);
    float r = kBytes / (ms / 5) / 1e6;
    cudaEventRecord(a);
    for (int i = 0; i < 5; ++i) kWrite<<<384, 512>>>((uint4*)d, n);
    cudaEventRecord(b);
    cudaEventSynchronize(b);
    cudaEventElapsedTime(&ms, a, b);
    printf("%-7s read %.0f GB/s, write %.0f GB/s\n", who, r, kBytes / (ms / 5) / 1e6);
}

static CUmemAllocationProp prop() {
    CUmemAllocationProp p = {};
    p.type = CU_MEM_ALLOCATION_TYPE_PINNED;
    p.location.type = CU_MEM_LOCATION_TYPE_DEVICE;
    p.location.id = 0;
    p.requestedHandleTypes = CU_MEM_HANDLE_TYPE_POSIX_FILE_DESCRIPTOR;
    return p;
}

static CUdeviceptr mapHandle(CUmemGenericAllocationHandle h) {
    CUdeviceptr d;
    CU(cuMemAddressReserve(&d, kBytes, 2 << 20, 0, 0));
    CU(cuMemMap(d, kBytes, 0, h, 0));
    CUmemAccessDesc acc = {};
    acc.location.type = CU_MEM_LOCATION_TYPE_DEVICE;
    acc.location.id = 0;
    acc.flags = CU_MEM_ACCESS_FLAGS_PROT_READWRITE;
    CU(cuMemSetAccess(d, kBytes, &acc, 1));
    return d;
}

int main(int argc, char** argv) {
    cudaFree(0);
    bool owner = argc > 1 && !strcmp(argv[1], "owner");
    sockaddr_un addr = {};
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, kSock, sizeof(addr.sun_path) - 1);
    if (owner) {
        CUmemAllocationProp p = prop();
        CUmemGenericAllocationHandle h;
        CU(cuMemCreate(&h, kBytes, &p, 0));
        CUdeviceptr d = mapHandle(h);
        bench("owner", d);
        int fd;
        CU(cuMemExportToShareableHandle(&fd, h, CU_MEM_HANDLE_TYPE_POSIX_FILE_DESCRIPTOR, 0));
        unlink(kSock);
        int ls = socket(AF_UNIX, SOCK_STREAM, 0);
        bind(ls, (sockaddr*)&addr, sizeof(addr));
        listen(ls, 1);
        int cs = accept(ls, nullptr, nullptr);
        char byte = 0;
        iovec io = {&byte, 1};
        char ctl[CMSG_SPACE(sizeof(int))] = {};
        msghdr m = {};
        m.msg_iov = &io;
        m.msg_iovlen = 1;
        m.msg_control = ctl;
        m.msg_controllen = sizeof(ctl);
        cmsghdr* c = CMSG_FIRSTHDR(&m);
        c->cmsg_level = SOL_SOCKET;
        c->cmsg_type = SCM_RIGHTS;
        c->cmsg_len = CMSG_LEN(sizeof(int));
        memcpy(CMSG_DATA(c), &fd, sizeof(int));
        sendmsg(cs, &m, 0);
        read(cs, &byte, 1);  // wait for reader to finish
        unlink(kSock);
    } else {
        int s = socket(AF_UNIX, SOCK_STREAM, 0);
        for (int i = 0; connect(s, (sockaddr*)&addr, sizeof(addr)) != 0; ++i) {
            if (i > 100) { perror("connect"); return 1; }
            usleep(100000);
        }
        char byte;
        iovec io = {&byte, 1};
        char ctl[CMSG_SPACE(sizeof(int))] = {};
        msghdr m = {};
        m.msg_iov = &io;
        m.msg_iovlen = 1;
        m.msg_control = ctl;
        m.msg_controllen = sizeof(ctl);
        recvmsg(s, &m, 0);
        int fd;
        memcpy(&fd, CMSG_DATA(CMSG_FIRSTHDR(&m)), sizeof(int));
        CUmemGenericAllocationHandle h;
        CU(cuMemImportFromShareableHandle(&h, (void*)(uintptr_t)fd, CU_MEM_HANDLE_TYPE_POSIX_FILE_DESCRIPTOR));
        CUdeviceptr d = mapHandle(h);
        bench("reader", d);
        write(s, &byte, 1);
    }
    return 0;
}
