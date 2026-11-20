// lft3-capture: one 100 GbE link (or a synthetic / pcap source) -> the voltage ring (docs/runtime.md).
//
// Threads:
//   DAQIRI worker (--worker-core)       polls the NIC receive queue, fills bursts (DAQIRI-owned)
//   main thread (--processing-core)     get_rx_burst -> parse headers (CPU) -> Assembler -> stage ScatterJobs
//                                       -> one scatter kernel per burst into the ring blocks -> return the
//                                       burst to DAQIRI once its kernel finished -> seal blocks (zero-fill)
//                                       -> publish them in time order once their kernels completed
//   fd server (ring)                    hands the ring memory to reader processes
//
// The writer never waits for readers (except synthetic/file sources with --backpressure, for lossless
// tests and benchmarks).

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstring>
#include <deque>
#include <random>
#include <string>
#include <thread>
#include <vector>

#include <cuda_runtime.h>
#include <daqiri/daqiri.h>

#include "capture/assembler.hh"
#include "capture/daqiri_config.hh"
#include "capture/pcap.hh"
#include "capture/scatter.hh"
#include "capture/synth.hh"
#include "common/layout.hh"
#include "common/packet.hh"
#include "common/ring.hh"
#include "common/util.hh"

namespace Lft3 {
namespace {

const char* kUsage = R"(lft3-capture --stream NAME [options]
  --source network|synthetic|file   (default network)
  --interface PCI                   ConnectX function, e.g. 0000:01:00.0 (network)
  --source-ip A.B.C.D --udp-port N  flow match (default any, 60000)
  --board-id N                      packets with another fid are filtered (default 0)
  --master-core N --worker-core N --processing-core N   (default 5, 6, 7)
  --channels START:COUNT            coarse channels kept, multiples of 1024 (default 0:6144)
  --spectra-per-block N             (default 1024 = 6.71 ms)
  --input-beam 0,0,1,1 --input-pol 0,1,0,1
  --slots N | --ring-seconds S      ring size (default 2 s)
  --nco-mhz F --obs-id S --config-hash S
  --max-open-blocks N --max-block-lag N   (default 4, 2)
  --packets-per-burst N --max-bursts N    (default 1024, 64)
  synthetic/file: --fast (no real-time pacing) --backpressure (wait for lossless readers)
                  --wait-readers N (start once N lossless readers are attached) --stop-after-blocks N
  synthetic: --tone-channel N --tone-input N --tone-frequency F (cycles/sample) --drop-fraction F
             --noise periodic|random   periodic (default): 1024 spectra replayed from host memory (parity
                                       tests); random: non-repeating noise + tone generated on the GPU
             random only: --tone-drift HZ_PER_S --tone-amplitude A (default 1) --noise-sigma S (default 8)
                          --seed N --tone-channel -1 (no tone)
  file: --file cap.pcap --loop
)";

struct Options {
    std::string stream, source = "network", interface, sourceIp, file, obsId, configHash;
    std::uint16_t udpPort = 60000;
    std::uint32_t boardId = 0;
    int masterCore = 5, workerCore = 6, processingCore = 7;
    Layout layout;
    std::uint64_t slots = 0;
    double ringSeconds = 2.0, ncoMHz = 0.0;
    std::uint64_t maxOpenBlocks = 4, maxBlockLag = 2, packetsPerBurst = 1024, maxBursts = 64;
    bool fast = false, backpressure = false, loop = false;
    std::uint64_t waitReaders = 0, stopAfterBlocks = 0;
    std::int64_t toneChannel = 4000;
    std::uint64_t toneInput = 1;
    double toneFrequency = 0.1015625, dropFraction = 0.0;
    std::string noise = "periodic";
    double toneDrift = 0.0, toneAmplitude = -1.0, noiseSigma = 8.0;
    std::uint64_t seed = 1;
};

#define CUDA_OK(expr, what)                                                                 \
    do {                                                                                    \
        const cudaError_t e_ = (expr);                                                      \
        if (e_ != cudaSuccess) {                                                            \
            LFT3_ERROR("%s failed: %s", what, cudaGetErrorString(e_));                      \
            return false;                                                                   \
        }                                                                                   \
    } while (0)

class Capture {
 public:
    explicit Capture(const Options& o) : o_(o) {}
    ~Capture() { destroy(); }
    bool create();
    bool run();

 private:
    struct OpenMeta {
        std::uint64_t blockIndex = 0;
        ChannelHeader header;
    };
    struct Staging {
        ScatterJob* jobs = nullptr;     // arrival order
        ScatterJob* sorted = nullptr;   // grouped by channel offset (read by the GPU)
        cudaEvent_t done = nullptr;
    };
    struct InFlight {
        std::uint64_t staging;
        daqiri::BurstParams* burst;
    };
    struct Sealed {
        std::int64_t slot;
        RingSlotMeta meta;
    };
    struct Batch {
        std::uint64_t staging = 0;
        std::uint32_t jobs = 0;
        std::uint64_t received = 0, filtered = 0;
    };
    struct ZeroJobs {
        ScatterJob* jobs = nullptr;
        cudaEvent_t done = nullptr;
    };

