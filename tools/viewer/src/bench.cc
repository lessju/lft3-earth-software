// lft3-view-bench FILE [--threads N] [--warm]
//
// 1. Checks Fbh5File::readRow against the HDF5 library (H5Dread) for a few rows, bit for bit, and the reducer
//    against a straightforward single-threaded max/mean.
// 2. Times typical viewer requests: whole-file overview, frequency zoom, time zoom, full resolution.
//    By default the file is evicted from the page cache before every request (posix_fadvise DONTNEED, no root
//    needed for clean pages), so the times are cold-disk figures; --warm keeps the cache.

#include <fcntl.h>
#include <hdf5.h>
#include <unistd.h>

#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "fbh5_file.hh"
#include "reduce.hh"

using namespace lft3view;

namespace {

void evict(const std::string& path) {
    int fd = ::open(path.c_str(), O_RDONLY);
    if (fd < 0) return;
    ::posix_fadvise(fd, 0, 0, POSIX_FADV_DONTNEED);
    ::close(fd);
}

std::vector<float> h5Row(const std::string& path, size_t row, size_t ifno, size_t nchans) {
    hid_t fapl = H5Pcreate(H5P_FILE_ACCESS);
    H5Pset_file_locking(fapl, false, true);
    hid_t f = H5Fopen(path.c_str(), H5F_ACC_RDONLY, fapl);
    hid_t d = H5Dopen2(f, "/data", H5P_DEFAULT);
    hid_t fs = H5Dget_space(d);
    hsize_t start[3] = {row, ifno, 0}, count[3] = {1, 1, nchans};
    H5Sselect_hyperslab(fs, H5S_SELECT_SET, start, nullptr, count, nullptr);
    hid_t ms = H5Screate_simple(3, count, nullptr);
    std::vector<float> v(nchans);
    if (H5Dread(d, H5T_NATIVE_FLOAT, ms, fs, H5P_DEFAULT, v.data()) < 0) v.clear();
    H5Sclose(ms);
    H5Sclose(fs);
    H5Dclose(d);
    H5Fclose(f);
    H5Pclose(fapl);
    return v;
}

int check(const Fbh5File& file, Reducer& red) {
    const Header& h = file.header();
    int bad = 0;
    ReadScratch s;
    std::vector<float> mine(h.nchans);
    for (size_t row : {size_t(0), h.nrows / 2, h.nrows - 1}) {
        auto ref = h5Row(file.path(), row, 0, h.nchans);
        file.readRow(row, 0, 0, h.nchans, mine.data(), s);
        bool same = ref.size() == mine.size() && std::memcmp(ref.data(), mine.data(), ref.size() * 4) == 0;
        // and a ranged read straddling stored-block boundaries
        size_t c0 = 65535 / 4 - 7, c1 = std::min(h.nchans, c0 + 3 * 65535 / 4 + 11);
        std::vector<float> part(c1 - c0);
        file.readRow(row, 0, c0, c1, part.data(), s);
        bool same_part = std::memcmp(ref.data() + c0, part.data(), part.size() * 4) == 0;
        std::printf("  row %zu: full row %s, ranged read %s\n", row, same ? "identical to H5Dread" : "MISMATCH",
                    same_part ? "identical" : "MISMATCH");
        bad += !same + !same_part;
    }
    // reducer vs naive on a small window with uneven bins
    ViewRequest q;
    q.t0 = 0;
    q.t1 = std::min<size_t>(h.nrows, 37);
    q.c0 = 1001;
    q.c1 = std::min(h.nchans, q.c0 + 50000);
    q.width = 333;
    q.height = 7;
    Tile t = red.reduce(file, q);
    double worst = 0;
    std::vector<float> row(q.c1 - q.c0);
    for (size_t i = 0; i < t.rows; ++i) {
        size_t ra = q.t0 + i * (q.t1 - q.t0) / t.rows, rb = q.t0 + (i + 1) * (q.t1 - q.t0) / t.rows;
        std::vector<double> sum(t.cols, 0);
        std::vector<float> mx(t.cols, -INFINITY);
        std::vector<size_t> cnt(t.cols, 0);
        for (size_t r = ra; r < rb; ++r) {
            file.readRow(r, 0, q.c0, q.c1, row.data(), s);
            size_t j = 0;
            for (size_t c = 0; c < row.size(); ++c) {
                while (j + 1 < t.cols && (j + 1) * row.size() / t.cols <= c) ++j;
                sum[j] += row[c];
                mx[j] = std::max(mx[j], row[c]);
                cnt[j]++;
            }
        }
        for (size_t j = 0; j < t.cols; ++j) {
            double m = sum[j] / cnt[j];
            worst = std::max(worst, std::fabs(m - t.mean[i * t.cols + j]) / std::max(1e-30, std::fabs(m)));
            if (mx[j] != t.max[i * t.cols + j]) worst = 1;
        }
    }
    std::printf("  reducer vs naive: max relative error %.2e %s\n", worst, worst < 1e-5 ? "(ok)" : "MISMATCH");
    return bad + (worst >= 1e-5);
}

void timeView(const char* name, Fbh5File& file, Reducer& red, ViewRequest q, bool warm,
              const std::vector<float>* inv = nullptr) {
    if (!warm) evict(file.path());
    Tile t = red.reduce(file, q, inv);
    std::printf("  %-34s %6zu x %-7zu -> %4zu x %-5zu %8.3f s  %7.2f GB/s  (%.2f GB)\n", name, q.t1 - q.t0,
                q.c1 - q.c0, t.rows, t.cols, t.seconds, t.bytes_read / t.seconds / 1e9, t.bytes_read / 1e9);
}

}  // namespace

