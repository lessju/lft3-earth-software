#include "reduce.hh"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <limits>
#include <stdexcept>

namespace lft3view {

// ---------------------------------------------------------------------------------------------------------
// ThreadPool

ThreadPool::ThreadPool(size_t threads) {
    if (threads == 0) threads = std::max(1u, std::thread::hardware_concurrency());
    for (size_t i = 0; i < threads; ++i) workers_.emplace_back([this, i] { run(i); });
}

ThreadPool::~ThreadPool() {
    {
        std::lock_guard<std::mutex> l(mu_);
        stop_ = true;
    }
    cv_.notify_all();
    for (auto& t : workers_) t.join();
}

void ThreadPool::parallelFor(size_t n, const std::function<void(size_t, size_t)>& fn) {
    if (n == 0) return;
    std::unique_lock<std::mutex> l(mu_);
    fn_ = &fn;
    n_ = n;
    next_ = 0;
    finished_ = 0;
    error_ = nullptr;
    ++generation_;
    cv_.notify_all();
    done_cv_.wait(l, [&] { return finished_ == n_; });
    fn_ = nullptr;
    if (error_) std::rethrow_exception(error_);
}

void ThreadPool::run(size_t worker) {
    size_t seen = 0;
    std::unique_lock<std::mutex> l(mu_);
    for (;;) {
        cv_.wait(l, [&] { return stop_ || (generation_ != seen && fn_ && next_ < n_); });
        if (stop_) return;
        while (fn_ && next_ < n_) {
            size_t task = next_++;
            const auto* fn = fn_;
            l.unlock();
            std::exception_ptr err;
            try {
                (*fn)(task, worker);
            } catch (...) {
                err = std::current_exception();
            }
            l.lock();
            if (err && !error_) error_ = err;
            if (++finished_ == n_) done_cv_.notify_all();
        }
        seen = generation_;
    }
}

// ---------------------------------------------------------------------------------------------------------
// Reducer

namespace {

// Start of output bin j when n inputs are split into m bins (bins differ in size by at most one).
inline size_t binStart(size_t j, size_t n, size_t m) { return static_cast<size_t>((static_cast<unsigned __int128>(j) * n) / m); }

constexpr size_t kMinSegmentChannels = 16384;   // 64 KiB per read: keeps preads large enough for the NVMe

}  // namespace

Reducer::Reducer(size_t threads) : pool_(threads) {
    scratch_.resize(pool_.size());
    rowbuf_.resize(pool_.size());
    sumbuf_.resize(pool_.size());
    maxbuf_.resize(pool_.size());
}

Tile Reducer::reduce(const RowSource& file, const ViewRequest& r, const std::vector<float>* inv) {
    const Header& h = file.header();
    ViewRequest req = r;
    req.t1 = std::min(req.t1, h.nrows);
    req.c1 = std::min(req.c1, h.nchans);
    if (req.t0 >= req.t1 || req.c0 >= req.c1 || req.ifno >= h.nifs || req.width == 0 || req.height == 0)
        throw std::invalid_argument("empty or invalid view");
    const size_t nt = req.t1 - req.t0, nc = req.c1 - req.c0;
    const size_t nfpc = (inv && h.nfpc && inv->size() == h.nfpc) ? h.nfpc : 0;
    if (req.flatten && !nfpc) req.flatten = false;

    Tile tile;
    tile.req = req;
    tile.rows = std::min(req.height, nt);
    tile.cols = std::min(req.width, nc);
    tile.mean.assign(tile.rows * tile.cols, 0.0f);
    tile.max.assign(tile.rows * tile.cols, 0.0f);

    // Tasks: (output row, column segment). Split columns so that there are a few tasks per thread even when
    // only a handful of output rows are requested, but keep each segment's reads reasonably large.
    size_t segs = std::max<size_t>(1, (4 * pool_.size() + tile.rows - 1) / tile.rows);
    segs = std::min({segs, tile.cols, std::max<size_t>(1, nc / kMinSegmentChannels)});
    if (!file.rangedReads()) {
        // Compressed chunks are decoded whole: at most one segment per chunk, so a chunk is decoded at most twice.
        const size_t cw = file.chunkChannels();
        segs = std::min(segs, (req.c1 + cw - 1) / cw - req.c0 / cw);
    }

    const auto t_start = std::chrono::steady_clock::now();
    pool_.parallelFor(tile.rows * segs, [&](size_t task, size_t w) {
        const size_t i = task / segs, s = task % segs;
        const size_t ja = binStart(s, tile.cols, segs), jb = binStart(s + 1, tile.cols, segs);
        const size_t ca = req.c0 + binStart(ja, nc, tile.cols), cb = req.c0 + binStart(jb, nc, tile.cols);
        const size_t ra = req.t0 + binStart(i, nt, tile.rows), rb = req.t0 + binStart(i + 1, nt, tile.rows);

        auto& buf = rowbuf_[w];
        auto& sum = sumbuf_[w];
        auto& mx = maxbuf_[w];
        buf.resize(cb - ca);
        sum.assign(jb - ja, 0.0);
        mx.assign(jb - ja, -std::numeric_limits<float>::infinity());

        for (size_t row = ra; row < rb; ++row) {
            file.readRow(row, req.ifno, ca, cb, buf.data(), scratch_[w]);
            if (req.flatten) {
                const float* resp = inv->data();
                size_t k = ca % nfpc;
                for (size_t c = 0; c < buf.size(); ++c) {
                    buf[c] *= resp[k];
                    if (++k == nfpc) k = 0;
                }
            }
            for (size_t j = ja; j < jb; ++j) {
                const size_t a = req.c0 + binStart(j, nc, tile.cols) - ca;
                const size_t b = req.c0 + binStart(j + 1, nc, tile.cols) - ca;
                const float* x = buf.data();
                float m = -std::numeric_limits<float>::infinity(), acc = 0.0f;
#pragma omp simd reduction(max : m) reduction(+ : acc)
                for (size_t c = a; c < b; ++c) {
                    m = std::max(m, x[c]);
                    acc += x[c];
                }
                mx[j - ja] = std::max(mx[j - ja], m);
                sum[j - ja] += acc;
            }
        }
        const double rows_in_bin = static_cast<double>(rb - ra);
        for (size_t j = ja; j < jb; ++j) {
            const double chans = static_cast<double>(binStart(j + 1, nc, tile.cols) - binStart(j, nc, tile.cols));
            tile.mean[i * tile.cols + j] = static_cast<float>(sum[j - ja] / (rows_in_bin * chans));
            tile.max[i * tile.cols + j] = mx[j - ja];
        }
    });
    tile.seconds = std::chrono::duration<double>(std::chrono::steady_clock::now() - t_start).count();
    tile.bytes_read = nt * file.bytesForRange(req.c0, req.c1);
    return tile;
}

std::vector<float> Reducer::inverseCoarseResponse(const RowSource& file, size_t ifno, size_t nspectra) {
    const Header& h = file.header();
    const size_t nfpc = h.nfpc;
    if (!nfpc || h.nrows == 0 || h.nchans < nfpc) return {};
    const size_t ncoarse = h.nchans / nfpc, nrows = std::min(nspectra, h.nrows);

    // Mean of the first spectra, read in parallel (one row per task).
    std::vector<std::vector<float>> rows(nrows, std::vector<float>(h.nchans));
    pool_.parallelFor(nrows, [&](size_t t, size_t w) { file.readRow(t, ifno, 0, h.nchans, rows[t].data(), scratch_[w]); });
    std::vector<double> x(ncoarse * nfpc, 0.0);
    for (auto& r : rows)
        for (size_t c = 0; c < x.size(); ++c) x[c] += r[c];

    auto median = [](std::vector<double>& v) {
        const size_t n = v.size(), m = n / 2;
        std::nth_element(v.begin(), v.begin() + m, v.end());
        double hi = v[m];
        if (n % 2) return hi;
        double lo = *std::max_element(v.begin(), v.begin() + m);
        return 0.5 * (lo + hi);
    };
    // Normalise every coarse channel by its own median, then take the median over coarse channels per bin.
    std::vector<double> tmp(nfpc);
    for (size_t k = 0; k < ncoarse; ++k) {
        std::copy(x.begin() + k * nfpc, x.begin() + (k + 1) * nfpc, tmp.begin());
        double med = median(tmp);
        for (size_t c = 0; c < nfpc; ++c) x[k * nfpc + c] = med > 0 ? x[k * nfpc + c] / med : 1.0;
    }
    std::vector<double> shape(nfpc), col(ncoarse);
    for (size_t c = 0; c < nfpc; ++c) {
        for (size_t k = 0; k < ncoarse; ++k) col[k] = x[k * nfpc + c];
        shape[c] = median(col);
    }
    double mean = 0;
    for (double s : shape) mean += s;
    mean /= static_cast<double>(nfpc);
    std::vector<float> inv(nfpc);
    for (size_t c = 0; c < nfpc; ++c) inv[c] = shape[c] > 0 ? static_cast<float>(mean / shape[c]) : 1.0f;
    return inv;
}

}  // namespace lft3view
