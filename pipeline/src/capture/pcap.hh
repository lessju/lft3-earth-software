#ifndef LFT3_RECEIVER_DETAIL_PCAP_HH
#define LFT3_RECEIVER_DETAIL_PCAP_HH

// Reads LFT3 channel packets from a classic pcap capture (tcpdump -w), for replaying recorded data through
// the receiver (source: file). Keeps IPv4/UDP packets to `udpPort` with an 8256-byte payload.

#include <cstdint>
#include <string>
#include <vector>

#include "common/packet.hh"

namespace Lft3 {

struct PcapPackets {
    std::vector<ChannelHeader> headers;
    std::vector<std::uint8_t> payloads;     // kPayloadBytes per packet, in file order
    std::uint64_t skipped = 0;              // other traffic / wrong size / truncated
};

bool ReadPcapChannelPackets(const std::string& path, std::uint16_t udpPort, PcapPackets& out, std::string& error);

}  // namespace Lft3

#endif  // LFT3_RECEIVER_DETAIL_PCAP_HH
