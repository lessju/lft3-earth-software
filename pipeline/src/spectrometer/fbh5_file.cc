#include "fbh5_file.hh"

#include <algorithm>
#include <atomic>
#include <cstring>
#include <thread>

#include <hdf5.h>
#include <zlib.h>

namespace Lft3 {

namespace {

constexpr const char* kTimestampDataset = "lft3_timestamp";
constexpr const char* kMissingDataset = "lft3_missing_packets";

bool WriteRootDouble(hid_t file, const std::string& name, double value) {
    const hid_t space = H5Screate(H5S_SCALAR);
    const hid_t attr = H5Acreate2(file, name.c_str(), H5T_IEEE_F64LE, space, H5P_DEFAULT, H5P_DEFAULT);
    const bool ok = attr >= 0 && H5Awrite(attr, H5T_NATIVE_DOUBLE, &value) >= 0;
    if (attr >= 0) H5Aclose(attr);
    H5Sclose(space);
    return ok;
}

bool WriteRootString(hid_t file, const std::string& name, const std::string& value) {
    const hid_t type = H5Tcopy(H5T_C_S1);
    H5Tset_size(type, value.size() + 1);
    const hid_t space = H5Screate(H5S_SCALAR);
    const hid_t attr = H5Acreate2(file, name.c_str(), type, space, H5P_DEFAULT, H5P_DEFAULT);
    const bool ok = attr >= 0 && H5Awrite(attr, type, value.c_str()) >= 0;
    if (attr >= 0) H5Aclose(attr);
    H5Sclose(space);
    H5Tclose(type);
    return ok;
}

// A 1-D unlimited uint64 dataset, extended by one element per append.
bool CreateScalarSeries(hid_t file, const char* name) {
    const hsize_t dims[1] = {0};
    const hsize_t maxDims[1] = {H5S_UNLIMITED};
    const hsize_t chunk[1] = {1024};
    const hid_t space = H5Screate_simple(1, dims, maxDims);
    const hid_t props = H5Pcreate(H5P_DATASET_CREATE);
    H5Pset_chunk(props, 1, chunk);
    const hid_t ds = H5Dcreate2(file, name, H5T_STD_U64LE, space, H5P_DEFAULT, props, H5P_DEFAULT);
    const bool ok = ds >= 0;
    if (ds >= 0) H5Dclose(ds);
    H5Pclose(props);
    H5Sclose(space);
    return ok;
}

// Extend a [time, 1, nchans] dataset to row+1 spectra and write one whole spectrum into it (HDF5 splits it
// into the frequency chunks and applies the filters).
bool WriteRow(hid_t ds, hid_t memType, std::uint64_t row, std::uint64_t nchans, const void* data) {
    const hsize_t extent[3] = {row + 1, 1, nchans};
    if (H5Dset_extent(ds, extent) < 0) return false;
    const hid_t fileSpace = H5Dget_space(ds);
    const hsize_t start[3] = {row, 0, 0};
    const hsize_t count[3] = {1, 1, nchans};
    bool ok = H5Sselect_hyperslab(fileSpace, H5S_SELECT_SET, start, nullptr, count, nullptr) >= 0;
    const hid_t memSpace = H5Screate_simple(3, count, nullptr);
    ok = ok && H5Dwrite(ds, memType, memSpace, fileSpace, H5P_DEFAULT, data) >= 0;
    H5Sclose(memSpace);
    H5Sclose(fileSpace);
    return ok;
}

// HDF5's shuffle filter: byte j of element i goes to j * n + i (n elements of `size` bytes); a remainder that is
// not a whole element is copied unchanged at the end.
void Shuffle(const std::uint8_t* in, std::size_t bytes, std::size_t size, std::uint8_t* out) {
    const std::size_t n = bytes / size;
    for (std::size_t j = 0; j < size; j++) {
        std::uint8_t* o = out + j * n;
        const std::uint8_t* p = in + j;
        for (std::size_t i = 0; i < n; i++) o[i] = p[i * size];
    }
    std::memcpy(out + n * size, in + n * size, bytes - n * size);
}

// HDF5's deflate filter: a zlib stream (compress2) of the whole chunk.
bool Deflate(const std::uint8_t* in, std::size_t bytes, int level, std::vector<std::uint8_t>& out) {
    uLongf len = compressBound(static_cast<uLong>(bytes));
    out.resize(len);
    if (compress2(out.data(), &len, in, static_cast<uLong>(bytes), level) != Z_OK) return false;
    out.resize(len);
    return true;
}

bool WriteChunk(hid_t ds, std::uint64_t row, std::uint64_t firstChannel, const std::vector<std::uint8_t>& buf) {
    const hsize_t offset[3] = {row, 0, firstChannel};
    return H5Dwrite_chunk(ds, H5P_DEFAULT, 0, offset, buf.size(), buf.data()) >= 0;
}

// Chunk width (channels) of a [time, 1, nchans] dataset, 0 if it is not chunked [1, 1, w].
std::uint64_t ChunkChannels(hid_t ds) {
    const hid_t dcpl = H5Dget_create_plist(ds);
    hsize_t dims[3] = {0, 0, 0};
    const int rank = H5Pget_chunk(dcpl, 3, dims);
    H5Pclose(dcpl);
    return rank == 3 && dims[0] == 1 && dims[1] == 1 ? dims[2] : 0;
}

}  // namespace

Fbh5File::~Fbh5File() {
    close();
}

bool Fbh5File::open(const std::string& path, const Fbh5Header& h,
                    const std::map<std::string, double>& numericAttributes,
                    const std::map<std::string, std::string>& stringAttributes, std::string& error) {
    close();
    fb_ = {};
    filterbank_header_t& fh = fb_.header;
    fh.machine_id = h.machineId;
    fh.telescope_id = h.telescopeId;
    fh.data_type = 1;               // filterbank
    fh.barycentric = 0;
    fh.pulsarcentric = 0;
    fh.src_raj = h.srcRajHours;
    fh.src_dej = h.srcDejDegrees;
    fh.az_start = h.azStart;
    fh.za_start = h.zaStart;
    fh.fch1 = h.fch1MHz;
    fh.foff = h.foffMHz;
    fh.nchans = static_cast<int>(h.nchans);
    fh.nbeams = h.nbeams;
    fh.ibeam = h.ibeam;
    fh.nbits = 32;
    fh.tstart = h.tstartMjd;
    fh.tsamp = h.tsampSeconds;
    fh.nifs = 1;
    fh.nfpc = h.fineChannelsPerCoarse;
    std::strncpy(fh.source_name, h.sourceName.c_str(), sizeof(fh.source_name) - 1);
    std::strncpy(fh.rawdatafile, h.rawDataFile.c_str(), sizeof(fh.rawdatafile) - 1);

    // Chunks of one spectrum x kChunkChannels (filterbankc99 takes the chunk shape from these). append() writes
    // whole rows itself, so nchans need not be a multiple of the chunk width.
    fb_.ntimes_per_write = 1;
    fb_.nchans_per_write = std::min<std::uint64_t>(h.nchans, kChunkChannels);
    fb_.ds_data.filter_flag = H5_FILTER_FLAG_SHUFFLE_DEFLATE_0 + 1;   // shuffle + deflate level 1
    fb_.ds_mask.filter_flag = H5_FILTER_FLAG_DEFLATE_4;

    if (filterbank_h5_open_explicit(path.c_str(), &fb_, H5Tcopy(H5T_IEEE_F32LE), H5P_DEFAULT) != 0) {
        error = "cannot create FBH5 file '" + path + "'";
        fb_ = {};
        return false;
    }
    open_ = true;
    path_ = path;
    written_ = 0;
    mask_.assign(h.nchans, 0);

    // Parallel chunk compression when the spectrum is a whole number of [1, 1, w] chunks in both datasets.
    chunkChannels_ = ChunkChannels(fb_.ds_data.D_id);
    compressThreads_ = h.compressThreads;
    directChunks_ = compressThreads_ > 1 && chunkChannels_ > 0 && h.nchans % chunkChannels_ == 0 &&
                    ChunkChannels(fb_.ds_mask.D_id) == chunkChannels_;
    if (directChunks_) {
        chunkOut_.assign(h.nchans / chunkChannels_, {});
        const std::vector<std::uint8_t> zeros(chunkChannels_, 0);
        directChunks_ = Deflate(zeros.data(), zeros.size(), 4, zeroMaskChunk_);   // the mask's deflate 4
    }

    bool ok = CreateScalarSeries(fb_.file_id, kTimestampDataset) && CreateScalarSeries(fb_.file_id, kMissingDataset);
    for (const auto& [k, v] : numericAttributes) ok = ok && WriteRootDouble(fb_.file_id, k, v);
    for (const auto& [k, v] : stringAttributes) ok = ok && WriteRootString(fb_.file_id, k, v);
    if (!ok) {
        error = "cannot write LFT3 metadata to '" + path + "'";
        close();
        return false;
    }
    return true;
}

bool Fbh5File::appendScalar(const char* name, std::uint64_t value, std::string& error) {
    const hid_t ds = H5Dopen2(fb_.file_id, name, H5P_DEFAULT);
    if (ds < 0) {
        error = std::string("missing dataset ") + name;
        return false;
    }
    const hsize_t newSize[1] = {written_ + 1};
    bool ok = H5Dset_extent(ds, newSize) >= 0;
    const hid_t fileSpace = H5Dget_space(ds);
    const hsize_t start[1] = {written_};
    const hsize_t count[1] = {1};
    ok = ok && H5Sselect_hyperslab(fileSpace, H5S_SELECT_SET, start, nullptr, count, nullptr) >= 0;
    const hid_t memSpace = H5Screate_simple(1, count, nullptr);
    ok = ok && H5Dwrite(ds, H5T_NATIVE_UINT64, memSpace, fileSpace, H5P_DEFAULT, &value) >= 0;
    H5Sclose(memSpace);
    H5Sclose(fileSpace);
    H5Dclose(ds);
    if (!ok) error = std::string("cannot append to ") + name;
    return ok;
}

bool Fbh5File::append(const float* spectrum, std::uint64_t timestamp, std::uint64_t missingPackets,
                      std::string& error) {
    if (!open_) {
        error = "file is not open";
        return false;
    }
    const std::uint64_t nchans = mask_.size();
    const bool ok = directChunks_
        ? writeChunksParallel(spectrum, written_)
        : WriteRow(fb_.ds_data.D_id, H5T_NATIVE_FLOAT, written_, nchans, spectrum) &&
              WriteRow(fb_.ds_mask.D_id, H5T_NATIVE_UINT8, written_, nchans, mask_.data());
    if (!ok) {
        error = "cannot append spectrum to '" + path_ + "'";
        return false;
    }
    if (!appendScalar(kTimestampDataset, timestamp, error) || !appendScalar(kMissingDataset, missingPackets, error)) {
        return false;
    }
    written_++;
    // Flush metadata each spectrum (cheap at ~1 spectrum/s) so a crash leaves a readable file.
    H5Fflush(fb_.file_id, H5F_SCOPE_LOCAL);
    return true;
}

bool Fbh5File::writeChunksParallel(const float* spectrum, std::uint64_t row) {
    const std::uint64_t nchans = mask_.size();
    const hsize_t extent[3] = {row + 1, 1, nchans};
    if (H5Dset_extent(fb_.ds_data.D_id, extent) < 0 || H5Dset_extent(fb_.ds_mask.D_id, extent) < 0) return false;
    // Shuffle + deflate 1 per chunk on compressThreads_ threads (HDF5 is not thread-safe: only the writes below
    // call it, from this thread).
    const std::size_t nChunks = chunkOut_.size();
    const std::size_t chunkBytes = chunkChannels_ * sizeof(float);
    std::atomic<std::size_t> next{0};
    std::atomic<bool> failed{false};
    const auto work = [&]() {
        std::vector<std::uint8_t> shuffled(chunkBytes);
        for (std::size_t c = next++; c < nChunks; c = next++) {
            Shuffle(reinterpret_cast<const std::uint8_t*>(spectrum + c * chunkChannels_), chunkBytes, sizeof(float),
                    shuffled.data());
            if (!Deflate(shuffled.data(), chunkBytes, 1, chunkOut_[c])) failed = true;
        }
    };
    const unsigned workers = static_cast<unsigned>(std::min<std::size_t>(compressThreads_, nChunks));
    std::vector<std::thread> pool;
    for (unsigned i = 1; i < workers; i++) pool.emplace_back(work);
    work();
    for (auto& t : pool) t.join();
    if (failed) return false;
    for (std::size_t c = 0; c < nChunks; c++) {
        if (!WriteChunk(fb_.ds_data.D_id, row, c * chunkChannels_, chunkOut_[c]) ||
            !WriteChunk(fb_.ds_mask.D_id, row, c * chunkChannels_, zeroMaskChunk_)) {
            return false;
        }
    }
    return true;
}

void Fbh5File::close() {
    if (open_) {
        filterbank_h5_close(&fb_);
        open_ = false;
    }
    fb_ = {};
}

}  // namespace Lft3
