// lft3-dump: on a trigger, copy a window of raw beam voltages out of the ring and write it as GUPPI RAW.
//
// Triggers arrive as one-line commands on the unix socket /dev/shm/lft3-<stream>-dump.sock:
//     trigger <now|UNIX_TIME> [PRE_S] [POST_S] [LABEL...]      ->  "ok <id> <first_t> <blocks>" | "busy" | "error ..."
//     status                                                     ->  "idle" | "collecting <id>" | "writing <id>"
// The window [time - PRE, time + POST) is first copied from the ring into a pinned host buffer (GPU copy, fast:
// the ring only keeps a few seconds), then written to disk at NVMe speed, one file per beam. Blocks that are no
// longer (or never) in the ring are zero-filled and listed in the sidecar JSON. One trigger at a time.

#include <cmath>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <string>
#include <thread>
#include <vector>

#include <cuda_runtime.h>
#include <poll.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#include "common/packet.hh"
#include "common/ring.hh"
#include "common/util.hh"
#include "dump/guppi.hh"
#include "spectrometer/axes.hh"

namespace Lft3 {
namespace {

const char* kUsage = R"(lft3-dump --stream NAME --output-dir DIR [options]
  --max-seconds S        largest window (pinned host buffer; default 1.0 s = 7.5 GB at full band)
  --pre S --post S       default window around the trigger time (default 0.5, 0.5)
  --prefix NAME --beam-offset N --source-name S --telescope NAME --obs-id S
  --nco-mhz F            exact NCO (default: the ring's)
  --name NAME
)";

struct Options {
    std::string stream, outputDir = ".", prefix = "lft3", sourceName = "LFT3", telescope = "LFT3-Earth", obsId, name = "dump";
    double maxSeconds = 1.0, pre = 0.5, post = 0.5, ncoMHz = 0.0;
    std::uint32_t beamOffset = 0;
};

std::string SocketPath(const std::string& stream) { return "/dev/shm/lft3-" + stream + "-dump.sock"; }

class Dumper {
 public:
    Dumper(const Options& o) : o_(o) {}
    int run();

 private:
    bool handle(const std::string& line, std::string& reply);
    bool collect();
    bool write();

