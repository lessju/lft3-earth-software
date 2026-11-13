// memload: write N x 7.6 GB/s into host memory, standing in for the ConnectX DMA of N 100 GbE streams
// (each stream's NIC writes ~7.6 GB/s of packets into pinned host memory). Paced in 10 ms steps, spread over
// threads pinned to the given cores. Uses CPU memset, no GPU time: it only loads the shared LPDDR5x bus.
//   g++ -O2 -pthread -o memload memload.cc && ./memload GBPS SECONDS CORE[,CORE...]
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <sstream>
#include <string>
#include <thread>
#include <vector>
#include <pthread.h>

int main(int argc, char** argv) {
    if (argc < 4) { std::fprintf(stderr, "usage: memload GBPS SECONDS CORES\n"); return 2; }
    const double gbps = std::atof(argv[1]), seconds = std::atof(argv[2]);
    std::vector<int> cores;
    std::stringstream ss(argv[3]);
    for (std::string c; std::getline(ss, c, ',');) cores.push_back(std::stoi(c));
    const std::size_t step = static_cast<std::size_t>(gbps * 1e9 * 0.010 / cores.size());   // bytes per thread per 10 ms
    std::atomic<std::uint64_t> total{0}, late{0};
    std::vector<std::thread> th;
    for (int core : cores) {
        th.emplace_back([&, core]() {
            cpu_set_t set; CPU_ZERO(&set); CPU_SET(core, &set);
            pthread_setaffinity_np(pthread_self(), sizeof(set), &set);
            const std::size_t bufBytes = 512ull << 20;
            char* buf = static_cast<char*>(std::aligned_alloc(4096, bufBytes));
            std::memset(buf, 1, bufBytes);
            std::size_t off = 0;
            auto next = std::chrono::steady_clock::now();
            const auto end = next + std::chrono::duration<double>(seconds);
            int v = 0;
            while (std::chrono::steady_clock::now() < end) {
                std::size_t left = step;
                while (left) {
                    const std::size_t n = std::min(left, bufBytes - off);
                    std::memset(buf + off, ++v, n);
                    off = (off + n) % bufBytes;
                    left -= n;
                }
                total += step;
                next += std::chrono::milliseconds(10);
                if (std::chrono::steady_clock::now() > next) late++;
                std::this_thread::sleep_until(next);
            }
            std::free(buf);
        });
    }
    for (auto& t : th) t.join();
    std::printf("memload: %.1f GB written in %.0f s = %.2f GB/s (target %.2f), late steps %lu\n", total / 1e9, seconds,
                total / 1e9 / seconds, gbps, static_cast<unsigned long>(late.load()));
    return 0;
}