    bool createSynthetic();
    bool createFile();
    bool beginBatch(Batch& b);
    void addPacket(Batch& b, const ChannelHeader& h, const std::uint8_t* payload);
    bool endBatch(Batch& b, daqiri::BurstParams* burst);
    bool processBurst(daqiri::BurstParams* burst);
    bool produceSynthetic();
    bool produceFile();
    bool pace(std::uint64_t t);
    bool waitWritable();
    bool handleSealed(std::vector<SealedBlock>& sealed);
    bool reap(bool waitForOne);
    bool promote();
    void publishStats();
    void logStatus(double dt);
    void discardBacklog();
    void destroy();

    Options o_;
    Layout l_;
    RingWriter ring_;
    std::unique_ptr<Assembler> asm_;
    ScatterGeometry geom_ = {};
    cudaStream_t stream_ = nullptr;
    std::vector<Staging> staging_;
    std::vector<std::uint64_t> freeStaging_;
    std::deque<InFlight> inFlight_;
    std::deque<Sealed> sealedQueue_;
    std::vector<cudaEvent_t> slotEvents_;
    std::vector<OpenMeta> openMeta_;
    std::vector<ZeroJobs> zeroJobs_;
    std::uint64_t zeroNext_ = 0;
    bool daqiri_ = false;
    bool aligned_ = false;
    std::uint64_t restartsSeen_ = 0;
    double unixT0_ = 0.0;
    std::uint64_t received_ = 0, filtered_ = 0;
    std::uint64_t published_ = 0;
    std::uint64_t stopMissing_ = 0, stopPartial_ = 0;   // incomplete blocks cut off by the stop (not loss)
    std::chrono::steady_clock::time_point start_;

