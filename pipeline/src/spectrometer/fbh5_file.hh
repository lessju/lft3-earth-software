#ifndef LFT3_FBH5_WRITER_DETAIL_FBH5_FILE_HH
#define LFT3_FBH5_WRITER_DETAIL_FBH5_FILE_HH

// One FBH5 file (one beam): the Breakthrough Listen filterbank-HDF5 layout written by filterbankc99
// (the library rawspec uses), opened with the default POSIX HDF5 driver.
//
//   /data   float32 [time, nifs=1, nchans]   + SIGPROC header fields as attributes (blimpy/hyperseti read these)
//   /mask   uint8   [time, nifs=1, nchans]   (all zero for now)
//
// Both are chunked [1, 1, kChunkChannels] (one spectrum, 64 Ki channels), so a reader that wants a frequency range
// only decompresses the chunks it covers. /data uses byte shuffle + deflate 1 (float spectra shrink ~1.4x), /mask
// deflate 4 (all-zero rows shrink ~1000x).
//   /lft3_timestamp       uint64 [time]   spectrum counter t of the first spectrum in each integration
//   /lft3_missing_packets uint64 [time]   packets zero-filled within each integration
//   root attributes lft3_*: NCO, pfb_lsb, layout, time reference, software version

#include <cstdint>
#include <map>
#include <string>
#include <vector>

extern "C" {
#include "filterbankc99.h"
}

namespace Lft3 {

struct Fbh5Header {
    std::string sourceName = "LFT3";
    double fch1MHz = 0.0;
    double foffMHz = 0.0;
    std::uint64_t nchans = 0;
    double tstartMjd = 0.0;
    double tsampSeconds = 0.0;
    int telescopeId = 0;
    int machineId = 0;
    int nbeams = 1;
    int ibeam = 1;
    double srcRajHours = 0.0;
    double srcDejDegrees = 0.0;
    double azStart = 0.0;
    double zaStart = 0.0;
    std::string rawDataFile;
    int fineChannelsPerCoarse = 0;   // FBH5 "nfpc" (turboSETI uses it to find coarse channels)
};

class Fbh5File {
 public:
    static constexpr std::uint64_t kChunkChannels = 65536;

    Fbh5File() = default;
    ~Fbh5File();
    Fbh5File(const Fbh5File&) = delete;
    Fbh5File& operator=(const Fbh5File&) = delete;

    bool open(const std::string& path, const Fbh5Header& header,
              const std::map<std::string, double>& numericAttributes,
              const std::map<std::string, std::string>& stringAttributes, std::string& error);
    bool append(const float* spectrum, std::uint64_t timestamp, std::uint64_t missingPackets, std::string& error);
    void close();

    bool isOpen() const { return open_; }
    std::uint64_t spectraWritten() const { return written_; }
    const std::string& path() const { return path_; }

 private:
    bool appendScalar(const char* name, std::uint64_t value, std::string& error);

    filterbank_h5_file_t fb_ = {};
    std::vector<std::uint8_t> mask_;
    bool open_ = false;
    std::uint64_t written_ = 0;
    std::string path_;
};

}  // namespace Lft3

#endif  // LFT3_FBH5_WRITER_DETAIL_FBH5_FILE_HH
