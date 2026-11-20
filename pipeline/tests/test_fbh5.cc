#include <algorithm>
#include <catch2/catch_approx.hpp>
#include <catch2/catch_test_macros.hpp>

#include <filesystem>
#include <vector>

#include <hdf5.h>

#include "spectrometer/axes.hh"
#include "spectrometer/fbh5_file.hh"

using namespace Lft3;
using Catch::Approx;

TEST_CASE("Fine frequency axis matches the coarse-channel convention", "[lft3][fbh5][axes]") {
    // Full band, 128-point fine FFT, NCO -2031.25 MHz.
    const auto a = ComputeFineAxes(-2031.25, 0, 6144, 128);
    const double coarse = 1250.0 / 8192.0;
    REQUIRE(a.nchans == 6144 * 128);
    REQUIRE(a.foffMHz == Approx(coarse / 128));
    // Fine channel 0 = coarse k=0 (offset -3072 coarse channels), fine bin 0 (-64 fine bins).
    REQUIRE(a.fch1MHz == Approx(2031.25 - 3072 * coarse - 64 * coarse / 128));
    // Centre of coarse channel k=3072 (fine bin 64) is the NCO centre.
    REQUIRE(a.fch1MHz + (3072 * 128 + 64) * a.foffMHz == Approx(2031.25));
    // The 2000 MHz spur in the first capture (k = 2867) falls within its coarse channel.
    const double k2867 = a.fch1MHz + (2867 * 128 + 64) * a.foffMHz;
    REQUIRE(std::abs(k2867 - 2000.0) < coarse / 2);

    // Subband selection shifts fch1 by whole coarse channels.
    const auto b = ComputeFineAxes(-2031.25, 2048, 1024, 128);
    REQUIRE(b.fch1MHz == Approx(a.fch1MHz + 2048 * coarse));
    REQUIRE(b.nchans == 1024 * 128);
}

TEST_CASE("UNIX time converts to MJD", "[lft3][fbh5][axes]") {
    REQUIRE(UnixToMjd(0.0) == Approx(40587.0));
    REQUIRE(UnixToMjd(86400.0 * 10 + 43200.0) == Approx(40597.5));
    REQUIRE(UtcStamp(0.0) == "19700101T000000");
}

TEST_CASE("FBH5 file is written with filterbank attributes and LFT3 datasets", "[lft3][fbh5][file]") {
    const auto dir = std::filesystem::temp_directory_path() / "lft3_fbh5_test";
    std::filesystem::create_directories(dir);
    const std::string path = (dir / "test_beam0.h5").string();

    Fbh5Header h;
    h.sourceName = "TEST";
    h.fch1MHz = 1500.0;
    h.foffMHz = 0.001;
    h.nchans = 256;
    h.tstartMjd = 61000.5;
    h.tsampSeconds = 1.0;
    h.fineChannelsPerCoarse = 128;

    std::vector<float> spectrum(256);
    {
        Fbh5File f;
        std::string error;
        REQUIRE(f.open(path, h, {{"lft3_nco_mhz", -2031.25}}, {{"lft3_software", "test"}}, error));
        for (int k = 0; k < 3; k++) {
            for (std::size_t i = 0; i < spectrum.size(); i++) spectrum[i] = static_cast<float>(k * 1000 + i);
            REQUIRE(f.append(spectrum.data(), 100 + k, k, error));
        }
        REQUIRE(f.spectraWritten() == 3);
    }

    const hid_t file = H5Fopen(path.c_str(), H5F_ACC_RDONLY, H5P_DEFAULT);
    REQUIRE(file >= 0);
    const hid_t data = H5Dopen2(file, "data", H5P_DEFAULT);
    REQUIRE(data >= 0);
    const hid_t space = H5Dget_space(data);
    hsize_t dims[3] = {};
    REQUIRE(H5Sget_simple_extent_ndims(space) == 3);
    H5Sget_simple_extent_dims(space, dims, nullptr);
    REQUIRE(dims[0] == 3);
    REQUIRE(dims[1] == 1);
    REQUIRE(dims[2] == 256);

    std::vector<float> all(3 * 256);
    REQUIRE(H5Dread(data, H5T_NATIVE_FLOAT, H5S_ALL, H5S_ALL, H5P_DEFAULT, all.data()) >= 0);
    REQUIRE(all[0] == 0.0f);
    REQUIRE(all[256 + 5] == 1005.0f);
    REQUIRE(all[2 * 256 + 255] == 2255.0f);

    double fch1 = 0.0;
    const hid_t attr = H5Aopen(data, "fch1", H5P_DEFAULT);
    REQUIRE(attr >= 0);
    H5Aread(attr, H5T_NATIVE_DOUBLE, &fch1);
    REQUIRE(fch1 == Approx(1500.0));
    H5Aclose(attr);

    const hid_t ts = H5Dopen2(file, "lft3_timestamp", H5P_DEFAULT);
    REQUIRE(ts >= 0);
    std::uint64_t stamps[3] = {};
    REQUIRE(H5Dread(ts, H5T_NATIVE_UINT64, H5S_ALL, H5S_ALL, H5P_DEFAULT, stamps) >= 0);
    REQUIRE(stamps[0] == 100);
    REQUIRE(stamps[2] == 102);
    REQUIRE(H5Aexists(file, "lft3_nco_mhz") > 0);

    H5Dclose(ts);
    H5Sclose(space);
    H5Dclose(data);
    H5Fclose(file);
    std::filesystem::remove_all(dir);
}

