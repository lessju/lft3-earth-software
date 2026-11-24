// lft3-view: browser viewer for FBH5 spectra.
//
//   lft3-view [--data-dir /opt/lft3arth/data] [--scan DIR]... [--bind 0.0.0.0] [--port 8687] [--threads N]
//             [--web-dir DIR] [--triggers-log FILE] [--rfi-file FILE] [--no-token]
//
// Only the --scan directories (relative to --data-dir, default "spectra") are searched for *.h5, so benchmark
// and reference files elsewhere in the data dir are not listed. --scan . lists everything.
//
// Serves the page (web/index.html) and a small API. All data work (parallel reads, max/mean reduction to the
// requested screen size, coarse-channel flattening) happens here; the browser only colours and draws.
//
// Files are grouped into runs: the pipeline's hourly files that continue each other exactly are shown as one
// timeline (fbh5_sequence.hh). A run is named by its first file; any member file's path also finds it.
//
//   GET /api/files                         runs, newest first: id, files, rows, size
//   GET /api/meta?file=ID                  header, attributes, per-spectrum missing packets, member files
//   GET /api/overlays                      trigger log lines and the known-RFI list (config/known_rfi.json)
//   GET /api/tile?file=ID&t0&t1&c0&c1&w&h[&ifno][&stat=max|mean][&flatten=1][&format=f32|u16db]
//        -> geometry in the X-Tile header (JSON), body:
//           f32   (default) float32 [rows][cols] little-endian, linear power
//           u16db dB quantised to 16 bits over the tile's own range [db_lo, db_hi] (given in X-Tile), sent as two
//                 planes: all high bytes, then all low bytes. q = 0 means no data (power <= 0); otherwise
//                 dB = db_lo + (q - 1) * (db_hi - db_lo) / 65534. ~0.001 dB steps, ~2-3x smaller than f32.
//        Both are gzip-encoded (Content-Encoding) when the client accepts it.
//
// Access needs ?token=... (printed at start-up) unless --no-token. Files outside --data-dir are refused.

#include <httplib.h>
#include <zlib.h>

#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <map>
#include <memory>
#include <mutex>
#include <random>
#include <sstream>
#include <string>
#include <sys/stat.h>
#include <unistd.h>

#include "fbh5_file.hh"
#include "fbh5_sequence.hh"
#include "reduce.hh"

#ifndef LFT3VIEW_WEB_DIR
#define LFT3VIEW_WEB_DIR "web"
#endif
#ifndef LFT3VIEW_RFI_FILE
#define LFT3VIEW_RFI_FILE "config/known_rfi.json"
#endif

namespace fs = std::filesystem;
using namespace lft3view;

namespace {

struct Options {
    std::string data_dir = "/opt/lft3arth/data";
    std::vector<std::string> scan;         // relative to data_dir; default {"spectra"}
    std::string bind = "0.0.0.0";
    int port = 8687;
    size_t threads = 0;
    std::string web_dir = LFT3VIEW_WEB_DIR;
    std::string triggers_log;              // default <data_dir>/triggers.log
    std::string rfi_file = LFT3VIEW_RFI_FILE;
    bool token = true;
};

std::string randomToken() {
    std::random_device rd;
    std::string t;
    const char* hex = "0123456789abcdef";
    for (int i = 0; i < 24; ++i) t += hex[rd() % 16];
    return t;
}

std::string jsonStr(const std::string& s) {
    std::string o = "\"";
    for (char ch : s) {
        auto c = static_cast<unsigned char>(ch);
        if (c == '"' || c == '\\') {
            o += '\\';
            o += ch;
        } else if (c < 0x20) {
            char b[8];
            std::snprintf(b, sizeof b, "\\u%04x", c);
            o += b;
        } else {
            o += ch;
        }
    }
    return o + "\"";
}

std::string num(double v) {
    char b[32];
    std::snprintf(b, sizeof b, "%.17g", v);
    return b;
}

// Open files, re-opened when their size or mtime changes (a file being written grows). Old objects stay alive
// while a request still holds them.
class FileCache {
public:
    explicit FileCache(std::string root) : root_(fs::weakly_canonical(root).string()) {}
    const std::string& root() const { return root_; }

