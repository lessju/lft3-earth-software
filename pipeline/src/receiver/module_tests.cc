#include <catch2/catch_test_macros.hpp>

#include <complex>
#include <cstring>
#include <random>
#include <vector>

#include <cuda_runtime.h>

#include "detail/assembler.hh"
#include "detail/kernel.hh"
#include "detail/layout.hh"
#include "detail/packet.hh"
#include "detail/pcap.hh"

#include <cstdio>
#include <fstream>

using namespace Lft3;

namespace {

void PutBigEndian64(std::uint8_t* p, std::uint64_t v) {
    for (int i = 7; i >= 0; i--) {
        p[i] = static_cast<std::uint8_t>(v & 0xFF);
        v >>= 8;
    }
}

// Same layout as src/lft3/packets.py make_channel_packet (big-endian words).
std::vector<std::uint8_t> MakeHeader(std::uint64_t t, std::uint32_t b, std::uint32_t fid,
                                     std::array<std::int32_t, 4> nco, std::array<std::uint16_t, 4> lsb) {
    std::vector<std::uint8_t> h(kLft3HeaderBytes, 0);
    PutBigEndian64(h.data() + 0, (t << 6) | (static_cast<std::uint64_t>(b & 7) << 3) | (fid & 7));
    const auto u = [](std::int32_t v) { return static_cast<std::uint64_t>(static_cast<std::uint32_t>(v)); };
    PutBigEndian64(h.data() + 8, (u(nco[0]) << 32) | u(nco[1]));
    PutBigEndian64(h.data() + 16, (u(nco[2]) << 32) | u(nco[3]));
    PutBigEndian64(h.data() + 24, (static_cast<std::uint64_t>(lsb[0]) << 48) | (static_cast<std::uint64_t>(lsb[1]) << 32) |
                                      (static_cast<std::uint64_t>(lsb[2]) << 16) | lsb[3]);
    return h;
}

}  // namespace

TEST_CASE("LFT3 header parses like the Python decoder", "[lft3][receiver][packet]") {
    const auto h = MakeHeader(123456789, 5, 3, {11, -2031, 33, -1}, {1, 2, 3, 4});
    const auto p = ParseChannelHeader(h.data());
    REQUIRE(p.t == 123456789);
    REQUIRE(p.bindex == 5);
    REQUIRE(p.fid == 3);
    REQUIRE(p.nco == std::array<std::int32_t, 4>{11, -2031, 33, -1});
    REQUIRE(p.pfbLsb == std::array<std::uint16_t, 4>{1, 2, 3, 4});
}

TEST_CASE("Layout maps packets to slots and elements", "[lft3][receiver][layout]") {
    Layout l;
    l.channelStart = 2048;
    l.channelCount = 2048;
    l.fftLength = 4;
    l.segments = 3;
    REQUIRE(l.firstPacket() == 2);
    REQUIRE(l.packetsPerSpectrum() == 2);
    REQUIRE(l.spectraPerBlock() == 12);
    REQUIRE(l.packetsPerBlock() == 24);
    REQUIRE(l.keepsPacket(2));
    REQUIRE(l.keepsPacket(3));
    REQUIRE_FALSE(l.keepsPacket(1));
    REQUIRE_FALSE(l.keepsPacket(4));

    const std::uint64_t t = 12 * 7 + 9;   // block 7, spectrum 9 -> segment 2, sample 1
    REQUIRE(l.blockIndex(t) == 7);
    const auto slot = l.slot(t, 3);
    REQUIRE(slot == 9 * 2 + 1);
    REQUIRE(l.slotSpectrum(slot) == 9);
    REQUIRE(l.slotChannelOffset(slot) == 1024);
    REQUIRE(l.elementOffset(1, 5, 2, 1) == ((1 * 2048 + 5) * 4 + 2) * 2 + 1);
}