    // synthetic
    std::uint8_t* synth_ = nullptr;
    std::uint64_t synthSpectra_ = 1024, synthT_ = 0;
    bool synthRandom_ = false;
    SynthParams synthParams_;
    std::mt19937_64 dropRng_{12345};
    // file
    PcapPackets file_;
    std::uint8_t* filePayloads_ = nullptr;
    std::uint64_t fileIndex_ = 0, fileLoops_ = 0, fileFirstT_ = 0, fileSpan_ = 0;
    bool fileDone_ = false;
};

bool Capture::create() {
    l_ = o_.layout;
    const std::uint64_t slots = o_.slots ? o_.slots
        : static_cast<std::uint64_t>(std::ceil(o_.ringSeconds * kSpectraPerSecond / static_cast<double>(l_.spectraPerBlock)));
    geom_.channelCount = static_cast<std::uint32_t>(l_.channelCount);
    geom_.spectraPerBlock = static_cast<std::uint32_t>(l_.spectraPerBlock);
    for (int i = 0; i < 4; i++) {
        geom_.inputBeam[i] = l_.inputBeam[i];
        geom_.inputPol[i] = l_.inputPol[i];
    }
    CUDA_OK(cudaSetDevice(0), "cudaSetDevice");
    CUDA_OK(cudaStreamCreateWithFlags(&stream_, cudaStreamNonBlocking), "cudaStreamCreate");

    RingConfig rc;
    rc.stream = o_.stream;
    rc.layout = l_;
    rc.slots = slots;
    rc.boardId = o_.boardId;
    rc.ncoMHz = o_.ncoMHz;
    rc.obsId = o_.obsId;
    rc.configHash = o_.configHash;
    rc.backpressure = o_.backpressure && o_.source != "network";
    std::string error;
    if (!ring_.create(rc, error)) {
        LFT3_ERROR("ring: %s", error.c_str());
        return false;
    }
    slotEvents_.resize(slots);
    for (auto& e : slotEvents_) CUDA_OK(cudaEventCreateWithFlags(&e, cudaEventDisableTiming), "cudaEventCreate");
    openMeta_.resize(slots);

    staging_.resize(o_.maxBursts);
    for (std::uint64_t i = 0; i < o_.maxBursts; i++) {
        CUDA_OK(cudaMallocHost(&staging_[i].jobs, o_.packetsPerBurst * sizeof(ScatterJob)), "cudaMallocHost");
        CUDA_OK(cudaMallocHost(&staging_[i].sorted, o_.packetsPerBurst * sizeof(ScatterJob)), "cudaMallocHost");
        CUDA_OK(cudaEventCreateWithFlags(&staging_[i].done, cudaEventDisableTiming), "cudaEventCreate");
        freeStaging_.push_back(i);
    }
    zeroJobs_.resize(2 * o_.maxOpenBlocks + 4);
    for (auto& z : zeroJobs_) {
        CUDA_OK(cudaMallocHost(&z.jobs, l_.packetsPerBlock() * sizeof(ScatterJob)), "cudaMallocHost");
        CUDA_OK(cudaEventCreateWithFlags(&z.done, cudaEventDisableTiming), "cudaEventCreate");
    }

    AssemblerConfig ac;
    ac.spectraPerBlock = l_.spectraPerBlock;
    ac.packetsPerSpectrum = l_.packetsPerSpectrum();
    ac.maxOpenBlocks = o_.maxOpenBlocks;
    ac.maxBlockLag = o_.maxBlockLag;
    ac.zeroFill = true;
    asm_ = std::make_unique<Assembler>(ac, [this]() { return ring_.acquireSlot(); });

    if (o_.source == "synthetic") {
        if (!createSynthetic()) return false;
    } else if (o_.source == "file") {
        if (!createFile()) return false;
    } else {
        DaqiriRxParams p;
        p.interfaceAddress = o_.interface;
        p.masterCore = static_cast<std::uint64_t>(o_.masterCore);
        p.workerCore = static_cast<std::uint64_t>(o_.workerCore);
        p.packetsPerBurst = o_.packetsPerBurst;
        p.maxConcurrentBursts = o_.maxBursts;
        p.sourceAddress = o_.sourceIp;
        p.udpPort = o_.udpPort;
        p.payloadInDeviceMemory = false;   // DGX Spark: no GPUDirect RDMA; the GPU reads pinned host memory
        daqiri::NetworkConfig cfg = {};
        if (!BuildDaqiriRxConfig(p, cfg, error)) {
            LFT3_ERROR("%s", error.c_str());
            return false;
        }
        const auto status = daqiri::daqiri_init(cfg);
        if (status != daqiri::Status::SUCCESS) {
            LFT3_ERROR("DAQIRI initialisation failed (status %d). Root or CAP_NET_RAW/CAP_IPC_LOCK is required, and "
                       "'%s' must be a ConnectX interface.", static_cast<int>(status), o_.interface.c_str());
            if (status == daqiri::Status::INTERNAL_ERROR) daqiri::shutdown();
            return false;
        }
        daqiri_ = true;
    }
    LFT3_INFO("ring '%s': %lu slots x %.1f MB = %.2f GB (%.2f s); blocks [2, %lu, %lu, 2] CI8 of %.3f ms; source %s%s",
              o_.stream.c_str(), static_cast<unsigned long>(slots), l_.bytesPerBlock() / 1e6,
              ring_.control().ringBytes / 1e9, slots * l_.spectraPerBlock / kSpectraPerSecond,
              static_cast<unsigned long>(l_.channelCount), static_cast<unsigned long>(l_.spectraPerBlock),
              1e3 * l_.spectraPerBlock / kSpectraPerSecond, o_.source.c_str(),
              o_.source == "network" ? (" on " + o_.interface).c_str() : "");
    return true;
}

bool Capture::createSynthetic() {
    // Ring of R spectra x 6 packets, periodic in time; identical to the CyberEther receiver's generator so
    // results can be compared bit for bit.
    const std::uint64_t R = synthSpectra_;
    CUDA_OK(cudaMallocHost(&synth_, R * kPacketsPerSpectrum * kPayloadBytes), "cudaMallocHost");
    if (o_.noise == "random") {
        // Packets still flow (assembler, scatter, loss accounting); every sealed block is then overwritten on the
        // GPU with non-repeating noise + the drifting tone (synth.cu). The packet payloads are zeros.
        std::memset(synth_, 0, R * kPacketsPerSpectrum * kPayloadBytes);
        synthRandom_ = true;
        SynthParams& sp = synthParams_;
        sp.channelCount = static_cast<std::uint32_t>(l_.channelCount);
        sp.spectraPerBlock = static_cast<std::uint32_t>(l_.spectraPerBlock);
        sp.channelStart = static_cast<std::uint32_t>(l_.channelStart);
        for (int i = 0; i < 4; i++) {
            sp.inputBeam[i] = l_.inputBeam[i];
            sp.inputPol[i] = l_.inputPol[i];
        }
        sp.seed = o_.seed;
        sp.noiseSigma = static_cast<float>(o_.noiseSigma);
        sp.toneChannel = o_.toneChannel;
        sp.toneInput = static_cast<std::uint32_t>(o_.toneInput);
        sp.toneAmplitude = o_.toneAmplitude >= 0.0 ? o_.toneAmplitude : 1.0;
        sp.toneFrequency = o_.toneFrequency;
        sp.toneChirp = o_.toneDrift / (kSpectraPerSecond * kSpectraPerSecond);
        LFT3_INFO("synthetic source (random noise, sigma %.2f, seed %lu): tone in channel %ld, input %lu, amplitude %.3f, "
                  "%+.6f cycles/sample (%+.3f kHz) drifting %+.6g Hz/s, drop fraction %g",
                  sp.noiseSigma, static_cast<unsigned long>(sp.seed), static_cast<long>(sp.toneChannel),
                  static_cast<unsigned long>(sp.toneInput), sp.toneAmplitude, sp.toneFrequency,
                  sp.toneFrequency * kCoarseChannelHz / 1e3, o_.toneDrift, o_.dropFraction);
        return true;
    }
    const double amplitude = o_.toneAmplitude >= 0.0 ? o_.toneAmplitude : 24.0;
    const double f = std::round(o_.toneFrequency * static_cast<double>(R)) / static_cast<double>(R);
    std::normal_distribution<double> noise(0.0, 8.0);
    std::mt19937_64 rng(42);
    const auto clip = [](double v) { return static_cast<std::int8_t>(std::lround(std::fmin(127.0, std::fmax(-127.0, v)))); };
    auto* p = reinterpret_cast<std::int8_t*>(synth_);
    for (std::uint64_t n = 0; n < R; n++) {
        for (std::uint64_t k = 0; k < kTotalChannels; k++) {
            for (std::uint64_t input = 0; input < kInputs; input++) {
                double re = noise(rng), im = noise(rng);
                if (static_cast<std::int64_t>(k) == o_.toneChannel && input == o_.toneInput) {
                    re += amplitude * std::cos(2.0 * M_PI * f * static_cast<double>(n));
                    im += amplitude * std::sin(2.0 * M_PI * f * static_cast<double>(n));
                }
                *p++ = clip(re);
                *p++ = clip(im);
            }
        }
    }
    LFT3_INFO("synthetic source: tone in channel %ld, input %lu, %+.6f cycles/sample (%+.3f kHz), drop fraction %g",
              static_cast<long>(o_.toneChannel), static_cast<unsigned long>(o_.toneInput), f,
              f * kCoarseChannelHz / 1e3, o_.dropFraction);
    return true;
}

bool Capture::createFile() {
    std::string error;
    if (!ReadPcapChannelPackets(o_.file, o_.udpPort, file_, error)) {
        LFT3_ERROR("%s", error.c_str());
        return false;
    }
    const std::uint64_t n = file_.headers.size();
    CUDA_OK(cudaMallocHost(&filePayloads_, n * kPayloadBytes), "cudaMallocHost");
    std::memcpy(filePayloads_, file_.payloads.data(), n * kPayloadBytes);
    file_.payloads.clear();
    file_.payloads.shrink_to_fit();
    std::uint64_t tmin = file_.headers.front().t, tmax = tmin;
    for (const auto& h : file_.headers) {
        tmin = std::min<std::uint64_t>(tmin, h.t);
        tmax = std::max<std::uint64_t>(tmax, h.t);
    }
    const std::uint64_t L = l_.spectraPerBlock;
    const std::uint64_t a = (tmin + L - 1) / L * L, b = (tmax + 1) / L * L;
    if (b <= a) {
        LFT3_ERROR("'%s' covers less than one whole block", o_.file.c_str());
        return false;
    }
    std::uint64_t kept = 0;
    for (std::uint64_t i = 0; i < n; i++) {
        if (file_.headers[i].t >= a && file_.headers[i].t < b) {
            file_.headers[kept] = file_.headers[i];
            std::memmove(filePayloads_ + kept * kPayloadBytes, filePayloads_ + i * kPayloadBytes, kPayloadBytes);
            kept++;
        }
    }
    file_.headers.resize(kept);
    fileFirstT_ = a;
    fileSpan_ = b - a;
    LFT3_INFO("file source '%s': %lu packets, replaying %lu whole blocks%s", o_.file.c_str(),
              static_cast<unsigned long>(kept), static_cast<unsigned long>(fileSpan_ / L), o_.loop ? ", looping" : "");
    return true;
}

void Capture::discardBacklog() {
    std::uint64_t discarded = 0;
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(200);
    while (std::chrono::steady_clock::now() < deadline) {
        daqiri::BurstParams* burst = nullptr;
        if (daqiri::get_rx_burst(&burst) != daqiri::Status::SUCCESS || !burst) break;
        discarded += static_cast<std::uint64_t>(daqiri::get_num_packets(burst));
        daqiri::free_all_packets_and_burst_rx(burst);
    }
    LFT3_INFO("started; discarded %lu packets queued during start-up", static_cast<unsigned long>(discarded));
}

bool Capture::beginBatch(Batch& b) {
    if (freeStaging_.empty() && !reap(true)) return false;
    b = {};
    b.staging = freeStaging_.back();
    freeStaging_.pop_back();
    return true;
}

void Capture::addPacket(Batch& b, const ChannelHeader& h, const std::uint8_t* payload) {
    b.received++;
    if (h.fid != o_.boardId || !l_.keepsPacket(h.bindex) || b.jobs >= o_.packetsPerBurst) {
        b.filtered++;
        return;
    }
    if (!aligned_) {   // start at a block boundary so the first block is complete
        if (h.t % l_.spectraPerBlock != 0 || h.bindex != l_.firstPacket()) {
            b.filtered++;
            return;
        }
        aligned_ = true;
    }
    if (h.t > ring_.control().stats.latestT.load(std::memory_order_relaxed)) {
        ring_.control().stats.latestT.store(h.t, std::memory_order_relaxed);
    }
    Assembler::Placement pl;
    if (asm_->accept(h.t, h.bindex - l_.firstPacket(), pl) != Assembler::Accept::Ok) return;
    if (pl.newBlock) {
        openMeta_[pl.buffer].blockIndex = l_.blockIndex(h.t);
        openMeta_[pl.buffer].header = h;
        if (unixT0_ == 0.0 || asm_->stats().restarts != restartsSeen_) {
            // Wall-clock time of t = 0 from the first packet (and after each restart); ~ms accuracy.
            restartsSeen_ = asm_->stats().restarts;
            unixT0_ = UnixNow() - static_cast<double>(h.t) / kSpectraPerSecond;
        }
    }
    ScatterJob& j = staging_[b.staging].jobs[b.jobs++];
    j.source = payload;
    j.destination = ring_.blockPtr(pl.buffer);
    j.spectrum = static_cast<std::uint32_t>(l_.slotSpectrum(pl.slot));
    j.channelOffset = static_cast<std::uint32_t>(l_.slotChannelOffset(pl.slot));
}

bool Capture::endBatch(Batch& b, daqiri::BurstParams* burst) {
    Staging& s = staging_[b.staging];
    SortJobsForScatter(s.jobs, b.jobs, s.sorted);
    CUDA_OK(LaunchScatter(s.sorted, b.jobs, geom_, stream_), "scatter kernel launch");
    CUDA_OK(cudaEventRecord(s.done, stream_), "cudaEventRecord");
    inFlight_.push_back({b.staging, burst});
    received_ += b.received;
    filtered_ += b.filtered;
    // Seal after launching this batch's kernel: blocks sealed now may have received packets from it.
    std::vector<SealedBlock> sealed;
    asm_->collect(sealed);
    if (!handleSealed(sealed)) return false;
    publishStats();
    return true;
}

bool Capture::processBurst(daqiri::BurstParams* burst) {
    Batch b;
    if (!beginBatch(b)) return false;
    const std::int64_t n = daqiri::get_num_packets(burst);
    for (std::int64_t i = 0; i < n; i++) {
        const auto* frame = static_cast<const std::uint8_t*>(daqiri::get_segment_packet_ptr(burst, kRxHeaderSegment, static_cast<int>(i)));
        const auto* payload = static_cast<const std::uint8_t*>(daqiri::get_segment_packet_ptr(burst, kRxDataSegment, static_cast<int>(i)));
        addPacket(b, ParseChannelHeader(frame + kNetworkHeaderBytes), payload);
    }
    return endBatch(b, burst);
}

// Real-time pacing for synthetic/file sources: false while it is too early to send spectrum t.
bool Capture::pace(std::uint64_t t) {
    if (o_.fast) return true;
    const double due = static_cast<double>(t) / kSpectraPerSecond;
    const double now = std::chrono::duration<double>(std::chrono::steady_clock::now() - start_).count();
    if (now >= due) return true;
    std::this_thread::sleep_for(std::chrono::microseconds(static_cast<std::int64_t>((due - now) * 1e6) + 1));
    return false;
}

// Synthetic/file: a batch can open at most two blocks; keep two slots ahead free of unpublished blocks and
// (with --backpressure) of data lossless readers still need. Keeps publishing while waiting.
bool Capture::waitWritable() {
    while (!StopRequested().load()) {
        const std::uint64_t pending = asm_->openBlocks() + sealedQueue_.size();
        if (pending + 2 < ring_.slots()) return true;
        if (!reap(false) || !promote()) return false;
        std::this_thread::sleep_for(std::chrono::microseconds(50));
    }
    return false;
}

bool Capture::produceSynthetic() {
    if (!pace(synthT_)) return true;
    if (!waitWritable()) return true;
    Batch b;
    if (!beginBatch(b)) return false;
    std::uniform_real_distribution<double> uniform(0.0, 1.0);
    const std::uint64_t spectra = std::max<std::uint64_t>(1, o_.packetsPerBurst / kPacketsPerSpectrum);
    for (std::uint64_t n = 0; n < spectra; n++, synthT_++) {
        for (std::uint32_t bi = 0; bi < kPacketsPerSpectrum; bi++) {
            if (o_.dropFraction > 0.0 && uniform(dropRng_) < o_.dropFraction) continue;
            ChannelHeader h;
            h.t = synthT_;
            h.bindex = bi;
            h.fid = o_.boardId;
            h.nco = {-2031, -2031, -2031, -2031};
            const std::uint64_t idx = (synthT_ % synthSpectra_) * kPacketsPerSpectrum + bi;
            addPacket(b, h, synth_ + idx * kPayloadBytes);
        }
    }
    return endBatch(b, nullptr);
}

bool Capture::produceFile() {
    if (fileDone_) {
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
        return true;
    }
    const std::uint64_t n = file_.headers.size();
    const std::uint64_t offset = fileLoops_ * fileSpan_;
    if (!pace(file_.headers[fileIndex_].t + offset - fileFirstT_)) return true;
    if (!waitWritable()) return true;
    Batch b;
    if (!beginBatch(b)) return false;
    for (std::uint64_t k = 0; k < o_.packetsPerBurst && fileIndex_ < n; k++, fileIndex_++) {
        ChannelHeader h = file_.headers[fileIndex_];
        h.t += offset;
        addPacket(b, h, filePayloads_ + fileIndex_ * kPayloadBytes);
    }
    if (!endBatch(b, nullptr)) return false;
    if (fileIndex_ >= n) {
        if (o_.loop) {
            fileIndex_ = 0;
            fileLoops_++;
        } else {
            std::vector<SealedBlock> sealed;
            asm_->flush(sealed);
            if (!handleSealed(sealed)) return false;
            fileDone_ = true;
            LFT3_INFO("file replay finished: %lu packets", static_cast<unsigned long>(n));
        }
    }
    return true;
}

bool Capture::handleSealed(std::vector<SealedBlock>& sealed) {
    for (auto& s : sealed) {
        if (!s.emit) {
            ring_.release(s.buffer);
            continue;
        }
        if (synthRandom_) {
            CUDA_OK(LaunchSynthesizeBlock(ring_.blockPtr(s.buffer), s.index, synthParams_, stream_), "synthesis kernel launch");
        }
        if (!s.missingSlots.empty()) {
            // Zero-fill the missing packets' regions: the scatter kernel with null sources.
            ZeroJobs& z = zeroJobs_[zeroNext_++ % zeroJobs_.size()];
            CUDA_OK(cudaEventSynchronize(z.done), "cudaEventSynchronize");   // previous use finished (ms ago)
            for (std::uint64_t k = 0; k < s.missingSlots.size(); k++) {
                const std::uint64_t slot = s.missingSlots[k];
                z.jobs[k] = {nullptr, ring_.blockPtr(s.buffer), static_cast<std::uint32_t>(l_.slotSpectrum(slot)),
                             static_cast<std::uint32_t>(l_.slotChannelOffset(slot))};
            }
            CUDA_OK(LaunchScatter(z.jobs, static_cast<std::uint32_t>(s.missingSlots.size()), geom_, stream_),
                    "zero-fill kernel launch");
            CUDA_OK(cudaEventRecord(z.done, stream_), "cudaEventRecord");
        }
        CUDA_OK(cudaEventRecord(slotEvents_[s.buffer], stream_), "cudaEventRecord");
        Sealed q;
        q.slot = s.buffer;
        const OpenMeta& om = openMeta_[s.buffer];
        q.meta.blockIndex = s.index;
        q.meta.t0 = l_.blockFirstSpectrum(s.index);
        q.meta.missingPackets = s.missingSlots.size();
        q.meta.restarts = asm_->stats().restarts;
        for (int i = 0; i < 4; i++) {
            q.meta.nco[i] = om.header.nco[i];
            q.meta.pfbLsb[i] = om.header.pfbLsb[i];
        }
        q.meta.fid = om.header.fid;
        q.meta.flags = s.missingSlots.empty() ? 0 : 1;
        q.meta.unixTimeAtT0 = unixT0_;
        sealedQueue_.push_back(q);
    }
    return true;
}

bool Capture::reap(bool waitForOne) {
    while (!inFlight_.empty()) {
        auto& f = inFlight_.front();
        cudaError_t st = cudaEventQuery(staging_[f.staging].done);
        if (st == cudaErrorNotReady) {
            if (!waitForOne) break;
            st = cudaEventSynchronize(staging_[f.staging].done);
            waitForOne = false;
        }
        CUDA_OK(st, "scatter kernel");
        if (f.burst) daqiri::free_all_packets_and_burst_rx(f.burst);   // payloads consumed: back to DAQIRI
        freeStaging_.push_back(f.staging);
        inFlight_.pop_front();
    }
    return true;
}

bool Capture::promote() {
    while (!sealedQueue_.empty()) {
        const Sealed& q = sealedQueue_.front();
        const cudaError_t st = cudaEventQuery(slotEvents_[q.slot]);
        if (st == cudaErrorNotReady) break;   // one stream: events complete in order
        CUDA_OK(st, "block kernels");
        ring_.publish(q.slot, q.meta);
        published_++;
        sealedQueue_.pop_front();
    }
    return true;
}

void Capture::publishStats() {
    const auto& a = asm_->stats();
    auto& s = ring_.control().stats;
    s.packetsReceived.store(received_, std::memory_order_relaxed);
    s.packetsFiltered.store(filtered_, std::memory_order_relaxed);
    s.packetsAccepted.store(a.packetsAccepted, std::memory_order_relaxed);
    s.packetsDuplicate.store(a.packetsDuplicate, std::memory_order_relaxed);
    s.packetsLate.store(a.packetsLate, std::memory_order_relaxed);
    s.packetsNoBuffer.store(a.packetsNoBuffer, std::memory_order_relaxed);
    s.packetsMissing.store(a.packetsMissing - stopMissing_, std::memory_order_relaxed);
    s.blocksComplete.store(a.blocksComplete, std::memory_order_relaxed);
    s.blocksPartial.store(a.blocksPartial - stopPartial_, std::memory_order_relaxed);
    s.blocksSkipped.store(a.blocksSkipped, std::memory_order_relaxed);
    s.restarts.store(a.restarts, std::memory_order_relaxed);
}

void Capture::logStatus(double dt) {
    static std::uint64_t lastPublished = 0;
    const auto& s = ring_.control().stats;
    const double speed = static_cast<double>(published_ - lastPublished) * l_.spectraPerBlock / kSpectraPerSecond / dt;
    lastPublished = published_;
    std::string readers;
    for (const auto& r : ring_.control().readers) {
        if (!r.pid.load()) continue;
        char buf[128];
        std::snprintf(buf, sizeof(buf), " %s(lag %lu, overruns %lu)", r.name,
                      static_cast<unsigned long>(r.lagBlocks.load()), static_cast<unsigned long>(r.overruns.load()));
        readers += buf;
    }
    LFT3_INFO("%.2f Gb/s | published %lu (%.2fx real time) complete %lu partial %lu skipped %lu | packets accepted %lu "
              "missing %lu late %lu dup %lu no-slot %lu filtered %lu | readers:%s",
              s.rateMilliGbps.load() / 1e3, static_cast<unsigned long>(published_), speed,
              static_cast<unsigned long>(s.blocksComplete.load()), static_cast<unsigned long>(s.blocksPartial.load()),
              static_cast<unsigned long>(s.blocksSkipped.load()), static_cast<unsigned long>(s.packetsAccepted.load()),
              static_cast<unsigned long>(s.packetsMissing.load()), static_cast<unsigned long>(s.packetsLate.load()),
              static_cast<unsigned long>(s.packetsDuplicate.load()), static_cast<unsigned long>(s.packetsNoBuffer.load()),
              static_cast<unsigned long>(filtered_), readers.empty() ? " none" : readers.c_str());
}

bool Capture::run() {
    PinCurrentThread(o_.processingCore);
    CUDA_OK(cudaSetDevice(0), "cudaSetDevice");
    if (o_.waitReaders > 0) {
        LFT3_INFO("waiting for %lu lossless reader(s)", static_cast<unsigned long>(o_.waitReaders));
        while (!StopRequested().load()) {
            std::uint64_t n = 0;
            for (const auto& r : ring_.control().readers) n += (r.pid.load() && r.lossless.load()) ? 1 : 0;
            if (n >= o_.waitReaders) break;
            ring_.heartbeat();
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
    }
    ring_.setRunning();
    if (daqiri_) discardBacklog();
    start_ = std::chrono::steady_clock::now();
    auto lastRate = start_, lastLog = start_;
    std::uint64_t lastReceived = 0;
    bool ok = true;

    while (!StopRequested().load()) {
        if (o_.source == "synthetic") {
            ok = produceSynthetic();
        } else if (o_.source == "file") {
            ok = produceFile();
        } else {
            daqiri::BurstParams* burst = nullptr;
            if (daqiri::get_rx_burst(&burst) == daqiri::Status::SUCCESS && burst) {
                ok = processBurst(burst);
            } else {
                std::this_thread::sleep_for(std::chrono::microseconds(20));
            }
        }
        if (!ok || !reap(false) || !promote()) {
            ok = false;
            break;
        }
        const auto now = std::chrono::steady_clock::now();
        const double dt = std::chrono::duration<double>(now - lastRate).count();
        if (dt >= 0.5) {
            const double gbps = static_cast<double>(received_ - lastReceived) * (kHeaderSegmentBytes + kPayloadBytes) * 8.0 / dt / 1e9;
            ring_.control().stats.rateMilliGbps.store(static_cast<std::uint64_t>(gbps * 1e3), std::memory_order_relaxed);
            lastReceived = received_;
            lastRate = now;
            ring_.heartbeat();
        }
        const double logDt = std::chrono::duration<double>(now - lastLog).count();
        if (logDt >= 5.0) {
            logStatus(logDt);
            lastLog = now;
        }
        if (o_.stopAfterBlocks && published_ >= o_.stopAfterBlocks) {
            LFT3_INFO("published %lu blocks: stopping", static_cast<unsigned long>(published_));
            break;
        }
    }
    // Seal and publish what is left so readers see every complete block. Blocks still incomplete at this point
    // were cut short by the stop, not lost on the way: drop them and do not count their empty slots as missing.
    if (ok) {
        std::vector<SealedBlock> sealed;
        asm_->flush(sealed);
        for (auto& s : sealed) {
            if (s.emit && !s.missingSlots.empty()) {
                stopMissing_ += s.missingSlots.size();
                stopPartial_++;
                s.emit = false;
            }
        }
        if (stopPartial_) {
            LFT3_INFO("stop: dropped %lu incomplete block(s) still being received (%lu packets not counted as missing)",
                      static_cast<unsigned long>(stopPartial_), static_cast<unsigned long>(stopMissing_));
        }
        ok = handleSealed(sealed);
        cudaStreamSynchronize(stream_);
        ok = ok && reap(false) && promote();
        publishStats();
        logStatus(std::max(1e-3, std::chrono::duration<double>(std::chrono::steady_clock::now() - lastLog).count()));
    }
    return ok;
}

void Capture::destroy() {
    if (stream_) cudaStreamSynchronize(stream_);
    while (!inFlight_.empty()) {
        if (inFlight_.front().burst) daqiri::free_all_packets_and_burst_rx(inFlight_.front().burst);
        inFlight_.pop_front();
    }
    if (daqiri_) {
        daqiri::shutdown();
        daqiri_ = false;
    }
    ring_.close();
    for (auto& s : staging_) {
        if (s.jobs) cudaFreeHost(s.jobs);
        if (s.sorted) cudaFreeHost(s.sorted);
        if (s.done) cudaEventDestroy(s.done);
    }
    staging_.clear();
    for (auto& z : zeroJobs_) {
        if (z.jobs) cudaFreeHost(z.jobs);
        if (z.done) cudaEventDestroy(z.done);
    }
    zeroJobs_.clear();
    for (auto& e : slotEvents_) cudaEventDestroy(e);
    slotEvents_.clear();
    if (synth_) cudaFreeHost(synth_);
    if (filePayloads_) cudaFreeHost(filePayloads_);
    synth_ = filePayloads_ = nullptr;
    if (stream_) cudaStreamDestroy(stream_);
    stream_ = nullptr;
}

bool ParseOptions(const Args& a, Options& o) {
    o.stream = a.str("stream");
    o.source = a.str("source", "network");
    o.interface = a.str("interface");
    o.sourceIp = a.str("source-ip");
    o.file = a.str("file");
    o.obsId = a.str("obs-id");
    o.configHash = a.str("config-hash");
    o.udpPort = static_cast<std::uint16_t>(a.i64("udp-port", 60000));
    o.boardId = static_cast<std::uint32_t>(a.i64("board-id", 0));
    o.masterCore = static_cast<int>(a.i64("master-core", 5));
    o.workerCore = static_cast<int>(a.i64("worker-core", 6));
    o.processingCore = static_cast<int>(a.i64("processing-core", 7));
    const std::string ch = a.str("channels", "0:6144");
    const auto colon = ch.find(':');
    if (colon == std::string::npos) return false;
    o.layout.channelStart = std::stoull(ch.substr(0, colon));
    o.layout.channelCount = std::stoull(ch.substr(colon + 1));
    o.layout.spectraPerBlock = static_cast<std::uint64_t>(a.i64("spectra-per-block", 1024));
    int ib[4] = {0, 0, 1, 1}, ip[4] = {0, 1, 0, 1};
    if (a.has("input-beam") && !ParseIntList(a.str("input-beam"), ib)) return false;
    if (a.has("input-pol") && !ParseIntList(a.str("input-pol"), ip)) return false;
    for (int i = 0; i < 4; i++) {
        if (ib[i] < 0 || ib[i] > 1 || ip[i] < 0 || ip[i] > 1) return false;
        o.layout.inputBeam[i] = static_cast<std::uint8_t>(ib[i]);
        o.layout.inputPol[i] = static_cast<std::uint8_t>(ip[i]);
    }
    o.slots = static_cast<std::uint64_t>(a.i64("slots", 0));
    o.ringSeconds = a.f64("ring-seconds", 2.0);
    o.ncoMHz = a.f64("nco-mhz", 0.0);
    o.maxOpenBlocks = static_cast<std::uint64_t>(a.i64("max-open-blocks", 4));
    o.maxBlockLag = static_cast<std::uint64_t>(a.i64("max-block-lag", 2));
    o.packetsPerBurst = static_cast<std::uint64_t>(a.i64("packets-per-burst", 1024));
    o.maxBursts = static_cast<std::uint64_t>(a.i64("max-bursts", 64));
    o.fast = a.flag("fast");
    o.backpressure = a.flag("backpressure");
    o.loop = a.flag("loop");
    o.waitReaders = static_cast<std::uint64_t>(a.i64("wait-readers", 0));
    o.stopAfterBlocks = static_cast<std::uint64_t>(a.i64("stop-after-blocks", 0));
    o.toneChannel = a.i64("tone-channel", 4000);
    o.toneInput = static_cast<std::uint64_t>(a.i64("tone-input", 1));
    o.toneFrequency = a.f64("tone-frequency", 0.1015625);
    o.dropFraction = a.f64("drop-fraction", 0.0);
    o.noise = a.str("noise", "periodic");
    o.toneDrift = a.f64("tone-drift", 0.0);
    o.toneAmplitude = a.f64("tone-amplitude", -1.0);
    o.noiseSigma = a.f64("noise-sigma", 8.0);
    o.seed = static_cast<std::uint64_t>(a.i64("seed", 1));
    if (o.noise != "periodic" && o.noise != "random") return false;
    if (o.noise == "periodic" && (o.toneDrift != 0.0 || o.toneChannel < 0)) {
        LFT3_ERROR("--tone-drift and --tone-channel -1 need --noise random (the periodic generator repeats every block)");
        return false;
    }
    const Layout& l = o.layout;
    if (o.stream.empty() || (o.source != "network" && o.source != "synthetic" && o.source != "file")) return false;
    if (o.source == "network" && o.interface.empty()) return false;
    if (o.source == "file" && o.file.empty()) return false;
    if (l.channelStart % kChannelsPerPacket || l.channelCount % kChannelsPerPacket || l.channelCount == 0 ||
        l.channelStart + l.channelCount > kTotalChannels || l.spectraPerBlock == 0 || l.spectraPerBlock > 65536) {
        return false;
    }
    return true;
}

}  // namespace
}  // namespace Lft3

int main(int argc, char** argv) {
    using namespace Lft3;
    LogTag() = "capture";
    Args args(argc, argv, {"fast", "backpressure", "loop", "help"});
    Options o;
    if (args.has("help") || !ParseOptions(args, o)) {
        std::fputs(kUsage, stderr);
        return 2;
    }
    if (!args.unused().empty()) {
        LFT3_ERROR("unknown option --%s", args.unused().front().c_str());
        return 2;
    }
    LogTag() = "capture:" + o.stream;
    InstallStopHandlers();
    Capture c(o);
    if (!c.create()) return 1;
    const bool ok = c.run();
    LFT3_INFO("stopped%s", ok ? "" : " after an error");
    return ok ? 0 : 1;
}
