// Reduce a time x frequency window of an FBH5 file to (at most) a screen-sized tile, max and mean per pixel,
// reading the file in parallel on all cores.
#pragma once

#include <condition_variable>
#include <exception>
#include <cstddef>
#include <functional>
#include <mutex>
#include <thread>
#include <vector>

#include "fbh5_file.hh"

namespace lft3view {

// Fixed worker pool. parallelFor(n, fn) runs fn(task, worker) for task in [0, n) and returns when all are done.
class ThreadPool {
public:
    explicit ThreadPool(size_t threads);
    ~ThreadPool();
    size_t size() const { return workers_.size(); }
    void parallelFor(size_t n, const std::function<void(size_t task, size_t worker)>& fn);

private:
    void run(size_t worker);
    std::vector<std::thread> workers_;
    std::mutex mu_;
    std::condition_variable cv_, done_cv_;
    const std::function<void(size_t, size_t)>* fn_ = nullptr;
    size_t n_ = 0, next_ = 0, finished_ = 0, generation_ = 0;
    bool stop_ = false;
    std::exception_ptr error_;
};

struct ViewRequest {
    size_t ifno = 0;
    size_t t0 = 0, t1 = 0;     // rows [t0, t1)
    size_t c0 = 0, c1 = 0;     // channels [c0, c1)
    size_t width = 2048;       // max output columns
    size_t height = 1024;      // max output rows
    bool flatten = false;      // divide out the coarse-channel response first
};

struct Tile {
    ViewRequest req;
    size_t rows = 0, cols = 0;       // output size: min(height, t1-t0) x min(width, c1-c0)
    std::vector<float> mean, max;    // [rows][cols]
    size_t bytes_read = 0;
    double seconds = 0.0;
};

class Reducer {
public:
    explicit Reducer(size_t threads = 0);   // 0 = all cores
    size_t threads() const { return pool_.size(); }

    Tile reduce(const RowSource& file, const ViewRequest& req, const std::vector<float>* inv_response = nullptr);

    // 1 / (average shape of a coarse channel across its nfpc fine channels, mean 1), as
    // lft3.products.fbh5.coarse_channel_response: median over coarse channels of the mean of the first spectra.
    std::vector<float> inverseCoarseResponse(const RowSource& file, size_t ifno, size_t nspectra = 8);

private:
    ThreadPool pool_;
    std::vector<ReadScratch> scratch_;
    std::vector<std::vector<float>> rowbuf_;
    std::vector<std::vector<double>> sumbuf_;
    std::vector<std::vector<float>> maxbuf_;
};

}  // namespace lft3view