TEST_CASE("Assembler emits complete blocks in order", "[lft3][receiver][assembler]") {
    AssemblerConfig c;
    c.spectraPerBlock = 4;
    c.packetsPerSpectrum = 2;
    c.maxOpenBlocks = 3;
    c.maxBlockLag = 1;
    std::int64_t next = 0;
    Assembler a(c, [&]() { return next++; });

    // Block 0 is partly received, then block 1 completes: block 1 must wait behind block 0.
    Assembler::Placement p;
    REQUIRE(a.accept(0, 0, p) == Assembler::Accept::Ok);
    for (std::uint64_t t = 4; t < 8; t++)
        for (std::uint64_t b = 0; b < 2; b++) REQUIRE(a.accept(t, b, p) == Assembler::Accept::Ok);
    std::vector<SealedBlock> sealed;
    a.collect(sealed);
    REQUIRE(sealed.empty());

    for (std::uint64_t t = 0; t < 4; t++)
        for (std::uint64_t b = 0; b < 2; b++)
            if (!(t == 0 && b == 0)) REQUIRE(a.accept(t, b, p) == Assembler::Accept::Ok);
    a.collect(sealed);
    REQUIRE(sealed.size() == 2);
    REQUIRE(sealed[0].index == 0);
    REQUIRE(sealed[1].index == 1);
    REQUIRE(sealed[0].missingSlots.empty());
    REQUIRE(a.stats().blocksComplete == 2);

    // Packets for a sealed block are late; duplicates are rejected.
    REQUIRE(a.accept(2, 0, p) == Assembler::Accept::Late);
    REQUIRE(a.accept(8, 0, p) == Assembler::Accept::Ok);
    REQUIRE(a.accept(8, 0, p) == Assembler::Accept::Duplicate);
}

TEST_CASE("Assembler zero-fills stale blocks and counts skipped ones", "[lft3][receiver][assembler]") {
    AssemblerConfig c;
    c.spectraPerBlock = 2;
    c.packetsPerSpectrum = 3;
    c.maxOpenBlocks = 4;
    c.maxBlockLag = 1;
    std::int64_t next = 0;
    Assembler a(c, [&]() { return next++; });
    Assembler::Placement p;
    std::vector<SealedBlock> sealed;

    // Block 0: one packet missing (t=1, b=2).
    for (std::uint64_t t = 0; t < 2; t++)
        for (std::uint64_t b = 0; b < 3; b++)
            if (!(t == 1 && b == 2)) a.accept(t, b, p);
    // Block 3 arrives (blocks 1 and 2 never do): block 0 is now 3 behind -> stale.
    a.accept(6, 0, p);
    a.collect(sealed);
    REQUIRE(sealed.size() == 1);
    REQUIRE(sealed[0].index == 0);
    REQUIRE(sealed[0].emit);
    REQUIRE(sealed[0].missingSlots == std::vector<std::uint32_t>{1 * 3 + 2});

    // Fill block 3 completely: it seals next, and blocks 1-2 are counted as skipped.
    for (std::uint64_t t = 6; t < 8; t++)
        for (std::uint64_t b = 0; b < 3; b++) a.accept(t, b, p);
    a.collect(sealed);
    REQUIRE(sealed.size() == 2);
    REQUIRE(sealed[1].index == 3);
    REQUIRE(a.stats().blocksSkipped == 2);
    REQUIRE(a.stats().packetsMissing == 1 + 2 * 6);
}

TEST_CASE("Assembler drops incomplete blocks without zero-fill, and restarts", "[lft3][receiver][assembler]") {
    AssemblerConfig c;
    c.spectraPerBlock = 2;
    c.packetsPerSpectrum = 1;
    c.maxOpenBlocks = 2;
    c.maxBlockLag = 1;
    c.zeroFill = false;
    c.restartThresholdBlocks = 10;
    std::int64_t next = 0;
    Assembler a(c, [&]() { return next++; });
    Assembler::Placement p;
    std::vector<SealedBlock> sealed;

    a.accept(1000, 0, p);       // block 500, half full
    a.accept(1004, 0, p);       // block 502
    a.collect(sealed);
    REQUIRE(sealed.size() == 1);
    REQUIRE_FALSE(sealed[0].emit);
    REQUIRE(a.stats().blocksDropped == 1);

    // t jumps back to 0 (firmware re-sync): restart, not "late".
    REQUIRE(a.accept(0, 0, p) == Assembler::Accept::Ok);
    REQUIRE(a.stats().restarts == 1);
}

