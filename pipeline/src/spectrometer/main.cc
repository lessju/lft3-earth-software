// lft3-spectrometer: ring reader -> fused fine spectrometer (or coarse power) -> integration -> FBH5.
//
//   fine mode    fftLength-point FFT per coarse channel, |X|^2 + |Y|^2 (Stokes I), integrated over N blocks
//   coarse mode  power per board channel (no FFT), for quick-look products; optional "latest" snapshot files
//
// With --spectra-ring N every finished integration is also copied (device to device) into the spectra ring
// "<stream>-spectra" of N slots in GPU memory, for the live search (lft3.search.live). The spectra ring has
// backpressure when this reader is --lossless (synthetic / file tests), so a lossless search sees every spectrum.
//
// Integrations are aligned to absolute block numbers (window w = blocks [w*N, (w+1)*N)), so every reader and
// every link agrees on the time grid. A reader that attaches mid-window starts at the next boundary.
// Missing data (zero-filled packets, blocks the capture skipped, blocks this reader lost to lag) is counted
// per spectrum in /lft3_missing_packets. A gap of whole integrations or a stream restart starts new files.

#include <cmath>
#include <condition_variable>
#include <cstring>
#include <deque>
#include <filesystem>
#include <fstream>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include <cuda_runtime.h>

#include "common/packet.hh"
#include "common/ring.hh"
#include "common/util.hh"
#include "spectrometer/axes.hh"
#include "spectrometer/fbh5_file.hh"
#include "spectrometer/finespec.hh"

