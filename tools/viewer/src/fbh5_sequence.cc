#include "fbh5_sequence.hh"

#include <algorithm>
#include <cmath>
#include <regex>
#include <stdexcept>

namespace lft3view {

Fbh5Sequence::Fbh5Sequence(std::vector<std::shared_ptr<Fbh5File>> files, std::vector<std::string> names)
    : files_(std::move(files)), names_(std::move(names)) {
    if (files_.empty()) throw std::invalid_argument("empty sequence");
    header_ = files_.front()->header();
    header_.nrows = 0;
    chunk_channels_ = files_.front()->chunkChannels();
    for (const auto& f : files_) {
        first_row_.push_back(header_.nrows);
        const size_t n = f->header().nrows;
        header_.nrows += n;
        chunk_channels_ = std::min(chunk_channels_, f->chunkChannels());
        ranged_ = ranged_ && f->rangedReads();
        // per-row datasets: pad to the file's row count so rows stay aligned even if one is short
        auto ts = f->timestamps();
        auto mp = f->missingPackets();
        ts.resize(n, 0);
        mp.resize(n, 0);
        timestamps_.insert(timestamps_.end(), ts.begin(), ts.end());
        missing_.insert(missing_.end(), mp.begin(), mp.end());
    }
}

void Fbh5Sequence::readRow(size_t row, size_t ifno, size_t c0, size_t c1, float* out, ReadScratch& s) const {
    if (row >= header_.nrows) throw std::out_of_range("readRow: outside the sequence");
    const size_t k = static_cast<size_t>(std::upper_bound(first_row_.begin(), first_row_.end(), row) - first_row_.begin()) - 1;
    files_[k]->readRow(row - first_row_[k], ifno, c0, c1, out, s);
}

size_t Fbh5Sequence::bytesForRange(size_t c0, size_t c1) const { return files_.front()->bytesForRange(c0, c1); }

bool continues(const Fbh5File& a, const Fbh5File& b) {
    const Header &x = a.header(), &y = b.header();
    if (x.nchans != y.nchans || x.nifs != y.nifs || x.fch1 != y.fch1 || x.foff != y.foff) return false;
    if (std::fabs(x.tsamp - y.tsamp) > 1e-6 * std::fabs(x.tsamp)) return false;
    const auto &ta = a.timestamps(), &tb = b.timestamps();
    if (ta.empty() || tb.empty() || ta.size() < x.nrows) return false;
    uint64_t step = 0;
    if (ta.size() >= 2)
        step = ta[1] - ta[0];
    else if (tb.size() >= 2)
        step = tb[1] - tb[0];
    return step > 0 && tb.front() == ta[x.nrows - 1] + step;
}

RunName parseRunName(const std::string& rel) {
    static const std::regex re(R"(^(.*)_(\d{8}T\d{6})_beam(\d+)\.h5$)");
    std::smatch m;
    RunName r;
    if (!std::regex_match(rel, m, re)) return r;
    r.ok = true;
    r.group = m[1].str() + "|beam" + m[3].str();
    r.stamp = m[2].str();
    return r;
}

}  // namespace lft3view