TEST_CASE("Assembler reports NoBuffer when the pool is empty", "[lft3][receiver][assembler]") {
    AssemblerConfig c;
    c.spectraPerBlock = 1;
    c.packetsPerSpectrum = 1;
    Assembler a(c, []() { return -1; });
    Assembler::Placement p;
    REQUIRE(a.accept(0, 0, p) == Assembler::Accept::NoBuffer);
    REQUIRE(a.stats().packetsNoBuffer == 1);
}

TEST_CASE("Scatter kernel places packets and zero-fills on the GPU", "[lft3][receiver][kernel][cuda]") {
    int devices = 0;
    if (cudaGetDeviceCount(&devices) != cudaSuccess || devices == 0) {
        SKIP("No CUDA device.");
    }

    Layout l;
    l.channelStart = 1024;
    l.channelCount = 2048;   // two packets per spectrum
    l.fftLength = 4;
    l.segments = 2;
    l.inputBeam = {1, 0, 0, 1};   // non-default mapping to test the routing
    l.inputPol = {0, 1, 0, 1};

    ScatterGeometry g = {};
    g.channelCount = l.channelCount;
    g.fftLength = l.fftLength;
    g.segments = l.segments;
    for (int i = 0; i < 4; i++) {
        g.inputBeam[i] = l.inputBeam[i];
        g.inputPol[i] = l.inputPol[i];
    }
    g.outputCF32 = true;
    g.fftShiftSign = true;

    const std::uint64_t elements = l.elementsPerBlock();
    std::complex<float>* out = nullptr;
    REQUIRE(cudaMallocManaged(&out, elements * sizeof(std::complex<float>)) == cudaSuccess);
    for (std::uint64_t e = 0; e < elements; e++) out[e] = {1000.0f, 1000.0f};   // outside the int8 range

    // Packets: every spectrum 0..7, both packets, except (spectrum 5, packet 1) which is "missing".
    std::mt19937 rng(1);
    std::uniform_int_distribution<int> dist(-128, 127);
    const std::uint64_t packets = l.packetsPerBlock();
    std::uint8_t* payloads = nullptr;
    ScatterJob* jobs = nullptr;
    REQUIRE(cudaMallocHost(&payloads, packets * kPayloadBytes) == cudaSuccess);
    REQUIRE(cudaMallocHost(&jobs, packets * sizeof(ScatterJob)) == cudaSuccess);
    for (std::uint64_t i = 0; i < packets * kPayloadBytes; i++) payloads[i] = static_cast<std::uint8_t>(dist(rng));

    std::uint32_t n = 0;
    for (std::uint64_t slot = 0; slot < packets; slot++) {
        const bool missing = (l.slotSpectrum(slot) == 5 && l.slotChannelOffset(slot) == 1024);
        jobs[n++] = {missing ? nullptr : payloads + slot * kPayloadBytes, out,
                     static_cast<std::uint32_t>(l.slotSpectrum(slot)),
                     static_cast<std::uint32_t>(l.slotChannelOffset(slot))};
    }
    REQUIRE(LaunchScatter(jobs, n, g, nullptr) == cudaSuccess);
    REQUIRE(cudaDeviceSynchronize() == cudaSuccess);

    // CPU reference.
    std::uint64_t mismatches = 0;
    for (std::uint64_t slot = 0; slot < packets; slot++) {
        const std::uint64_t spectrum = l.slotSpectrum(slot);
        const std::uint64_t seg = spectrum / l.fftLength;
        const std::uint64_t s = spectrum % l.fftLength;
        const bool missing = (spectrum == 5 && l.slotChannelOffset(slot) == 1024);
        const float sign = (s & 1) ? -1.0f : 1.0f;
        for (std::uint64_t ch = 0; ch < kChannelsPerPacket; ch++) {
            const std::uint64_t c = l.slotChannelOffset(slot) + ch;
            for (int input = 0; input < 4; input++) {
                const std::uint64_t aspect = l.inputBeam[input] * l.segments + seg;
                const auto got = out[l.elementOffset(aspect, c, s, l.inputPol[input])];
                std::complex<float> want = {0.0f, 0.0f};
                if (!missing) {
                    const auto* bytes = reinterpret_cast<const std::int8_t*>(payloads + slot * kPayloadBytes + ch * 8);
                    want = {sign * bytes[2 * input], sign * bytes[2 * input + 1]};
                }
                if (got != want) mismatches++;
            }
        }
    }
    REQUIRE(mismatches == 0);

    // Every element was written exactly by the jobs (no sentinel left).
    std::uint64_t untouched = 0;
    for (std::uint64_t e = 0; e < elements; e++) untouched += (out[e] == std::complex<float>{1000.0f, 1000.0f});
    REQUIRE(untouched == 0);

    cudaFree(out);
    cudaFreeHost(payloads);
    cudaFreeHost(jobs);
}

