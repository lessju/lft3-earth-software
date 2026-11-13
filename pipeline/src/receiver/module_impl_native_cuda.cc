// LFT3 receiver, CUDA implementation.
//
// Threads:
//   DAQIRI worker (workerCore)   polls the NIC receive queue and fills bursts of packets (DAQIRI-owned)
//   receive thread (processingCore, ours)
//       get_rx_burst -> parse 64-B headers (CPU) -> Assembler::accept -> stage ScatterJobs
//       -> launch scatter kernel on scatterStream -> keep the burst until its kernel finished, then
//       return it to DAQIRI -> seal finished blocks (zero-fill missing packets) -> mark them ready
//   scheduler thread (CyberEther)
//       hasPendingCompute() waits for a ready block; computeSubmit() swaps it into the output tensor,
//       publishes its metadata as tensor attributes, and recycles the previously emitted buffer.
//
// Memory ownership:
//   * Packet payloads stay in DAQIRI's pinned receive buffers; the GPU reads them in place (zero-copy).
//   * Output blocks are a fixed pool of CUDA tensors. A buffer is "open" while packets are scattered
//     into it, "sealed" once its kernels are queued, "ready" when they have run, then "emitted"
//     (swapped into the output tensor), then recycled. A recycled buffer is only reused after the
//     downstream kernels that read it have completed (release event on the scheduler stream).

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstring>
#include <deque>
#include <memory>
#include <mutex>
#include <random>
#include <cmath>
#include <optional>
#include <pthread.h>
#include <sched.h>
#include <string>
#include <thread>
#include <vector>

#include <cuda_runtime.h>
#include <daqiri/daqiri.h>

#include <jetstream/backend/base.hh>
#include <jetstream/backend/devices/cuda/helpers.hh>
#include <jetstream/registry.hh>
#include <jetstream/runtime_context_native_cuda.hh>
#include <jetstream/scheduler_context.hh>

#include "module_impl.hh"
#include "detail/assembler.hh"
#include "detail/daqiri_config.hh"
#include "detail/kernel.hh"
#include "detail/packet.hh"
#include "detail/pcap.hh"

