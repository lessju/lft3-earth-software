#include <lft3/receiver/block.hh>
#include <lft3/receiver/module.hh>
#include <jetstream/detail/block_impl.hh>

#include "module_impl.hh"

namespace Jetstream::Blocks {

struct Lft3ReceiverImpl : public Block::Impl, public DynamicConfig<Blocks::Lft3Receiver> {
    Result configure() override;
    Result define() override;
    Result create() override;

 protected:
    Result defineCounter(const std::string& key, const std::string& title, const std::string& help,
                         Tools::Snapshot<U64> Modules::Lft3ReceiverMetrics::*member);

    std::shared_ptr<Modules::Lft3Receiver> moduleConfig = std::make_shared<Modules::Lft3Receiver>();
    Modules::Lft3ReceiverImpl* moduleImpl = nullptr;
};

Result Lft3ReceiverImpl::configure() {
    moduleConfig->source = source;
    moduleConfig->sourceFile = sourceFile;
    moduleConfig->sourceLoop = sourceLoop;
    moduleConfig->realTime = realTime;
    moduleConfig->syntheticToneChannel = syntheticToneChannel;
    moduleConfig->syntheticToneInput = syntheticToneInput;
    moduleConfig->syntheticToneFrequency = syntheticToneFrequency;
    moduleConfig->syntheticDropFraction = syntheticDropFraction;
    moduleConfig->interfaceAddress = interfaceAddress;
    moduleConfig->sourceAddress = sourceAddress;
    moduleConfig->udpPort = udpPort;
    moduleConfig->boardId = boardId;
    moduleConfig->masterCore = masterCore;
    moduleConfig->workerCore = workerCore;
    moduleConfig->processingCore = processingCore;
    moduleConfig->packetsPerBurst = packetsPerBurst;
    moduleConfig->maxConcurrentBursts = maxConcurrentBursts;
    moduleConfig->channelStart = channelStart;
    moduleConfig->channelCount = channelCount;
    moduleConfig->fftLength = fftLength;
    moduleConfig->segments = segments;
    moduleConfig->inputBeam = inputBeam;
    moduleConfig->inputPolarization = inputPolarization;
    moduleConfig->dataType = dataType;
    moduleConfig->fftShiftSign = fftShiftSign;
    moduleConfig->zeroFill = zeroFill;
    moduleConfig->maxOpenBlocks = maxOpenBlocks;
    moduleConfig->maxBlockLag = maxBlockLag;
    moduleConfig->outputPoolSize = outputPoolSize;
    return Result::SUCCESS;
}

Result Lft3ReceiverImpl::defineCounter(const std::string& key, const std::string& title, const std::string& help,
                                       Tools::Snapshot<U64> Modules::Lft3ReceiverMetrics::*member) {
    return defineInterfaceMetric(key, title, help, {{"type", "label"}}, [this, member]() -> std::any {
        return std::any(jst::fmt::format("{}", moduleImpl ? (moduleImpl->metrics().*member).get() : U64{0}));
    });
}

Result Lft3ReceiverImpl::define() {
    JST_CHECK(defineInterfaceOutput("output", "Output",
                                    "CF32/CI8 [2*segments, channels, fftLength, 2] voltage blocks."));

    const auto uintField = [this](const char* key, const char* title, const char* help, const char* unit) {
        return defineInterfaceConfig(key, title, help, {{"type", "uint"}, {"unit", unit}});
    };
    JST_CHECK(defineInterfaceConfig("source", "Source", "Packet source: network (DAQIRI), synthetic or file (pcap replay).",
                                    {{"type", "dropdown"}, {"options", Parser::Sequence{
                                        Parser::Map{{"label", "Network"}, {"value", "network"}},
                                        Parser::Map{{"label", "Synthetic"}, {"value", "synthetic"}},
                                        Parser::Map{{"label", "File"}, {"value", "file"}},
                                    }}}));
    JST_CHECK(defineInterfaceConfig("sourceFile", "Capture File", "File source: classic pcap of the channel stream.",
                                    {{"type", "filepicker"}, {"extensions", Parser::MakeSequence({"pcap"})}}));
    JST_CHECK(defineInterfaceConfig("sourceLoop", "Loop", "File source: replay forever.", {{"type", "bool"}}));
    JST_CHECK(defineInterfaceConfig("realTime", "Real Time", "Synthetic/file: pace at the real spectrum rate.",
                                    {{"type", "bool"}}));
    JST_CHECK(uintField("syntheticToneChannel", "Tone Channel", "Synthetic: coarse channel of the tone.", "ch"));
    JST_CHECK(uintField("syntheticToneInput", "Tone Input", "Synthetic: ADC input of the tone.", "input"));
    JST_CHECK(defineInterfaceConfig("syntheticToneFrequency", "Tone Offset", "Synthetic: cycles/sample in the channel.",
                                    {{"type", "float"}, {"precision", 6}}));
    JST_CHECK(defineInterfaceConfig("syntheticDropFraction", "Drop Fraction", "Synthetic: fraction of packets dropped.",
                                    {{"type", "float"}, {"precision", 4}}));
    JST_CHECK(defineInterfaceConfig("interfaceAddress", "Interface", "NIC PCI address or netdev (e.g. 0000:01:00.0).",
                                    {{"type", "text"}}));
    JST_CHECK(defineInterfaceConfig("sourceAddress", "Board IP", "Board 100GbE source IP; empty matches any.",
                                    {{"type", "text"}}));
    JST_CHECK(uintField("udpPort", "UDP Port", "Destination UDP port of the channel stream.", "port"));
    JST_CHECK(uintField("boardId", "Board ID", "Expected fid in the packet header (0..7).", "id"));
    JST_CHECK(uintField("masterCore", "Master Core", "CPU core for DAQIRI control.", "core"));
    JST_CHECK(uintField("workerCore", "Worker Core", "CPU core polling the NIC queue.", "core"));
    JST_CHECK(uintField("processingCore", "Processing Core", "CPU core for header parsing and kernel launches.", "core"));
    JST_CHECK(uintField("packetsPerBurst", "Packets Per Burst", "Packets handed over per DAQIRI burst.", "packets"));
    JST_CHECK(uintField("maxConcurrentBursts", "Bursts In Flight", "Receive buffers = bursts x packets.", "bursts"));
    JST_CHECK(uintField("channelStart", "Channel Start", "First coarse channel (multiple of 1024).", "ch"));
    JST_CHECK(uintField("channelCount", "Channel Count", "Coarse channels kept (multiple of 1024).", "ch"));
    JST_CHECK(uintField("fftLength", "FFT Length", "Samples per fine FFT (time axis of the output).", "samples"));
    JST_CHECK(uintField("segments", "Segments", "FFT-length segments per block, folded into the first axis.", "x"));
    JST_CHECK(defineInterfaceConfig("inputBeam", "Input Beam", "Beam (0/1) of ADC inputs 0..3.",
                                    {{"type", "vector-inline"}, {"value_type", "uint"}}));
    JST_CHECK(defineInterfaceConfig("inputPolarization", "Input Pol", "Polarisation (0=X, 1=Y) of ADC inputs 0..3.",
                                    {{"type", "vector-inline"}, {"value_type", "uint"}}));
    JST_CHECK(defineInterfaceConfig("dataType", "Data Type", "Output sample type.",
                                    {{"type", "dropdown"}, {"options", Parser::Sequence{
                                        Parser::Map{{"label", "CF32"}, {"value", "CF32"}},
                                        Parser::Map{{"label", "CI8"}, {"value", "CI8"}},
                                    }}}));
    JST_CHECK(defineInterfaceConfig("fftShiftSign", "FFT Shift Sign",
                                    "Multiply sample s by (-1)^s so a plain FFT comes out centred.", {{"type", "bool"}}));
    JST_CHECK(defineInterfaceConfig("zeroFill", "Zero Fill", "Emit incomplete blocks with missing packets zeroed.",
                                    {{"type", "bool"}}));
    JST_CHECK(uintField("maxOpenBlocks", "Open Blocks", "Blocks being filled concurrently.", "blocks"));
    JST_CHECK(uintField("maxBlockLag", "Block Lag", "Seal a block this many blocks behind the newest.", "blocks"));
    JST_CHECK(uintField("outputPoolSize", "Output Pool", "Reusable output blocks.", "blocks"));

    using M = Modules::Lft3ReceiverMetrics;
    JST_CHECK(defineInterfaceMetric("throughput", "Throughput", "Received rate on the wire (headers + payload).",
                                    {{"type", "label"}}, [this]() -> std::any {
        return std::any(jst::fmt::format("{:.2f} Gb/s", moduleImpl ? moduleImpl->metrics().throughputGbps.get() : 0.0));
    }));
    JST_CHECK(defineCounter("packetsReceived", "Packets Received", "Packets pulled from DAQIRI.", &M::packetsReceived));
    JST_CHECK(defineCounter("packetsAccepted", "Packets Accepted", "Packets placed into a block.", &M::packetsAccepted));
    JST_CHECK(defineCounter("packetsMissing", "Packets Missing", "Zero-filled or lost with a skipped block.", &M::packetsMissing));
    JST_CHECK(defineCounter("packetsLate", "Packets Late", "Arrived after their block was sealed.", &M::packetsLate));
    JST_CHECK(defineCounter("packetsDuplicate", "Packets Duplicate", "Same (t, bindex) seen twice.", &M::packetsDuplicate));
    JST_CHECK(defineCounter("packetsNoBuffer", "Packets No Buffer", "Dropped: downstream too slow, no free block.", &M::packetsNoBuffer));
    JST_CHECK(defineCounter("packetsFiltered", "Packets Filtered", "Other board id or outside the channel selection.", &M::packetsFiltered));
    JST_CHECK(defineCounter("blocksComplete", "Blocks Complete", "Sealed with every packet.", &M::blocksComplete));
    JST_CHECK(defineCounter("blocksPartial", "Blocks Partial", "Sealed with zero-filled packets.", &M::blocksPartial));
    JST_CHECK(defineCounter("blocksSkipped", "Blocks Skipped", "No packet arrived at all.", &M::blocksSkipped));
    JST_CHECK(defineCounter("blocksEmitted", "Blocks Emitted", "Handed to the next block.", &M::blocksEmitted));
    JST_CHECK(defineCounter("restarts", "Restarts", "Stream restarts (t jumped backwards).", &M::restarts));
    JST_CHECK(defineCounter("latestTimestamp", "Latest t", "Newest spectrum counter seen.", &M::latestTimestamp));
    JST_CHECK(defineCounter("readyBlocks", "Ready Blocks", "Assembled, waiting for the pipeline.", &M::readyBlocks));
    JST_CHECK(defineCounter("freeBuffers", "Free Buffers", "Unused output blocks.", &M::freeBuffers));

    return Result::SUCCESS;
}

Result Lft3ReceiverImpl::create() {
    JST_CHECK(moduleCreate("lft3_receiver", moduleConfig, {}));
    JST_CHECK(moduleExposeOutput("output", {"lft3_receiver", "output"}));
    moduleImpl = moduleHandle("lft3_receiver")->getImpl<Modules::Lft3ReceiverImpl>();
    return Result::SUCCESS;
}

JST_REGISTER_BLOCK(Lft3ReceiverImpl, {"lft3_receiver"});

}  // namespace Jetstream::Blocks
