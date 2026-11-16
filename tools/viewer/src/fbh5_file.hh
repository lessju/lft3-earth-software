// Fast read-only access to filterbank-HDF5 (FBH5) spectra, as written by lft3-spectrometer (pipeline/src/spectrometer/
// fbh5_file.cc, filterbankc99) and, for older files, the former CyberEther lft3_fbh5_writer.
//
// HDF5 is used only to open the file, read metadata and list where each chunk of /data lives. The spectra
// themselves are read with pread() straight from those byte offsets, from any number of threads at once:
// the HDF5 library's own read path is single-threaded (one global lock) and was the bottleneck (~0.5 GB/s).
//
// Chunks may be stored raw (no filter), with deflate, or with byte shuffle + deflate (the writer's default since
// 2026-11-13: 64 Ki-channel chunks, so a frequency zoom only decodes the chunks it covers). A deflate chunk
// written at level 0 is a zlib stream of "stored" blocks (65535-byte payloads behind 5-byte headers), so any byte
// range of it can be read without inflating; every header that is touched is checked. Everything else is decoded
// a whole chunk at a time (inflate, then unshuffle), and the last decoded chunk is kept per thread.

#pragma once

#include <cstddef>
#include <cstdint>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

namespace lft3view {

struct Header {
    size_t nrows = 0;      // spectra present (only rows whose chunks are all written)
    size_t nifs = 0;
    size_t nchans = 0;
    double fch1 = 0.0;     // MHz, centre of channel 0
    double foff = 0.0;     // MHz per channel
    double tsamp = 0.0;    // s per spectrum
    double tstart = 0.0;   // MJD of row 0
    size_t nfpc = 0;       // fine channels per coarse channel (0 if unknown)
    std::string source_name;
    std::string attrs_json;   // all /data and root attributes as a JSON object
};

// Per-thread scratch for reads (inflate buffers etc.), so reads never allocate after warm-up.
struct ReadScratch {
    std::vector<uint8_t> raw;
    std::vector<uint8_t> inflated;
    std::vector<uint8_t> unshuffled;
    const void* decoded_file = nullptr;    // file + offset of the chunk currently held decoded
    uint64_t decoded_addr = UINT64_MAX;
    const uint8_t* decoded = nullptr;
};

// Anything the reducer can read spectra from: one file, or a chain of files of one run (fbh5_sequence.hh).
class RowSource {
public:
    virtual ~RowSource() = default;
    virtual const Header& header() const = 0;
    virtual const std::vector<uint64_t>& timestamps() const = 0;
    virtual const std::vector<uint64_t>& missingPackets() const = 0;
    // Channels [c0, c1) of spectrum `row`, IF `ifno`, into out[0 .. c1-c0). Thread-safe (no HDF5 calls).
    virtual void readRow(size_t row, size_t ifno, size_t c0, size_t c1, float* out, ReadScratch& scratch) const = 0;
    // Bytes that readRow() must fetch from disk for that range (for throughput figures).
    virtual size_t bytesForRange(size_t c0, size_t c1) const = 0;
    virtual size_t chunkChannels() const = 0;
    // True when any channel range can be read without decoding whole chunks (raw or level-0 deflate).
    virtual bool rangedReads() const = 0;
};

class Fbh5File : public RowSource {
public:
    explicit Fbh5File(const std::string& path);
    ~Fbh5File();
    Fbh5File(const Fbh5File&) = delete;
    Fbh5File& operator=(const Fbh5File&) = delete;

    // Re-read the chunk index and small datasets (a file that is still being written grows).
    void refresh();

    const std::string& path() const { return path_; }
    const Header& header() const override { return header_; }
    const std::vector<uint64_t>& timestamps() const override { return timestamps_; }   // lft3_timestamp (may be empty)
    const std::vector<uint64_t>& missingPackets() const override { return missing_; }  // lft3_missing_packets
    std::string layoutDescription() const;

    void readRow(size_t row, size_t ifno, size_t c0, size_t c1, float* out, ReadScratch& scratch) const override;
    size_t bytesForRange(size_t c0, size_t c1) const override;
    size_t chunkChannels() const override { return cdim_[2]; }
    bool rangedReads() const override { return ranged_; }

private:
    struct Chunk {
        uint64_t addr = UINT64_MAX;   // file offset; UINT64_MAX = not written
        uint64_t size = 0;
        bool deflated = false;        // deflate applied to this chunk
        bool shuffled = false;        // byte shuffle applied to this chunk
    };

    void readIndex();
    const Chunk& chunkAt(size_t ti, size_t ii, size_t fi) const {
        return chunks_[(ti * grid_[1] + ii) * grid_[2] + fi];
    }
    // Copy decoded bytes [off, off+len) of one chunk into dst.
    void readChunkBytes(const Chunk& c, size_t off, size_t len, uint8_t* dst, ReadScratch& s) const;
    void decodeChunk(const Chunk& c, ReadScratch& s) const;
    bool readStored(const Chunk& c, size_t off, size_t len, uint8_t* dst, ReadScratch& s) const;
    void preadAll(void* dst, size_t len, uint64_t offset) const;

    std::string path_;
    int fd_ = -1;
    Header header_;
    size_t extent_[3] = {0, 0, 0};
    size_t cdim_[3] = {0, 0, 0};     // chunk shape (time, if, chan)
    size_t grid_[3] = {0, 0, 0};     // chunks along each axis
    bool deflate_ = false;           // dataset has the deflate filter
    bool shuffle_ = false;           // ... and the byte shuffle filter (before deflate)
    int deflate_idx_ = -1, shuffle_idx_ = -1;   // positions in the filter pipeline (per-chunk skip mask bits)
    bool ranged_ = true;
    std::vector<Chunk> chunks_;
    std::vector<uint64_t> timestamps_;
    std::vector<uint64_t> missing_;
    mutable std::mutex hdf5_mutex_;
};

}  // namespace lft3view
