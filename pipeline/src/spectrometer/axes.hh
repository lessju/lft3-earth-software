#ifndef LFT3_FBH5_WRITER_DETAIL_AXES_HH
#define LFT3_FBH5_WRITER_DETAIL_AXES_HH

// Frequency and time axes of the fine spectra, in SIGPROC/FBH5 terms.
//
// Fine channel i = k * fftLength + j (coarse channel k of the selection, fine bin j after fftshift):
//   coarse channel kAbs = channelStart + k sits at baseband offset (kAbs - 3072) * 152.587890625 kHz,
//   fine bin j is offset by (j - fftLength/2) * 152.587890625 kHz / fftLength.
// RF = rfCentre + baseband offset, with rfCentre = -ncoMHz (the RFDC mixes with a negative NCO
// frequency; listener.py's fft_bin_rf_freq uses the same convention, and the 125 MHz-harmonic spurs in
// the first capture land on exact multiples only with this sign). Frequencies ascend with i, so
// foff > 0 and fch1 is the centre of fine channel 0.

#include <cmath>
#include <cstdint>
#include <ctime>
#include <string>

namespace Lft3 {

struct FineAxes {
    double fch1MHz = 0.0;
    double foffMHz = 0.0;
    std::uint64_t nchans = 0;
};

inline FineAxes ComputeFineAxes(double ncoMHz, std::uint64_t channelStart, std::uint64_t channelCount,
                                std::uint64_t fftLength) {
    constexpr double kCoarseMHz = 1250.0 / 8192.0;
    constexpr double kDcChannel = 3072.0;
    FineAxes a;
    a.foffMHz = kCoarseMHz / static_cast<double>(fftLength);
    const double firstFineOffset = -static_cast<double>(fftLength / 2) * a.foffMHz;
    a.fch1MHz = -ncoMHz + (static_cast<double>(channelStart) - kDcChannel) * kCoarseMHz + firstFineOffset;
    a.nchans = channelCount * fftLength;
    return a;
}

inline double UnixToMjd(double unixSeconds) {
    return unixSeconds / 86400.0 + 40587.0;
}

// "20261112T153045" (UTC) for file names.
inline std::string UtcStamp(double unixSeconds) {
    const std::time_t t = static_cast<std::time_t>(std::floor(unixSeconds));
    std::tm tm = {};
    gmtime_r(&t, &tm);
    char buf[32];
    std::strftime(buf, sizeof(buf), "%Y%m%dT%H%M%S", &tm);
    return buf;
}

}  // namespace Lft3

#endif  // LFT3_FBH5_WRITER_DETAIL_AXES_HH
