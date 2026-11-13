#ifndef LFT3_RECEIVER_DETAIL_LAYOUT_HH
#define LFT3_RECEIVER_DETAIL_LAYOUT_HH

// Where every received sample lands in the receiver's output tensor.
//
// Output tensor shape: [beams * segments, channelCount, fftLength, 2 pols]
//
//   * A "block" is spectraPerBlock = fftLength * segments consecutive spectra (time samples).
//   * The time axis is folded: spectrum n of the block (0 <= n < spectraPerBlock) is written to
//         segment m = n / fftLength, sample s = n % fftLength
//     and the segment is folded into the first ("aspect") axis: aspect = beam * segments + m.
//   * The next stage FFTs along the sample axis (length fftLength), so each aspect row holds one
//     independent fine-FFT input. Folding several FFTs per block into the aspect axis lets one
//     tensor carry many FFTs (fewer, larger pipeline cycles) while keeping fftLength = FFT size.
//     After detection, integrating over the aspect axis in groups of `segments` collapses the fold
//     back to one spectrum per beam.
//   * channelCount must be a multiple of 1024 (whole packets); channelStart selects which packets
//     (bindex) are kept, e.g. channelStart=2048, channelCount=2048 keeps bindex 2 and 3.
//
// Element offset of (aspect a, channel c, sample s, pol p) = ((a * C + c) * S + s) * 2 + p.

#include <array>
#include <cstdint>

#include "packet.hh"

namespace Lft3 {

struct Layout {
    std::uint64_t channelStart = 0;
    std::uint64_t channelCount = kTotalChannels;
    std::uint64_t fftLength = 128;
    std::uint64_t segments = 8;
    // For each ADC input (0..3): which beam (0/1) and polarisation (0 = X, 1 = Y) it carries.
    std::array<std::uint8_t, kInputs> inputBeam = {0, 0, 1, 1};
    std::array<std::uint8_t, kInputs> inputPol = {0, 1, 0, 1};

    static constexpr std::uint64_t kBeams = 2;
    static constexpr std::uint64_t kPols = 2;

    std::uint64_t firstPacket() const { return channelStart / kChannelsPerPacket; }
    std::uint64_t packetsPerSpectrum() const { return channelCount / kChannelsPerPacket; }
    std::uint64_t spectraPerBlock() const { return fftLength * segments; }
    std::uint64_t packetsPerBlock() const { return spectraPerBlock() * packetsPerSpectrum(); }
    std::uint64_t aspects() const { return kBeams * segments; }
    std::uint64_t elementsPerBlock() const { return aspects() * channelCount * fftLength * kPols; }

    bool keepsPacket(std::uint64_t bindex) const {
        return bindex >= firstPacket() && bindex < firstPacket() + packetsPerSpectrum();
    }
    std::uint64_t blockIndex(std::uint64_t t) const { return t / spectraPerBlock(); }
    std::uint64_t blockFirstSpectrum(std::uint64_t blockIdx) const { return blockIdx * spectraPerBlock(); }
    // Slot = one packet's position in the block: spectrum-major, then packet (bindex) within it.
    std::uint64_t slot(std::uint64_t t, std::uint64_t bindex) const {
        return (t % spectraPerBlock()) * packetsPerSpectrum() + (bindex - firstPacket());
    }
    std::uint64_t slotSpectrum(std::uint64_t slotIdx) const { return slotIdx / packetsPerSpectrum(); }
    std::uint64_t slotChannelOffset(std::uint64_t slotIdx) const {
        return (slotIdx % packetsPerSpectrum()) * kChannelsPerPacket;
    }
    std::uint64_t elementOffset(std::uint64_t aspect, std::uint64_t channel,
                                std::uint64_t sample, std::uint64_t pol) const {
        return ((aspect * channelCount + channel) * fftLength + sample) * kPols + pol;
    }
};

}  // namespace Lft3

#endif  // LFT3_RECEIVER_DETAIL_LAYOUT_HH