TEST_CASE("pcap reader extracts LFT3 channel packets", "[lft3][receiver][pcap]") {
    const std::string path = "/tmp/lft3_pcap_test.pcap";
    {
        std::ofstream f(path, std::ios::binary);
        const std::uint32_t global[6] = {0xA1B2C3D4, 0x00040002, 0, 0, 65535, 1};
        f.write(reinterpret_cast<const char*>(global), sizeof(global));
        auto frame = [&](std::uint16_t dport, std::uint64_t t, std::uint32_t b, std::uint8_t fill) {
            std::vector<std::uint8_t> pkt(14 + 20 + 8);
            pkt[12] = 0x08; pkt[13] = 0x00;                        // IPv4
            pkt[14] = 0x45; pkt[23] = 17;                          // IHL 5, UDP
            const std::uint16_t ulen = 8 + kLft3HeaderBytes + kPayloadBytes;
            pkt[34 + 2] = dport >> 8; pkt[34 + 3] = dport & 0xFF;
            pkt[34 + 4] = ulen >> 8; pkt[34 + 5] = ulen & 0xFF;
            auto hdr = MakeHeader(t, b, 0, {-2031, -2031, -2031, -2031}, {0, 0, 0, 0});
            pkt.insert(pkt.end(), hdr.begin(), hdr.end());
            pkt.insert(pkt.end(), kPayloadBytes, fill);
            const std::uint32_t rec[4] = {0, 0, static_cast<std::uint32_t>(pkt.size()), static_cast<std::uint32_t>(pkt.size())};
            f.write(reinterpret_cast<const char*>(rec), sizeof(rec));
            f.write(reinterpret_cast<const char*>(pkt.data()), pkt.size());
        };
        frame(60000, 100, 0, 7);
        frame(40000, 100, 1, 8);     // ADC port: skipped
        frame(60000, 100, 2, 9);
    }
    PcapPackets out;
    std::string error;
    REQUIRE(ReadPcapChannelPackets(path, 60000, out, error));
    REQUIRE(out.headers.size() == 2);
    REQUIRE(out.skipped == 1);
    REQUIRE(out.headers[0].t == 100);
    REQUIRE(out.headers[1].bindex == 2);
    REQUIRE(out.headers[0].nco[0] == -2031);
    REQUIRE(out.payloads.size() == 2 * kPayloadBytes);
    REQUIRE(out.payloads[0] == 7);
    REQUIRE(out.payloads[kPayloadBytes] == 9);
    REQUIRE_FALSE(ReadPcapChannelPackets(path, 12345, out, error));
    std::remove(path.c_str());
}
