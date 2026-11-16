#include "fbh5_file.hh"

#include <hdf5.h>
#include <zlib.h>

#include <algorithm>
#include <cerrno>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <sstream>
#include <stdexcept>
#include <unistd.h>

namespace lft3view {

namespace {

constexpr size_t kStoredBlock = 65535;           // payload of a full deflate stored block
constexpr size_t kStoredStride = kStoredBlock + 5;  // header (1 byte BFINAL/BTYPE + LEN + NLEN) + payload

// RAII for HDF5 identifiers.
struct Hid {
    hid_t id = H5I_INVALID_HID;
    herr_t (*close)(hid_t) = nullptr;
    Hid(hid_t i, herr_t (*c)(hid_t)) : id(i), close(c) {}
    ~Hid() {
        if (id >= 0 && close) close(id);
    }
    Hid(const Hid&) = delete;
    Hid& operator=(const Hid&) = delete;
    operator hid_t() const { return id; }
    bool ok() const { return id >= 0; }
};

std::string jsonEscape(const std::string& s) {
    std::string o;
    o.reserve(s.size() + 2);
    for (char ch : s) {
        auto c = static_cast<unsigned char>(ch);
        if (c == '"' || c == '\\') {
            o += '\\';
            o += ch;
        } else if (c < 0x20) {
            char buf[8];
            std::snprintf(buf, sizeof buf, "\\u%04x", c);
            o += buf;
        } else {
            o += ch;
        }
    }
    return o;
}

std::string jsonNumber(double v) {
    if (!std::isfinite(v)) return "null";
    char buf[32];
    std::snprintf(buf, sizeof buf, "%.17g", v);
    return buf;
}

// Read one attribute as JSON (numbers, strings, small numeric/string arrays). Returns "" if unsupported.
std::string attrToJson(hid_t attr) {
    Hid type(H5Aget_type(attr), H5Tclose);
    Hid space(H5Aget_space(attr), H5Sclose);
    hssize_t n = H5Sget_simple_extent_npoints(space);
    if (n < 1 || n > 64) return "";
    H5T_class_t cls = H5Tget_class(type);
    if (cls == H5T_INTEGER || cls == H5T_FLOAT) {
        std::vector<double> v(static_cast<size_t>(n));
        if (H5Aread(attr, H5T_NATIVE_DOUBLE, v.data()) < 0) return "";
        if (n == 1) return jsonNumber(v[0]);
        std::string o = "[";
        for (size_t i = 0; i < v.size(); ++i) o += (i ? "," : "") + jsonNumber(v[i]);
        return o + "]";
    }
    if (cls == H5T_STRING) {
        std::vector<std::string> out;
        if (H5Tis_variable_str(type) > 0) {
            std::vector<char*> p(static_cast<size_t>(n), nullptr);
            Hid mem(H5Tcopy(H5T_C_S1), H5Tclose);
            H5Tset_size(mem, H5T_VARIABLE);
            if (H5Aread(attr, mem, p.data()) < 0) return "";
            for (char* s : p) out.emplace_back(s ? s : "");
            H5Dvlen_reclaim(mem, space, H5P_DEFAULT, p.data());
        } else {
            size_t sz = H5Tget_size(type);
            std::vector<char> buf(sz * static_cast<size_t>(n) + 1, 0);
            if (H5Aread(attr, type, buf.data()) < 0) return "";
            for (hssize_t i = 0; i < n; ++i) {
                std::string s(buf.data() + i * sz, sz);
                s.erase(std::find(s.begin(), s.end(), '\0'), s.end());
                out.push_back(s);
            }
        }
        if (n == 1) return "\"" + jsonEscape(out[0]) + "\"";
        std::string o = "[";
        for (size_t i = 0; i < out.size(); ++i) o += std::string(i ? "," : "") + "\"" + jsonEscape(out[i]) + "\"";
        return o + "]";
    }
    return "";
}

struct AttrCollector {
    std::vector<std::pair<std::string, std::string>> items;
};

herr_t collectAttr(hid_t loc, const char* name, const H5A_info_t*, void* op) {
    auto* c = static_cast<AttrCollector*>(op);
    Hid a(H5Aopen(loc, name, H5P_DEFAULT), H5Aclose);
    if (!a.ok()) return 0;
    std::string v = attrToJson(a);
    if (!v.empty()) c->items.emplace_back(name, v);
    return 0;
}

double numAttr(const AttrCollector& c, const std::string& key, double dflt) {
    for (auto& [k, v] : c.items)
        if (k == key) {
            try {
                return std::stod(v);
            } catch (...) {
                return dflt;
            }
        }
    return dflt;
}

std::vector<uint64_t> readU64(hid_t file, const char* name, size_t maxn) {
    std::vector<uint64_t> v;
    if (H5Lexists(file, name, H5P_DEFAULT) <= 0) return v;
    Hid d(H5Dopen2(file, name, H5P_DEFAULT), H5Dclose);
    if (!d.ok()) return v;
    Hid sp(H5Dget_space(d), H5Sclose);
    hssize_t n = H5Sget_simple_extent_npoints(sp);
    if (n <= 0) return v;
    v.resize(static_cast<size_t>(n));
    if (H5Dread(d, H5T_NATIVE_UINT64, H5S_ALL, H5S_ALL, H5P_DEFAULT, v.data()) < 0) return {};
    if (v.size() > maxn) v.resize(maxn);
    return v;
}

}  // namespace

Fbh5File::Fbh5File(const std::string& path) : path_(path) {
    static std::once_flag quiet;
    std::call_once(quiet, [] { H5Eset_auto2(H5E_DEFAULT, nullptr, nullptr); });
    fd_ = ::open(path.c_str(), O_RDONLY | O_CLOEXEC);
    if (fd_ < 0) throw std::runtime_error(path + ": " + std::strerror(errno));
    try {
        readIndex();
    } catch (...) {
        ::close(fd_);
        throw;
    }
}

Fbh5File::~Fbh5File() {
    if (fd_ >= 0) ::close(fd_);
}

void Fbh5File::refresh() { readIndex(); }

void Fbh5File::readIndex() {
    std::lock_guard<std::mutex> lock(hdf5_mutex_);

    // No file locking: the pipeline may still be writing this file, and it holds a write lock.
    Hid fapl(H5Pcreate(H5P_FILE_ACCESS), H5Pclose);
    H5Pset_file_locking(fapl, false, true);
    Hid file(H5Fopen(path_.c_str(), H5F_ACC_RDONLY, fapl), H5Fclose);
    if (!file.ok()) throw std::runtime_error(path_ + ": not an HDF5 file, or unreadable");
    Hid dset(H5Dopen2(file, "/data", H5P_DEFAULT), H5Dclose);
    if (!dset.ok()) throw std::runtime_error(path_ + ": no /data dataset");

    Hid type(H5Dget_type(dset), H5Tclose);
    if (H5Tequal(type, H5T_IEEE_F32LE) <= 0)
        throw std::runtime_error(path_ + ": /data is not little-endian float32");

    Hid space(H5Dget_space(dset), H5Sclose);
    if (H5Sget_simple_extent_ndims(space) != 3) throw std::runtime_error(path_ + ": /data is not 3-D");
    hsize_t dims[3];
    H5Sget_simple_extent_dims(space, dims, nullptr);

    Hid dcpl(H5Dget_create_plist(dset), H5Pclose);
    if (H5Pget_layout(dcpl) != H5D_CHUNKED) throw std::runtime_error(path_ + ": /data is not chunked");
    hsize_t cd[3];
    H5Pget_chunk(dcpl, 3, cd);
    bool deflate = false, shuffle = false;
    int deflate_idx = -1, shuffle_idx = -1;
    int nf = H5Pget_nfilters(dcpl);
    for (int i = 0; i < nf; ++i) {
        unsigned flags = 0, cdv[8];
        size_t ncd = 8;
        char name[64];
        unsigned cfg = 0;
        H5Z_filter_t f = H5Pget_filter2(dcpl, static_cast<unsigned>(i), &flags, &ncd, cdv, sizeof name, name, &cfg);
        if (f == H5Z_FILTER_DEFLATE) {
            deflate = true;
            deflate_idx = i;
        } else if (f == H5Z_FILTER_SHUFFLE && !deflate) {   // decoding assumes shuffle runs before deflate
            shuffle = true;
            shuffle_idx = i;
        } else
            throw std::runtime_error(path_ + ": unsupported filter on /data: " + name);
    }

    size_t extent[3], cdim[3], grid[3];
    for (int i = 0; i < 3; ++i) {
        extent[i] = dims[i];
        cdim[i] = cd[i];
        grid[i] = (extent[i] + cdim[i] - 1) / cdim[i];
    }

    // Chunk index: where each chunk starts in the file. Missing (not yet written) chunks keep UINT64_MAX.
    // One pass over HDF5's chunk index (H5Dchunk_iter) instead of a lookup per chunk: ~40k chunks per hour.
    std::vector<Chunk> chunks(grid[0] * grid[1] * grid[2]);
    struct Raw {
        hsize_t off[3];
        unsigned mask;
        haddr_t addr;
        hsize_t size;
    };
    std::vector<Raw> raw;
    raw.reserve(chunks.size());
    auto cb = [](const hsize_t* offset, unsigned mask, haddr_t addr, hsize_t size, void* op) -> int {
        static_cast<std::vector<Raw>*>(op)->push_back({{offset[0], offset[1], offset[2]}, mask, addr, size});
        return H5_ITER_CONT;
    };
    if (H5Dchunk_iter(dset, H5P_DEFAULT, cb, &raw) < 0) throw std::runtime_error(path_ + ": cannot list chunks");
    // Offsets are in dataset elements (HDF5 >= 1.14.4); older releases gave chunk indices. Tell them apart: element
    // offsets are always multiples of the chunk shape.
    bool scaled = false;
    for (const auto& r : raw)
        for (int d = 0; d < 3; ++d)
            if (r.off[d] % cdim[d] != 0) scaled = true;
    for (const auto& r : raw) {
        size_t idx[3];
        for (int d = 0; d < 3; ++d) idx[d] = scaled ? r.off[d] : r.off[d] / cdim[d];
        if (idx[0] >= grid[0] || idx[1] >= grid[1] || idx[2] >= grid[2] || r.addr == HADDR_UNDEF) continue;
        Chunk& c = chunks[(idx[0] * grid[1] + idx[1]) * grid[2] + idx[2]];
        c.addr = r.addr;
        c.size = r.size;
        c.deflated = deflate && !(r.mask & (1u << deflate_idx));
        c.shuffled = shuffle && !(r.mask & (1u << shuffle_idx));
    }
    size_t written_trows = 0;   // leading chunk-rows (time) that are complete
    for (size_t ti = 0; ti < grid[0]; ++ti) {
        bool complete = true;
        for (size_t k = 0; k < grid[1] * grid[2] && complete; ++k)
            complete = chunks[ti * grid[1] * grid[2] + k].addr != UINT64_MAX;
        if (!complete) break;
        written_trows = ti + 1;
    }

    Header h;
    h.nifs = extent[1];
    h.nchans = extent[2];
    h.nrows = std::min(extent[0], written_trows * cdim[0]);

    AttrCollector da, ra;
    hsize_t idx = 0;
    H5Aiterate2(dset, H5_INDEX_NAME, H5_ITER_NATIVE, &idx, collectAttr, &da);
    idx = 0;
    H5Aiterate2(file, H5_INDEX_NAME, H5_ITER_NATIVE, &idx, collectAttr, &ra);
    h.fch1 = numAttr(da, "fch1", 0.0);
    h.foff = numAttr(da, "foff", 1.0);
    h.tsamp = numAttr(da, "tsamp", 1.0);
    h.tstart = numAttr(da, "tstart", 0.0);
    h.nfpc = static_cast<size_t>(std::max(0.0, numAttr(da, "nfpc", 0.0)));
    for (auto& [k, v] : da.items)
        if (k == "source_name" && v.size() >= 2) h.source_name = v.substr(1, v.size() - 2);
    std::string js = "{";
    bool first = true;
    for (auto* group : {&da, &ra})
        for (auto& [k, v] : group->items) {
            if (k == "CLASS" || k == "VERSION" || k == "DIMENSION_LABELS") continue;
            js += (first ? "\"" : ",\"") + jsonEscape(k) + "\":" + v;
            first = false;
        }
    h.attrs_json = js + "}";

    auto ts = readU64(file, "lft3_timestamp", h.nrows);
    auto mp = readU64(file, "lft3_missing_packets", h.nrows);

    header_ = h;
    std::copy(extent, extent + 3, extent_);
    std::copy(cdim, cdim + 3, cdim_);
    std::copy(grid, grid + 3, grid_);
    chunks_ = std::move(chunks);
    deflate_ = deflate;
    shuffle_ = shuffle;
    deflate_idx_ = deflate_idx;
    shuffle_idx_ = shuffle_idx;
    ranged_ = !shuffle && (!deflate || (!chunks_.empty() && chunks_[0].addr != UINT64_MAX && [&] {
        const size_t d = cdim[0] * cdim[1] * cdim[2] * 4, nb = (d + kStoredBlock - 1) / kStoredBlock;
        return chunks_[0].size == 2 + nb * 5 + d + 4;
    }()));
    timestamps_ = std::move(ts);
    missing_ = std::move(mp);
}

std::string Fbh5File::layoutDescription() const {
    std::ostringstream o;
    o << "shape (" << extent_[0] << ", " << extent_[1] << ", " << extent_[2] << "), chunks (" << cdim_[0] << ", "
      << cdim_[1] << ", " << cdim_[2] << "), "
      << (shuffle_ ? "shuffle+deflate" : deflate_ ? "deflate" : "no filter");
    if (deflate_) o << (ranged_ ? " (level 0: stored blocks, ranged reads)" : " (decoded per chunk)");
    uint64_t stored = 0;
    size_t n = 0;
    for (const auto& c : chunks_)
        if (c.addr != UINT64_MAX) {
            stored += c.size;
            ++n;
        }
    if (n) {
        char b[64];
        std::snprintf(b, sizeof b, ", %.2fx smaller on disk",
                      static_cast<double>(n * cdim_[0] * cdim_[1] * cdim_[2] * 4) / static_cast<double>(stored));
        o << b;
    }
    return o.str();
}

void Fbh5File::preadAll(void* dst, size_t len, uint64_t offset) const {
    auto* p = static_cast<uint8_t*>(dst);
    while (len) {
        ssize_t n = ::pread(fd_, p, len, static_cast<off_t>(offset));
        if (n < 0) {
            if (errno == EINTR) continue;
            throw std::runtime_error(path_ + ": read failed: " + std::strerror(errno));
        }
        if (n == 0) throw std::runtime_error(path_ + ": unexpected end of file");
        p += n;
        len -= static_cast<size_t>(n);
        offset += static_cast<uint64_t>(n);
    }
}

bool Fbh5File::readStored(const Chunk& c, size_t off, size_t len, uint8_t* dst, ReadScratch& s) const {
    const size_t d = cdim_[0] * cdim_[1] * cdim_[2] * 4;   // decoded chunk size
    const size_t nb = (d + kStoredBlock - 1) / kStoredBlock;
    if (c.size != 2 + nb * 5 + d + 4) return false;   // zlib header + blocks + adler32
    const size_t k0 = off / kStoredBlock, k1 = (off + len - 1) / kStoredBlock;
    const uint64_t span0 = 2 + k0 * kStoredStride;
    const uint64_t span1 = 2 + k1 * kStoredStride + 5 + (off + len - k1 * kStoredBlock);
    s.raw.resize(span1 - span0);
    preadAll(s.raw.data(), s.raw.size(), c.addr + span0);
    for (size_t k = k0; k <= k1; ++k) {
        const uint8_t* hdr = s.raw.data() + (k - k0) * kStoredStride;
        const bool last = (k == nb - 1);
        const size_t want = last ? d - k * kStoredBlock : kStoredBlock;
        const unsigned blen = hdr[1] | (hdr[2] << 8), nlen = hdr[3] | (hdr[4] << 8);
        if (hdr[0] != (last ? 1 : 0) || blen != want || (blen ^ nlen) != 0xFFFFu) return false;
        const size_t a = std::max(off, k * kStoredBlock), b = std::min(off + len, k * kStoredBlock + want);
        std::memcpy(dst + (a - off), hdr + 5 + (a - k * kStoredBlock), b - a);
    }
    return true;
}

void Fbh5File::decodeChunk(const Chunk& c, ReadScratch& s) const {
    if (s.decoded_file == this && s.decoded_addr == c.addr) return;
    const size_t d = cdim_[0] * cdim_[1] * cdim_[2] * 4;
    s.raw.resize(c.size);
    preadAll(s.raw.data(), c.size, c.addr);
    const uint8_t* src = s.raw.data();
    if (c.deflated) {
        s.inflated.resize(d);
        uLongf out = d;
        if (::uncompress(s.inflated.data(), &out, s.raw.data(), c.size) != Z_OK || out != d)
            throw std::runtime_error(path_ + ": corrupt deflate chunk at offset " + std::to_string(c.addr));
        src = s.inflated.data();
    } else if (c.size != d) {
        throw std::runtime_error(path_ + ": unexpected chunk size at offset " + std::to_string(c.addr));
    }
    if (c.shuffled) {
        // HDF5 shuffle (element size 4): all first bytes, then all second bytes, ... -> interleave back.
        const size_t n = d / 4;
        s.unshuffled.resize(d);
        const uint8_t *b0 = src, *b1 = src + n, *b2 = src + 2 * n, *b3 = src + 3 * n;
        uint8_t* o = s.unshuffled.data();
        for (size_t i = 0; i < n; ++i) {
            o[4 * i] = b0[i];
            o[4 * i + 1] = b1[i];
            o[4 * i + 2] = b2[i];
            o[4 * i + 3] = b3[i];
        }
        src = s.unshuffled.data();
    }
    s.decoded = src;
    s.decoded_file = this;
    s.decoded_addr = c.addr;
}

void Fbh5File::readChunkBytes(const Chunk& c, size_t off, size_t len, uint8_t* dst, ReadScratch& s) const {
    if (!c.deflated && !c.shuffled) {
        preadAll(dst, len, c.addr + off);
        return;
    }
    if (c.deflated && !c.shuffled && readStored(c, off, len, dst, s)) return;
    decodeChunk(c, s);
    std::memcpy(dst, s.decoded + off, len);
}

void Fbh5File::readRow(size_t row, size_t ifno, size_t c0, size_t c1, float* out, ReadScratch& s) const {
    if (row >= header_.nrows || ifno >= extent_[1] || c1 > extent_[2] || c0 >= c1)
        throw std::out_of_range("readRow: outside the dataset");
    const size_t ti = row / cdim_[0], tt = row % cdim_[0];
    const size_t ii = ifno / cdim_[1], it = ifno % cdim_[1];
    for (size_t fi = c0 / cdim_[2]; fi * cdim_[2] < c1; ++fi) {
        const size_t a = std::max(c0, fi * cdim_[2]), b = std::min(c1, (fi + 1) * cdim_[2]);
        const size_t off = ((tt * cdim_[1] + it) * cdim_[2] + (a - fi * cdim_[2])) * 4;
        readChunkBytes(chunkAt(ti, ii, fi), off, (b - a) * 4, reinterpret_cast<uint8_t*>(out + (a - c0)), s);
    }
}

size_t Fbh5File::bytesForRange(size_t c0, size_t c1) const {
    if (ranged_) return (c1 - c0) * 4;
    // whole chunks are decoded: count the (decoded) chunk bytes covered
    const size_t cw = cdim_[2];
    return ((c1 + cw - 1) / cw - c0 / cw) * cw * 4;
}

}  // namespace lft3view
