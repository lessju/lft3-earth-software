#ifndef LFT3_COMMON_RING_HH
#define LFT3_COMMON_RING_HH

// The voltage ring shared by lft3-capture (single writer) and any number of reader processes
// (docs/runtime.md §3). The same machinery carries the spectra ring "<stream>-spectra" that lft3-spectrometer
// --spectra-ring writes (kind = Spectra: one integrated fine spectrum per slot, float32 [beam][channel]) for
// the live search; there channelCount is the coarse channel count, spectraPerBlock the spectra per
// integration, blockIndex the integration (window) number and t0 its first spectrum.
//
//   control plane  POSIX shm "/lft3-<stream>" (RingControl below): layout, publication head, per-slot
//                  metadata, per-reader cursors and statistics, capture statistics. CPU atomics + futex.
//   data plane     one CUDA VMM allocation of `slots` blocks (2 MiB pages, full GPU bandwidth in every
//                  process). The writer exports it as a POSIX fd and hands it to readers over the unix
//                  socket /dev/shm/lft3-<stream>.sock (SCM_RIGHTS). Readers map it read-only.
//
// Publication (single writer, lock-free readers, seqlock per slot):
//   writer: acquireSlot() invalidates the slot (seq = kInvalidSeq) before any GPU write into it; once the
//           block's kernels have completed, publish() fills the metadata, stores slot.seq = n, records
//           seqToSlot[n % slots] = slot, then head = n + 1, and wakes futex waiters. It never waits for
//           readers, unless backpressure is enabled (synthetic / file sources) and a lossless reader still
//           needs the slot.
//   reader: wait(n) until head > n; slot = seqToSlot[n % slots]; check slot.seq == n; read the data (GPU);
//           after the GPU work has completed check slot.seq == n again. A mismatch means the slot was
//           reused while reading (the reader fell `slots` blocks behind): an overrun, the result is discarded.
//
// The control block has a fixed binary layout (static_asserts in ring.cc). src/lft3/ring.py mirrors the
// offsets for status, metrics and Python readers: change both together and bump kRingVersion.

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <string>
#include <thread>
#include <vector>

#include "common/layout.hh"

