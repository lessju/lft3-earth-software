#include <catch2/catch_test_macros.hpp>

#include <algorithm>

#include <atomic>
#include <chrono>
#include <cstring>
#include <thread>
#include <vector>

#include <cuda_runtime.h>
#include <unistd.h>

#include "common/ring.hh"

using namespace Lft3;

namespace {

bool HaveGpu() {
    int n = 0;
    return cudaGetDeviceCount(&n) == cudaSuccess && n > 0;
}

RingConfig SmallRing(const std::string& tag, bool backpressure = false) {
    RingConfig c;
    c.stream = "test-" + tag + "-" + std::to_string(getpid());
    c.layout.channelStart = 1024;
    c.layout.channelCount = 1024;
    c.layout.spectraPerBlock = 64;
    c.slots = 16;
    c.boardId = 3;
    c.ncoMHz = -2031.25;
    c.obsId = "obs-test";
    c.backpressure = backpressure;
    return c;
}

void Publish(RingWriter& w, std::uint64_t blockIndex, std::uint8_t fill) {
    const std::int64_t slot = w.acquireSlot();
    REQUIRE(slot >= 0);
    REQUIRE(cudaMemset(w.blockPtr(slot), fill, w.control().blockBytes) == cudaSuccess);
    REQUIRE(cudaDeviceSynchronize() == cudaSuccess);
    RingSlotMeta m;
    m.blockIndex = blockIndex;
    m.t0 = blockIndex * 64;
    m.missingPackets = fill;
    m.nco[0] = -2031;
    m.unixTimeAtT0 = 1.7e9;
    w.publish(slot, m);
}

}  // namespace

TEST_CASE("Ring: a reader in another mapping sees published blocks and their metadata", "[lft3][ring][cuda]") {
    if (!HaveGpu()) SKIP("No CUDA device.");
    RingWriter w;
    std::string error;
    const RingConfig cfg = SmallRing("basic");
    REQUIRE(w.create(cfg, error));
    w.setRunning();
    RingReader r;
    REQUIRE(r.attach(cfg.stream, "test-reader", false, 5.0, error));
    REQUIRE(r.layout().channelCount == 1024);
    REQUIRE(r.layout().spectraPerBlock == 64);
    REQUIRE(r.control().boardId == 3);
    REQUIRE(r.control().ncoMHz == -2031.25);
    REQUIRE(std::string(r.control().obsId) == "obs-test");
    REQUIRE(r.head() == 0);
    REQUIRE(r.wait(0, 10) == RingReader::Wait::Timeout);

    Publish(w, 100, 7);
    REQUIRE(r.wait(0, 1000) == RingReader::Wait::Ready);
    RingSlotMeta m;
    const std::uint8_t* data = nullptr;
    REQUIRE(r.begin(0, m, data));
    REQUIRE(m.blockIndex == 100);
    REQUIRE(m.t0 == 6400);
    REQUIRE(m.missingPackets == 7);
    REQUIRE(m.nco[0] == -2031);
    std::vector<std::uint8_t> host(r.control().blockBytes);
    REQUIRE(cudaMemcpy(host.data(), data, host.size(), cudaMemcpyDeviceToHost) == cudaSuccess);
    REQUIRE(std::all_of(host.begin(), host.end(), [](std::uint8_t v) { return v == 7; }));
    REQUIRE(r.stillValid(0));
    r.consumed(1);
    REQUIRE(r.entry().cursor.load() == 1);
    REQUIRE(r.entry().lagBlocks.load() == 0);
}

TEST_CASE("Ring: overwritten blocks are detected (seqlock)", "[lft3][ring][cuda]") {
    if (!HaveGpu()) SKIP("No CUDA device.");
    RingWriter w;
    std::string error;
    const RingConfig cfg = SmallRing("overrun");
    REQUIRE(w.create(cfg, error));
    w.setRunning();
    RingReader r;
    REQUIRE(r.attach(cfg.stream, "slow", false, 5.0, error));
    Publish(w, 0, 1);
    RingSlotMeta m;
    const std::uint8_t* data = nullptr;
    REQUIRE(r.begin(0, m, data));
    // The reader is "still reading" block 0 while the writer laps the ring.
    for (std::uint64_t b = 1; b <= 16; b++) Publish(w, b, static_cast<std::uint8_t>(b));
    REQUIRE_FALSE(r.stillValid(0));
    REQUIRE_FALSE(r.begin(0, m, data));
    REQUIRE(r.head() == 17);
    REQUIRE(r.oldestReadable(8) == 17 - 8);
    REQUIRE(r.begin(16, m, data));
    REQUIRE(m.blockIndex == 16);
    // Acquired but unpublished slots are invalid for readers immediately.
    const std::int64_t slot = w.acquireSlot();
    REQUIRE(slot >= 0);
    REQUIRE_FALSE(r.begin(1, m, data));   // slot 1 held seq 1 ... now being rewritten (seq 17 goes to slot 1)
    w.release(slot);
}

