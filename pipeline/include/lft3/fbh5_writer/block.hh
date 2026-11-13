#ifndef LFT3_FBH5_WRITER_BLOCK_HH
#define LFT3_FBH5_WRITER_BLOCK_HH

#include <string>

#include <jetstream/block.hh>

namespace Jetstream::Blocks {

struct Lft3Fbh5Writer : public Block::Config {
    std::string outputDirectory = ".";
    std::string filePrefix = "lft3";
    bool recording = true;
    F64 ncoMHz = 0.0;
    F64 syncTimeUnix = 0.0;
    U64 spectraPerFile = 3600;
    std::string sourceName = "LFT3";
    U64 telescopeId = 0;
    U64 machineId = 0;
    F64 srcRajHours = 0.0;
    F64 srcDejDegrees = 0.0;
    F64 azStart = 0.0;
    F64 zaStart = 0.0;

    JST_BLOCK_TYPE(lft3_fbh5_writer);
    JST_BLOCK_DOMAIN("LFT3");
    JST_BLOCK_NODE_SIZE(M);
    JST_BLOCK_PARAMS(outputDirectory, filePrefix, recording, ncoMHz, syncTimeUnix, spectraPerFile,
                     sourceName, telescopeId, machineId, srcRajHours, srcDejDegrees, azStart, zaStart);
    JST_BLOCK_DESCRIPTION(
        "LFT3 FBH5 Writer",
        "Writes integrated LFT3 spectra to filterbank-HDF5, one file per beam.",
        "# LFT3 FBH5 Writer\n"
        "Writes `[beams, fine channels, 1, 1]` power spectra (F32, or the real part of CF32) to FBH5 files, "
        "the Breakthrough Listen filterbank-HDF5 format read by blimpy, turboSETI and hyperseti.\n\n"
        "The frequency axis (fch1, foff, nfpc) and time axis (tstart, tsamp) are computed from the `lft3.*` "
        "attributes of the LFT3 Receiver upstream; tsamp is measured from the spacing of consecutive "
        "integrations, so files are opened when the second spectrum arrives. Per-spectrum timestamps and "
        "zero-filled packet counts are stored in the `lft3_timestamp` and `lft3_missing_packets` datasets.\n\n"
        "Files: `<outputDirectory>/<filePrefix>_<UTC start>_beam<N>.h5`."
    );
};

}  // namespace Jetstream::Blocks

#endif  // LFT3_FBH5_WRITER_BLOCK_HH