namespace Lft3 {

constexpr std::uint64_t kRingMagic = 0x4C46543352494E47ULL;   // "LFT3RING"
constexpr std::uint32_t kRingVersion = 2;   // 2: kind + spectra fields (were reserved in 1)
constexpr std::uint64_t kInvalidSeq = ~0ULL;
constexpr std::uint32_t kMaxReaders = 16;
constexpr std::uint32_t kMaxSlots = 4096;
// Readers treat blocks older than head - (slots - kReadMargin) as unsafe (the writer may already be
// rewriting them); with backpressure the writer stays this far behind lossless readers' cursors.
constexpr std::uint64_t kReadMargin = 8;

enum class RingState : std::uint32_t { Starting = 0, Running = 1, Stopped = 2 };
enum class RingKind : std::uint32_t { Voltages = 0, Spectra = 1 };

constexpr std::uint32_t kSlotFlagPartial = 1;   // zero-filled packets in this block / spectrum
constexpr std::uint32_t kSlotFlagNewRun = 2;    // spectra ring: discontinuity before this spectrum

// Metadata of one published block (128 bytes).
struct alignas(64) RingSlot {
    std::atomic<std::uint64_t> seq;   // publish sequence of the block in this slot; kInvalidSeq while written
    std::uint64_t blockIndex;         // t0 / spectraPerBlock
    std::uint64_t t0;                 // spectrum counter of the block's first spectrum
    std::uint64_t missingPackets;     // zero-filled packets in this block
    std::uint64_t restarts;           // capture's stream-restart count (changes = new time base)
    std::int32_t nco[4];              // header NCO (whole MHz) of the block's first packet
    std::uint16_t pfbLsb[4];
    std::uint32_t fid;
    std::uint32_t flags;              // bit 0: partial (zero-filled packets)
    double unixTimeAtT0;              // capture's estimate of the UNIX time of t = 0
    double publishUnix;               // when the block was published (latency monitoring)
    std::uint8_t reserved[40];
};

// One registered reader (128 bytes). Owned by the reader; the writer reads it for backpressure.
struct alignas(64) RingReaderEntry {
    std::atomic<std::uint64_t> pid;          // 0 = free entry
    std::atomic<std::uint64_t> cursor;       // next seq the reader will consume
    std::atomic<std::uint64_t> heartbeatNs;  // CLOCK_MONOTONIC of the last update
    std::atomic<std::uint32_t> lossless;     // 1: the writer waits for this reader (if backpressure is on)
    std::uint32_t reserved0;
    char name[32];
    std::atomic<std::uint64_t> blocksRead;
    std::atomic<std::uint64_t> overruns;     // blocks overwritten while being read, or skipped for lag
    std::atomic<std::uint64_t> lagBlocks;    // head - cursor at the last update
    std::atomic<std::uint64_t> busyNs;       // processing time of the last block
    std::atomic<std::uint64_t> outputs;      // products written (spectra, dumps, ...)
    std::uint8_t reserved[24];
};

// Capture statistics (cumulative), written by the capture process.
struct RingCaptureStats {
    std::atomic<std::uint64_t> packetsReceived;
    std::atomic<std::uint64_t> packetsAccepted;
    std::atomic<std::uint64_t> packetsFiltered;
    std::atomic<std::uint64_t> packetsDuplicate;
    std::atomic<std::uint64_t> packetsLate;
    std::atomic<std::uint64_t> packetsNoBuffer;
    std::atomic<std::uint64_t> packetsMissing;
    std::atomic<std::uint64_t> blocksComplete;
    std::atomic<std::uint64_t> blocksPartial;
    std::atomic<std::uint64_t> blocksSkipped;
    std::atomic<std::uint64_t> blocksPublished;
    std::atomic<std::uint64_t> restarts;
    std::atomic<std::uint64_t> backpressureNs;   // time the writer waited for lossless readers
    std::atomic<std::uint64_t> latestT;
    std::atomic<std::uint64_t> rateMilliGbps;    // received Gb/s x 1000 (last 0.5 s)
    std::atomic<std::uint64_t> slotsUnavailable; // acquireSlot found the next slot still busy (ring too small)
};

struct RingControl {
    std::uint64_t magic;
    std::uint32_t version;
    std::uint32_t controlBytes;
    std::uint64_t ringId;               // random per capture start: readers detect a new ring
    char stream[48];
    char obsId[64];
    char software[64];
    char configHash[64];
    std::uint64_t channelStart;
    std::uint64_t channelCount;
    std::uint64_t spectraPerBlock;
    std::uint64_t beams;
    std::uint64_t slots;
    std::uint64_t blockBytes;
    std::uint64_t ringBytes;            // VMM allocation size (>= slots * blockBytes, 2 MiB granularity)
    std::uint8_t inputBeam[4];
    std::uint8_t inputPol[4];
    std::uint32_t boardId;
    std::atomic<std::uint32_t> state;   // RingState
    double ncoMHz;                      // exact NCO from the configuration (headers carry whole MHz)
    double spectraPerSecond;
    std::atomic<std::uint64_t> unixTimeAtT0Bits;   // latest estimate, double bits
    std::atomic<std::uint32_t> futex;   // incremented on every publish
    std::atomic<std::uint32_t> backpressure;
    std::atomic<std::uint64_t> head;    // number of blocks published (next seq)
    std::atomic<std::uint64_t> writerPid;
    std::atomic<std::uint64_t> writerHeartbeatNs;
    double startUnix;
    RingCaptureStats stats;
    std::uint32_t kind;                 // RingKind
    std::uint32_t fineChannelsPerCoarse;   // spectra ring: fine channels per coarse channel in a slot
    double fch1MHz;                     // spectra ring: centre of channel 0 (0 if the NCO was unknown)
    double foffMHz;                     // spectra ring: channel width
    double tsampS;                      // spectra ring: integration time of one slot
    std::uint8_t reserved0[16];
    RingReaderEntry readers[kMaxReaders];
    std::atomic<std::uint64_t> seqToSlot[kMaxSlots];
    RingSlot slot[kMaxSlots];
};

struct RingSlotMeta {
    std::uint64_t seq = 0;
    std::uint64_t blockIndex = 0;
    std::uint64_t t0 = 0;
    std::uint64_t missingPackets = 0;
    std::uint64_t restarts = 0;
    std::int32_t nco[4] = {};
    std::uint16_t pfbLsb[4] = {};
    std::uint32_t fid = 0;
    std::uint32_t flags = 0;
    double unixTimeAtT0 = 0.0;
    double publishUnix = 0.0;
};

std::string RingShmName(const std::string& stream);      // "/lft3-<stream>"
std::string RingSocketPath(const std::string& stream);   // "/dev/shm/lft3-<stream>.sock"

struct RingConfig {
    std::string stream;
    Layout layout;
    std::uint64_t slots = 256;
    std::uint32_t boardId = 0;
    double ncoMHz = 0.0;
    std::string obsId;
    std::string configHash;
    bool backpressure = false;
    RingKind kind = RingKind::Voltages;
    std::uint64_t blockBytes = 0;       // 0: layout.bytesPerBlock() (voltages)
    std::uint32_t fineChannelsPerCoarse = 0;
    double fch1MHz = 0.0, foffMHz = 0.0, tsampS = 0.0;
};

class RingWriter {
 public:
    RingWriter() = default;
    ~RingWriter();
    RingWriter(const RingWriter&) = delete;
    RingWriter& operator=(const RingWriter&) = delete;

