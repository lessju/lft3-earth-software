// A run of FBH5 files that continue each other in time, viewed as one long file.
//
// lft3-spectrometer rotates files <prefix>_<UTC>_beam<N>.h5 every --spectra-per-file spectra (default 3600).
// A rotation continues the spectrum counter exactly, while a re-sync or lost blocks start new files after a jump. So two files are
// chained only when they are the same beam/prefix/directory, have the same axes, and the first lft3_timestamp of
// the second is exactly one integration after the last of the first. Anything else starts a new run.
#pragma once

#include <memory>
#include <string>
#include <vector>

#include "fbh5_file.hh"

namespace lft3view {

class Fbh5Sequence : public RowSource {
public:
    // files in time order, already known to be contiguous; names are their paths relative to the data dir
    Fbh5Sequence(std::vector<std::shared_ptr<Fbh5File>> files, std::vector<std::string> names);

    const Header& header() const override { return header_; }
    const std::vector<uint64_t>& timestamps() const override { return timestamps_; }
    const std::vector<uint64_t>& missingPackets() const override { return missing_; }
    void readRow(size_t row, size_t ifno, size_t c0, size_t c1, float* out, ReadScratch& scratch) const override;
    size_t bytesForRange(size_t c0, size_t c1) const override;
    size_t chunkChannels() const override { return chunk_channels_; }
    bool rangedReads() const override { return ranged_; }

    const std::vector<std::shared_ptr<Fbh5File>>& files() const { return files_; }
    const std::vector<std::string>& names() const { return names_; }
    const std::vector<size_t>& firstRows() const { return first_row_; }   // global row of each file's row 0

private:
    std::vector<std::shared_ptr<Fbh5File>> files_;
    std::vector<std::string> names_;
    std::vector<size_t> first_row_;
    Header header_;
    std::vector<uint64_t> timestamps_, missing_;
    size_t chunk_channels_ = 0;
    bool ranged_ = true;
};

// True if b continues a without a gap (same axes, timestamps exactly one integration apart).
bool continues(const Fbh5File& a, const Fbh5File& b);

// Parts of "<prefix>_<YYYYMMDDTHHMMSS>_beam<N>.h5"; ok = false for other names.
struct RunName {
    bool ok = false;
    std::string group;   // directory + prefix + beam: files that may chain
    std::string stamp;   // sort key within the group
};
RunName parseRunName(const std::string& rel_path);

}  // namespace lft3view