    // Resolve a path relative to the data dir; refuse anything outside it or not *.h5.
    std::string resolve(const std::string& rel) const {
        fs::path p = fs::weakly_canonical(fs::path(root_) / rel);
        std::string s = p.string();
        if (s.compare(0, root_.size(), root_) != 0 || (s.size() > root_.size() && s[root_.size()] != '/'))
            throw std::invalid_argument("file outside the data directory");
        if (p.extension() != ".h5") throw std::invalid_argument("not an .h5 file");
        return s;
    }

    struct Entry {
        std::shared_ptr<Fbh5File> file;
        std::map<size_t, std::vector<float>> inv_response;   // per IF, computed on first use
        std::string error;
        off_t size = -1;
        int64_t mtime_ns = -1;
    };

    std::shared_ptr<Entry> get(const std::string& rel) {
        std::string path = resolve(rel);
        struct stat st {};
        if (::stat(path.c_str(), &st) != 0) throw std::invalid_argument("no such file: " + rel);
        int64_t mt = static_cast<int64_t>(st.st_mtim.tv_sec) * 1000000000 + st.st_mtim.tv_nsec;
        std::lock_guard<std::mutex> l(mu_);
        auto& e = entries_[path];
        if (!e || e->size != st.st_size || e->mtime_ns != mt) {
            auto ne = std::make_shared<Entry>();
            ne->size = st.st_size;
            ne->mtime_ns = mt;
            try {
                ne->file = std::make_shared<Fbh5File>(path);
            } catch (const std::exception& ex) {
                ne->error = ex.what();   // not FBH5 (e.g. a board capture): remembered until the file changes
            }
            e = ne;
        }
        if (!e->file) throw std::invalid_argument(e->error);
        return e;
    }

    std::shared_ptr<Entry> tryGet(const std::string& rel) {
        try {
            return get(rel);
        } catch (const std::exception&) {
            return nullptr;
        }
    }

private:
    std::string root_;
    std::mutex mu_;
    std::map<std::string, std::shared_ptr<Entry>> entries_;
};

size_t qnum(const httplib::Request& r, const char* k, size_t dflt) {
    if (!r.has_param(k)) return dflt;
    return static_cast<size_t>(std::stoull(r.get_param_value(k)));
}

// Runs of contiguous files, rebuilt at most every 2 s (a listing stats every *.h5 and reopens changed ones).
class Catalog {
public:
    Catalog(FileCache& cache, std::vector<std::string> scan) : cache_(cache), scan_(std::move(scan)) {}

    struct Run {
        std::string id;                      // first file, relative to the data dir
        std::shared_ptr<Fbh5Sequence> seq;
        uintmax_t size = 0;
        int64_t mtime = 0;                   // newest member
    };

    std::shared_ptr<const Run> find(const std::string& id_or_member) {
        for (int attempt = 0; attempt < 2; ++attempt) {
            refresh(attempt > 0);   // not found: maybe a file that appeared since the last listing
            std::lock_guard<std::mutex> l(mu_);
            auto it = by_member_.find(id_or_member);
            if (it != by_member_.end()) return it->second;
        }
        throw std::invalid_argument("no such run or FBH5 file: " + id_or_member);
    }