TEST_CASE("FBH5 data is chunked in frequency and compressed (shuffle + deflate)", "[lft3][fbh5][file]") {
    const auto dir = std::filesystem::temp_directory_path() / "lft3_fbh5_compress_test";
    std::filesystem::create_directories(dir);
    const std::string path = (dir / "test_beam0.h5").string();

    Fbh5Header h;
    h.fch1MHz = 1500.0;
    h.foffMHz = 0.001;
    h.nchans = 2 * Fbh5File::kChunkChannels + 1000;   // not a multiple of the chunk width
    h.tsampSeconds = 1.0;
    const std::size_t n = h.nchans;

    // Noise-like positive floats (like real spectra): compressible only after the byte shuffle.
    std::vector<float> spectra(3 * n);
    std::uint32_t x = 12345;
    for (auto& v : spectra) {
        x = x * 1664525u + 1013904223u;
        v = 1.0e8f * (1.0f + static_cast<float>(x >> 8) / 16777216.0f);
    }
    {
        Fbh5File f;
        std::string error;
        REQUIRE(f.open(path, h, {}, {}, error));
        for (int k = 0; k < 3; k++) REQUIRE(f.append(spectra.data() + k * n, k, 0, error));
    }

    const hid_t file = H5Fopen(path.c_str(), H5F_ACC_RDONLY, H5P_DEFAULT);
    REQUIRE(file >= 0);
    const hid_t data = H5Dopen2(file, "data", H5P_DEFAULT);
    const hid_t dcpl = H5Dget_create_plist(data);
    hsize_t chunk[3] = {};
    REQUIRE(H5Pget_chunk(dcpl, 3, chunk) == 3);
    REQUIRE(chunk[0] == 1);
    REQUIRE(chunk[2] == Fbh5File::kChunkChannels);
    REQUIRE(H5Pget_nfilters(dcpl) == 2);
    unsigned flags = 0, cd[4] = {};
    std::size_t ncd = 4;
    REQUIRE(H5Pget_filter2(dcpl, 0, &flags, &ncd, cd, 0, nullptr, nullptr) == H5Z_FILTER_SHUFFLE);
    ncd = 4;
    REQUIRE(H5Pget_filter2(dcpl, 1, &flags, &ncd, cd, 0, nullptr, nullptr) == H5Z_FILTER_DEFLATE);
    REQUIRE(cd[0] == 1);
    REQUIRE(H5Dget_storage_size(data) < 3 * n * sizeof(float));

    std::vector<float> back(3 * n);
    REQUIRE(H5Dread(data, H5T_NATIVE_FLOAT, H5S_ALL, H5S_ALL, H5P_DEFAULT, back.data()) >= 0);
    REQUIRE(back == spectra);

    const hid_t mask = H5Dopen2(file, "mask", H5P_DEFAULT);
    REQUIRE(H5Dget_storage_size(mask) < n / 50);   // three all-zero rows
    std::vector<std::uint8_t> m(3 * n, 1);
    REQUIRE(H5Dread(mask, H5T_NATIVE_UINT8, H5S_ALL, H5S_ALL, H5P_DEFAULT, m.data()) >= 0);
    REQUIRE(std::all_of(m.begin(), m.end(), [](std::uint8_t v) { return v == 0; }));

    H5Dclose(mask);
    H5Pclose(dcpl);
    H5Dclose(data);
    H5Fclose(file);
    std::filesystem::remove_all(dir);
}

