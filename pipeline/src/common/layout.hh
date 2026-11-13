#ifndef LFT3_COMMON_LAYOUT_HH
#define LFT3_COMMON_LAYOUT_HH

// Where every received sample lands in a ring block (docs/runtime.md §3.1).
//
// A block holds `spectraPerBlock` consecutive spectra (time samples) of `channelCount` coarse channels for
// both beams:
//
//     block[beam][channel][t][pol]   complex int8 (I, Q) = 2 bytes per element, 4 bytes per (beam, channel, t)
//
// Each (beam, channel) row is `spectraPerBlock` dual-pol samples, contiguous in time: GUPPI RAW order per
// beam. A fine FFT of length N <= spectraPerBlock reads N consecutive samples of a row; longer FFTs gather
// rows of consecutive blocks. Inputs (ADC 0..3) map to (beam, pol) through inputBeam / inputPol.
//
// A packet carries 1024 channels x 4 inputs at one t: it fills 1024 rows of each beam at one t.
// channelStart/channelCount select whole packets (multiples of 1024 channels).

#include <array>
#include <cstdint>

#include "common/packet.hh"

namespace Lft3 {

struct Layout {
    std::uint64_t channelStart = 0;
    std::uint64_t channelCount = kTotalChannels;
    std::uint64_t spectraPerBlock = 1024;
    // For each ADC input (0..3): which beam (0/1) and polarisation (0 = X, 1 = Y) it carries.
    std::array<std::uint8_t, kInputs> inputBeam = {0, 0, 1, 1};
    std::array<std::uint8_t, kInputs> inputPol = {0, 1, 0, 1};

    static constexpr std::uint64_t kBeams = 2;
    static constexpr std::uint64_t kPols = 2;
    static constexpr std::uint64_t kBytesPerElement = 2;   // int8 I + int8 Q

    std::uint64_t firstPacket() const { return channelStart / kChannelsPerPacket; }
    std::uint64_t packetsPerSpectrum() const { return channelCount / kChannelsPerPacket; }
    std::uint64_t packetsPerBlock() const { return spectraPerBlock * packetsPerSpectrum(); }
    std::uint64_t elementsPerBlock() const { return kBeams * channelCount * spectraPerBlock * kPols; }
    std::uint64_t bytesPerBlock() const { return elementsPerBlock() * kBytesPerElement; }
    std::uint64_t bytesPerBeam() const { return bytesPerBlock() / kBeams; }

    bool keepsPacket(std::uint64_t bindex) const {
        return bindex >= firstPacket() && bindex < firstPacket() + packetsPerSpectrum();
    }
    std::uint64_t blockIndex(std::uint64_t t) const { return t / spectraPerBlock; }
    std::uint64_t blockFirstSpectrum(std::uint64_t blockIdx) const { return blockIdx * spectraPerBlock; }
    // Slot = one packet's position in the block: spectrum-major, then packet (bindex) within it.
    std::uint64_t slot(std::uint64_t t, std::uint64_t bindex) const {
        return (t % spectraPerBlock) * packetsPerSpectrum() + (bindex - firstPacket());
    }
    std::uint64_t slotSpectrum(std::uint64_t slotIdx) const { return slotIdx / packetsPerSpectrum(); }
    std::uint64_t slotChannelOffset(std::uint64_t slotIdx) const {
        return (slotIdx % packetsPerSpectrum()) * kChannelsPerPacket;
    }
    // Element index (units of one complex int8) of (beam, channel, t, pol) within a block.
    std::uint64_t elementOffset(std::uint64_t beam, std::uint64_t channel, std::uint64_t t, std::uint64_t pol) const {
        return ((beam * channelCount + channel) * spectraPerBlock + t) * kPols + pol;
    }
};

}  // namespace Lft3

#endif  // LFT3_COMMON_LAYOUT_HH