namespace Lft3 {
namespace {

const char* kUsage = R"(lft3-spectrometer --stream NAME [options]
  --mode fine|coarse            (default fine)
  --fft-length N                fine FFT length, power of two <= spectraPerBlock and <= 2048 (default 128)
  --integration S | --integration-blocks N   (default 1.0 s)
  --bins-per-output N           sum N fine (fine mode) or coarse (coarse mode) channels per output (default 1)
  --output-dir DIR --prefix NAME --beam-offset N   files DIR/PREFIX_<UTC>_beam<N+offset>.h5
  --no-record                   do not write FBH5
  --latest DIR                  also write DIR/PREFIX_latest.{json,f32} after every spectrum (fine mode: the
                                coarse power per board channel derived from the fine spectrum)
  --coarse-dir DIR              fine mode: also write that derived coarse product as FBH5 in DIR
  --spectra-per-file N          rotate files (default 3600, 0 = never)
  --nco-mhz F                   exact NCO (default: the ring's, else the rounded header value)
  --sync-time-unix F            UNIX time of t = 0 (default: capture's estimate)
  --source-name S --telescope-id N --machine-id N
  --name NAME --lossless        reader name; lossless: capture waits for this reader (synthetic/file only)
  --attach-timeout S            (default 30)
  --spectra-ring N              also publish every spectrum into the GPU ring <stream>-spectra of N slots
)";

struct Options {
    std::string stream, name, mode = "fine", outputDir = ".", prefix = "lft3", latestDir, coarseDir, sourceName = "LFT3";
    std::uint32_t fftLength = 128, binsPerOutput = 1, beamOffset = 0;
    double integrationS = 1.0, ncoMHz = 0.0, syncTimeUnix = 0.0, attachTimeout = 30.0;
    std::uint64_t integrationBlocks = 0, spectraPerFile = 3600, spectraRing = 0;
    int telescopeId = 0, machineId = 0;
    bool record = true, lossless = false;
};

struct Product {
    std::vector<float>* data = nullptr;   // [beams][channelsOut], from the pool
    std::uint64_t window = 0;
    std::uint64_t startT = 0;
    std::uint64_t missing = 0;
    bool newRun = false;                  // discontinuity before this spectrum: start new files
    RingSlotMeta meta;                    // of the window's first block
};

class Writer {
 public:
    Writer(const Options& o, const Layout& l, std::uint32_t boardId, double ringNco, std::uint64_t channelsOut,
           std::uint64_t spectraPerIntegration, const RingControl& ctl)
        : o_(o), l_(l), boardId_(boardId), ringNco_(ringNco), channelsOut_(channelsOut),
          spectraPerIntegration_(spectraPerIntegration), stream_(ctl.stream), obsId_(ctl.obsId),
          configHash_(ctl.configHash) {
        const bool fine = o_.mode == "fine";
        primary_.dir = o_.outputDir;
        primary_.mode = o_.mode;
        primary_.perCoarse = fine ? o_.fftLength : 1;
        primary_.bins = o_.binsPerOutput;
        primary_.channelsOut = channelsOut_;
        deriveCoarse_ = fine && (!o_.coarseDir.empty() || !o_.latestDir.empty()) &&
                        o_.binsPerOutput <= o_.fftLength && o_.fftLength % o_.binsPerOutput == 0;
        if (deriveCoarse_) {
            coarse_.dir = o_.coarseDir;
            coarse_.mode = "coarse (from fine)";
            coarse_.channelsOut = l_.channelCount;
            coarseBuf_.resize(Layout::kBeams * l_.channelCount);
        }
        for (int i = 0; i < 4; i++) pool_.emplace_back(Layout::kBeams * channelsOut_);
        for (auto& v : pool_) free_.push_back(&v);
        thread_ = std::thread([this]() { loop(); });
    }
    ~Writer() { stop(); }

    std::vector<float>* acquire() {   // blocks while the writer is behind
        std::unique_lock<std::mutex> lk(mu_);
        cv_.wait(lk, [this]() { return !free_.empty(); });
        auto* v = free_.front();
        free_.pop_front();
        return v;
    }
    void push(const Product& p) {
        {
            std::lock_guard<std::mutex> lk(mu_);
            queue_.push_back(p);
        }
        cv_.notify_all();
    }
    void stop() {
        {
            std::lock_guard<std::mutex> lk(mu_);
            if (stopping_) return;
            stopping_ = true;
        }
        cv_.notify_all();
        if (thread_.joinable()) thread_.join();
        closeFiles();
    }
    std::uint64_t written() const { return written_.load(); }
    bool failed() const { return failed_.load(); }

 private:
    void loop() {
        while (true) {
            Product p;
            {
                std::unique_lock<std::mutex> lk(mu_);
                cv_.wait(lk, [this]() { return stopping_ || !queue_.empty(); });
                if (queue_.empty()) return;
                p = queue_.front();
                queue_.pop_front();
            }
            if (!failed_.load() && !write(p)) failed_.store(true);
            {
                std::lock_guard<std::mutex> lk(mu_);
                free_.push_back(p.data);
            }
            cv_.notify_all();
        }
    }

    double nco(const Product& p) const {
        if (o_.ncoMHz != 0.0) return o_.ncoMHz;
        if (ringNco_ != 0.0) return ringNco_;
        return static_cast<double>(p.meta.nco[0]);
    }

    // One product written as one FBH5 file per beam: the primary product (fine, or coarse in coarse mode) and,
    // in fine mode with --coarse-dir, the coarse power per board channel derived from it (Parseval).
    struct FileSet {
        std::string dir, mode;
        std::uint64_t perCoarse = 1;      // fine channels per coarse channel in this product (before binning)
        std::uint32_t bins = 1;           // channels summed per output value
        std::uint64_t channelsOut = 0;
        std::vector<std::unique_ptr<Fbh5File>> files;
    };

    bool openFiles(FileSet& fs, const Product& p) {
        closeFiles(fs);
        const double ncoMHz = nco(p);
        if (o_.ncoMHz == 0.0 && ringNco_ == 0.0) {
            LFT3_WARN("no exact NCO configured: using the header's whole-MHz value %g", ncoMHz);
        }
        const double t0 = o_.syncTimeUnix > 0.0 ? o_.syncTimeUnix : p.meta.unixTimeAtT0;
        const double startUnix = t0 + static_cast<double>(p.startT) / kSpectraPerSecond;
        const auto axes = ComputeFineAxes(ncoMHz, l_.channelStart, l_.channelCount, fs.perCoarse);
        Fbh5Header h;
        h.sourceName = o_.sourceName;
        h.foffMHz = axes.foffMHz * fs.bins;
        h.fch1MHz = axes.fch1MHz + 0.5 * (fs.bins - 1) * axes.foffMHz;
        h.nchans = fs.channelsOut;
        h.tstartMjd = UnixToMjd(startUnix);
        h.tsampSeconds = static_cast<double>(spectraPerIntegration_) / kSpectraPerSecond;
        h.telescopeId = o_.telescopeId;
        h.machineId = o_.machineId;
        h.nbeams = static_cast<int>(Layout::kBeams);
        h.fineChannelsPerCoarse = static_cast<int>(fs.perCoarse >= fs.bins ? fs.perCoarse / fs.bins : 1);
        std::map<std::string, double> num = {
            {"lft3_nco_mhz", ncoMHz},
            {"lft3_unix_time_at_t0", t0},
            {"lft3_start_t", static_cast<double>(p.startT)},
            {"lft3_channel_start", static_cast<double>(l_.channelStart)},
            {"lft3_fft_length", static_cast<double>(fs.perCoarse)},
            {"lft3_bins_per_output", static_cast<double>(fs.bins)},
            {"lft3_spectra_per_integration", static_cast<double>(spectraPerIntegration_)},
            {"lft3_spectra_per_block", static_cast<double>(l_.spectraPerBlock)},
            {"lft3_board_id", static_cast<double>(boardId_)},
            {"lft3_fft_shift_sign", 0.0},
            {"lft3_beam_offset", static_cast<double>(o_.beamOffset)},
        };
        for (int i = 0; i < 4; i++) {
            num["lft3_header_nco" + std::to_string(i)] = static_cast<double>(p.meta.nco[i]);
            num["lft3_pfb_lsb" + std::to_string(i)] = static_cast<double>(p.meta.pfbLsb[i]);
        }
        const std::map<std::string, std::string> str = {
            {"lft3_software", std::string("lft3-earth-software runtime ") + LFT3_VERSION},
            {"lft3_time_reference", o_.syncTimeUnix > 0.0 ? "configured syncTimeUnix"
                                                          : "estimated from packet arrival (~ms accuracy)"},
            {"lft3_frequency_convention", "RF = -nco + baseband offset; frequency ascends with channel index"},
            {"lft3_stream", stream_},
            {"lft3_obs_id", obsId_},
            {"lft3_config_hash", configHash_},
            {"lft3_mode", fs.mode},
        };
        const std::string stamp = UtcStamp(startUnix);
        for (std::uint64_t b = 0; b < Layout::kBeams; b++) {
            h.ibeam = static_cast<int>(b + o_.beamOffset + 1);
            const std::string path = fs.dir + "/" + o_.prefix + "_" + stamp + "_beam" +
                                     std::to_string(b + o_.beamOffset) + ".h5";
            auto f = std::make_unique<Fbh5File>();
            std::string error;
            if (!f->open(path, h, num, str, error)) {
                LFT3_ERROR("%s", error.c_str());
                return false;
            }
            LFT3_INFO("writing %s: fch1 %.6f MHz, foff %.6f kHz, %lu channels, tsamp %.4f s", path.c_str(), h.fch1MHz,
                      h.foffMHz * 1e3, static_cast<unsigned long>(h.nchans), h.tsampSeconds);
            fs.files.push_back(std::move(f));
        }
        return true;
    }

    void closeFiles(FileSet& fs) {
        for (auto& f : fs.files) {
            if (f && f->isOpen()) {
                LFT3_INFO("closed %s (%lu spectra)", f->path().c_str(), static_cast<unsigned long>(f->spectraWritten()));
            }
        }
        fs.files.clear();
    }
    void closeFiles() {
        closeFiles(primary_);
        closeFiles(coarse_);
    }

    bool writeSet(FileSet& fs, const float* data, const Product& p) {
        if (p.newRun || (!fs.files.empty() && o_.spectraPerFile > 0 && fs.files.front()->spectraWritten() >= o_.spectraPerFile)) {
            closeFiles(fs);
        }
        if (fs.files.empty() && !openFiles(fs, p)) return false;
        for (std::uint64_t b = 0; b < Layout::kBeams; b++) {
            std::string error;
            if (!fs.files[b]->append(data + b * fs.channelsOut, p.startT, p.missing, error)) {
                LFT3_ERROR("%s", error.c_str());
                return false;
            }
        }
        return true;
    }

    bool writeLatest(const FileSet& fs, const float* data, const Product& p) {
        const std::string base = o_.latestDir + "/" + o_.prefix + "_latest";
        {
            std::ofstream f(base + ".f32.tmp", std::ios::binary);
            f.write(reinterpret_cast<const char*>(data), static_cast<std::streamsize>(Layout::kBeams * fs.channelsOut * 4));
        }
        const auto axes = ComputeFineAxes(nco(p), l_.channelStart, l_.channelCount, fs.perCoarse);
        const double t0 = o_.syncTimeUnix > 0.0 ? o_.syncTimeUnix : p.meta.unixTimeAtT0;
        char json[1024];
        std::snprintf(json, sizeof(json),
                      "{\"stream\": \"%s\", \"mode\": \"%s\", \"beams\": %lu, \"beam_offset\": %u, \"channels\": %lu, "
                      "\"fch1_mhz\": %.9f, \"foff_mhz\": %.9f, \"tsamp_s\": %.9f, \"start_t\": %lu, "
                      "\"start_unix\": %.6f, \"missing_packets\": %lu, \"dtype\": \"float32\", \"file\": \"%s\"}\n",
                      stream_.c_str(), fs.mode.c_str(), static_cast<unsigned long>(Layout::kBeams), o_.beamOffset,
                      static_cast<unsigned long>(fs.channelsOut),
                      axes.fch1MHz + 0.5 * (fs.bins - 1) * axes.foffMHz, axes.foffMHz * fs.bins,
                      static_cast<double>(spectraPerIntegration_) / kSpectraPerSecond,
                      static_cast<unsigned long>(p.startT), t0 + static_cast<double>(p.startT) / kSpectraPerSecond,
                      static_cast<unsigned long>(p.missing),
                      (std::filesystem::path(base).filename().string() + ".f32").c_str());
        {
            std::ofstream f(base + ".json.tmp");
            f << json;
        }
        std::error_code ec;
        std::filesystem::rename(base + ".f32.tmp", base + ".f32", ec);
        std::filesystem::rename(base + ".json.tmp", base + ".json", ec);
        return !ec;
    }

    bool write(const Product& p) {
        const float* data = p.data->data();
        const float* coarse = data;
        const FileSet* latestSet = &primary_;
        if (deriveCoarse_) {
            // Coarse power per board channel = sum of its fine bins / fftLength (Parseval), as the coarse mode
            // would compute it from the voltages: the quick-look product without a second pass over the ring.
            const std::uint64_t perCoarse = primary_.perCoarse / primary_.bins;
            for (std::uint64_t b = 0; b < Layout::kBeams; b++) {
                for (std::uint64_t c = 0; c < coarse_.channelsOut; c++) {
                    const float* f = data + b * primary_.channelsOut + c * perCoarse;
                    double sum = 0.0;
                    for (std::uint64_t k = 0; k < perCoarse; k++) sum += f[k];
                    coarseBuf_[b * coarse_.channelsOut + c] = static_cast<float>(sum / static_cast<double>(o_.fftLength));
                }
            }
            coarse = coarseBuf_.data();
            latestSet = &coarse_;
        }
        if (!o_.latestDir.empty() && !writeLatest(*latestSet, coarse, p)) {
            LFT3_WARN("cannot write the latest snapshot in %s", o_.latestDir.c_str());
        }
        if (o_.record && !writeSet(primary_, data, p)) return false;
        if (deriveCoarse_ && !o_.coarseDir.empty() && !writeSet(coarse_, coarse, p)) return false;
        written_++;
        return true;
    }

    Options o_;
    Layout l_;
    std::uint32_t boardId_;
    double ringNco_;
    std::uint64_t channelsOut_, spectraPerIntegration_;
    std::string stream_, obsId_, configHash_;
    std::vector<std::vector<float>> pool_;
    std::deque<std::vector<float>*> free_;
    std::deque<Product> queue_;
    std::mutex mu_;
    std::condition_variable cv_;
    bool stopping_ = false;
    std::thread thread_;
    FileSet primary_, coarse_;
    bool deriveCoarse_ = false;
    std::vector<float> coarseBuf_;
    std::atomic<std::uint64_t> written_{0};
    std::atomic<bool> failed_{false};
};

#define CUDA_OK(expr, what)                                                    \
    do {                                                                       \
        const cudaError_t e_ = (expr);                                         \
        if (e_ != cudaSuccess) {                                               \
            LFT3_ERROR("%s failed: %s", what, cudaGetErrorString(e_));         \
            return 1;                                                          \
        }                                                                      \
    } while (0)

int Run(const Options& o) {
    RingReader ring;
    std::string error;
    if (!ring.attach(o.stream, o.name, o.lossless, o.attachTimeout, error)) {
        LFT3_ERROR("%s", error.c_str());
        return 1;
    }
    const Layout l = ring.layout();
    const RingControl& ctl = ring.control();
    const bool fine = o.mode == "fine";
    const std::uint64_t T = l.spectraPerBlock;
    if (fine && (o.fftLength < 2 || o.fftLength > kFineSpecMaxFftLength || (o.fftLength & (o.fftLength - 1)) ||
                 T % o.fftLength)) {
        LFT3_ERROR("fft-length %u must be a power of two <= %u dividing spectraPerBlock %lu", o.fftLength,
                   kFineSpecMaxFftLength, static_cast<unsigned long>(T));
        return 2;
    }
    const std::uint64_t N = o.integrationBlocks ? o.integrationBlocks
                                                : std::max<std::uint64_t>(1, std::llround(o.integrationS * kSpectraPerSecond / T));
    FineSpecGeometry g;
    g.beams = Layout::kBeams;
    g.channels = static_cast<std::uint32_t>(l.channelCount);
    g.fftLength = fine ? o.fftLength : static_cast<std::uint32_t>(T);
    g.segments = static_cast<std::uint32_t>(T / g.fftLength);
    g.rowLength = static_cast<std::uint32_t>(T);
    g.binsPerOutput = o.binsPerOutput;
    const std::uint64_t channelsOut = fine ? l.channelCount * o.fftLength / o.binsPerOutput : l.channelCount / o.binsPerOutput;
    if ((fine ? l.channelCount * o.fftLength : l.channelCount) % o.binsPerOutput) {
        LFT3_ERROR("bins-per-output %u does not divide the channels", o.binsPerOutput);
        return 2;
    }
    if (o.record) {
        std::error_code ec;
        std::filesystem::create_directories(o.outputDir, ec);
        if (ec) {
            LFT3_ERROR("cannot create %s: %s", o.outputDir.c_str(), ec.message().c_str());
            return 1;
        }
    }
    for (const auto& d : {o.latestDir, o.coarseDir}) {
        std::error_code ec;
        if (!d.empty()) std::filesystem::create_directories(d, ec);
    }

    CUDA_OK(cudaSetDevice(0), "cudaSetDevice");
    cudaStream_t stream;
    CUDA_OK(cudaStreamCreateWithFlags(&stream, cudaStreamNonBlocking), "cudaStreamCreate");
    float* accum = nullptr;
    const std::uint64_t outBytes = Layout::kBeams * channelsOut * sizeof(float);
    CUDA_OK(cudaMalloc(&accum, outBytes), "cudaMalloc");
    float* hostOut = nullptr;
    CUDA_OK(cudaMallocHost(&hostOut, outBytes), "cudaMallocHost");

    Writer writer(o, l, ctl.boardId, ctl.ncoMHz, channelsOut, N * T, ctl);

    // Spectra ring for the live search: one integration per slot, float32 [beam][channel].
    RingWriter specRing;
    const bool publishSpectra = o.spectraRing > 0;
    if (publishSpectra) {
        RingConfig rc;
        rc.stream = o.stream + "-spectra";
        rc.layout = l;
        rc.layout.spectraPerBlock = N * T;
        rc.slots = o.spectraRing;
        rc.boardId = ctl.boardId;
        rc.ncoMHz = o.ncoMHz != 0.0 ? o.ncoMHz : ctl.ncoMHz;
        rc.obsId = ctl.obsId;
        rc.configHash = ctl.configHash;
        rc.backpressure = o.lossless;
        rc.kind = RingKind::Spectra;
        rc.blockBytes = outBytes;
        const std::uint64_t perCoarse = fine ? o.fftLength : 1;
        rc.fineChannelsPerCoarse = static_cast<std::uint32_t>(perCoarse >= o.binsPerOutput ? perCoarse / o.binsPerOutput : 1);
        if (rc.ncoMHz != 0.0) {
            const auto axes = ComputeFineAxes(rc.ncoMHz, l.channelStart, l.channelCount, perCoarse);
            rc.foffMHz = axes.foffMHz * o.binsPerOutput;
            rc.fch1MHz = axes.fch1MHz + 0.5 * (o.binsPerOutput - 1) * axes.foffMHz;
        }
        rc.tsampS = static_cast<double>(N * T) / kSpectraPerSecond;
        if (!specRing.create(rc, error)) {
            LFT3_ERROR("spectra ring: %s", error.c_str());
            return 1;
        }
        specRing.setRunning();
        LFT3_INFO("spectra ring '%s': %lu slots x %.1f MB in GPU memory%s", rc.stream.c_str(),
                  static_cast<unsigned long>(rc.slots), outBytes / 1e6, rc.backpressure ? " (backpressure)" : "");
    }
    LFT3_INFO("%s mode, %s, %lu blocks (%.4f s) per spectrum, %lu channels x %lu beams -> %s", o.mode.c_str(),
              fine ? ("FFT " + std::to_string(o.fftLength) + " (" + std::to_string(kCoarseChannelHz / o.fftLength) + " Hz)").c_str()
                   : "coarse power",
              static_cast<unsigned long>(N), N * T / kSpectraPerSecond, static_cast<unsigned long>(channelsOut),
              static_cast<unsigned long>(Layout::kBeams), o.record ? o.outputDir.c_str() : "not recorded");

    const std::uint64_t packetsPerBlock = l.packetsPerBlock();
    // Start where the reader registered (head at attach): a lossless reader then sees every block from there,
    // even if the capture started producing while this process was still setting up.
    std::uint64_t seq = ring.entry().cursor.load();
    bool inWindow = false, dirty = false, newRun = true;
    std::uint64_t window = 0, blocks = 0, missing = 0, lastEmitted = ~0ULL, lastBlock = 0, restarts = ~0ULL;
    std::uint64_t overruns = 0, read = 0, emitted = 0, skippedLag = 0;
    RingSlotMeta first;
    auto lastLog = std::chrono::steady_clock::now();
    int rc = 0;

    const auto emit = [&]() {
        if (!inWindow) return;
        inWindow = false;
        if (dirty) {
            LFT3_WARN("integration %lu discarded: its data was overwritten while being read",
                      static_cast<unsigned long>(window));
            newRun = true;
            return;
        }
        Product p;
        p.data = writer.acquire();
        p.window = window;
        p.startT = window * N * T;
        p.missing = missing + (N - blocks) * packetsPerBlock;
        p.newRun = newRun || (lastEmitted != ~0ULL && window != lastEmitted + 1);
        p.meta = first;
        std::int64_t specSlot = -1;
        if (publishSpectra && (specSlot = specRing.acquireSlot()) >= 0) {
            cudaMemcpyAsync(specRing.blockPtr(specSlot), accum, outBytes, cudaMemcpyDeviceToDevice, stream);
        }
        cudaMemcpyAsync(hostOut, accum, outBytes, cudaMemcpyDeviceToHost, stream);
        cudaStreamSynchronize(stream);
        std::memcpy(p.data->data(), hostOut, outBytes);
        if (specSlot >= 0) {
            RingSlotMeta m = first;
            m.blockIndex = window;
            m.t0 = p.startT;
            m.missingPackets = p.missing;
            m.flags = (p.missing ? kSlotFlagPartial : 0) | (p.newRun ? kSlotFlagNewRun : 0);
            if (o.syncTimeUnix > 0.0) m.unixTimeAtT0 = o.syncTimeUnix;
            specRing.publish(specSlot, m);
        }
        writer.push(p);
        newRun = false;
        lastEmitted = window;
        emitted++;
        ring.entry().outputs.store(emitted, std::memory_order_relaxed);
    };

    while (!StopRequested().load()) {
        if (publishSpectra) specRing.heartbeat();
        const auto w = ring.wait(seq, 200);
        if (w == RingReader::Wait::Stopped) {
            LFT3_INFO("capture stopped");
            break;
        }
        const auto now = std::chrono::steady_clock::now();
        if (std::chrono::duration<double>(now - lastLog).count() >= 5.0) {
            LFT3_INFO("read %lu blocks, %lu spectra (%lu written), lag %lu blocks, overruns %lu (%lu skipped for lag)",
                      static_cast<unsigned long>(read), static_cast<unsigned long>(emitted),
                      static_cast<unsigned long>(writer.written()), static_cast<unsigned long>(ring.head() - std::min(seq, ring.head())),
                      static_cast<unsigned long>(overruns), static_cast<unsigned long>(skippedLag));
            lastLog = now;
        }
        if (writer.failed()) {
            rc = 1;
            break;
        }
        if (w == RingReader::Wait::Timeout) {
            ring.consumed(seq);
            continue;
        }
        // Too far behind: jump to the oldest block that is safe to read.
        const std::uint64_t oldest = ring.oldestReadable();
        if (seq < oldest) {
            const std::uint64_t lost = oldest - seq;
            overruns += lost;
            skippedLag += lost;
            ring.entry().overruns.fetch_add(lost, std::memory_order_relaxed);
            seq = oldest;
            continue;
        }
        const std::uint64_t t0 = MonotonicNs();
        RingSlotMeta meta;
        const std::uint8_t* data = nullptr;
        if (!ring.begin(seq, meta, data)) {
            overruns++;
            ring.entry().overruns.fetch_add(1, std::memory_order_relaxed);
            seq++;
            continue;
        }
        // Time base: a capture restart or t going backwards starts over.
        if (meta.restarts != restarts || (read > 0 && meta.blockIndex <= lastBlock)) {
            if (restarts != ~0ULL) LFT3_WARN("stream restart (re-sync): new time base");
            inWindow = false;
            newRun = true;
            lastEmitted = ~0ULL;
            restarts = meta.restarts;
        }
        lastBlock = meta.blockIndex;
        const std::uint64_t wnd = meta.blockIndex / N;
        if (inWindow && wnd != window) emit();
        if (!inWindow) {
            if (meta.blockIndex % N != 0) {   // wait for the next integration boundary
                read++;
                ring.consumed(++seq);
                continue;
            }
            inWindow = true;
            dirty = false;
            window = wnd;
            blocks = 0;
            missing = 0;
            first = meta;
            CUDA_OK(cudaMemsetAsync(accum, 0, outBytes, stream), "cudaMemsetAsync");
        }
        const cudaError_t e = fine ? LaunchFineSpec(data, false, accum, g, stream) : LaunchCoarseDetect(data, false, accum, g, stream);
        CUDA_OK(e, "spectrometer kernel launch");
        CUDA_OK(cudaStreamSynchronize(stream), "spectrometer kernel");
        if (!ring.stillValid(seq)) {
            dirty = true;
            overruns++;
            ring.entry().overruns.fetch_add(1, std::memory_order_relaxed);
        }
        blocks++;
        missing += meta.missingPackets;
        read++;
        ring.entry().blocksRead.store(read, std::memory_order_relaxed);
        ring.consumed(++seq, MonotonicNs() - t0);
        if (meta.blockIndex % N == N - 1) emit();
    }
    emit();   // a window cut short by the end of the stream is written with its missing blocks counted
    writer.stop();
    if (publishSpectra) specRing.close();   // readers see "stopped" and can drain what was published
    LFT3_INFO("done: read %lu blocks, %lu spectra, overruns %lu", static_cast<unsigned long>(read),
              static_cast<unsigned long>(emitted), static_cast<unsigned long>(overruns));
    cudaFree(accum);
    cudaFreeHost(hostOut);
    cudaStreamDestroy(stream);
    return writer.failed() ? 1 : rc;
}

}  // namespace
}  // namespace Lft3

int main(int argc, char** argv) {
    using namespace Lft3;
    LogTag() = "spectrometer";
    Args a(argc, argv, {"no-record", "lossless", "help"});
    Options o;
    o.stream = a.str("stream");
    o.mode = a.str("mode", "fine");
    o.name = a.str("name", o.mode == "fine" ? "spectrometer" : "quicklook");
    o.fftLength = static_cast<std::uint32_t>(a.i64("fft-length", 128));
    o.integrationS = a.f64("integration", 1.0);
    o.integrationBlocks = static_cast<std::uint64_t>(a.i64("integration-blocks", 0));
    o.binsPerOutput = static_cast<std::uint32_t>(a.i64("bins-per-output", 1));
    o.outputDir = a.str("output-dir", ".");
    o.prefix = a.str("prefix", "lft3");
    o.beamOffset = static_cast<std::uint32_t>(a.i64("beam-offset", 0));
    o.record = !a.flag("no-record");
    o.latestDir = a.str("latest");
    o.coarseDir = a.str("coarse-dir");
    o.spectraPerFile = static_cast<std::uint64_t>(a.i64("spectra-per-file", 3600));
    o.ncoMHz = a.f64("nco-mhz", 0.0);
    o.syncTimeUnix = a.f64("sync-time-unix", 0.0);
    o.sourceName = a.str("source-name", "LFT3");
    o.telescopeId = static_cast<int>(a.i64("telescope-id", 0));
    o.machineId = static_cast<int>(a.i64("machine-id", 0));
    o.lossless = a.flag("lossless");
    o.attachTimeout = a.f64("attach-timeout", 30.0);
    o.spectraRing = static_cast<std::uint64_t>(a.i64("spectra-ring", 0));
    if (a.has("help") || o.stream.empty() || (o.mode != "fine" && o.mode != "coarse") || o.binsPerOutput == 0 ||
        o.prefix.empty() || o.prefix.find('/') != std::string::npos) {
        std::fputs(kUsage, stderr);
        return 2;
    }
    if (!a.unused().empty()) {
        LFT3_ERROR("unknown option --%s", a.unused().front().c_str());
        return 2;
    }
    LogTag() = o.name + ":" + o.stream;
    InstallStopHandlers();
    return Run(o);
}
