#ifndef LFT3_COMMON_UTIL_HH
#define LFT3_COMMON_UTIL_HH

// Small helpers shared by the runtime executables: logging, command-line options, clocks, thread pinning
// and stop signals. No dependencies beyond the C++ standard library and POSIX.

#include <atomic>
#include <chrono>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <ctime>
#include <map>
#include <stdexcept>
#include <string>
#include <vector>

#include <pthread.h>
#include <sched.h>
#include <signal.h>

namespace Lft3 {

// --- logging: "<UTC ISO time> <tag> <message>" on stdout (the orchestrator captures it) ---------------
inline std::string& LogTag() {
    static std::string tag = "lft3";
    return tag;
}

__attribute__((format(printf, 2, 3))) inline void Log(const char* level, const char* fmt, ...) {
    timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    tm t;
    gmtime_r(&ts.tv_sec, &t);
    char stamp[64];
    std::snprintf(stamp, sizeof(stamp), "%04d-%02d-%02dT%02d:%02d:%02d.%03ldZ", t.tm_year + 1900, t.tm_mon + 1,
                  t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec, ts.tv_nsec / 1000000);
    char msg[2048];
    va_list ap;
    va_start(ap, fmt);
    std::vsnprintf(msg, sizeof(msg), fmt, ap);
    va_end(ap);
    std::fprintf(stdout, "%s [%s] %s: %s\n", stamp, LogTag().c_str(), level, msg);
    std::fflush(stdout);
}
#define LFT3_INFO(...) ::Lft3::Log("INFO", __VA_ARGS__)
#define LFT3_WARN(...) ::Lft3::Log("WARN", __VA_ARGS__)
#define LFT3_ERROR(...) ::Lft3::Log("ERROR", __VA_ARGS__)

// --- clocks ---------------------------------------------------------------------------------------------
inline std::uint64_t MonotonicNs() {
    timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return static_cast<std::uint64_t>(ts.tv_sec) * 1000000000ULL + static_cast<std::uint64_t>(ts.tv_nsec);
}
inline double UnixNow() {
    return std::chrono::duration<double>(std::chrono::system_clock::now().time_since_epoch()).count();
}

inline void PinCurrentThread(int core) {
    if (core < 0) return;
    cpu_set_t set;
    CPU_ZERO(&set);
    CPU_SET(core, &set);
    pthread_setaffinity_np(pthread_self(), sizeof(set), &set);
}

// --- stop on SIGINT / SIGTERM ------------------------------------------------------------------------------
inline std::atomic<bool>& StopRequested() {
    static std::atomic<bool> stop{false};
    return stop;
}
inline void InstallStopHandlers() {
    struct sigaction sa = {};
    sa.sa_handler = [](int) { StopRequested().store(true); };
    sigemptyset(&sa.sa_mask);
    sigaction(SIGINT, &sa, nullptr);
    sigaction(SIGTERM, &sa, nullptr);
    signal(SIGPIPE, SIG_IGN);
}

// --- command line: --key value | --flag ---------------------------------------------------------------------
class Args {
 public:
    Args(int argc, char** argv, const std::vector<std::string>& flags = {}) {
        for (int i = 1; i < argc; i++) {
            std::string a = argv[i];
            if (a.rfind("--", 0) != 0) {
                positional_.push_back(a);
                continue;
            }
            a = a.substr(2);
            const auto eq = a.find('=');
            if (eq != std::string::npos) {
                values_[a.substr(0, eq)] = a.substr(eq + 1);
            } else if (IsFlag(flags, a) || i + 1 >= argc) {
                values_[a] = "1";
            } else {
                values_[a] = argv[++i];
            }
        }
    }
    bool has(const std::string& k) const { return values_.count(k) != 0; }
    std::string str(const std::string& k, const std::string& def = "") const {
        used_[k] = true;
        auto it = values_.find(k);
        return it == values_.end() ? def : it->second;
    }
    std::int64_t i64(const std::string& k, std::int64_t def) const {
        const std::string v = str(k);
        return v.empty() ? def : std::stoll(v, nullptr, 0);
    }
    double f64(const std::string& k, double def) const {
        const std::string v = str(k);
        return v.empty() ? def : std::stod(v);
    }
    bool flag(const std::string& k) const {
        const std::string v = str(k);
        return !v.empty() && v != "0" && v != "false";
    }
    // Options given but never read: typos. Call after reading everything.
    std::vector<std::string> unused() const {
        std::vector<std::string> out;
        for (const auto& [k, v] : values_) {
            if (!used_.count(k)) out.push_back(k);
        }
        return out;
    }
    const std::vector<std::string>& positional() const { return positional_; }

 private:
    static bool IsFlag(const std::vector<std::string>& flags, const std::string& k) {
        for (const auto& f : flags) {
            if (f == k) return true;
        }
        return false;
    }
    std::map<std::string, std::string> values_;
    mutable std::map<std::string, bool> used_;
    std::vector<std::string> positional_;
};

// Parses "[0, 0, 1, 1]" or "0,0,1,1" into up to N small integers.
template <typename T, std::size_t N>
inline bool ParseIntList(const std::string& s, T (&out)[N]) {
    std::size_t n = 0;
    std::string cur;
    for (char c : s + ",") {
        if ((c >= '0' && c <= '9') || c == '-') {
            cur += c;
        } else if (!cur.empty()) {
            if (n >= N) return false;
            out[n++] = static_cast<T>(std::stoi(cur));
            cur.clear();
        }
    }
    return n == N;
}

}  // namespace Lft3

#endif  // LFT3_COMMON_UTIL_HH