    Options o_;
    RingReader ring_;
    Layout l_;
    std::uint8_t* host_ = nullptr;
    std::uint64_t maxBlocks_ = 0;
    cudaStream_t stream_ = nullptr;
    int listen_ = -1;
    std::string state_ = "idle";
    // the current trigger
    std::uint64_t id_ = 0, b0_ = 0, nBlocks_ = 0;
    double triggerUnix_ = 0.0, unixT0_ = 0.0;
    std::string label_;
    std::vector<std::uint8_t> have_;
    std::vector<RingSlotMeta> meta_;
};

bool Dumper::handle(const std::string& line, std::string& reply) {
    std::vector<std::string> w;
    std::string cur;
    for (char c : line + " ") {
        if (c == ' ' || c == '\n' || c == '\r' || c == '\t') {
            if (!cur.empty()) w.push_back(cur);
            cur.clear();
        } else {
            cur += c;
        }
    }
    if (w.empty()) {
        reply = "error empty command";
        return false;
    }
    if (w[0] == "status") {
        reply = state_ + (state_ == "idle" ? "" : " " + std::to_string(id_));
        return false;
    }
    if (w[0] != "trigger" || w.size() < 2) {
        reply = "error usage: trigger <now|UNIX_TIME> [PRE_S] [POST_S] [LABEL]";
        return false;
    }
    const double pre = w.size() > 2 ? std::stod(w[2]) : o_.pre;
    const double post = w.size() > 3 ? std::stod(w[3]) : o_.post;
    label_.clear();
    for (std::size_t i = 4; i < w.size(); i++) label_ += (label_.empty() ? "" : " ") + w[i];
    std::uint64_t bits = ring_.control().unixTimeAtT0Bits.load();
    std::memcpy(&unixT0_, &bits, sizeof(bits));
    if (unixT0_ == 0.0 || ring_.head() == 0) {
        reply = "error no data in the ring yet";
        return false;
    }
    triggerUnix_ = w[1] == "now" ? UnixNow() : std::stod(w[1]);
    const double tc = (triggerUnix_ - unixT0_) * kSpectraPerSecond;
    const double T = static_cast<double>(l_.spectraPerBlock);
    const double first = std::floor((tc - pre * kSpectraPerSecond) / T);
    const double last = std::ceil((tc + post * kSpectraPerSecond) / T);
    if (pre < 0 || post < 0 || first < 0 || last <= first) {
        reply = "error bad window";
        return false;
    }
    b0_ = static_cast<std::uint64_t>(first);
    nBlocks_ = std::min<std::uint64_t>(static_cast<std::uint64_t>(last - first), maxBlocks_);
    have_.assign(nBlocks_, 0);
    meta_.assign(nBlocks_, RingSlotMeta{});
    id_++;
    reply = "ok " + std::to_string(id_) + " " + std::to_string(b0_ * l_.spectraPerBlock) + " " + std::to_string(nBlocks_);
    return true;
}

// Copies every block of [b0, b0 + n) that is (or becomes) available. Returns when all are in or the window can
// no longer be completed (the newest block is past it, or nothing new arrived for 2 s).
bool Dumper::collect() {
    state_ = "collecting";
    const std::uint64_t T = l_.spectraPerBlock;
    std::uint64_t seq = ring_.oldestReadable();
    std::uint64_t got = 0, lastProgress = MonotonicNs();
    while (got < nBlocks_ && !StopRequested().load()) {
        if (seq < ring_.oldestReadable()) seq = ring_.oldestReadable();
        const auto w = ring_.wait(seq, 100);
        if (w == RingReader::Wait::Stopped) break;
        if (w == RingReader::Wait::Timeout) {
            ring_.consumed(seq);
            if (MonotonicNs() - lastProgress > 2'000'000'000ULL) break;
            continue;
        }
        RingSlotMeta m;
        const std::uint8_t* data = nullptr;
        if (!ring_.begin(seq, m, data)) {
            seq++;
            continue;
        }
        if (m.blockIndex >= b0_ + nBlocks_) break;   // past the window
        if (m.blockIndex >= b0_ && !have_[m.blockIndex - b0_]) {
            const std::uint64_t k = m.blockIndex - b0_;
            cudaMemcpyAsync(host_ + k * l_.bytesPerBlock(), data, l_.bytesPerBlock(), cudaMemcpyDeviceToHost, stream_);
            cudaStreamSynchronize(stream_);
            if (ring_.stillValid(seq)) {
                have_[k] = 1;
                meta_[k] = m;
                got++;
                lastProgress = MonotonicNs();
            }
        }
        ring_.consumed(++seq);
    }
    (void)T;
    return true;
}

bool Dumper::write() {
    state_ = "writing";
    const RingControl& c = ring_.control();
    const double nco = o_.ncoMHz != 0.0 ? o_.ncoMHz : c.ncoMHz;
    const double chanBw = kCoarseChannelHz / 1e6;
    const double c0 = -nco + (static_cast<double>(l_.channelStart) - static_cast<double>(kDcChannel)) * chanBw;
    const double obsBw = chanBw * static_cast<double>(l_.channelCount);
    const double obsFreq = c0 - 0.5 * chanBw + 0.5 * obsBw;
    const double startUnix = unixT0_ + static_cast<double>(b0_ * l_.spectraPerBlock) / kSpectraPerSecond;
    const double mjd = UnixToMjd(startUnix);
    const auto imjd = static_cast<std::int64_t>(std::floor(mjd));
    const double secs = (mjd - static_cast<double>(imjd)) * 86400.0;
    const std::string stamp = UtcStamp(startUnix);
    std::filesystem::create_directories(o_.outputDir);
    std::uint64_t missingBlocks = 0;
    for (auto h : have_) missingBlocks += h ? 0 : 1;
    std::vector<std::uint8_t> zeros;

    for (std::uint64_t beam = 0; beam < Layout::kBeams; beam++) {
        const std::string path = o_.outputDir + "/" + o_.prefix + "_" + stamp + "_beam" +
                                 std::to_string(beam + o_.beamOffset) + ".0000.raw";
        std::ofstream f(path, std::ios::binary);
        if (!f) {
            LFT3_ERROR("cannot create %s", path.c_str());
            return false;
        }
        for (std::uint64_t k = 0; k < nBlocks_; k++) {
            const std::uint64_t t0 = (b0_ + k) * l_.spectraPerBlock;
            GuppiHeader h;
            h.set("BACKEND", std::string("LFT3"));
            h.set("TELESCOP", o_.telescope);
            h.set("SRC_NAME", o_.sourceName);
            h.set("OBSID", o_.obsId.empty() ? std::string(c.obsId) : o_.obsId);
            h.set("OBSFREQ", obsFreq);
            h.set("OBSBW", obsBw);
            h.set("CHAN_BW", chanBw);
            h.set("OBSNCHAN", static_cast<std::int64_t>(l_.channelCount));
            h.set("NPOL", static_cast<std::int64_t>(4));
            h.set("NBITS", static_cast<std::int64_t>(8));
            h.set("TBIN", 1.0 / kCoarseChannelHz);
            h.set("BLOCSIZE", static_cast<std::int64_t>(l_.bytesPerBeam()));
            h.set("DIRECTIO", static_cast<std::int64_t>(0));
            h.set("OVERLAP", static_cast<std::int64_t>(0));
            h.set("NANTS", static_cast<std::int64_t>(1));
            h.set("PKTIDX", static_cast<std::int64_t>(t0));
            h.set("PIPERBLK", static_cast<std::int64_t>(l_.spectraPerBlock));
            h.set("STT_IMJD", imjd);
            h.set("STT_SMJD", static_cast<std::int64_t>(std::floor(secs)));
            h.set("STT_OFFS", secs - std::floor(secs));
            h.set("BEAM_ID", static_cast<std::int64_t>(beam + o_.beamOffset));
            h.set("LFT3STRM", std::string(c.stream));
            h.set("LFT3BRD", static_cast<std::int64_t>(c.boardId));
            h.set("LFT3NCO", nco);
            h.set("LFT3CH0", static_cast<std::int64_t>(l_.channelStart));
            h.set("LFT3T0U", unixT0_);
            h.set("LFT3MISS", static_cast<std::int64_t>(have_[k] ? meta_[k].missingPackets : l_.packetsPerBlock()));
            h.set("LFT3VALD", static_cast<std::int64_t>(have_[k] ? 1 : 0));
            h.set("TRIGID", static_cast<std::int64_t>(id_));
            h.set("TRIGUNIX", triggerUnix_);
            h.set("TRIGLBL", label_.empty() ? std::string("manual") : label_);
            h.set("SOFTWARE", std::string(c.software));
            const std::string hdr = h.render();
            f.write(hdr.data(), static_cast<std::streamsize>(hdr.size()));
            if (have_[k]) {
                f.write(reinterpret_cast<const char*>(host_ + k * l_.bytesPerBlock() + beam * l_.bytesPerBeam()),
                        static_cast<std::streamsize>(l_.bytesPerBeam()));
            } else {
                zeros.resize(l_.bytesPerBeam(), 0);
                f.write(reinterpret_cast<const char*>(zeros.data()), static_cast<std::streamsize>(zeros.size()));
            }
        }
        f.close();
        if (!f) {
            LFT3_ERROR("write failed: %s", path.c_str());
            return false;
        }
        LFT3_INFO("wrote %s (%lu blocks, %lu zero-filled)", path.c_str(), static_cast<unsigned long>(nBlocks_),
                  static_cast<unsigned long>(missingBlocks));
    }
    std::ofstream js(o_.outputDir + "/" + o_.prefix + "_" + stamp + ".json");
    js << "{\"trigger_id\": " << id_ << ", \"trigger_unix\": " << std::to_string(triggerUnix_) << ", \"label\": \""
       << label_ << "\", \"first_t\": " << b0_ * l_.spectraPerBlock << ", \"blocks\": " << nBlocks_
       << ", \"missing_blocks\": " << missingBlocks << ", \"start_unix\": " << std::to_string(startUnix)
       << ", \"stream\": \"" << c.stream << "\"}\n";
    return true;
}

int Dumper::run() {
    std::string error;
    if (!ring_.attach(o_.stream, o_.name, false, 30.0, error)) {
        LFT3_ERROR("%s", error.c_str());
        return 1;
    }
    l_ = ring_.layout();
    maxBlocks_ = std::max<std::uint64_t>(1, static_cast<std::uint64_t>(std::ceil(o_.maxSeconds * kSpectraPerSecond / l_.spectraPerBlock)));
    if (maxBlocks_ + 8 > ring_.slots()) {
        LFT3_WARN("max window %lu blocks is close to the ring size %lu: old blocks may already be gone",
                  static_cast<unsigned long>(maxBlocks_), static_cast<unsigned long>(ring_.slots()));
    }
    cudaSetDevice(0);
    if (cudaStreamCreateWithFlags(&stream_, cudaStreamNonBlocking) != cudaSuccess ||
        cudaMallocHost(&host_, maxBlocks_ * l_.bytesPerBlock()) != cudaSuccess) {
        LFT3_ERROR("cannot allocate %.1f GB of pinned memory for the dump window", maxBlocks_ * l_.bytesPerBlock() / 1e9);
        return 1;
    }
    const std::string path = SocketPath(o_.stream);
    unlink(path.c_str());
    listen_ = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    sockaddr_un addr = {};
    addr.sun_family = AF_UNIX;
    std::strncpy(addr.sun_path, path.c_str(), sizeof(addr.sun_path) - 1);
    if (bind(listen_, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0 || listen(listen_, 4) != 0) {
        LFT3_ERROR("cannot listen on %s", path.c_str());
        return 1;
    }
    chmod(path.c_str(), 0666);
    LFT3_INFO("ready: triggers on %s, window up to %.2f s (%lu blocks, %.1f GB pinned) -> %s", path.c_str(),
              maxBlocks_ * l_.spectraPerBlock / kSpectraPerSecond, static_cast<unsigned long>(maxBlocks_),
              maxBlocks_ * l_.bytesPerBlock() / 1e9, o_.outputDir.c_str());

    while (!StopRequested().load() && !ring_.writerStopped()) {
        ring_.consumed(ring_.head());   // idle: keep the heartbeat, never hold the writer
        pollfd p{listen_, POLLIN, 0};
        if (poll(&p, 1, 200) <= 0) continue;
        const int client = accept4(listen_, nullptr, nullptr, SOCK_CLOEXEC);
        if (client < 0) continue;
        char buf[512] = {};
        const ssize_t n = recv(client, buf, sizeof(buf) - 1, 0);
        std::string reply;
        const bool go = n > 0 && handle(std::string(buf, static_cast<std::size_t>(n)), reply);
        reply += "\n";
        send(client, reply.data(), reply.size(), MSG_NOSIGNAL);
        close(client);
        if (!go) continue;
        LFT3_INFO("trigger %lu at %.3f (%s): blocks %lu..%lu", static_cast<unsigned long>(id_), triggerUnix_,
                  label_.c_str(), static_cast<unsigned long>(b0_), static_cast<unsigned long>(b0_ + nBlocks_ - 1));
        const std::uint64_t t0 = MonotonicNs();
        collect();
        const std::uint64_t t1 = MonotonicNs();
        if (!write()) LFT3_ERROR("dump %lu failed", static_cast<unsigned long>(id_));
        ring_.entry().outputs.fetch_add(1);
        LFT3_INFO("dump %lu done: collected in %.2f s, written in %.2f s", static_cast<unsigned long>(id_),
                  (t1 - t0) / 1e9, (MonotonicNs() - t1) / 1e9);
        state_ = "idle";
    }
    close(listen_);
    unlink(path.c_str());
    cudaFreeHost(host_);
    cudaStreamDestroy(stream_);
    return 0;
}

}  // namespace
}  // namespace Lft3

int main(int argc, char** argv) {
    using namespace Lft3;
    LogTag() = "dump";
    Args a(argc, argv, {"help"});
    Options o;
    o.stream = a.str("stream");
    o.outputDir = a.str("output-dir", ".");
    o.prefix = a.str("prefix", "lft3");
    o.sourceName = a.str("source-name", "LFT3");
    o.telescope = a.str("telescope", "LFT3-Earth");
    o.obsId = a.str("obs-id");
    o.name = a.str("name", "dump");
    o.maxSeconds = a.f64("max-seconds", 1.0);
    o.pre = a.f64("pre", 0.5);
    o.post = a.f64("post", 0.5);
    o.ncoMHz = a.f64("nco-mhz", 0.0);
    o.beamOffset = static_cast<std::uint32_t>(a.i64("beam-offset", 0));
    if (a.has("help") || o.stream.empty()) {
        std::fputs(kUsage, stderr);
        return 2;
    }
    if (!a.unused().empty()) {
        LFT3_ERROR("unknown option --%s", a.unused().front().c_str());
        return 2;
    }
    LogTag() = o.name + ":" + o.stream;
    InstallStopHandlers();
    Dumper d(o);
    return d.run();
}
