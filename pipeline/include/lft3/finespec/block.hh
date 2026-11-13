#ifndef LFT3_FINESPEC_BLOCK_HH
#define LFT3_FINESPEC_BLOCK_HH

#include <string>

#include <jetstream/block.hh>

namespace Jetstream::Blocks {

struct Lft3FineSpec : public Block::Config {
    std::string mode = "fine";
    U64 beams = 2;
    U64 integrationBlocks = 149;
    U64 binsPerOutput = 1;

    JST_BLOCK_TYPE(lft3_finespec);
    JST_BLOCK_DOMAIN("LFT3");
    JST_BLOCK_NODE_SIZE(M);
    JST_BLOCK_PARAMS(mode, beams, integrationBlocks, binsPerOutput);
    JST_BLOCK_DESCRIPTION(
        "LFT3 Fine Spectrometer",
        "Fused fine FFT + Stokes I detection + integration of lft3_receiver blocks.",
        "# LFT3 Fine Spectrometer\n"
        "One CUDA kernel per block: reads the receiver's `[beams*segments, channels, fftLength, 2]` voltages "
        "(CI8 or CF32), FFTs each coarse channel's fftLength samples in shared memory, forms "
        "|X|^2 + |Y|^2, sums the segments, and accumulates `integrationBlocks` blocks.\n\n"
        "## Output\n"
        "`[beams, channels * fftLength / binsPerOutput, 1, 1]` F32, emitted once per integration (downstream blocks are "
        "skipped in between). Fine channels are coarse-channel major and centred (fftshift), ascending in "
        "frequency.\n\n"
        "## Coarse mode\n"
        "`mode: coarse` skips the FFT and outputs |X|^2 + |Y|^2 per coarse channel (the board's own "
        "152.6 kHz channels), with `binsPerOutput` grouping coarse channels: "
        "`[beams, channels / binsPerOutput, 1, 1]`. For quick-look displays.\n\n"
        "## Why fused\n"
        "Separate FFT, detect and integrate passes move ~1.3 GB per 6.7 ms block at full band, beyond GB10's "
        "memory bandwidth; fused, the kernel reads ~50 MB of int8 voltages per block.\n\n"
        "## Limits\n"
        "fftLength is a power of two up to 2048 (shared-memory FFT). Hz-level resolution (2^16-2^17) needs a "
        "cuFFT-based path (rawspec-style); see docs/pipeline.md."
    );
};

}  // namespace Jetstream::Blocks

#endif  // LFT3_FINESPEC_BLOCK_HH
