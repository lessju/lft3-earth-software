#ifndef LFT3_FINESPEC_MODULE_HH
#define LFT3_FINESPEC_MODULE_HH

#include <string>

#include <jetstream/module.hh>

namespace Jetstream::Modules {

struct Lft3FineSpec : public Module::Config {
    std::string mode = "fine";     // "fine": FFT per coarse channel; "coarse": power per coarse channel, no FFT
    U64 beams = 2;                 // the input's first axis is beams * segments
    U64 integrationBlocks = 149;   // input blocks summed into one output spectrum
    U64 binsPerOutput = 1;         // channels summed per output value (fine channels, or coarse in coarse mode)

    JST_MODULE_TYPE(lft3_finespec);
    JST_MODULE_PARAMS(mode, beams, integrationBlocks, binsPerOutput);
};

}  // namespace Jetstream::Modules

#endif  // LFT3_FINESPEC_MODULE_HH
