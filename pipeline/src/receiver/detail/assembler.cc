#include "assembler.hh"

#include <utility>

namespace Lft3 {

Assembler::Assembler(const AssemblerConfig& config, AcquireBuffer acquire)
    : config_(config), acquire_(std::move(acquire)) {}

void Assembler::restart() {
    // Flush whatever is open (in order), then forget the time base.
    for (auto it = open_.begin(); it != open_.end();) {
        auto next = std::next(it);
        seal(it, pending_);
        it = next;
    }
    anySeen_ = false;
    anySealed_ = false;
    latest_ = 0;
    lastSealed_ = 0;
    stats_.restarts++;
}

Assembler::Accept Assembler::accept(std::uint64_t t, std::uint64_t packetInSpectrum, Placement& placement) {
    const std::uint64_t index = t / config_.spectraPerBlock;
    const std::uint64_t slot = (t % config_.spectraPerBlock) * config_.packetsPerSpectrum + packetInSpectrum;

    // Stream restart: t went far backwards (firmware re-sync). Anything less drastic is just late.
    if (anySeen_ && index + config_.restartThresholdBlocks < latest_) {
        restart();
    }

    if (anySealed_ && index <= lastSealed_) {
        stats_.packetsLate++;
        return Accept::Late;
    }

    if (!anySeen_ || index > latest_) {
        latest_ = index;
    }
    anySeen_ = true;

    auto it = open_.find(index);
    placement.newBlock = false;
    if (it == open_.end()) {
        // Make room: seal the oldest open block, unless the new block is even older (then it is
        // effectively late, because sealing must stay in time order).
        if (open_.size() >= config_.maxOpenBlocks) {
            if (index < open_.begin()->first) {
                stats_.packetsLate++;
                return Accept::Late;
            }
            seal(open_.begin(), pending_);
        }
        const std::int64_t buffer = acquire_();
        if (buffer < 0) {
            stats_.packetsNoBuffer++;
            return Accept::NoBuffer;
        }
        OpenBlock block;
        block.buffer = buffer;
        block.filled.assign(config_.packetsPerBlock(), 0);
        it = open_.emplace(index, std::move(block)).first;
        placement.newBlock = true;
    }

    OpenBlock& block = it->second;
    if (block.filled[slot]) {
        stats_.packetsDuplicate++;
        return Accept::Duplicate;
    }
    block.filled[slot] = 1;
    block.received++;
    stats_.packetsAccepted++;

    placement.buffer = block.buffer;
    placement.slot = slot;
    return Accept::Ok;
}

void Assembler::seal(std::map<std::uint64_t, OpenBlock>::iterator it, std::vector<SealedBlock>& out) {
    const std::uint64_t index = it->first;
    OpenBlock& block = it->second;
    const std::uint64_t ppb = config_.packetsPerBlock();

    if (anySealed_ && index > lastSealed_ + 1) {
        const std::uint64_t skipped = index - lastSealed_ - 1;
        stats_.blocksSkipped += skipped;
        stats_.packetsMissing += skipped * ppb;
    }

    SealedBlock sealed;
    sealed.index = index;
    sealed.buffer = block.buffer;
    sealed.received = block.received;

    if (block.received == ppb) {
        stats_.blocksComplete++;
        sealed.emit = true;
    } else {
        stats_.packetsMissing += ppb - block.received;
        if (config_.zeroFill) {
            stats_.blocksPartial++;
            sealed.emit = true;
            sealed.missingSlots.reserve(ppb - block.received);
            for (std::uint64_t s = 0; s < ppb; s++) {
                if (!block.filled[s]) {
                    sealed.missingSlots.push_back(static_cast<std::uint32_t>(s));
                }
            }
        } else {
            stats_.blocksDropped++;
            sealed.emit = false;
        }
    }

    anySealed_ = true;
    lastSealed_ = index;
    out.push_back(std::move(sealed));
    open_.erase(it);
}

void Assembler::collect(std::vector<SealedBlock>& sealed) {
    for (auto& block : pending_) {
        sealed.push_back(std::move(block));
    }
    pending_.clear();

    while (!open_.empty()) {
        auto it = open_.begin();
        const bool complete = it->second.received == config_.packetsPerBlock();
        const bool stale = latest_ > it->first && latest_ - it->first > config_.maxBlockLag;
        if (!complete && !stale) {
            break;
        }
        seal(it, sealed);
    }
}

void Assembler::flush(std::vector<SealedBlock>& sealed) {
    collect(sealed);
    while (!open_.empty()) {
        seal(open_.begin(), sealed);
    }
}

}  // namespace Lft3