    bool create(const RingConfig& config, std::string& error);
    void close();

    RingControl& control() { return *ctl_; }
    std::uint8_t* blockPtr(std::int64_t slot) const { return base_ + static_cast<std::uint64_t>(slot) * blockBytes_; }
    std::uint64_t slots() const { return slots_; }

    // Next slot in allocation order, invalidated for readers. -1 if it is still waiting to be published (the
    // ring is too small for the open-block window) or the writer is stopping while waiting for backpressure.
    std::int64_t acquireSlot();
    // The slot's GPU writes have completed: make it visible as the next sequence number.
    void publish(std::int64_t slot, const RingSlotMeta& meta);
    // The slot will not be published (block dropped): return it.
    void release(std::int64_t slot);
    void heartbeat();
    void setRunning();

 private:
    bool waitForLosslessReaders(std::uint64_t slotLastSeq);
    void serveFds();

    RingControl* ctl_ = nullptr;
    std::string shmName_;
    std::string socketPath_;
    int listenFd_ = -1;
    int memFd_ = -1;
    std::thread server_;
    std::atomic<bool> serving_{false};
    std::uint64_t handle_ = 0;          // CUmemGenericAllocationHandle
    std::uint64_t devPtr_ = 0;          // CUdeviceptr
    std::uint8_t* base_ = nullptr;
    std::uint64_t blockBytes_ = 0;
    std::uint64_t slots_ = 0;
    std::uint64_t ringBytes_ = 0;
    std::uint64_t allocCounter_ = 0;
    std::uint64_t nextSeq_ = 0;
    std::vector<std::uint8_t> busy_;            // acquired, not yet published / released
    std::vector<std::uint64_t> lastSeq_;        // seq last published from each slot (kInvalidSeq: none)
};

class RingReader {
 public:
    enum class Wait { Ready, Timeout, Stopped };

    RingReader() = default;
    ~RingReader();
    RingReader(const RingReader&) = delete;
    RingReader& operator=(const RingReader&) = delete;

    // Opens the control block and imports the data plane (read-only). Waits up to timeoutS for the ring.
    bool attach(const std::string& stream, const std::string& name, bool lossless, double timeoutS,
                std::string& error);
    void detach();

    const RingControl& control() const { return *ctl_; }
    Layout layout() const;
    std::uint64_t head() const { return ctl_->head.load(std::memory_order_acquire); }
    std::uint64_t slots() const { return ctl_->slots; }
    bool writerStopped() const {
        return ctl_->state.load(std::memory_order_acquire) == static_cast<std::uint32_t>(RingState::Stopped);
    }
    // Oldest sequence that can still be read safely, leaving `margin` slots for the writer's open blocks.
    std::uint64_t oldestReadable(std::uint64_t margin = kReadMargin) const;

    // Wait until block `seq` is published.
    Wait wait(std::uint64_t seq, int timeoutMs) const;
    // Metadata and device pointer of a published block; false if it was already overwritten.
    bool begin(std::uint64_t seq, RingSlotMeta& meta, const std::uint8_t*& data) const;
    // After the GPU work on `seq` has completed: true if the data was not overwritten meanwhile.
    bool stillValid(std::uint64_t seq) const;
    // Everything before `nextSeq` is consumed (cursor for backpressure + lag statistics).
    void consumed(std::uint64_t nextSeq, std::uint64_t busyNs = 0);
    RingReaderEntry& entry() { return ctl_->readers[entry_]; }

 private:
    RingControl* ctl_ = nullptr;
    int entry_ = -1;
    std::uint64_t handle_ = 0;
    std::uint64_t devPtr_ = 0;
    std::uint64_t ringBytes_ = 0;
    const std::uint8_t* base_ = nullptr;
    std::uint64_t blockBytes_ = 0;
};

}  // namespace Lft3

#endif  // LFT3_COMMON_RING_HH
