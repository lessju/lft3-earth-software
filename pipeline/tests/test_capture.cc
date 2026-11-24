#include <catch2/catch_test_macros.hpp>

#include <cmath>
#include <complex>
#include <cstring>
#include <random>
#include <algorithm>
#include <vector>

#include <cuda_runtime.h>

#include "capture/assembler.hh"
#include "capture/pcap.hh"
#include "capture/scatter.hh"
#include "capture/synth.hh"
#include "common/layout.hh"
#include "common/packet.hh"

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

TEST_CASE("Layout maps packets to slots and elements", "[lft3][capture][layout]") {
    Layout l;
    l.channelStart = 2048;
    l.channelCount = 2048;
    l.spectraPerBlock = 12;
    REQUIRE(l.firstPacket() == 2);
    REQUIRE(l.packetsPerSpectrum() == 2);
    REQUIRE(l.packetsPerBlock() == 24);
    REQUIRE(l.keepsPacket(2));
    REQUIRE(l.keepsPacket(3));
    REQUIRE_FALSE(l.keepsPacket(1));
    REQUIRE_FALSE(l.keepsPacket(4));

    const std::uint64_t t = 12 * 7 + 9;   // block 7, spectrum 9
    REQUIRE(l.blockIndex(t) == 7);
    const auto slot = l.slot(t, 3);
    REQUIRE(slot == 9 * 2 + 1);
    REQUIRE(l.slotSpectrum(slot) == 9);
    REQUIRE(l.slotChannelOffset(slot) == 1024);
    // [beam][channel][t][pol]
    REQUIRE(l.elementOffset(1, 5, 2, 1) == ((1 * 2048 + 5) * 12 + 2) * 2 + 1);
    REQUIRE(l.bytesPerBlock() == 2 * 2048 * 12 * 2 * 2);
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

TEST_CASE("Assembler drops incomplete warm-up blocks without counting them as missing", "[lft3][receiver][assembler]") {
    AssemblerConfig c;
    c.spectraPerBlock = 2;
    c.packetsPerSpectrum = 1;
    c.maxOpenBlocks = 4;
    c.maxBlockLag = 1;
    c.warmupBlocks = 2;
    c.restartThresholdBlocks = 10;
    std::int64_t next = 0;
    Assembler a(c, [&]() { return next++; });
    Assembler::Placement p;
    std::vector<SealedBlock> sealed;

    // Picked up mid-stream: block 50 half full, block 51 half full, then blocks 52 and 53 half full.
    for (std::uint64_t t : {101, 102, 104, 106, 108, 109}) a.accept(t, 0, p);
    a.collect(sealed);   // latest = 54: 50..52 are stale
    REQUIRE(sealed.size() == 3);
    REQUIRE_FALSE(sealed[0].emit);   // warm-up
    REQUIRE_FALSE(sealed[1].emit);   // warm-up
    REQUIRE(sealed[2].emit);         // past warm-up: zero-filled and counted
    REQUIRE(a.stats().warmupBlocksDropped == 2);
    REQUIRE(a.stats().warmupPacketsMissing == 2);
    REQUIRE(a.stats().packetsMissing == 1);
    REQUIRE(a.stats().blocksPartial == 1);

    // A firmware re-sync starts a new warm-up.
    a.flush(sealed);
    sealed.clear();
    a.accept(1, 0, p);   // block 0, half full
    a.accept(6, 0, p);   // block 3
    a.collect(sealed);
    REQUIRE(a.stats().restarts == 1);
    REQUIRE_FALSE(sealed.at(0).emit);
    REQUIRE(a.stats().warmupBlocksDropped == 3);
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

TEST_CASE("Scatter kernel places packets into ring blocks and zero-fills", "[lft3][capture][kernel][cuda]") {
    int devices = 0;
    if (cudaGetDeviceCount(&devices) != cudaSuccess || devices == 0) {
        SKIP("No CUDA device.");
    }
    Layout l;
    l.channelStart = 1024;
    l.channelCount = 2048;   // two packets per spectrum
    l.spectraPerBlock = 40;  // not a multiple of the 32-job tile
    l.inputBeam = {1, 0, 0, 1};   // non-default mapping to test the routing
    l.inputPol = {0, 1, 0, 1};
    ScatterGeometry g = {};
    g.channelCount = static_cast<std::uint32_t>(l.channelCount);
    g.spectraPerBlock = static_cast<std::uint32_t>(l.spectraPerBlock);
    for (int i = 0; i < 4; i++) {
        g.inputBeam[i] = l.inputBeam[i];
        g.inputPol[i] = l.inputPol[i];
    }
    // Two blocks; sentinel 0x7F7F everywhere.
    const std::uint64_t bytes = l.bytesPerBlock();
    std::uint8_t* out = nullptr;
    REQUIRE(cudaMallocManaged(&out, 2 * bytes) == cudaSuccess);
    std::memset(out, 0x7F, 2 * bytes);

    std::mt19937 rng(1);
    std::uniform_int_distribution<int> dist(-127, 126);   // never 0x7F
    const std::uint64_t packets = l.packetsPerBlock();
    std::uint8_t* payloads = nullptr;
    ScatterJob* jobs = nullptr;
    ScatterJob* sorted = nullptr;
    REQUIRE(cudaMallocHost(&payloads, 2 * packets * kPayloadBytes) == cudaSuccess);
    REQUIRE(cudaMallocHost(&jobs, 2 * packets * sizeof(ScatterJob)) == cudaSuccess);
    REQUIRE(cudaMallocHost(&sorted, 2 * packets * sizeof(ScatterJob)) == cudaSuccess);
    for (std::uint64_t i = 0; i < 2 * packets * kPayloadBytes; i++) payloads[i] = static_cast<std::uint8_t>(dist(rng));

    // Arrival order t-major across both blocks, shuffled a little; (block 1, spectrum 5, packet 1) missing.
    auto isMissing = [&](std::uint64_t blk, std::uint64_t slot) {
        return blk == 1 && l.slotSpectrum(slot) == 5 && l.slotChannelOffset(slot) == 1024;
    };
    std::uint32_t n = 0;
    for (std::uint64_t blk = 0; blk < 2; blk++) {
        for (std::uint64_t slot = 0; slot < packets; slot++) {
            jobs[n++] = {isMissing(blk, slot) ? nullptr : payloads + (blk * packets + slot) * kPayloadBytes,
                         out + blk * bytes, static_cast<std::uint32_t>(l.slotSpectrum(slot)),
                         static_cast<std::uint32_t>(l.slotChannelOffset(slot))};
        }
    }
    std::shuffle(jobs + 3, jobs + 20, rng);
    SortJobsForScatter(jobs, n, sorted);
    for (std::uint32_t i = 1; i < n; i++) REQUIRE(sorted[i - 1].channelOffset <= sorted[i].channelOffset);
    REQUIRE(LaunchScatter(sorted, n, g, nullptr) == cudaSuccess);
    REQUIRE(cudaDeviceSynchronize() == cudaSuccess);

    std::uint64_t mismatches = 0, written = 0;
    for (std::uint64_t blk = 0; blk < 2; blk++) {
        const auto* b = reinterpret_cast<const std::int8_t*>(out + blk * bytes);
        for (std::uint64_t slot = 0; slot < packets; slot++) {
            const std::uint64_t t = l.slotSpectrum(slot);
            for (std::uint64_t ch = 0; ch < kChannelsPerPacket; ch++) {
                const std::uint64_t c = l.slotChannelOffset(slot) + ch;
                for (int input = 0; input < 4; input++) {
                    const std::uint64_t e = l.elementOffset(l.inputBeam[input], c, t, l.inputPol[input]);
                    std::int8_t wi = 0, wq = 0;
                    if (!isMissing(blk, slot)) {
                        const auto* p = reinterpret_cast<const std::int8_t*>(payloads + (blk * packets + slot) * kPayloadBytes + ch * 8);
                        wi = p[2 * input];
                        wq = p[2 * input + 1];
                    }
                    mismatches += (b[2 * e] != wi || b[2 * e + 1] != wq);
                    written++;
                }
            }
        }
    }
    REQUIRE(written * 2 == 2 * bytes);   // every byte of both blocks is covered exactly once
    REQUIRE(mismatches == 0);
    cudaFree(out);
    cudaFreeHost(payloads);
    cudaFreeHost(jobs);
    cudaFreeHost(sorted);
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

TEST_CASE("Random synthetic source: GPU block matches the host reference, no repeats, tone where asked",
          "[lft3][capture][synth][cuda]") {
    int n = 0;
    if (cudaGetDeviceCount(&n) != cudaSuccess || n == 0) SKIP("No CUDA device.");
    SynthParams p;
    p.channelCount = 1024;
    p.spectraPerBlock = 64;
    p.channelStart = 2048;
    p.seed = 7;
    p.toneChannel = 2048 + 100;
    p.toneInput = 3;                  // beam 1, pol Y with the default mapping
    p.toneAmplitude = 40.0;
    p.toneFrequency = 0.125;
    p.toneChirp = 1e-6;
    const std::size_t bytes = 2ULL * p.channelCount * p.spectraPerBlock * 4;
    std::uint8_t* d = nullptr;
    REQUIRE(cudaMalloc(&d, 2 * bytes) == cudaSuccess);
    REQUIRE(LaunchSynthesizeBlock(d, 5, p, nullptr) == cudaSuccess);
    REQUIRE(LaunchSynthesizeBlock(d + bytes, 6, p, nullptr) == cudaSuccess);
    std::vector<std::int8_t> h(2 * bytes / 1);
    REQUIRE(cudaMemcpy(h.data(), d, 2 * bytes, cudaMemcpyDeviceToHost) == cudaSuccess);
    cudaFree(d);
    Layout l;
    l.channelStart = p.channelStart;
    l.channelCount = p.channelCount;
    l.spectraPerBlock = p.spectraPerBlock;
    std::size_t mismatches = 0, toneMismatches = 0, same = 0;
    double sumsq = 0.0;
    for (std::uint32_t beam = 0; beam < 2; beam++) {
        for (std::uint32_t c = 0; c < p.channelCount; c += 7) {
            for (std::uint32_t t = 0; t < p.spectraPerBlock; t++) {
                for (std::uint32_t pol = 0; pol < 2; pol++) {
                    std::int8_t ref[2];
                    SynthesizeSampleHost(5, beam, c, t, pol, p, ref);
                    const std::size_t e = l.elementOffset(beam, c, t, pol) * 2;
                    const bool tone = beam == 1 && pol == 1 && c == 100;
                    for (int k = 0; k < 2; k++) {
                        const int diff = std::abs(int(h[e + k]) - int(ref[k]));
                        if (tone) toneMismatches += diff > 1;   // FMA contraction may move a rounding by one
                        else mismatches += diff != 0;
                        if (!tone) sumsq += double(h[e + k]) * h[e + k];
                        same += h[e + k] == h[bytes + e + k];
                    }
                }
            }
        }
    }
    REQUIRE(mismatches == 0);
    REQUIRE(toneMismatches == 0);
    const double samples = 2.0 * 147 * 64 * 2 * 2;   // channels 0, 7, ..., 1022 (147 of them)
    const double rms = std::sqrt(sumsq / samples);
    REQUIRE(rms > 7.7);
    REQUIRE(rms < 8.3);
    REQUIRE(same < samples * 0.1);   // block 6 is new noise (equal values only by chance, ~5 %)
    // The tone channel carries ~40 amplitude on beam 1 pol Y only: its power is far above the noise.
    double tonePow = 0.0;
    for (std::uint32_t t = 0; t < p.spectraPerBlock; t++) {
        const std::size_t e = l.elementOffset(1, 100, t, 1) * 2;
        tonePow += double(h[e]) * h[e] + double(h[e + 1]) * h[e + 1];
    }
    REQUIRE(tonePow / p.spectraPerBlock > 1500.0);
}