    std::string listJson() {
        refresh(true);
        std::lock_guard<std::mutex> l(mu_);
        std::string o = "[";
        for (size_t i = 0; i < runs_.size(); ++i) {
            const auto& r = *runs_[i];
            const Header& h = r.seq->header();
            o += std::string(i ? "," : "") + "{\"id\":" + jsonStr(r.id) + ",\"files\":[";
            for (size_t k = 0; k < r.seq->names().size(); ++k) o += (k ? "," : "") + jsonStr(r.seq->names()[k]);
            o += "],\"nrows\":" + std::to_string(h.nrows) + ",\"size\":" + std::to_string(r.size) +
                 ",\"mtime\":" + std::to_string(r.mtime) + ",\"tstart\":" + num(h.tstart) +
                 ",\"tsamp\":" + num(h.tsamp) + ",\"nchans\":" + std::to_string(h.nchans) +
                 ",\"foff\":" + num(h.foff) + "}";
        }
        return o + "]";
    }

private:
    void refresh(bool force) {
        std::lock_guard<std::mutex> rl(refresh_mu_);
        const auto now = std::chrono::steady_clock::now();
        if (!force && built_ && now - built_at_ < std::chrono::seconds(2)) return;
        if (force && built_ && now - built_at_ < std::chrono::milliseconds(500)) return;

        struct F {
            std::string rel;
            std::shared_ptr<FileCache::Entry> e;
            RunName name;
        };
        std::vector<F> files;
        std::error_code ec;
        const std::string& root = cache_.root();
        for (const auto& dir : scan_) {
            const fs::path top = fs::path(root) / dir;
            if (!fs::is_directory(top, ec)) continue;
            for (auto it = fs::recursive_directory_iterator(top, fs::directory_options::skip_permission_denied, ec);
                 it != fs::recursive_directory_iterator(); it.increment(ec)) {
                if (ec) break;
                if (!it->is_regular_file(ec) || it->path().extension() != ".h5") continue;
                std::string rel = fs::relative(it->path(), root).string();
                if (auto e = cache_.tryGet(rel)) files.push_back({rel, e, parseRunName(rel)});
            }
        }
        // group by run name, in time order; unparsable names are runs of their own
        std::sort(files.begin(), files.end(), [](const F& a, const F& b) {
            if (a.name.group != b.name.group) return a.name.group < b.name.group;
            return a.name.stamp != b.name.stamp ? a.name.stamp < b.name.stamp : a.rel < b.rel;
        });
        std::vector<std::shared_ptr<const Run>> runs;
        std::map<std::string, std::shared_ptr<const Run>> by_member;
        for (size_t i = 0; i < files.size();) {
            size_t j = i + 1;
            if (files[i].name.ok)
                while (j < files.size() && files[j].name.ok && files[j].name.group == files[i].name.group &&
                       continues(*files[j - 1].e->file, *files[j].e->file))
                    ++j;
            std::vector<std::shared_ptr<Fbh5File>> fv;
            std::vector<std::string> names;
            auto run = std::make_shared<Run>();
            for (size_t k = i; k < j; ++k) {
                fv.push_back(files[k].e->file);
                names.push_back(files[k].rel);
                run->size += static_cast<uintmax_t>(files[k].e->size);
                run->mtime = std::max(run->mtime, files[k].e->mtime_ns / 1000000000);
            }
            run->id = names.front();
            run->seq = std::make_shared<Fbh5Sequence>(std::move(fv), names);
            for (const auto& n : names) by_member[n] = run;
            runs.push_back(run);
            i = j;
        }
        std::sort(runs.begin(), runs.end(), [](const auto& a, const auto& b) { return a->mtime > b->mtime; });
        std::lock_guard<std::mutex> l(mu_);
        runs_ = std::move(runs);
        by_member_ = std::move(by_member);
        built_ = true;
        built_at_ = now;
    }

