#include "common/ring.hh"

#include <cerrno>
#include <cstring>
#include <random>

#include <cuda.h>
#include <cuda_runtime.h>
#include <fcntl.h>
#include <linux/futex.h>
#include <poll.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/un.h>
#include <unistd.h>

#include "common/util.hh"

namespace Lft3 {

// Fixed binary layout, mirrored in src/lft3/ring.py.
static_assert(std::atomic<std::uint64_t>::is_always_lock_free);
static_assert(std::atomic<std::uint32_t>::is_always_lock_free);
static_assert(sizeof(std::atomic<std::uint64_t>) == 8 && sizeof(std::atomic<std::uint32_t>) == 4);
static_assert(sizeof(RingSlot) == 128);
static_assert(sizeof(RingReaderEntry) == 128);
static_assert(sizeof(RingCaptureStats) == 128);
static_assert(offsetof(RingControl, stream) == 24);
static_assert(offsetof(RingControl, channelStart) == 264);
static_assert(offsetof(RingControl, inputBeam) == 320);
static_assert(offsetof(RingControl, boardId) == 328);
static_assert(offsetof(RingControl, state) == 332);
static_assert(offsetof(RingControl, ncoMHz) == 336);
static_assert(offsetof(RingControl, unixTimeAtT0Bits) == 352);
static_assert(offsetof(RingControl, futex) == 360);
static_assert(offsetof(RingControl, head) == 368);
static_assert(offsetof(RingControl, writerHeartbeatNs) == 384);
static_assert(offsetof(RingControl, startUnix) == 392);
static_assert(offsetof(RingControl, stats) == 400);
static_assert(offsetof(RingControl, kind) == 528);
static_assert(offsetof(RingControl, fch1MHz) == 536);
static_assert(offsetof(RingControl, tsampS) == 552);
static_assert(offsetof(RingControl, readers) == 576);
static_assert(offsetof(RingControl, seqToSlot) == 576 + kMaxReaders * 128);
static_assert(offsetof(RingControl, slot) == 576 + kMaxReaders * 128 + kMaxSlots * 8);

namespace {

constexpr std::uint64_t kReaderTimeoutNs = 5'000'000'000ULL;   // a reader without heartbeat for 5 s is gone

std::string CuError(CUresult r, const std::string& what) {
    const char* s = nullptr;
    cuGetErrorString(r, &s);
    return what + ": " + (s ? s : "unknown CUDA driver error");
}

void FutexWakeAll(std::atomic<std::uint32_t>* word) {
    syscall(SYS_futex, reinterpret_cast<std::uint32_t*>(word), FUTEX_WAKE, INT32_MAX, nullptr, nullptr, 0);
}

void FutexWait(const std::atomic<std::uint32_t>* word, std::uint32_t expected, int timeoutMs) {
    timespec ts{timeoutMs / 1000, static_cast<long>(timeoutMs % 1000) * 1000000L};
    syscall(SYS_futex, reinterpret_cast<const std::uint32_t*>(word), FUTEX_WAIT, expected, &ts, nullptr, 0);
}

void CopyString(char* dst, std::size_t n, const std::string& src) {
    std::memset(dst, 0, n);
    std::memcpy(dst, src.data(), std::min(n - 1, src.size()));
}

CUmemAllocationProp AllocationProp() {
    CUmemAllocationProp p = {};
    p.type = CU_MEM_ALLOCATION_TYPE_PINNED;
    p.location.type = CU_MEM_LOCATION_TYPE_DEVICE;
    p.location.id = 0;
    p.requestedHandleTypes = CU_MEM_HANDLE_TYPE_POSIX_FILE_DESCRIPTOR;
    return p;
}

bool MapHandle(CUmemGenericAllocationHandle h, std::uint64_t bytes, bool readOnly, CUdeviceptr& ptr,
               std::string& error) {
    CUresult r = cuMemAddressReserve(&ptr, bytes, 2ULL << 20, 0, 0);
    if (r != CUDA_SUCCESS) {
        error = CuError(r, "cuMemAddressReserve");
        return false;
    }
    if ((r = cuMemMap(ptr, bytes, 0, h, 0)) != CUDA_SUCCESS) {
        error = CuError(r, "cuMemMap");
        cuMemAddressFree(ptr, bytes);
        return false;
    }
    CUmemAccessDesc acc = {};
    acc.location.type = CU_MEM_LOCATION_TYPE_DEVICE;
    acc.location.id = 0;
    acc.flags = readOnly ? CU_MEM_ACCESS_FLAGS_PROT_READ : CU_MEM_ACCESS_FLAGS_PROT_READWRITE;
    if ((r = cuMemSetAccess(ptr, bytes, &acc, 1)) != CUDA_SUCCESS) {
        error = CuError(r, "cuMemSetAccess");
        cuMemUnmap(ptr, bytes);
        cuMemAddressFree(ptr, bytes);
        return false;
    }
    return true;
}

void UnmapHandle(std::uint64_t handle, std::uint64_t ptr, std::uint64_t bytes) {
    if (ptr) {
        cuMemUnmap(static_cast<CUdeviceptr>(ptr), bytes);
        cuMemAddressFree(static_cast<CUdeviceptr>(ptr), bytes);
    }
    if (handle) {
        cuMemRelease(static_cast<CUmemGenericAllocationHandle>(handle));
    }
}

}  // namespace

std::string RingShmName(const std::string& stream) { return "/lft3-" + stream; }
std::string RingSocketPath(const std::string& stream) { return "/dev/shm/lft3-" + stream + ".sock"; }

// ------------------------------------------------------------------------------------------------ writer

RingWriter::~RingWriter() { close(); }

bool RingWriter::create(const RingConfig& config, std::string& error) {
    const Layout& l = config.layout;
    if (config.stream.empty() || config.stream.find('/') != std::string::npos) {
        error = "stream name must be a plain non-empty name";
        return false;
    }
    if (config.slots < 16 || config.slots > kMaxSlots) {
        error = "slots must be in 16.." + std::to_string(kMaxSlots);
        return false;
    }
    blockBytes_ = config.blockBytes ? config.blockBytes : l.bytesPerBlock();
    slots_ = config.slots;

    // Data plane: VMM allocation, exportable as a POSIX fd.
    if (cudaFree(nullptr) != cudaSuccess) {   // initialises the primary context the driver API uses
        error = "CUDA initialisation failed";
        return false;
    }
    CUmemAllocationProp prop = AllocationProp();
    std::size_t gran = 0;
    CUresult r = cuMemGetAllocationGranularity(&gran, &prop, CU_MEM_ALLOC_GRANULARITY_RECOMMENDED);
    if (r != CUDA_SUCCESS) {
        error = CuError(r, "cuMemGetAllocationGranularity");
        return false;
    }
    ringBytes_ = (slots_ * blockBytes_ + gran - 1) / gran * gran;
    CUmemGenericAllocationHandle h;
    if ((r = cuMemCreate(&h, ringBytes_, &prop, 0)) != CUDA_SUCCESS) {
        error = CuError(r, "cuMemCreate (" + std::to_string(ringBytes_ >> 20) + " MiB)");
        return false;
    }
    handle_ = h;
    CUdeviceptr ptr = 0;
    if (!MapHandle(h, ringBytes_, false, ptr, error)) {
        return false;
    }
    devPtr_ = ptr;
    base_ = reinterpret_cast<std::uint8_t*>(ptr);
    if ((r = cuMemsetD8(ptr, 0, ringBytes_)) != CUDA_SUCCESS) {
        error = CuError(r, "cuMemsetD8");
        return false;
    }
    if ((r = cuMemExportToShareableHandle(&memFd_, h, CU_MEM_HANDLE_TYPE_POSIX_FILE_DESCRIPTOR, 0)) != CUDA_SUCCESS) {
        error = CuError(r, "cuMemExportToShareableHandle");
        return false;
    }

    // Control plane.
    shmName_ = RingShmName(config.stream);
    shm_unlink(shmName_.c_str());
    const int fd = shm_open(shmName_.c_str(), O_CREAT | O_EXCL | O_RDWR, 0666);
    if (fd < 0) {
        error = "shm_open " + shmName_ + ": " + std::strerror(errno);
        return false;
    }
    fchmod(fd, 0666);   // readers may run as another user
    if (ftruncate(fd, sizeof(RingControl)) != 0) {
        error = std::string("ftruncate: ") + std::strerror(errno);
        ::close(fd);
        return false;
    }
    void* m = mmap(nullptr, sizeof(RingControl), PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    ::close(fd);
    if (m == MAP_FAILED) {
        error = std::string("mmap: ") + std::strerror(errno);
        return false;
    }
    std::memset(m, 0, sizeof(RingControl));
    ctl_ = static_cast<RingControl*>(m);
    RingControl& c = *ctl_;
    c.version = kRingVersion;
    c.controlBytes = sizeof(RingControl);
    c.ringId = std::random_device{}() | (static_cast<std::uint64_t>(std::random_device{}()) << 32);
    CopyString(c.stream, sizeof(c.stream), config.stream);
    CopyString(c.obsId, sizeof(c.obsId), config.obsId);
    CopyString(c.software, sizeof(c.software), std::string("lft3-earth-software runtime ") + LFT3_VERSION);
    CopyString(c.configHash, sizeof(c.configHash), config.configHash);
    c.channelStart = l.channelStart;
    c.channelCount = l.channelCount;
    c.spectraPerBlock = l.spectraPerBlock;
    c.beams = Layout::kBeams;
    c.slots = slots_;
    c.blockBytes = blockBytes_;
    c.ringBytes = ringBytes_;
    for (int i = 0; i < 4; i++) {
        c.inputBeam[i] = l.inputBeam[i];
        c.inputPol[i] = l.inputPol[i];
    }
    c.boardId = config.boardId;
    c.ncoMHz = config.ncoMHz;
    c.spectraPerSecond = kSpectraPerSecond;
    c.kind = static_cast<std::uint32_t>(config.kind);
    c.fineChannelsPerCoarse = config.fineChannelsPerCoarse;
    c.fch1MHz = config.fch1MHz;
    c.foffMHz = config.foffMHz;
    c.tsampS = config.tsampS;
    c.backpressure.store(config.backpressure ? 1 : 0);
    c.writerPid.store(static_cast<std::uint64_t>(getpid()));
    c.startUnix = UnixNow();
    for (auto& s : c.slot) s.seq.store(kInvalidSeq);
    c.state.store(static_cast<std::uint32_t>(RingState::Starting));
    busy_.assign(slots_, 0);
    lastSeq_.assign(slots_, kInvalidSeq);
    heartbeat();
    std::atomic_thread_fence(std::memory_order_release);
    c.magic = kRingMagic;

    // fd server for readers.
    socketPath_ = RingSocketPath(config.stream);
    unlink(socketPath_.c_str());
    listenFd_ = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    sockaddr_un addr = {};
    addr.sun_family = AF_UNIX;
    std::strncpy(addr.sun_path, socketPath_.c_str(), sizeof(addr.sun_path) - 1);
    if (listenFd_ < 0 || bind(listenFd_, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0 ||
        listen(listenFd_, 16) != 0) {
        error = "unix socket " + socketPath_ + ": " + std::strerror(errno);
        return false;
    }
    chmod(socketPath_.c_str(), 0666);
    serving_.store(true);
    server_ = std::thread([this]() { serveFds(); });
    return true;
}

void RingWriter::serveFds() {
    while (serving_.load()) {
        pollfd p{listenFd_, POLLIN, 0};
        if (poll(&p, 1, 200) <= 0) continue;
        const int client = accept4(listenFd_, nullptr, nullptr, SOCK_CLOEXEC);
        if (client < 0) continue;
        // Payload: ring id + allocation size; ancillary: the VMM fd.
        std::uint64_t payload[2] = {ctl_->ringId, ringBytes_};
        iovec io{payload, sizeof(payload)};
        char ctl[CMSG_SPACE(sizeof(int))] = {};
        msghdr msg = {};
        msg.msg_iov = &io;
        msg.msg_iovlen = 1;
        msg.msg_control = ctl;
        msg.msg_controllen = sizeof(ctl);
        cmsghdr* cm = CMSG_FIRSTHDR(&msg);
        cm->cmsg_level = SOL_SOCKET;
        cm->cmsg_type = SCM_RIGHTS;
        cm->cmsg_len = CMSG_LEN(sizeof(int));
        std::memcpy(CMSG_DATA(cm), &memFd_, sizeof(int));
        sendmsg(client, &msg, MSG_NOSIGNAL);
        ::close(client);
    }
}

void RingWriter::setRunning() { ctl_->state.store(static_cast<std::uint32_t>(RingState::Running)); }

void RingWriter::heartbeat() {
    if (ctl_) ctl_->writerHeartbeatNs.store(MonotonicNs(), std::memory_order_relaxed);
}

bool RingWriter::waitForLosslessReaders(std::uint64_t slotLastSeq) {
    const std::uint64_t t0 = MonotonicNs();
    bool waited = false;
    while (true) {
        bool blocked = false;
        const std::uint64_t now = MonotonicNs();
        for (auto& r : ctl_->readers) {
            if (r.pid.load(std::memory_order_acquire) == 0 || !r.lossless.load()) continue;
            if (now - r.heartbeatNs.load() > kReaderTimeoutNs) continue;   // dead reader: ignore
            if (r.cursor.load(std::memory_order_acquire) <= slotLastSeq + kReadMargin) {
                blocked = true;
                break;
            }
        }
        if (!blocked) break;
        if (StopRequested().load()) return false;
        waited = true;
        heartbeat();
        std::this_thread::sleep_for(std::chrono::microseconds(50));
    }
    if (waited) ctl_->stats.backpressureNs.fetch_add(MonotonicNs() - t0, std::memory_order_relaxed);
    return true;
}

std::int64_t RingWriter::acquireSlot() {
    const std::uint64_t s = allocCounter_ % slots_;
    if (busy_[s]) {
        ctl_->stats.slotsUnavailable.fetch_add(1, std::memory_order_relaxed);
        return -1;
    }
    if (ctl_->backpressure.load(std::memory_order_relaxed) && lastSeq_[s] != kInvalidSeq &&
        !waitForLosslessReaders(lastSeq_[s])) {
        return -1;
    }
    busy_[s] = 1;
    ctl_->slot[s].seq.store(kInvalidSeq, std::memory_order_release);
    std::atomic_thread_fence(std::memory_order_seq_cst);
    allocCounter_++;
    return static_cast<std::int64_t>(s);
}

void RingWriter::release(std::int64_t slot) { busy_[slot] = 0; }

void RingWriter::publish(std::int64_t slot, const RingSlotMeta& meta) {
    RingSlot& s = ctl_->slot[slot];
    const std::uint64_t n = nextSeq_++;
    s.blockIndex = meta.blockIndex;
    s.t0 = meta.t0;
    s.missingPackets = meta.missingPackets;
    s.restarts = meta.restarts;
    for (int i = 0; i < 4; i++) {
        s.nco[i] = meta.nco[i];
        s.pfbLsb[i] = meta.pfbLsb[i];
    }
    s.fid = meta.fid;
    s.flags = meta.flags;
    s.unixTimeAtT0 = meta.unixTimeAtT0;
    s.publishUnix = UnixNow();
    s.seq.store(n, std::memory_order_release);
    ctl_->seqToSlot[n % kMaxSlots].store(static_cast<std::uint64_t>(slot), std::memory_order_release);
    double t0 = meta.unixTimeAtT0;
    std::uint64_t bits;
    std::memcpy(&bits, &t0, sizeof(bits));
    ctl_->unixTimeAtT0Bits.store(bits, std::memory_order_relaxed);
    ctl_->head.store(n + 1, std::memory_order_release);
    ctl_->stats.blocksPublished.store(n + 1, std::memory_order_relaxed);
    ctl_->futex.fetch_add(1, std::memory_order_release);
    FutexWakeAll(&ctl_->futex);
    busy_[slot] = 0;
    lastSeq_[slot] = n;
}

void RingWriter::close() {
    if (ctl_) {
        ctl_->state.store(static_cast<std::uint32_t>(RingState::Stopped));
        ctl_->futex.fetch_add(1);
        FutexWakeAll(&ctl_->futex);
    }
    if (serving_.exchange(false) && server_.joinable()) server_.join();
    if (listenFd_ >= 0) {
        ::close(listenFd_);
        listenFd_ = -1;
        unlink(socketPath_.c_str());
    }
    if (ctl_) {
        munmap(ctl_, sizeof(RingControl));
        ctl_ = nullptr;
        shm_unlink(shmName_.c_str());   // attached readers keep their mappings
    }
    if (memFd_ >= 0) {
        ::close(memFd_);
        memFd_ = -1;
    }
    UnmapHandle(handle_, devPtr_, ringBytes_);
    handle_ = devPtr_ = 0;
    base_ = nullptr;
}

// ------------------------------------------------------------------------------------------------ reader

RingReader::~RingReader() { detach(); }

bool RingReader::attach(const std::string& stream, const std::string& name, bool lossless, double timeoutS,
                        std::string& error) {
    const std::uint64_t deadline = MonotonicNs() + static_cast<std::uint64_t>(timeoutS * 1e9);
    const std::string shm = RingShmName(stream);
    // 1. Control block, once the writer has initialised it.
    while (true) {
        const int fd = shm_open(shm.c_str(), O_RDWR, 0);
        if (fd >= 0) {
            struct stat st = {};
            if (fstat(fd, &st) == 0 && static_cast<std::size_t>(st.st_size) >= sizeof(RingControl)) {
                void* m = mmap(nullptr, sizeof(RingControl), PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
                if (m != MAP_FAILED) {
                    auto* c = static_cast<RingControl*>(m);
                    std::atomic_thread_fence(std::memory_order_acquire);
                    if (c->magic == kRingMagic && c->version == kRingVersion && c->controlBytes == sizeof(RingControl)) {
                        ctl_ = c;
                    } else {
                        if (c->magic == kRingMagic) {
                            error = "ring '" + stream + "' has an incompatible version";
                            munmap(m, sizeof(RingControl));
                            ::close(fd);
                            return false;
                        }
                        munmap(m, sizeof(RingControl));
                    }
                }
            }
            ::close(fd);
        }
        if (ctl_) break;
        if (MonotonicNs() > deadline || StopRequested().load()) {
            error = "no ring '" + stream + "' (" + shm + ") within " + std::to_string(timeoutS) + " s";
            return false;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    blockBytes_ = ctl_->blockBytes;

    // 2. The data plane fd, over the writer's socket.
    int memFd = -1;
    std::uint64_t payload[2] = {0, 0};
    while (memFd < 0) {
        const int s = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
        sockaddr_un addr = {};
        addr.sun_family = AF_UNIX;
        std::strncpy(addr.sun_path, RingSocketPath(stream).c_str(), sizeof(addr.sun_path) - 1);
        if (connect(s, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) == 0) {
            iovec io{payload, sizeof(payload)};
            char cbuf[CMSG_SPACE(sizeof(int))] = {};
            msghdr msg = {};
            msg.msg_iov = &io;
            msg.msg_iovlen = 1;
            msg.msg_control = cbuf;
            msg.msg_controllen = sizeof(cbuf);
            if (recvmsg(s, &msg, MSG_WAITALL) == static_cast<ssize_t>(sizeof(payload))) {
                cmsghdr* cm = CMSG_FIRSTHDR(&msg);
                if (cm && cm->cmsg_type == SCM_RIGHTS) std::memcpy(&memFd, CMSG_DATA(cm), sizeof(int));
            }
        }
        ::close(s);
        if (memFd >= 0) break;
        if (MonotonicNs() > deadline || StopRequested().load()) {
            error = "cannot get the ring memory from " + RingSocketPath(stream);
            detach();
            return false;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    ringBytes_ = payload[1];

    if (cudaFree(nullptr) != cudaSuccess) {
        error = "CUDA initialisation failed";
        ::close(memFd);
        detach();
        return false;
    }
    CUmemGenericAllocationHandle h;
    CUresult r = cuMemImportFromShareableHandle(&h, reinterpret_cast<void*>(static_cast<std::uintptr_t>(memFd)),
                                                CU_MEM_HANDLE_TYPE_POSIX_FILE_DESCRIPTOR);
    ::close(memFd);
    if (r != CUDA_SUCCESS) {
        error = CuError(r, "cuMemImportFromShareableHandle");
        detach();
        return false;
    }
    handle_ = h;
    CUdeviceptr ptr = 0;
    if (!MapHandle(h, ringBytes_, true, ptr, error)) {
        detach();
        return false;
    }
    devPtr_ = ptr;
    base_ = reinterpret_cast<const std::uint8_t*>(ptr);

    // 3. Register (reclaim an entry whose reader stopped beating).
    const std::uint64_t me = static_cast<std::uint64_t>(getpid()) | (1ULL << 63);
    const std::uint64_t now = MonotonicNs();
    for (std::uint32_t i = 0; i < kMaxReaders && entry_ < 0; i++) {
        RingReaderEntry& e = ctl_->readers[i];
        std::uint64_t cur = e.pid.load();
        if (cur != 0 && now - e.heartbeatNs.load() < kReaderTimeoutNs) continue;
        if (e.pid.compare_exchange_strong(cur, me)) {
            e.cursor.store(head(), std::memory_order_release);
            e.heartbeatNs.store(now);
            e.lossless.store(lossless ? 1 : 0);
            CopyString(e.name, sizeof(e.name), name);
            e.blocksRead.store(0);
            e.overruns.store(0);
            e.lagBlocks.store(0);
            e.busyNs.store(0);
            e.outputs.store(0);
            entry_ = static_cast<int>(i);
        }
    }
    if (entry_ < 0) {
        error = "all " + std::to_string(kMaxReaders) + " reader entries are in use";
        detach();
        return false;
    }
    return true;
}

void RingReader::detach() {
    if (ctl_ && entry_ >= 0) {
        ctl_->readers[entry_].lossless.store(0);
        ctl_->readers[entry_].pid.store(0, std::memory_order_release);
    }
    entry_ = -1;
    UnmapHandle(handle_, devPtr_, ringBytes_);
    handle_ = devPtr_ = 0;
    base_ = nullptr;
    if (ctl_) {
        munmap(ctl_, sizeof(RingControl));
        ctl_ = nullptr;
    }
}

Layout RingReader::layout() const {
    Layout l;
    l.channelStart = ctl_->channelStart;
    l.channelCount = ctl_->channelCount;
    l.spectraPerBlock = ctl_->spectraPerBlock;
    for (int i = 0; i < 4; i++) {
        l.inputBeam[i] = ctl_->inputBeam[i];
        l.inputPol[i] = ctl_->inputPol[i];
    }
    return l;
}

std::uint64_t RingReader::oldestReadable(std::uint64_t margin) const {
    const std::uint64_t h = head();
    const std::uint64_t keep = ctl_->slots > margin ? ctl_->slots - margin : 1;
    return h > keep ? h - keep : 0;
}

RingReader::Wait RingReader::wait(std::uint64_t seq, int timeoutMs) const {
    const std::uint64_t deadline = MonotonicNs() + static_cast<std::uint64_t>(timeoutMs) * 1000000ULL;
    while (true) {
        const std::uint32_t word = ctl_->futex.load(std::memory_order_acquire);
        if (head() > seq) return Wait::Ready;
        if (writerStopped()) return head() > seq ? Wait::Ready : Wait::Stopped;
        const std::uint64_t now = MonotonicNs();
        if (now >= deadline || StopRequested().load()) return Wait::Timeout;
        const int ms = static_cast<int>(std::min<std::uint64_t>((deadline - now) / 1000000ULL + 1, 100));
        FutexWait(&ctl_->futex, word, ms);
    }
}

bool RingReader::begin(std::uint64_t seq, RingSlotMeta& meta, const std::uint8_t*& data) const {
    const std::uint64_t slot = ctl_->seqToSlot[seq % kMaxSlots].load(std::memory_order_acquire);
    if (slot >= ctl_->slots) return false;
    const RingSlot& s = ctl_->slot[slot];
    if (s.seq.load(std::memory_order_acquire) != seq) return false;
    meta.seq = seq;
    meta.blockIndex = s.blockIndex;
    meta.t0 = s.t0;
    meta.missingPackets = s.missingPackets;
    meta.restarts = s.restarts;
    for (int i = 0; i < 4; i++) {
        meta.nco[i] = s.nco[i];
        meta.pfbLsb[i] = s.pfbLsb[i];
    }
    meta.fid = s.fid;
    meta.flags = s.flags;
    meta.unixTimeAtT0 = s.unixTimeAtT0;
    meta.publishUnix = s.publishUnix;
    std::atomic_thread_fence(std::memory_order_acquire);
    if (s.seq.load(std::memory_order_relaxed) != seq) return false;
    data = base_ + slot * blockBytes_;
    return true;
}

bool RingReader::stillValid(std::uint64_t seq) const {
    std::atomic_thread_fence(std::memory_order_seq_cst);
    const std::uint64_t slot = ctl_->seqToSlot[seq % kMaxSlots].load(std::memory_order_acquire);
    return slot < ctl_->slots && ctl_->slot[slot].seq.load(std::memory_order_acquire) == seq;
}

void RingReader::consumed(std::uint64_t nextSeq, std::uint64_t busyNs) {
    RingReaderEntry& e = ctl_->readers[entry_];
    e.cursor.store(nextSeq, std::memory_order_release);
    const std::uint64_t h = head();
    e.lagBlocks.store(h > nextSeq ? h - nextSeq : 0, std::memory_order_relaxed);
    if (busyNs) e.busyNs.store(busyNs, std::memory_order_relaxed);
    e.heartbeatNs.store(MonotonicNs(), std::memory_order_relaxed);
}

}  // namespace Lft3
