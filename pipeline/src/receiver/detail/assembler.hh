#ifndef LFT3_RECEIVER_DETAIL_ASSEMBLER_HH
#define LFT3_RECEIVER_DETAIL_ASSEMBLER_HH

// Block assembler: packet bookkeeping for the LFT3 receiver, independent of DAQIRI and CUDA.
//
// Packets are identified by (t, bindex). Each block covers `spectraPerBlock` consecutive spectra and
// has `packetsPerBlock` slots. The assembler:
//   * assigns each accepted packet a (buffer, slot) placement, rejecting duplicates and late packets,
//   * keeps up to `maxOpenBlocks` blocks open at once (packets can arrive slightly out of order),
//   * seals blocks strictly in time order: a block is sealed when complete, or when it is more than
//     `maxBlockLag` blocks behind the newest block seen (then its missing slots are reported so the
//     caller can zero-fill them, or the block is dropped if zero-fill is disabled),
//   * detects stream restarts (t jumping backwards by more than `restartThresholdBlocks` blocks, e.g.
//     after a firmware re-sync) and starts over instead of treating everything as late.
//
// Buffers are opaque integer ids handed out by the owner through acquireBuffer(); a sealed block
// returns its id, and the owner recycles it once the data has been consumed.

#include <cstdint>
#include <functional>
#include <map>
#include <vector>

namespace Lft3 {

struct AssemblerConfig {
    std::uint64_t spectraPerBlock = 1024;
    std::uint64_t packetsPerSpectrum = 6;
    std::uint64_t maxOpenBlocks = 4;
    std::uint64_t maxBlockLag = 2;
    std::uint64_t restartThresholdBlocks = 1024;
    bool zeroFill = true;

    std::uint64_t packetsPerBlock() const { return spectraPerBlock * packetsPerSpectrum; }
};

struct SealedBlock {
    std::uint64_t index = 0;           // block index = t / spectraPerBlock
    std::int64_t buffer = -1;          // owner's buffer id
    std::uint64_t received = 0;        // filled slots
    std::vector<std::uint32_t> missingSlots;   // slots to zero-fill (empty when complete)
    bool emit = true;                  // false: drop (incomplete and zero-fill disabled, or flushed)
};

struct AssemblerStats {
    std::uint64_t packetsAccepted = 0;
    std::uint64_t packetsDuplicate = 0;
    std::uint64_t packetsLate = 0;          // arrived after their block was sealed
    std::uint64_t packetsNoBuffer = 0;      // dropped: no free output buffer
    std::uint64_t packetsMissing = 0;       // empty slots in sealed blocks + slots of skipped blocks
    std::uint64_t blocksComplete = 0;
    std::uint64_t blocksPartial = 0;        // sealed with missing slots and emitted (zero-filled)
    std::uint64_t blocksDropped = 0;        // sealed incomplete and not emitted
    std::uint64_t blocksSkipped = 0;        // no packet at all arrived for the block
    std::uint64_t restarts = 0;
};

class Assembler {
 public:
    enum class Accept { Ok, Duplicate, Late, NoBuffer };

    struct Placement {
        std::int64_t buffer = -1;
        std::uint64_t slot = 0;
        bool newBlock = false;   // first packet of this block (owner may capture header metadata)
    };

    using AcquireBuffer = std::function<std::int64_t()>;   // returns -1 when none is free

    Assembler(const AssemblerConfig& config, AcquireBuffer acquire);

    // `packetInSpectrum` is bindex - firstPacket (0 .. packetsPerSpectrum-1).
    Accept accept(std::uint64_t t, std::uint64_t packetInSpectrum, Placement& placement);

    // Appends every block that is ready to seal, in increasing block index.
    void collect(std::vector<SealedBlock>& sealed);

    // Seals every open block (e.g. on shutdown). Incomplete blocks follow the zero-fill rule.
    void flush(std::vector<SealedBlock>& sealed);

    const AssemblerStats& stats() const { return stats_; }
    std::uint64_t openBlocks() const { return open_.size(); }
    std::uint64_t latestBlock() const { return latest_; }

 private:
    struct OpenBlock {
        std::int64_t buffer = -1;
        std::uint64_t received = 0;
        std::vector<std::uint8_t> filled;
    };

    void seal(std::map<std::uint64_t, OpenBlock>::iterator it, std::vector<SealedBlock>& out);
    void restart();

    AssemblerConfig config_;
    AcquireBuffer acquire_;
    std::map<std::uint64_t, OpenBlock> open_;     // ordered by block index
    std::vector<SealedBlock> pending_;            // sealed during accept(), handed out by collect()
    bool anySeen_ = false;
    bool anySealed_ = false;
    std::uint64_t latest_ = 0;
    std::uint64_t lastSealed_ = 0;
    AssemblerStats stats_;
};

}  // namespace Lft3

#endif  // LFT3_RECEIVER_DETAIL_ASSEMBLER_HH
