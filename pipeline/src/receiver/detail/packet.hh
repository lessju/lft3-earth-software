#ifndef LFT3_RECEIVER_DETAIL_PACKET_HH
#define LFT3_RECEIVER_DETAIL_PACKET_HH

// LFT3 RFSoC 4x2 channel-stream packet (spectrometer_8way_4x firmware).
// See docs/packet_format.md in the repository root; mirrors src/lft3/packets.py.
//
// Frame:   Ethernet(14) + IPv4(20) + UDP(8)  = 42 B
//          LFT3 header: eight big-endian 64-bit words = 64 B
//          payload: 1024 channels x 4 inputs x (I int8, Q int8) = 8192 B
//
// DAQIRI splits each frame into two segments (header-data split): segment 0 holds the first
// kHeaderSegmentBytes (network headers + LFT3 header, read by the CPU), segment 1 holds the payload
// (read only by the GPU scatter kernel).

#include <array>
#include <cstdint>
#include <cstring>
#include <endian.h>

namespace Lft3 {

constexpr std::uint64_t kNetworkHeaderBytes = 42;
constexpr std::uint64_t kLft3HeaderBytes = 64;
constexpr std::uint64_t kHeaderSegmentBytes = kNetworkHeaderBytes + kLft3HeaderBytes;  // 106
constexpr std::uint64_t kChannelsPerPacket = 1024;
constexpr std::uint64_t kInputs = 4;                    // ADC inputs per board
constexpr std::uint64_t kPayloadBytes = kChannelsPerPacket * kInputs * 2;               // 8192
constexpr std::uint64_t kPacketsPerSpectrum = 6;        // bindex 0..5
constexpr std::uint64_t kTotalChannels = kChannelsPerPacket * kPacketsPerSpectrum;     // 6144
constexpr std::uint64_t kFftSize = 8192;                // first-stage (FPGA) PFB length
constexpr double kSampleRateHz = 1250e6;                // complex samples/s after decimation
constexpr double kCoarseChannelHz = kSampleRateHz / kFftSize;   // 152587.890625 Hz
constexpr double kSpectraPerSecond = kCoarseChannelHz;          // one spectrum per PFB frame
constexpr std::uint64_t kDcChannel = 3072;              // k at the NCO centre (signed bin 0)

constexpr int kRxHeaderSegment = 0;
constexpr int kRxDataSegment = 1;

struct ChannelHeader {
    std::uint64_t t = 0;          // spectrum counter since the last sync (58 bits)
    std::uint32_t bindex = 0;     // which 1024-channel slice, 0..5
    std::uint32_t fid = 0;        // board id (3 bits)
    std::array<std::int32_t, 4> nco = {};        // signed, whole MHz (rounded by software)
    std::array<std::uint16_t, 4> pfbLsb = {};    // requantiser bit-select per input
};

inline std::uint64_t LoadBigEndian64(const std::uint8_t* p) {
    std::uint64_t v;
    std::memcpy(&v, p, sizeof(v));
    return be64toh(v);
}

// `lft3Header` points at the first byte after the UDP header.
inline ChannelHeader ParseChannelHeader(const std::uint8_t* lft3Header) {
    ChannelHeader h;
    const std::uint64_t w0 = LoadBigEndian64(lft3Header + 0);
    const std::uint64_t w1 = LoadBigEndian64(lft3Header + 8);
    const std::uint64_t w2 = LoadBigEndian64(lft3Header + 16);
    const std::uint64_t w3 = LoadBigEndian64(lft3Header + 24);
    h.t = w0 >> 6;
    h.bindex = static_cast<std::uint32_t>((w0 >> 3) & 0x7);
    h.fid = static_cast<std::uint32_t>(w0 & 0x7);
    h.nco[0] = static_cast<std::int32_t>(static_cast<std::uint32_t>(w1 >> 32));
    h.nco[1] = static_cast<std::int32_t>(static_cast<std::uint32_t>(w1));
    h.nco[2] = static_cast<std::int32_t>(static_cast<std::uint32_t>(w2 >> 32));
    h.nco[3] = static_cast<std::int32_t>(static_cast<std::uint32_t>(w2));
    for (int i = 0; i < 4; i++) {
        h.pfbLsb[i] = static_cast<std::uint16_t>(w3 >> (48 - 16 * i));
    }
    return h;
}

}  // namespace Lft3

#endif  // LFT3_RECEIVER_DETAIL_PACKET_HH