    FileCache& cache_;
    std::vector<std::string> scan_;   // directories searched, relative to the data dir
    std::mutex refresh_mu_, mu_;
    bool built_ = false;
    std::chrono::steady_clock::time_point built_at_;
    std::vector<std::shared_ptr<const Run>> runs_;
    std::map<std::string, std::shared_ptr<const Run>> by_member_;
};

std::string metaJson(const Catalog::Run& run) {
    const Fbh5Sequence& q = *run.seq;
    const Header& h = q.header();
    std::string o = "{";
    o += "\"id\":" + jsonStr(run.id);
    o += ",\"layout\":" + jsonStr(q.files().front()->layoutDescription());
    o += ",\"nrows\":" + std::to_string(h.nrows) + ",\"nifs\":" + std::to_string(h.nifs) +
         ",\"nchans\":" + std::to_string(h.nchans) + ",\"nfpc\":" + std::to_string(h.nfpc);
    o += ",\"fch1\":" + num(h.fch1) + ",\"foff\":" + num(h.foff) + ",\"tsamp\":" + num(h.tsamp) +
         ",\"tstart\":" + num(h.tstart);
    o += ",\"source_name\":" + jsonStr(h.source_name);
    o += ",\"attrs\":" + h.attrs_json;
    o += ",\"files\":[";
    for (size_t k = 0; k < q.files().size(); ++k)
        o += std::string(k ? "," : "") + "{\"path\":" + jsonStr(q.names()[k]) + ",\"row0\":" +
             std::to_string(q.firstRows()[k]) + ",\"nrows\":" + std::to_string(q.files()[k]->header().nrows) + "}";
    o += "],\"missing\":[";
    const auto& m = q.missingPackets();
    for (size_t i = 0; i < m.size(); ++i) o += (i ? "," : "") + std::to_string(m[i]);
    return o + "]}";
}

std::string readSmallFile(const std::string& path, size_t max_bytes) {
    std::ifstream in(path, std::ios::binary);
    if (!in) return "";
    std::string s(max_bytes, '\0');
    in.read(s.data(), static_cast<std::streamsize>(max_bytes));
    s.resize(static_cast<size_t>(in.gcount()));
    return s;
}

// Last lines of the trigger log as a JSON array of strings ("<ISO time> <text>"; the page parses the time).
std::string triggersJson(const std::string& path) {
    std::ifstream in(path);
    std::vector<std::string> lines;
    for (std::string line; std::getline(in, line);)
        if (!line.empty()) lines.push_back(line);
    const size_t first = lines.size() > 20000 ? lines.size() - 20000 : 0;
    std::string o = "[";
    for (size_t i = first; i < lines.size(); ++i) o += (i > first ? "," : "") + jsonStr(lines[i]);
    return o + "]";
}

// float32 linear power -> u16db planes (see the header comment). Returns db_lo/db_hi.
std::string encodeU16Db(const std::vector<float>& v, double& db_lo, double& db_hi) {
    const size_t n = v.size();
    std::vector<float> db(n);
    float lo = INFINITY, hi = -INFINITY;
    for (size_t i = 0; i < n; ++i) {
        db[i] = v[i] > 0.0f ? 10.0f * std::log10(v[i]) : NAN;
        if (db[i] < lo) lo = db[i];   // NaN compares false
        if (db[i] > hi) hi = db[i];
    }
    if (!(hi >= lo)) lo = hi = 0.0f;
    if (hi == lo) hi = lo + 1.0f;
    const double scale = 65534.0 / (static_cast<double>(hi) - lo);
    std::string out(2 * n, '\0');
    auto* h = reinterpret_cast<uint8_t*>(out.data());
    uint8_t* l = h + n;
    for (size_t i = 0; i < n; ++i) {
        unsigned q = 0;
        if (db[i] == db[i]) q = 1 + static_cast<unsigned>(std::lround((db[i] - lo) * scale));
        if (q > 65535) q = 65535;
        h[i] = static_cast<uint8_t>(q >> 8);
        l[i] = static_cast<uint8_t>(q & 0xFF);
    }
    db_lo = lo;
    db_hi = hi;
    return out;
}

// gzip (one member). For u16db the high-byte plane is run-length coded and the low-byte plane, which is noise,
// is stored, so this costs about a memcpy for the half that does not compress.
std::string gzipBody(const std::string& in, size_t compress_bytes) {
    z_stream z{};
    if (deflateInit2(&z, 1, Z_DEFLATED, 15 + 16, 8, Z_RLE) != Z_OK) throw std::runtime_error("deflateInit2 failed");
    std::string out(deflateBound(&z, in.size()) + 64, '\0');
    z.next_out = reinterpret_cast<Bytef*>(out.data());
    z.avail_out = static_cast<uInt>(out.size());
    z.next_in = reinterpret_cast<Bytef*>(const_cast<char*>(in.data()));
    z.avail_in = static_cast<uInt>(compress_bytes);
    int rc = deflate(&z, Z_NO_FLUSH);
    if (rc == Z_OK && compress_bytes < in.size()) {
        // flush what is pending, then switch to stored blocks for the rest
        rc = deflateParams(&z, 0, Z_DEFAULT_STRATEGY);
        z.avail_in += static_cast<uInt>(in.size() - compress_bytes);
    }
    if (rc == Z_OK || rc == Z_BUF_ERROR) rc = deflate(&z, Z_FINISH);
    if (rc != Z_STREAM_END) {
        deflateEnd(&z);
        throw std::runtime_error("gzip failed");
    }
    out.resize(z.total_out);
    deflateEnd(&z);
    return out;
}

}  // namespace