TEST_CASE("FBH5 parallel chunk compression decodes like HDF5's own filters", "[lft3][fbh5][file]") {
    const auto dir = std::filesystem::temp_directory_path() / "lft3_fbh5_parallel_test";
    std::filesystem::create_directories(dir);
    Fbh5Header h;
    h.fch1MHz = 1500.0;
    h.foffMHz = 0.001;
    h.nchans = 4 * Fbh5File::kChunkChannels;   // whole chunks: the parallel path
    h.tsampSeconds = 1.0;
    const std::size_t n = h.nchans;
    std::vector<float> spectra(5 * n);
    std::uint32_t x = 777;
    for (auto& v : spectra) {
        x = x * 1664525u + 1013904223u;
        v = 3.0e7f * (1.0f + static_cast<float>(x >> 8) / 16777216.0f);
    }
    const auto write = [&](const std::string& name, unsigned threads) {
        const std::string path = (dir / name).string();
        Fbh5Header hh = h;
        hh.compressThreads = threads;
        Fbh5File f;
        std::string error;
        REQUIRE(f.open(path, hh, {}, {}, error));
        for (int k = 0; k < 5; k++) REQUIRE(f.append(spectra.data() + k * n, k, 0, error));
        return path;
    };
    const std::string serial = write("serial_beam0.h5", 1), parallel = write("parallel_beam0.h5", 4);
    const auto read = [&](const std::string& path, std::vector<float>& data, std::vector<std::uint8_t>& mask,
                          hsize_t& storage) {
        const hid_t file = H5Fopen(path.c_str(), H5F_ACC_RDONLY, H5P_DEFAULT);
        REQUIRE(file >= 0);
        const hid_t d = H5Dopen2(file, "data", H5P_DEFAULT);
        const hid_t m = H5Dopen2(file, "mask", H5P_DEFAULT);
        data.assign(5 * n, 0.0f);
        mask.assign(5 * n, 1);
        REQUIRE(H5Dread(d, H5T_NATIVE_FLOAT, H5S_ALL, H5S_ALL, H5P_DEFAULT, data.data()) >= 0);
        REQUIRE(H5Dread(m, H5T_NATIVE_UINT8, H5S_ALL, H5S_ALL, H5P_DEFAULT, mask.data()) >= 0);
        storage = H5Dget_storage_size(d);
        H5Dclose(m);
        H5Dclose(d);
        H5Fclose(file);
    };
    std::vector<float> a, b;
    std::vector<std::uint8_t> ma, mb;
    hsize_t sa = 0, sb = 0;
    read(serial, a, ma, sa);
    read(parallel, b, mb, sb);
    REQUIRE(a == spectra);
    REQUIRE(b == spectra);
    REQUIRE(std::all_of(mb.begin(), mb.end(), [](std::uint8_t v) { return v == 0; }));
    REQUIRE(sb < 5 * n * sizeof(float));
    REQUIRE(sb == sa);   // same zlib, same level: the same compressed chunks
    std::filesystem::remove_all(dir);
}
