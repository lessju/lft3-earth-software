#ifndef LFT3_RECEIVER_BLOCK_HH
#define LFT3_RECEIVER_BLOCK_HH

#include <string>
#include <vector>

#include <jetstream/block.hh>

namespace Jetstream::Blocks {

struct Lft3Receiver : public Block::Config {
    std::string source = "network";
    std::string sourceFile;
    bool sourceLoop = false;
    bool realTime = true;
    U64 syntheticToneChannel = 4000;
    U64 syntheticToneInput = 1;
    F64 syntheticToneFrequency = 0.1015625;
    F64 syntheticDropFraction = 0.0;
    std::string interfaceAddress = "0000:01:00.0";
    std::string sourceAddress = "10.17.16.60";
    U64 udpPort = 60000;
    U64 boardId = 0;
    U64 masterCore = 5;
    U64 workerCore = 6;
    U64 processingCore = 7;
    U64 packetsPerBurst = 1024;
    U64 maxConcurrentBursts = 64;
    U64 channelStart = 0;
    U64 channelCount = 6144;
    U64 fftLength = 128;
    U64 segments = 8;
    std::vector<U64> inputBeam = {0, 0, 1, 1};
    std::vector<U64> inputPolarization = {0, 1, 0, 1};
    std::string dataType = "CF32";
    bool fftShiftSign = false;
    bool zeroFill = true;
    U64 maxOpenBlocks = 4;
    U64 maxBlockLag = 2;
    U64 outputPoolSize = 8;

    JST_BLOCK_TYPE(lft3_receiver);
    JST_BLOCK_DOMAIN("LFT3");
    JST_BLOCK_NODE_SIZE(L);
    JST_BLOCK_PARAMS(source, sourceFile, sourceLoop, realTime, syntheticToneChannel, syntheticToneInput,
                      syntheticToneFrequency, syntheticDropFraction,
                      interfaceAddress, sourceAddress, udpPort, boardId, masterCore, workerCore,
                     processingCore, packetsPerBurst, maxConcurrentBursts, channelStart, channelCount,
                     fftLength,
                     segments, inputBeam, inputPolarization, dataType, fftShiftSign, zeroFill,
                     maxOpenBlocks, maxBlockLag, outputPoolSize);
    JST_BLOCK_DESCRIPTION(
        "LFT3 Receiver",
        "Receives the LFT3 RFSoC 4x2 channel stream into GPU tensors.",
        "# LFT3 Receiver\n"
        "Captures the RFSoC 4x2 F-engine channel stream (UDP, 8256-byte payloads, 6 packets per spectrum) "
        "with NVIDIA DAQIRI (raw ibverbs, kernel bypass) and assembles it into dense GPU tensors.\n\n"
        "## Output\n"
        "`[2 * segments, channelCount, fftLength, 2]` (CF32 or CI8): beams x time-segments folded into the "
        "first axis, coarse channels, time samples, polarisations (X, Y). Each block covers "
        "`fftLength * segments` spectra (6.55 us each). Missing packets are zero-filled.\n\n"
        "## Attributes\n"
        "`timestamp` (first spectrum counter t of the block) and `lft3.*` metadata: NCO, pfb_lsb, board id, "
        "layout, per-block and cumulative packet loss, estimated UNIX time of t = 0.\n\n"
        "## Requirements\n"
        "Root (or CAP_NET_RAW + CAP_IPC_LOCK) for raw NIC access. See docs/pipeline.md."
    );
};

}  // namespace Jetstream::Blocks

#endif  // LFT3_RECEIVER_BLOCK_HH