int main(int argc, char** argv) {
    Options opt;
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        auto next = [&]() -> std::string {
            if (i + 1 >= argc) throw std::invalid_argument(a + " needs a value");
            return argv[++i];
        };
        try {
            if (a == "--data-dir") opt.data_dir = next();
            else if (a == "--scan") opt.scan.push_back(next());
            else if (a == "--bind") opt.bind = next();
            else if (a == "--port") opt.port = std::stoi(next());
            else if (a == "--threads") opt.threads = std::stoul(next());
            else if (a == "--web-dir") opt.web_dir = next();
            else if (a == "--triggers-log") opt.triggers_log = next();
            else if (a == "--rfi-file") opt.rfi_file = next();
            else if (a == "--no-token") opt.token = false;
            else if (a == "-h" || a == "--help") {
                std::printf("usage: lft3-view [--data-dir DIR] [--scan DIR]... [--bind ADDR] [--port N] [--threads N] "
                            "[--web-dir DIR] [--triggers-log FILE] [--rfi-file FILE] [--no-token]\n");
                return 0;
            } else {
                throw std::invalid_argument("unknown option " + a);
            }
        } catch (const std::exception& e) {
            std::fprintf(stderr, "lft3-view: %s\n", e.what());
            return 2;
        }
    }

    if (opt.triggers_log.empty()) opt.triggers_log = (fs::path(opt.data_dir) / "triggers.log").string();
    FileCache cache(opt.data_dir);
    if (opt.scan.empty()) opt.scan = {"spectra"};
    Catalog catalog(cache, opt.scan);
    Reducer reducer(opt.threads);
    std::mutex reduce_mu;   // one reduction at a time; each already uses every core
    const std::string token = opt.token ? randomToken() : "";

    httplib::Server srv;
    srv.set_pre_routing_handler([&](const httplib::Request& req, httplib::Response& res) {
        if (token.empty() || req.path.rfind("/api/", 0) != 0) return httplib::Server::HandlerResponse::Unhandled;
        if (req.get_param_value("token") == token || req.get_header_value("X-Token") == token)
            return httplib::Server::HandlerResponse::Unhandled;
        res.status = 403;
        res.set_content("missing or wrong token", "text/plain");
        return httplib::Server::HandlerResponse::Handled;
    });
    srv.set_exception_handler([](const httplib::Request&, httplib::Response& res, std::exception_ptr ep) {
        std::string msg = "error";
        try {
            std::rethrow_exception(ep);
        } catch (const std::invalid_argument& e) {
            res.status = 400;
            msg = e.what();
        } catch (const std::out_of_range& e) {
            res.status = 400;
            msg = e.what();
        } catch (const std::exception& e) {
            res.status = 500;
            msg = e.what();
        }
        res.set_content(msg, "text/plain");
    });

    srv.Get("/", [&](const httplib::Request&, httplib::Response& res) {
        std::ifstream in(opt.web_dir + "/index.html", std::ios::binary);
        if (!in) {
            res.status = 500;
            res.set_content("index.html not found in " + opt.web_dir, "text/plain");
            return;
        }
        std::stringstream ss;
        ss << in.rdbuf();
        res.set_header("Cache-Control", "no-store");
        res.set_content(ss.str(), "text/html; charset=utf-8");
    });

    srv.Get("/api/files", [&](const httplib::Request&, httplib::Response& res) {
        res.set_content(catalog.listJson(), "application/json");
    });

    srv.Get("/api/meta", [&](const httplib::Request& req, httplib::Response& res) {
        res.set_content(metaJson(*catalog.find(req.get_param_value("file"))), "application/json");
    });

    srv.Get("/api/overlays", [&](const httplib::Request&, httplib::Response& res) {
        std::string rfi = readSmallFile(opt.rfi_file, 1 << 20);
        res.set_header("Cache-Control", "no-store");
        res.set_content("{\"triggers_log\":" + jsonStr(opt.triggers_log) + ",\"triggers\":" +
                            triggersJson(opt.triggers_log) + ",\"rfi_file\":" + jsonStr(opt.rfi_file) +
                            ",\"known_rfi\":" + (rfi.empty() ? std::string("null") : rfi) + "}",
                        "application/json");
    });

    srv.Get("/api/tile", [&](const httplib::Request& req, httplib::Response& res) {
        auto run = catalog.find(req.get_param_value("file"));
        const Fbh5Sequence& seq = *run->seq;
        const Header& h = seq.header();
        ViewRequest q;
        q.ifno = qnum(req, "ifno", 0);
        q.t0 = qnum(req, "t0", 0);
        q.t1 = qnum(req, "t1", h.nrows);
        q.c0 = qnum(req, "c0", 0);
        q.c1 = qnum(req, "c1", h.nchans);
        q.width = std::min<size_t>(qnum(req, "w", 2048), 8192);
        q.height = std::min<size_t>(qnum(req, "h", 1024), 8192);
        q.flatten = req.get_param_value("flatten") == "1";
        const bool want_max = req.get_param_value("stat") != "mean";

        Tile t;
        {
            std::lock_guard<std::mutex> l(reduce_mu);
            const std::vector<float>* inv = nullptr;
            if (q.flatten && h.nfpc) {
                // from the first spectra of the run, i.e. of its first file: cache it there
                auto e = cache.get(seq.names().front());
                auto it = e->inv_response.find(q.ifno);
                if (it == e->inv_response.end())
                    it = e->inv_response.emplace(q.ifno, reducer.inverseCoarseResponse(*e->file, q.ifno)).first;
                inv = &it->second;
            }
            t = reducer.reduce(seq, q, inv);
        }
        const auto& v = want_max ? t.max : t.mean;
        const bool u16 = req.get_param_value("format") == "u16db";
        const bool gz = req.get_header_value("Accept-Encoding").find("gzip") != std::string::npos;
        const auto e0 = std::chrono::steady_clock::now();
        double db_lo = 0, db_hi = 0;
        std::string body = u16 ? encodeU16Db(v, db_lo, db_hi)
                               : std::string(reinterpret_cast<const char*>(v.data()), v.size() * sizeof(float));
        const size_t raw_bytes = v.size() * sizeof(float);
        if (gz) body = gzipBody(body, u16 ? body.size() / 2 : body.size());
        const double encode_s = std::chrono::duration<double>(std::chrono::steady_clock::now() - e0).count();
        std::string geom = "{\"t0\":" + std::to_string(t.req.t0) + ",\"t1\":" + std::to_string(t.req.t1) +
                           ",\"c0\":" + std::to_string(t.req.c0) + ",\"c1\":" + std::to_string(t.req.c1) +
                           ",\"rows\":" + std::to_string(t.rows) + ",\"cols\":" + std::to_string(t.cols) +
                           ",\"nrows\":" + std::to_string(h.nrows) + ",\"flatten\":" + (t.req.flatten ? "true" : "false") +
                           ",\"seconds\":" + num(t.seconds) + ",\"bytes\":" + std::to_string(t.bytes_read) +
                           ",\"format\":\"" + (u16 ? "u16db" : "f32") + "\",\"db_lo\":" + num(db_lo) +
                           ",\"db_hi\":" + num(db_hi) + ",\"encode_seconds\":" + num(encode_s) +
                           ",\"f32_bytes\":" + std::to_string(raw_bytes) + ",\"wire_bytes\":" +
                           std::to_string(body.size()) + "}";
        res.set_header("X-Tile", geom);
        res.set_header("Cache-Control", "no-store");
        res.set_header("Vary", "Accept-Encoding");
        if (gz) res.set_header("Content-Encoding", "gzip");
        res.set_content(std::move(body), "application/octet-stream");
    });

    char host[256] = "localhost";
    ::gethostname(host, sizeof host);
    std::string scanned;
    for (const auto& d : opt.scan) scanned += (scanned.empty() ? "" : ", ") + d;
    std::cout << "lft3-view: data " << cache.root() << " (scanning " << scanned << "), " << reducer.threads() << " threads\n"
              << "  open http://" << (opt.bind == "0.0.0.0" ? std::string(host) : opt.bind) << ":" << opt.port << "/"
              << (token.empty() ? "" : "?token=" + token) << std::endl;
    if (!srv.listen(opt.bind, opt.port)) {
        std::cerr << "lft3-view: cannot listen on " << opt.bind << ":" << opt.port << "\n";
        return 1;
    }
    return 0;
}
