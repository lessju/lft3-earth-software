#include <catch2/catch_approx.hpp>
#include <catch2/catch_test_macros.hpp>

#include <filesystem>
#include <vector>

#include <hdf5.h>

#include "detail/axes.hh"
#include "detail/fbh5_file.hh"
#include "detail/timeline.hh"

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

TEST_CASE("Integration timeline recovers start times and tsamp", "[lft3][fbh5][timeline]") {
    const std::uint64_t L = 1024;          // spectra per block
    const std::uint64_t N = 149;           // blocks per integration
    IntegrationTimeline tl(L);
    IntegrationTimeline::Spectrum s;

    // Integrations end with blocks starting at t = (N-1)L, (2N-1)L, ...
    REQUIRE(tl.push((N - 1) * L, 5, s) == IntegrationTimeline::Step::First);
    REQUIRE(tl.push((2 * N - 1) * L, 5, s) == IntegrationTimeline::Step::Next);
    REQUIRE(tl.interval() == N * L);
    REQUIRE(tl.firstSpectrum().startT == 0);
    REQUIRE(tl.firstSpectrum().missing == 5);
    REQUIRE(s.startT == N * L);
    REQUIRE(s.missing == 0);

    REQUIRE(tl.push((3 * N - 1) * L, 12, s) == IntegrationTimeline::Step::Next);
    REQUIRE(s.startT == 2 * N * L);
    REQUIRE(s.missing == 7);

    // A re-sync (t back to small values) is a discontinuity; the next push re-measures the interval.
    REQUIRE(tl.push((N - 1) * L, 12, s) == IntegrationTimeline::Step::Discontinuity);
    REQUIRE(tl.push((2 * N - 1) * L, 20, s) == IntegrationTimeline::Step::Next);
    REQUIRE(tl.firstSpectrum().startT == 0);
    REQUIRE(tl.firstSpectrum().missing == 0);
    REQUIRE(s.missing == 8);

    // A changed interval (e.g. lost blocks) is also a discontinuity.
    REQUIRE(tl.push((3 * N) * L, 20, s) == IntegrationTimeline::Step::Discontinuity);
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
