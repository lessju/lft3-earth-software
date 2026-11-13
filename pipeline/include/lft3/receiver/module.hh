#ifndef LFT3_RECEIVER_MODULE_HH
#define LFT3_RECEIVER_MODULE_HH

#include <string>
#include <vector>

#include <jetstream/module.hh>

namespace Jetstream::Modules {

struct Lft3Receiver : public Module::Config {
    // Packet source: "network" (DAQIRI), "synthetic" (generated in memory: noise + one tone) or "file"
    // (replay a tcpdump capture). The last two need neither hardware nor root.
    std::string source = "network";
    std::string sourceFile;                 // source=file: classic pcap of the channel stream
    bool sourceLoop = false;                // source=file: replay forever (timestamps continue across loops)
    bool realTime = true;                   // synthetic/file: pace at 152587.89 spectra/s; false = as fast as possible
    U64 syntheticToneChannel = 4000;        // coarse channel k (0..6143) carrying the tone
    U64 syntheticToneInput = 1;             // ADC input (0..3) carrying the tone
    F64 syntheticToneFrequency = 0.1015625; // cycles per sample within the coarse channel (-0.5..0.5)
    F64 syntheticDropFraction = 0.0;        // probability of dropping each packet

    // Network (DAQIRI raw ibverbs)
    std::string interfaceAddress = "0000:01:00.0";
    std::string sourceAddress = "10.17.16.60";   // board 100GbE IP; empty = any
    U64 udpPort = 60000;
    U64 boardId = 0;                             // expected fid; packets from other boards are dropped
    U64 masterCore = 5;
    U64 workerCore = 6;
    U64 processingCore = 7;
    U64 packetsPerBurst = 1024;
    U64 maxConcurrentBursts = 64;

    // Output layout: tensor [2 * segments, channelCount, fftLength, 2]
    U64 channelStart = 0;                        // multiple of 1024
    U64 channelCount = 6144;                     // multiple of 1024
    U64 fftLength = 128;
    U64 segments = 8;
    std::vector<U64> inputBeam = {0, 0, 1, 1};   // per ADC input: beam 0/1
    std::vector<U64> inputPolarization = {0, 1, 0, 1};   // per ADC input: 0 = X, 1 = Y
    std::string dataType = "CF32";               // CF32 or CI8
    bool fftShiftSign = false;                   // multiply sample s by (-1)^s

    // Assembly
    bool zeroFill = true;                        // emit incomplete blocks with missing packets zeroed
    U64 maxOpenBlocks = 4;
    U64 maxBlockLag = 2;
    U64 outputPoolSize = 8;

    JST_MODULE_TYPE(lft3_receiver);
    JST_MODULE_PARAMS(source, sourceFile, sourceLoop, realTime, syntheticToneChannel, syntheticToneInput,
                      syntheticToneFrequency, syntheticDropFraction,
                      interfaceAddress, sourceAddress, udpPort, boardId, masterCore, workerCore,
                      processingCore, packetsPerBurst, maxConcurrentBursts, channelStart, channelCount,
                      fftLength,
                      segments, inputBeam, inputPolarization, dataType, fftShiftSign, zeroFill,
                      maxOpenBlocks, maxBlockLag, outputPoolSize);
};

}  // namespace Jetstream::Modules

#endif  // LFT3_RECEIVER_MODULE_HH