int main(int argc, char** argv) {
    if (argc < 2) {
        std::fprintf(stderr, "usage: %s FILE.h5 [--threads N] [--warm]\n", argv[0]);
        return 2;
    }
    std::string path = argv[1];
    size_t threads = 0;
    bool warm = false;
    for (int i = 2; i < argc; ++i) {
        if (!std::strcmp(argv[i], "--threads") && i + 1 < argc) threads = std::stoul(argv[++i]);
        else if (!std::strcmp(argv[i], "--warm")) warm = true;
    }
    try {
        const auto o0 = std::chrono::steady_clock::now();
        Fbh5File file(path);
        const double open_s = std::chrono::duration<double>(std::chrono::steady_clock::now() - o0).count();
        Reducer red(threads);
        const Header& h = file.header();
        std::printf("%s\n  %s\n  rows %zu (opened in %.3f s), nfpc %zu, fch1 %.6f MHz, foff %.9f MHz, tsamp %.6f s, %zu threads, %s cache\n",
                    path.c_str(), file.layoutDescription().c_str(), h.nrows, open_s, h.nfpc, h.fch1, h.foff, h.tsamp,
                    red.threads(), warm ? "warm" : "cold");
        std::printf("correctness:\n");
        int bad = check(file, red);

        std::printf("timing:\n");
        const size_t N = h.nrows, C = h.nchans;
        ViewRequest q;
        q.t0 = 0; q.t1 = N; q.c0 = 0; q.c1 = C; q.width = 2048; q.height = 1024;
        timeView("overview (whole file)", file, red, q, warm);
        q.c0 = C / 2; q.c1 = C / 2 + C / 100;
        timeView("frequency zoom 1% of band", file, red, q, warm);
        q.c0 = C / 2; q.c1 = C / 2 + 2048;
        timeView("frequency zoom 2048 channels", file, red, q, warm);
        q.t0 = N / 2; q.t1 = std::min(N, N / 2 + 64); q.c0 = 0; q.c1 = C;
        timeView("time zoom 64 spectra, full band", file, red, q, warm);
        q.c0 = C / 3; q.c1 = C / 3 + 2048;
        timeView("full resolution 64 x 2048", file, red, q, warm);
        q.t0 = 0; q.t1 = N; q.c0 = 0; q.c1 = C;
        auto inv = red.inverseCoarseResponse(file, 0);
        q.flatten = true;
        timeView("overview, flattened", file, red, q, warm, &inv);
        return bad ? 1 : 0;
    } catch (const std::exception& e) {
        std::fprintf(stderr, "error: %s\n", e.what());
        return 1;
    }
}
