#include "pcap.hh"

#include <cstring>
#include <fstream>

namespace Lft3 {

namespace {

std::uint32_t Load32(const std::uint8_t* p, bool swap) {
    std::uint32_t v;
    std::memcpy(&v, p, 4);
    return swap ? __builtin_bswap32(v) : v;
}

std::uint16_t LoadBe16(const std::uint8_t* p) {
    return static_cast<std::uint16_t>((p[0] << 8) | p[1]);
}

}  // namespace

bool ReadPcapChannelPackets(const std::string& path, std::uint16_t udpPort, PcapPackets& out, std::string& error) {
    out = {};
    std::ifstream f(path, std::ios::binary);
    if (!f) {
        error = "cannot open '" + path + "'";
        return false;
    }
    std::uint8_t global[24];
    if (!f.read(reinterpret_cast<char*>(global), sizeof(global))) {
        error = "'" + path + "' is too short for a pcap header";
        return false;
    }
    std::uint32_t magic;
    std::memcpy(&magic, global, 4);
    bool swap;
    if (magic == 0xA1B2C3D4 || magic == 0xA1B23C4D) {
        swap = false;
    } else if (magic == 0xD4C3B2A1 || magic == 0x4D3CB2A1) {
        swap = true;
    } else {
        error = "'" + path + "' is not a classic pcap file (convert pcapng with: editcap -F pcap in out)";
        return false;
    }
    if (Load32(global + 20, swap) != 1) {
        error = "pcap link type is not Ethernet";
        return false;
    }

    std::vector<std::uint8_t> frame;
    std::uint8_t rec[16];
    while (f.read(reinterpret_cast<char*>(rec), sizeof(rec))) {
        const std::uint32_t incl = Load32(rec + 8, swap);
        const std::uint32_t orig = Load32(rec + 12, swap);
        frame.resize(incl);
        if (!f.read(reinterpret_cast<char*>(frame.data()), incl)) {
            break;
        }
        if (incl != orig || incl < 14) {
            out.skipped++;
            continue;
        }
        std::size_t off = 12;
        std::uint16_t ethertype = LoadBe16(&frame[off]);
        off += 2;
        while (ethertype == 0x8100 && off + 4 <= incl) {           // VLAN tags
            ethertype = LoadBe16(&frame[off + 2]);
            off += 4;
        }
        if (ethertype != 0x0800 || off + 20 > incl || frame[off + 9] != 17) {
            out.skipped++;
            continue;
        }
        const std::size_t udp = off + (frame[off] & 0x0F) * 4;
        if (udp + 8 > incl || LoadBe16(&frame[udp + 2]) != udpPort) {
            out.skipped++;
            continue;
        }
        const std::size_t payload = udp + 8;
        const std::size_t length = LoadBe16(&frame[udp + 4]) - 8u;
        if (length != kLft3HeaderBytes + kPayloadBytes || payload + length > incl) {
            out.skipped++;
            continue;
        }
        out.headers.push_back(ParseChannelHeader(&frame[payload]));
        out.payloads.insert(out.payloads.end(), frame.begin() + payload + kLft3HeaderBytes,
                            frame.begin() + payload + length);
    }
    if (out.headers.empty()) {
        error = "no LFT3 channel packets for UDP port " + std::to_string(udpPort) + " in '" + path + "'";
        return false;
    }
    return true;
}

}  // namespace Lft3