namespace Jetstream::Modules {

namespace {

#define LFT3_CUDA_OK(expr, what)                                                                  \
    do {                                                                                          \
        const cudaError_t lft3Err = (expr);                                                       \
        if (lft3Err != cudaSuccess) {                                                             \
            JST_ERROR("[LFT3_RECEIVER] {} failed: {}", what, cudaGetErrorString(lft3Err));        \
            return Result::ERROR;                                                                 \
        }                                                                                         \
    } while (0)

void PinCurrentThread(U64 core) {
    cpu_set_t set;
    CPU_ZERO(&set);
    CPU_SET(static_cast<int>(core), &set);
    pthread_setaffinity_np(pthread_self(), sizeof(set), &set);
}

F64 UnixNow() {
    return std::chrono::duration<F64>(std::chrono::system_clock::now().time_since_epoch()).count();
}

}  // namespace

struct Lft3ReceiverImplNativeCuda : public Lft3ReceiverImpl,
                                    public NativeCudaRuntimeContext,
                                    public Scheduler::Context {
 public:
    Result validate() final;
    Result create() final;
    Result destroy() final;
    Result hasPendingCompute() override;
    Result computeSubmit(const cudaStream_t& stream) override;

 private:
    // Metadata captured from the first packet of each block.
    struct BlockMeta {
        U64 index = 0;
        U64 missing = 0;
        Lft3::ChannelHeader header;
    };

    struct Buffer {
        std::shared_ptr<Tensor> tensor;
        cudaEvent_t sealed = nullptr;     // recorded after the block's last kernel
        cudaEvent_t released = nullptr;   // recorded on the scheduler stream when recycled
        bool releasePending = false;
        Lft3::ScatterJob* zeroJobs = nullptr;   // pinned, packetsPerBlock entries
        BlockMeta meta;
    };

    struct Staging {
        Lft3::ScatterJob* jobs = nullptr;     // pinned, packetsPerBurst entries
        cudaEvent_t done = nullptr;
    };

    struct InFlight {
        U64 staging = 0;
        daqiri::BurstParams* burst = nullptr;
    };

    // A batch = the packets of one DAQIRI burst (or one synthetic batch), scattered by one kernel launch.
    struct Batch {
        U64 stagingId = 0;
        std::uint32_t jobCount = 0;
        U64 received = 0;
        U64 filtered = 0;
    };

    Result createResources();
    Result createSynthetic();
    Result createFileSource();
    Result produceFile();
    Result receiveLoop();
    Result beginBatch(Batch& batch);
    void addPacket(Batch& batch, const Lft3::ChannelHeader& header, const std::uint8_t* payload);
    Result endBatch(Batch& batch, daqiri::BurstParams* burst);
    Result processBurst(daqiri::BurstParams* burst);
    Result produceSynthetic();
    bool waitForBuffers(U64 needed);
    Result handleSealed(std::vector<Lft3::SealedBlock>& sealed);
    Result reapBursts(bool waitForOne);
    Result promoteSealedBlocks();
    void publishAssemblerStats();
    std::int64_t acquireBuffer();
    void stopThread();
    void startThread();
    void discardBacklog();
    void freeInFlightBursts();

    Lft3::ScatterGeometry geometry = {};
    std::unique_ptr<Lft3::Assembler> assembler;

    cudaStream_t scatterStream = nullptr;
    std::vector<Buffer> buffers;
    std::vector<Staging> staging;
    std::vector<U64> freeStaging;
    std::deque<InFlight> inFlight;
    std::deque<std::int64_t> sealedQueue;     // receive thread only, in block order

    std::mutex poolMutex;                     // freeBuffers (receive thread + scheduler thread)
    std::deque<std::int64_t> freeBuffers;

    std::mutex readyMutex;
    std::condition_variable readyCondition;
    std::deque<std::int64_t> readyQueue;

    std::thread receiveThread;
    bool threadStarted = false;
    bool aligned = false;          // first block boundary seen (packets before it are discarded)
    std::atomic<bool> running = false;
    std::atomic<bool> failed = false;
    bool daqiriActive = false;

    // Synthetic source: a ring of pre-generated payloads, periodic in time (period = ring spectra).
    bool synthetic = false;
    std::uint8_t* syntheticRing = nullptr;
    U64 syntheticRingSpectra = 0;
    U64 syntheticT = 0;
    std::chrono::steady_clock::time_point syntheticStart;
    std::mt19937_64 syntheticRng{12345};

    // File source: the capture's packets in pinned memory, replayed in order (t shifted per loop).
    bool fileSource = false;
    Lft3::PcapPackets filePackets;
    std::uint8_t* filePayloads = nullptr;
    U64 fileIndex = 0;
    U64 fileLoops = 0;
    U64 fileFirstT = 0;
    U64 fileSpan = 0;
    bool fileFinished = false;

    U64 restartsSeen = 0;
    F64 unixTimeAtT0 = 0.0;
    U64 expectedPacketsEmitted = 0;
    U64 missingPacketsEmitted = 0;
};

Result Lft3ReceiverImplNativeCuda::validate() {
    return Lft3ReceiverImpl::validate();
}

Result Lft3ReceiverImplNativeCuda::create() {
    JST_CHECK(Lft3ReceiverImpl::create());

    const Result result = createResources();
    if (result != Result::SUCCESS) {
        destroy();
    }
    return result;
}

Result Lft3ReceiverImplNativeCuda::createResources() {
    geometry.channelCount = static_cast<std::uint32_t>(layout.channelCount);
    geometry.fftLength = static_cast<std::uint32_t>(layout.fftLength);
    geometry.segments = static_cast<std::uint32_t>(layout.segments);
    for (U64 i = 0; i < Lft3::kInputs; i++) {
        geometry.inputBeam[i] = layout.inputBeam[i];
        geometry.inputPol[i] = layout.inputPol[i];
    }
    geometry.outputCF32 = (dataType == "CF32");
    geometry.fftShiftSign = fftShiftSign;

    LFT3_CUDA_OK(cudaStreamCreateWithFlags(&scatterStream, cudaStreamNonBlocking), "cudaStreamCreate");

    // Output buffer pool.
    const Shape shape = {layout.aspects(), layout.channelCount, layout.fftLength, Lft3::Layout::kPols};
    buffers.resize(outputPoolSize);
    for (U64 i = 0; i < outputPoolSize; i++) {
        auto& b = buffers[i];
        b.tensor = std::make_shared<Tensor>();
        JST_CHECK(b.tensor->create(DeviceType::CUDA, NameToDataType(dataType), shape));
        LFT3_CUDA_OK(cudaEventCreateWithFlags(&b.sealed, cudaEventDisableTiming), "cudaEventCreate");
        LFT3_CUDA_OK(cudaEventCreateWithFlags(&b.released, cudaEventDisableTiming), "cudaEventCreate");
        LFT3_CUDA_OK(cudaMallocHost(reinterpret_cast<void**>(&b.zeroJobs),
                                    layout.packetsPerBlock() * sizeof(Lft3::ScatterJob)), "cudaMallocHost");
        freeBuffers.push_back(static_cast<std::int64_t>(i));
    }

    // Per-burst job staging.
    staging.resize(maxConcurrentBursts);
    for (U64 i = 0; i < maxConcurrentBursts; i++) {
        LFT3_CUDA_OK(cudaMallocHost(reinterpret_cast<void**>(&staging[i].jobs),
                                    packetsPerBurst * sizeof(Lft3::ScatterJob)), "cudaMallocHost");
        LFT3_CUDA_OK(cudaEventCreateWithFlags(&staging[i].done, cudaEventDisableTiming), "cudaEventCreate");
        freeStaging.push_back(i);
    }

    Lft3::AssemblerConfig ac;
    ac.spectraPerBlock = layout.spectraPerBlock();
    ac.packetsPerSpectrum = layout.packetsPerSpectrum();
    ac.maxOpenBlocks = maxOpenBlocks;
    ac.maxBlockLag = maxBlockLag;
    ac.zeroFill = zeroFill;
    assembler = std::make_unique<Lft3::Assembler>(ac, [this]() { return acquireBuffer(); });

    synthetic = (source == "synthetic");
    fileSource = (source == "file");
    if (synthetic) {
        JST_CHECK(createSynthetic());
    } else if (fileSource) {
        JST_CHECK(createFileSource());
    } else {
    // DAQIRI.
    Lft3::DaqiriRxParams params;
    params.interfaceAddress = interfaceAddress;
    params.masterCore = masterCore;
    params.workerCore = workerCore;
    params.packetsPerBurst = packetsPerBurst;
    params.maxConcurrentBursts = maxConcurrentBursts;
    params.sourceAddress = sourceAddress;
    params.udpPort = static_cast<std::uint16_t>(udpPort);
    params.payloadInDeviceMemory = !Backend::State<DeviceType::CUDA>()->hasUnifiedMemory();

    daqiri::NetworkConfig cfg = {};
    std::string error;
    if (!Lft3::BuildDaqiriRxConfig(params, cfg, error)) {
        JST_ERROR("[LFT3_RECEIVER] {}.", error);
        return Result::ERROR;
    }
    const auto status = daqiri::daqiri_init(cfg);
    if (status != daqiri::Status::SUCCESS) {
        JST_ERROR("[LFT3_RECEIVER] DAQIRI initialisation failed (status {}). Root or CAP_NET_RAW/CAP_IPC_LOCK "
                  "is required, and '{}' must be a ConnectX interface.", static_cast<int>(status), interfaceAddress);
        if (status == daqiri::Status::INTERNAL_ERROR) {
            daqiri::shutdown();
        }
        return Result::ERROR;
    }
    daqiriActive = true;
    }
    JST_INFO("[LFT3_RECEIVER] Receiving {}:{} on {} -> [{}, {}, {}, 2] {} blocks of {} spectra ({:.3f} ms).",
             sourceAddress.empty() ? "*" : sourceAddress, udpPort,
             synthetic ? std::string("synthetic source") : (fileSource ? "file " + sourceFile : interfaceAddress), layout.aspects(),
             layout.channelCount, layout.fftLength, dataType, layout.spectraPerBlock(),
             1e3 * static_cast<F64>(layout.spectraPerBlock()) / Lft3::kSpectraPerSecond);

    // The receive thread starts on the scheduler's first hasPendingCompute(), i.e. once the whole
    // flowgraph exists. Starting here would fill the buffer pool while downstream blocks are still
    // being created and lose the first blocks.
    failed.store(false);
    aligned = false;
    return Result::SUCCESS;
}

void Lft3ReceiverImplNativeCuda::startThread() {
    threadStarted = true;
    running.store(true);
    receiveThread = std::thread([this]() {
        PinCurrentThread(processingCore);
        if (receiveLoop() != Result::SUCCESS) {
            failed.store(true);
        }
        running.store(false);
        readyCondition.notify_all();
    });
}

void Lft3ReceiverImplNativeCuda::stopThread() {
    running.store(false);
    readyCondition.notify_all();
    if (receiveThread.joinable()) {
        receiveThread.join();
    }
}

void Lft3ReceiverImplNativeCuda::freeInFlightBursts() {
    if (scatterStream) {
        cudaStreamSynchronize(scatterStream);
    }
    while (!inFlight.empty()) {
        if (inFlight.front().burst) {
            daqiri::free_all_packets_and_burst_rx(inFlight.front().burst);
        }
        freeStaging.push_back(inFlight.front().staging);
        inFlight.pop_front();
    }
}

Result Lft3ReceiverImplNativeCuda::destroy() {
    stopThread();
    freeInFlightBursts();

    if (daqiriActive) {
        daqiri::shutdown();
        daqiriActive = false;
    }

    for (auto& b : buffers) {
        if (b.sealed) cudaEventDestroy(b.sealed);
        if (b.released) cudaEventDestroy(b.released);
        if (b.zeroJobs) cudaFreeHost(b.zeroJobs);
    }
    buffers.clear();
    for (auto& s : staging) {
        if (s.jobs) cudaFreeHost(s.jobs);
        if (s.done) cudaEventDestroy(s.done);
    }
    staging.clear();
    freeStaging.clear();
    if (syntheticRing) {
        cudaFreeHost(syntheticRing);
        syntheticRing = nullptr;
    }
    if (filePayloads) {
        cudaFreeHost(filePayloads);
        filePayloads = nullptr;
    }
    filePackets = {};
    freeBuffers.clear();
    readyQueue.clear();
    sealedQueue.clear();
    assembler.reset();

    if (scatterStream) {
        cudaStreamDestroy(scatterStream);
        scatterStream = nullptr;
    }
    return Result::SUCCESS;
}

std::int64_t Lft3ReceiverImplNativeCuda::acquireBuffer() {
    std::int64_t id = -1;
    {
        std::lock_guard<std::mutex> lock(poolMutex);
        if (freeBuffers.empty()) {
            return -1;
        }
        id = freeBuffers.front();
        freeBuffers.pop_front();
        stats.freeBuffers.publish(freeBuffers.size());
    }
    auto& b = buffers[id];
    if (b.releasePending) {
        // Downstream may still be reading this storage: make our writes wait for it on the GPU.
        cudaStreamWaitEvent(scatterStream, b.released, 0);
        b.releasePending = false;
    }
    b.meta = {};
    return id;
}

void Lft3ReceiverImplNativeCuda::discardBacklog() {
    // Bursts queued between DAQIRI start-up and now are stale (and the NIC dropped packets once the
    // queue filled), so throw them away and start from live traffic.
    U64 discarded = 0;
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(200);
    while (std::chrono::steady_clock::now() < deadline) {
        daqiri::BurstParams* burst = nullptr;
        if (daqiri::get_rx_burst(&burst) != daqiri::Status::SUCCESS || !burst) {
            break;
        }
        discarded += static_cast<U64>(daqiri::get_num_packets(burst));
        daqiri::free_all_packets_and_burst_rx(burst);
    }
    JST_INFO("[LFT3_RECEIVER] Started; discarded {} packets queued before the flowgraph was ready.", discarded);
}

Result Lft3ReceiverImplNativeCuda::receiveLoop() {
    LFT3_CUDA_OK(cudaSetDevice(0), "cudaSetDevice");
    if (!synthetic && !fileSource) {
        discardBacklog();
    } else {
        syntheticStart = std::chrono::steady_clock::now();
    }

    auto lastRate = std::chrono::steady_clock::now();
    auto lastLog = lastRate;
    U64 lastPackets = 0;
    U64 lastEmitted = 0;

    while (running.load()) {
        if (synthetic) {
            JST_CHECK(produceSynthetic());
        } else if (fileSource) {
            JST_CHECK(produceFile());
        } else {
            daqiri::BurstParams* burst = nullptr;
            if (daqiri::get_rx_burst(&burst) == daqiri::Status::SUCCESS && burst) {
                JST_CHECK(processBurst(burst));
            } else {
                std::this_thread::sleep_for(std::chrono::microseconds(20));
            }
        }

        JST_CHECK(reapBursts(false));
        JST_CHECK(promoteSealedBlocks());

        const auto now = std::chrono::steady_clock::now();
        const F64 dt = std::chrono::duration<F64>(now - lastRate).count();
        if (dt >= 0.5) {
            const U64 packets = stats.packetsReceived.get();
            const F64 gbps = static_cast<F64>(packets - lastPackets) *
                             (Lft3::kHeaderSegmentBytes + Lft3::kPayloadBytes) * 8.0 / dt / 1e9;
            stats.throughputGbps.publish(gbps);
            lastPackets = packets;
            lastRate = now;
        }
        // Periodic one-line summary for headless runs.
        const F64 logDt = std::chrono::duration<F64>(now - lastLog).count();
        if (logDt >= 5.0) {
            const U64 emitted = stats.blocksEmitted.get();
            const F64 speed = static_cast<F64>(emitted - lastEmitted) * static_cast<F64>(layout.spectraPerBlock()) /
                                 Lft3::kSpectraPerSecond / logDt;
            JST_INFO("[LFT3_RECEIVER] {:.2f} Gb/s | blocks emitted {} ({:.2f}x real time) complete {} partial {} "
                     "skipped {} | packets accepted {} missing {} late {} dup {} no-buffer {} | ready {} free {}",
                     stats.throughputGbps.get(), emitted, speed, stats.blocksComplete.get(),
                     stats.blocksPartial.get(), stats.blocksSkipped.get(), stats.packetsAccepted.get(),
                     stats.packetsMissing.get(), stats.packetsLate.get(), stats.packetsDuplicate.get(),
                     stats.packetsNoBuffer.get(), stats.readyBlocks.get(), stats.freeBuffers.get());
            lastEmitted = emitted;
            lastLog = now;
        }
    }

    // Seal and emit what is left so nothing is half-written when the pipeline stops.
    std::vector<Lft3::SealedBlock> sealed;
    assembler->flush(sealed);
    JST_CHECK(handleSealed(sealed));
    return Result::SUCCESS;
}

Result Lft3ReceiverImplNativeCuda::beginBatch(Batch& batch) {
    if (freeStaging.empty()) {
        JST_CHECK(reapBursts(true));
    }
    batch = {};
    batch.stagingId = freeStaging.back();
    freeStaging.pop_back();
    return Result::SUCCESS;
}

void Lft3ReceiverImplNativeCuda::addPacket(Batch& batch, const Lft3::ChannelHeader& header,
                                           const std::uint8_t* payload) {
    batch.received++;
    if (header.fid != boardId || !layout.keepsPacket(header.bindex) || batch.jobCount >= packetsPerBurst) {
        batch.filtered++;
        return;
    }
    // Start at a block boundary so the first block is complete (packets before it count as filtered).
    if (!aligned) {
        if (header.t % layout.spectraPerBlock() != 0 || header.bindex != layout.firstPacket()) {
            batch.filtered++;
            return;
        }
        aligned = true;
    }
    if (header.t > stats.latestTimestamp.get()) {
        stats.latestTimestamp.publish(header.t);
    }

    Lft3::Assembler::Placement placement;
    if (assembler->accept(header.t, header.bindex - layout.firstPacket(), placement) != Lft3::Assembler::Accept::Ok) {
        return;
    }

    auto& buffer = buffers[placement.buffer];
    if (placement.newBlock) {
        buffer.meta.index = layout.blockIndex(header.t);
        buffer.meta.header = header;
        // Estimate the wall-clock time of t = 0 from the first packet (and after each restart).
        // Good to ~ms (NIC + software latency); a PPS-aligned sync in firmware would make it exact.
        if (unixTimeAtT0 == 0.0 || assembler->stats().restarts != restartsSeen) {
            restartsSeen = assembler->stats().restarts;
            unixTimeAtT0 = UnixNow() - static_cast<F64>(header.t) / Lft3::kSpectraPerSecond;
        }
    }

    Lft3::ScatterJob& job = staging[batch.stagingId].jobs[batch.jobCount++];
    job.source = payload;
    job.destination = buffer.tensor->data();
    job.spectrum = static_cast<std::uint32_t>(layout.slotSpectrum(placement.slot));
    job.channelOffset = static_cast<std::uint32_t>(layout.slotChannelOffset(placement.slot));
}

Result Lft3ReceiverImplNativeCuda::endBatch(Batch& batch, daqiri::BurstParams* burst) {
    auto& stage = staging[batch.stagingId];
    LFT3_CUDA_OK(Lft3::LaunchScatter(stage.jobs, batch.jobCount, geometry, scatterStream), "scatter kernel launch");
    LFT3_CUDA_OK(cudaEventRecord(stage.done, scatterStream), "cudaEventRecord");
    inFlight.push_back({batch.stagingId, burst});

    stats.packetsReceived.publish(stats.packetsReceived.get() + batch.received);
    stats.packetsFiltered.publish(stats.packetsFiltered.get() + batch.filtered);

    // Seal after launching this batch's kernel: blocks sealed now may have received packets from it.
    std::vector<Lft3::SealedBlock> sealed;
    assembler->collect(sealed);
    JST_CHECK(handleSealed(sealed));
    publishAssemblerStats();
    return Result::SUCCESS;
}

Result Lft3ReceiverImplNativeCuda::processBurst(daqiri::BurstParams* burst) {
    Batch batch;
    JST_CHECK(beginBatch(batch));
    const int64_t numPackets = daqiri::get_num_packets(burst);
    for (int64_t i = 0; i < numPackets; i++) {
        const auto* frame = static_cast<const std::uint8_t*>(
            daqiri::get_segment_packet_ptr(burst, Lft3::kRxHeaderSegment, static_cast<int>(i)));
        const auto* payload = static_cast<const std::uint8_t*>(
            daqiri::get_segment_packet_ptr(burst, Lft3::kRxDataSegment, static_cast<int>(i)));
        addPacket(batch, Lft3::ParseChannelHeader(frame + Lft3::kNetworkHeaderBytes), payload);
    }
    return endBatch(batch, burst);
}

Result Lft3ReceiverImplNativeCuda::createSynthetic() {
    // Ring of R spectra x 6 packets. The tone frequency is rounded to a multiple of 1/R cycles/sample
    // so the ring is seamless; the noise repeats every R spectra (fine for functional tests).
    syntheticRingSpectra = 1024;
    const U64 R = syntheticRingSpectra;
    const U64 bytes = R * Lft3::kPacketsPerSpectrum * Lft3::kPayloadBytes;
    LFT3_CUDA_OK(cudaMallocHost(reinterpret_cast<void**>(&syntheticRing), bytes), "cudaMallocHost");

    const F64 f = std::round(syntheticToneFrequency * static_cast<F64>(R)) / static_cast<F64>(R);
    constexpr F64 kNoiseSigma = 8.0;
    constexpr F64 kToneAmplitude = 24.0;
    std::normal_distribution<F64> noise(0.0, kNoiseSigma);
    std::mt19937_64 rng(42);
    const auto clip = [](F64 v) {
        return static_cast<std::int8_t>(std::lround(std::fmin(127.0, std::fmax(-127.0, v))));
    };

    auto* p = reinterpret_cast<std::int8_t*>(syntheticRing);
    for (U64 n = 0; n < R; n++) {
        for (U64 k = 0; k < Lft3::kTotalChannels; k++) {
            for (U64 input = 0; input < Lft3::kInputs; input++) {
                F64 re = noise(rng);
                F64 im = noise(rng);
                if (k == syntheticToneChannel && input == syntheticToneInput) {
                    re += kToneAmplitude * std::cos(2.0 * M_PI * f * static_cast<F64>(n));
                    im += kToneAmplitude * std::sin(2.0 * M_PI * f * static_cast<F64>(n));
                }
                // Byte order as on the wire: channel-major, then input, then I, Q.
                *p++ = clip(re);
                *p++ = clip(im);
            }
        }
    }
    syntheticT = 0;
    syntheticStart = std::chrono::steady_clock::now();
    JST_INFO("[LFT3_RECEIVER] Synthetic source: tone in channel {}, input {}, {:+.6f} cycles/sample "
             "({:+.3f} kHz from the channel centre), drop fraction {}.", syntheticToneChannel, syntheticToneInput,
             f, f * Lft3::kCoarseChannelHz / 1e3, syntheticDropFraction);
    return Result::SUCCESS;
}

// Synthetic and file sources apply backpressure instead of dropping: a batch (<= 1024 packets) can open at
// most two blocks, so wait until two output buffers are free. Returns false if the thread is stopping.
bool Lft3ReceiverImplNativeCuda::waitForBuffers(U64 needed) {
    while (running.load()) {
        {
            std::lock_guard<std::mutex> lock(poolMutex);
            if (freeBuffers.size() >= needed) {
                return true;
            }
        }
        if (reapBursts(false) != Result::SUCCESS || promoteSealedBlocks() != Result::SUCCESS) {
            return false;
        }
        std::this_thread::sleep_for(std::chrono::microseconds(100));
    }
    return false;
}

Result Lft3ReceiverImplNativeCuda::createFileSource() {
    std::string error;
    if (!Lft3::ReadPcapChannelPackets(sourceFile, static_cast<std::uint16_t>(udpPort), filePackets, error)) {
        JST_ERROR("[LFT3_RECEIVER] {}.", error);
        return Result::ERROR;
    }
    const U64 n = filePackets.headers.size();
    LFT3_CUDA_OK(cudaMallocHost(reinterpret_cast<void**>(&filePayloads), n * Lft3::kPayloadBytes), "cudaMallocHost");
    std::memcpy(filePayloads, filePackets.payloads.data(), n * Lft3::kPayloadBytes);
    filePackets.payloads.clear();
    filePackets.payloads.shrink_to_fit();

    U64 tmin = filePackets.headers.front().t, tmax = tmin;
    for (const auto& h : filePackets.headers) {
        tmin = std::min<U64>(tmin, h.t);
        tmax = std::max<U64>(tmax, h.t);
    }
    // Replay whole blocks only: [first block boundary, last block boundary). Loops are then shifted by
    // exactly that span, so the time axis is continuous (no empty or overlapping blocks between loops).
    const U64 L = layout.spectraPerBlock();
    const U64 a = (tmin + L - 1) / L * L;
    const U64 b = (tmax + 1) / L * L;
    if (b <= a) {
        JST_ERROR("[LFT3_RECEIVER] '{}' covers {} spectra, less than one whole block of {}.", sourceFile,
                  tmax - tmin + 1, L);
        return Result::ERROR;
    }
    U64 kept = 0;
    for (U64 i = 0; i < n; i++) {
        if (filePackets.headers[i].t >= a && filePackets.headers[i].t < b) {
            filePackets.headers[kept] = filePackets.headers[i];
            std::memmove(filePayloads + kept * Lft3::kPayloadBytes, filePayloads + i * Lft3::kPayloadBytes,
                         Lft3::kPayloadBytes);
            kept++;
        }
    }
    filePackets.headers.resize(kept);
    fileFirstT = a;
    fileSpan = b - a;
    fileIndex = 0;
    fileLoops = 0;
    fileFinished = false;
    JST_INFO("[LFT3_RECEIVER] File source '{}': {} packets ({} other skipped); replaying {} whole blocks "
             "(t {}..{}, {:.3f} s, {} packets){}.", sourceFile, n, filePackets.skipped, fileSpan / L, a, b - 1,
             static_cast<F64>(fileSpan) / Lft3::kSpectraPerSecond, kept, sourceLoop ? ", looping" : "");
    return Result::SUCCESS;
}

Result Lft3ReceiverImplNativeCuda::produceFile() {
    if (fileFinished) {
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
        return Result::SUCCESS;
    }
    const U64 n = filePackets.headers.size();
    const U64 offset = fileLoops * fileSpan;
    if (realTime) {
        const U64 t = filePackets.headers[fileIndex].t + offset - fileFirstT;
        const F64 due = static_cast<F64>(t) / Lft3::kSpectraPerSecond;
        const F64 now = std::chrono::duration<F64>(std::chrono::steady_clock::now() - syntheticStart).count();
        if (now < due) {
            std::this_thread::sleep_for(std::chrono::microseconds(static_cast<int64_t>((due - now) * 1e6) + 1));
            return Result::SUCCESS;
        }
    }
    if (!waitForBuffers(2)) {
        return Result::SUCCESS;
    }
    Batch batch;
    JST_CHECK(beginBatch(batch));
    for (U64 k = 0; k < packetsPerBurst && fileIndex < n; k++, fileIndex++) {
        Lft3::ChannelHeader header = filePackets.headers[fileIndex];
        header.t += offset;
        addPacket(batch, header, filePayloads + fileIndex * Lft3::kPayloadBytes);
    }
    JST_CHECK(endBatch(batch, nullptr));
    if (fileIndex >= n) {
        if (sourceLoop) {
            fileIndex = 0;
            fileLoops++;
        } else {
            // Seal what is left so the last blocks are emitted, then idle.
            std::vector<Lft3::SealedBlock> sealed;
            assembler->flush(sealed);
            JST_CHECK(handleSealed(sealed));
            fileFinished = true;
            JST_INFO("[LFT3_RECEIVER] File replay finished: {} packets.", n);
        }
    }
    return Result::SUCCESS;
}

Result Lft3ReceiverImplNativeCuda::produceSynthetic() {
    if (realTime) {
        const F64 due = static_cast<F64>(syntheticT) / Lft3::kSpectraPerSecond;
        const F64 now = std::chrono::duration<F64>(std::chrono::steady_clock::now() - syntheticStart).count();
        if (now < due) {
            std::this_thread::sleep_for(std::chrono::microseconds(static_cast<int64_t>((due - now) * 1e6) + 1));
            return Result::SUCCESS;
        }
    }
    // Same shape as a DAQIRI burst: packetsPerBurst packets, whole spectra at a time.
    if (!waitForBuffers(2)) {
        return Result::SUCCESS;
    }
    Batch batch;
    JST_CHECK(beginBatch(batch));
    std::uniform_real_distribution<F64> uniform(0.0, 1.0);
    const U64 spectra = std::max<U64>(1, packetsPerBurst / Lft3::kPacketsPerSpectrum);
    for (U64 n = 0; n < spectra; n++, syntheticT++) {
        for (std::uint32_t b = 0; b < Lft3::kPacketsPerSpectrum; b++) {
            if (syntheticDropFraction > 0.0 && uniform(syntheticRng) < syntheticDropFraction) {
                continue;
            }
            Lft3::ChannelHeader header;
            header.t = syntheticT;
            header.bindex = b;
            header.fid = static_cast<std::uint32_t>(boardId);
            header.nco = {-2031, -2031, -2031, -2031};
            const U64 ringIndex = (syntheticT % syntheticRingSpectra) * Lft3::kPacketsPerSpectrum + b;
            addPacket(batch, header, syntheticRing + ringIndex * Lft3::kPayloadBytes);
        }
    }
    return endBatch(batch, nullptr);
}

Result Lft3ReceiverImplNativeCuda::handleSealed(std::vector<Lft3::SealedBlock>& sealed) {
    for (auto& s : sealed) {
        auto& buffer = buffers[s.buffer];
        if (!s.emit) {
            std::lock_guard<std::mutex> lock(poolMutex);
            freeBuffers.push_back(s.buffer);
            continue;
        }
        if (!s.missingSlots.empty()) {
            // Zero-fill the missing packets' regions (the same kernel with a null source).
            for (U64 k = 0; k < s.missingSlots.size(); k++) {
                const U64 slot = s.missingSlots[k];
                buffer.zeroJobs[k] = {nullptr, buffer.tensor->data(),
                                      static_cast<std::uint32_t>(layout.slotSpectrum(slot)),
                                      static_cast<std::uint32_t>(layout.slotChannelOffset(slot))};
            }
            LFT3_CUDA_OK(Lft3::LaunchScatter(buffer.zeroJobs, static_cast<std::uint32_t>(s.missingSlots.size()),
                                             geometry, scatterStream), "zero-fill kernel launch");
        }
        buffer.meta.index = s.index;
        buffer.meta.missing = s.missingSlots.size();
        LFT3_CUDA_OK(cudaEventRecord(buffer.sealed, scatterStream), "cudaEventRecord");
        sealedQueue.push_back(s.buffer);
    }
    return Result::SUCCESS;
}

Result Lft3ReceiverImplNativeCuda::reapBursts(bool waitForOne) {
    while (!inFlight.empty()) {
        auto& front = inFlight.front();
        cudaError_t status = cudaEventQuery(staging[front.staging].done);
        if (status == cudaErrorNotReady) {
            if (!waitForOne) {
                break;
            }
            status = cudaEventSynchronize(staging[front.staging].done);
            waitForOne = false;
        }
        LFT3_CUDA_OK(status, "scatter kernel");
        // The kernel has consumed the payloads: hand the burst's buffers back to DAQIRI.
        if (front.burst) {
            daqiri::free_all_packets_and_burst_rx(front.burst);
        }
        freeStaging.push_back(front.staging);
        inFlight.pop_front();
    }
    return Result::SUCCESS;
}

Result Lft3ReceiverImplNativeCuda::promoteSealedBlocks() {
    while (!sealedQueue.empty()) {
        const std::int64_t id = sealedQueue.front();
        const cudaError_t status = cudaEventQuery(buffers[id].sealed);
        if (status == cudaErrorNotReady) {
            break;   // events on one stream complete in order
        }
        LFT3_CUDA_OK(status, "block kernels");
        sealedQueue.pop_front();
        {
            std::lock_guard<std::mutex> lock(readyMutex);
            readyQueue.push_back(id);
            stats.readyBlocks.publish(readyQueue.size());
        }
        readyCondition.notify_one();
    }
    return Result::SUCCESS;
}

void Lft3ReceiverImplNativeCuda::publishAssemblerStats() {
    const auto& a = assembler->stats();
    stats.packetsAccepted.publish(a.packetsAccepted);
    stats.packetsDuplicate.publish(a.packetsDuplicate);
    stats.packetsLate.publish(a.packetsLate);
    stats.packetsNoBuffer.publish(a.packetsNoBuffer);
    stats.packetsMissing.publish(a.packetsMissing);
    stats.blocksComplete.publish(a.blocksComplete);
    stats.blocksPartial.publish(a.blocksPartial);
    stats.blocksDropped.publish(a.blocksDropped);
    stats.blocksSkipped.publish(a.blocksSkipped);
    stats.restarts.publish(a.restarts);
    stats.openBlocks.publish(assembler->openBlocks());
}

Result Lft3ReceiverImplNativeCuda::hasPendingCompute() {
    if (!threadStarted) {
        startThread();
    }
    std::unique_lock<std::mutex> lock(readyMutex);
    readyCondition.wait_for(lock, std::chrono::milliseconds(100), [this]() {
        return !readyQueue.empty() || failed.load() || !running.load();
    });
    if (failed.load()) {
        JST_ERROR("[LFT3_RECEIVER] Receive thread failed; see the log above.");
        return Result::ERROR;
    }
    return readyQueue.empty() ? Result::TIMEOUT : Result::SUCCESS;
}

Result Lft3ReceiverImplNativeCuda::computeSubmit(const cudaStream_t& stream) {
    std::int64_t id = -1;
    {
        std::lock_guard<std::mutex> lock(readyMutex);
        if (readyQueue.empty()) {
            return Result::YIELD;
        }
        id = readyQueue.front();
        readyQueue.pop_front();
        stats.readyBlocks.publish(readyQueue.size());
    }
    auto& buffer = buffers[id];

    // After the swap, outputTensor holds this block and buffer.tensor holds the previously emitted one.
    JST_CHECK(outputTensor.swapBuffers(*buffer.tensor));

    const auto& m = buffer.meta;
    expectedPacketsEmitted += layout.packetsPerBlock();
    missingPacketsEmitted += m.missing;
    JST_CHECK(outputTensor.setAttribute("timestamp", layout.blockFirstSpectrum(m.index)));
    for (int i = 0; i < 4; i++) {
        JST_CHECK(outputTensor.setAttribute(jst::fmt::format("lft3.nco{}", i), static_cast<I64>(m.header.nco[i])));
        JST_CHECK(outputTensor.setAttribute(jst::fmt::format("lft3.pfbLsb{}", i), static_cast<U64>(m.header.pfbLsb[i])));
    }
    JST_CHECK(outputTensor.setAttribute("lft3.blockMissingPackets", m.missing));
    JST_CHECK(outputTensor.setAttribute("lft3.totalMissingPackets", missingPacketsEmitted));
    JST_CHECK(outputTensor.setAttribute("lft3.totalExpectedPackets", expectedPacketsEmitted));
    JST_CHECK(outputTensor.setAttribute("lft3.unixTimeAtT0", unixTimeAtT0));

    // Recycle the previously emitted storage once the kernels already queued on the scheduler stream
    // (which read it during the previous cycle) have finished.
    LFT3_CUDA_OK(cudaEventRecord(buffer.released, stream), "cudaEventRecord");
    buffer.releasePending = true;
    {
        std::lock_guard<std::mutex> lock(poolMutex);
        freeBuffers.push_back(id);
        stats.freeBuffers.publish(freeBuffers.size());
    }
    stats.blocksEmitted.publish(stats.blocksEmitted.get() + 1);
    return Result::SUCCESS;
}

JST_REGISTER_MODULE(Lft3ReceiverImplNativeCuda, DeviceType::CUDA, RuntimeType::NATIVE, "generic");

}  // namespace Jetstream::Modules