TEST_CASE("Ring: backpressure waits for lossless readers only", "[lft3][ring][cuda]") {
    if (!HaveGpu()) SKIP("No CUDA device.");
    RingWriter w;
    std::string error;
    const RingConfig cfg = SmallRing("bp", true);
    REQUIRE(w.create(cfg, error));
    w.setRunning();
    RingReader lossy, lossless;
    REQUIRE(lossy.attach(cfg.stream, "lossy", false, 5.0, error));
    REQUIRE(lossless.attach(cfg.stream, "lossless", true, 5.0, error));
    for (std::uint64_t b = 0; b < 16; b++) Publish(w, b, 1);
    lossless.consumed(0);
    // Slot 0 holds seq 0. The writer must stay kReadMargin blocks behind the lossless reader's cursor
    // (the reader treats older blocks as unsafe), so acquiring it waits until the cursor passes 0 + margin.
    std::atomic<std::int64_t> got{-2};
    std::thread t([&]() { got.store(w.acquireSlot()); });
    std::this_thread::sleep_for(std::chrono::milliseconds(100));
    REQUIRE(got.load() == -2);
    lossless.consumed(kReadMargin);
    std::this_thread::sleep_for(std::chrono::milliseconds(50));
    REQUIRE(got.load() == -2);
    lossless.consumed(kReadMargin + 1);
    t.join();
    REQUIRE(got.load() == 0);
    REQUIRE(w.control().stats.backpressureNs.load() > 50'000'000ULL);
}

TEST_CASE("Ring: readers see the writer stop", "[lft3][ring][cuda]") {
    if (!HaveGpu()) SKIP("No CUDA device.");
    auto w = std::make_unique<RingWriter>();
    std::string error;
    const RingConfig cfg = SmallRing("stop");
    REQUIRE(w->create(cfg, error));
    w->setRunning();
    RingReader r;
    REQUIRE(r.attach(cfg.stream, "r", false, 5.0, error));
    Publish(*w, 0, 9);
    w->close();
    REQUIRE(r.writerStopped());
    REQUIRE(r.wait(0, 10) == RingReader::Wait::Ready);   // published data stays readable
    REQUIRE(r.wait(1, 10) == RingReader::Wait::Stopped);
    RingSlotMeta m;
    const std::uint8_t* data = nullptr;
    REQUIRE(r.begin(0, m, data));
    std::uint8_t v = 0;
    REQUIRE(cudaMemcpy(&v, data, 1, cudaMemcpyDeviceToHost) == cudaSuccess);   // memory outlives the writer
    REQUIRE(v == 9);
    RingReader late;
    REQUIRE_FALSE(late.attach(cfg.stream, "late", false, 0.2, error));
}

TEST_CASE("Ring: a spectra ring carries its kind, axes and a custom slot size", "[lft3][ring][cuda]") {
    if (!HaveGpu()) SKIP("No CUDA device.");
    RingConfig cfg = SmallRing("spectra");
    cfg.stream += "-spectra";
    cfg.kind = RingKind::Spectra;
    cfg.layout.spectraPerBlock = 149 * 1024;
    cfg.blockBytes = 2 * 1024 * 128 * sizeof(float);
    cfg.fineChannelsPerCoarse = 128;
    cfg.fch1MHz = 1500.0;
    cfg.foffMHz = 0.00119209;
    cfg.tsampS = 0.99992;
    RingWriter w;
    std::string error;
    REQUIRE(w.create(cfg, error));
    w.setRunning();
    RingReader r;
    REQUIRE(r.attach(cfg.stream, "search", false, 5.0, error));
    const RingControl& c = r.control();
    REQUIRE(c.version == 2);
    REQUIRE(c.kind == static_cast<std::uint32_t>(RingKind::Spectra));
    REQUIRE(c.blockBytes == cfg.blockBytes);
    REQUIRE(c.fineChannelsPerCoarse == 128);
    REQUIRE(c.fch1MHz == 1500.0);
    REQUIRE(c.foffMHz == 0.00119209);
    REQUIRE(c.tsampS == 0.99992);
    REQUIRE(c.spectraPerBlock == 149 * 1024);

    const std::int64_t slot = w.acquireSlot();
    REQUIRE(slot >= 0);
    std::vector<float> v(2 * 1024 * 128);
    for (std::size_t i = 0; i < v.size(); i++) v[i] = static_cast<float>(i);
    REQUIRE(cudaMemcpy(w.blockPtr(slot), v.data(), cfg.blockBytes, cudaMemcpyHostToDevice) == cudaSuccess);
    RingSlotMeta m;
    m.blockIndex = 42;
    m.t0 = 42ULL * 149 * 1024;
    m.flags = kSlotFlagNewRun;
    w.publish(slot, m);
    REQUIRE(r.wait(0, 1000) == RingReader::Wait::Ready);
    RingSlotMeta got;
    const std::uint8_t* data = nullptr;
    REQUIRE(r.begin(0, got, data));
    REQUIRE(got.blockIndex == 42);
    REQUIRE(got.flags == kSlotFlagNewRun);
    std::vector<float> back(v.size());
    REQUIRE(cudaMemcpy(back.data(), data, cfg.blockBytes, cudaMemcpyDeviceToHost) == cudaSuccess);
    REQUIRE(back == v);
}
