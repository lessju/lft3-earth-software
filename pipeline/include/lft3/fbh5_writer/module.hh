#ifndef LFT3_FBH5_WRITER_MODULE_HH
#define LFT3_FBH5_WRITER_MODULE_HH

#include <string>

#include <jetstream/module.hh>

namespace Jetstream::Modules {

struct Lft3Fbh5Writer : public Module::Config {
    std::string outputDirectory = ".";
    std::string filePrefix = "lft3";
    bool recording = true;
    F64 ncoMHz = 0.0;             // exact NCO as programmed (e.g. -2031.25); 0 = use the rounded header value
    F64 syncTimeUnix = 0.0;       // UNIX time of t = 0; 0 = receiver's estimate from packet arrival
    U64 spectraPerFile = 3600;    // start new files after this many spectra (0 = never)
    std::string sourceName = "LFT3";
    U64 telescopeId = 0;
    U64 machineId = 0;
    F64 srcRajHours = 0.0;
    F64 srcDejDegrees = 0.0;
    F64 azStart = 0.0;
    F64 zaStart = 0.0;

    JST_MODULE_TYPE(lft3_fbh5_writer);
    JST_MODULE_PARAMS(outputDirectory, filePrefix, recording, ncoMHz, syncTimeUnix, spectraPerFile,
                      sourceName, telescopeId, machineId, srcRajHours, srcDejDegrees, azStart, zaStart);
};

}  // namespace Jetstream::Modules

#endif  // LFT3_FBH5_WRITER_MODULE_HH
