#include "module_impl.hh"

#include <limits>
#include <set>

#include <jetstream/memory/axis.hh>

namespace Jetstream::Modules {

bool Lft3ReceiverImpl::BuildLayout(const Lft3Receiver& c, Lft3::Layout& out, std::string& error) {
    using namespace Lft3;

    if (c.channelCount == 0 || c.channelCount % kChannelsPerPacket != 0 ||
        c.channelStart % kChannelsPerPacket != 0 || c.channelStart + c.channelCount > kTotalChannels) {
        error = "channelStart and channelCount must be multiples of 1024 within 0..6144";
        return false;
    }
    if (c.fftLength < 2 || c.fftLength % 2 != 0) {
        error = "fftLength must be even and >= 2";
        return false;
    }
    if (c.segments == 0) {
        error = "segments must be >= 1";
        return false;
    }
    if (c.inputBeam.size() != kInputs || c.inputPolarization.size() != kInputs) {
        error = "inputBeam and inputPolarization must have 4 entries (one per ADC input)";
        return false;
    }
    std::set<U64> pairs;
    for (U64 i = 0; i < kInputs; i++) {
        if (c.inputBeam[i] >= Layout::kBeams || c.inputPolarization[i] >= Layout::kPols) {
            error = "inputBeam must be 0/1 and inputPolarization 0 (X) / 1 (Y)";
            return false;
        }
        pairs.insert(c.inputBeam[i] * 2 + c.inputPolarization[i]);
    }
    if (pairs.size() != kInputs) {
        error = "every (beam, polarization) pair must be used by exactly one input";
        return false;
    }

    Layout l;
    l.channelStart = c.channelStart;
    l.channelCount = c.channelCount;
    l.fftLength = c.fftLength;
    l.segments = c.segments;
    for (U64 i = 0; i < kInputs; i++) {
        l.inputBeam[i] = static_cast<std::uint8_t>(c.inputBeam[i]);
        l.inputPol[i] = static_cast<std::uint8_t>(c.inputPolarization[i]);
    }

    // Job fields are 32-bit; keep every per-block index representable, and the block byte size
    // (8 bytes per CF32 element) free of overflow.
    U64 bytes = 0;
    if (c.fftLength > std::numeric_limits<std::uint32_t>::max() ||
        c.segments > std::numeric_limits<std::uint32_t>::max() ||
        __builtin_mul_overflow(c.fftLength, c.segments, &bytes) ||
        bytes > std::numeric_limits<std::uint32_t>::max() ||
        l.packetsPerBlock() > std::numeric_limits<std::uint32_t>::max() ||
        __builtin_mul_overflow(l.aspects() * l.channelCount * Layout::kPols, c.fftLength * 8, &bytes)) {
        error = "block is too large (fftLength * segments)";
        return false;
    }

    out = l;
    return true;
}

Result Lft3ReceiverImpl::validate() {
    const auto& c = *candidate();
    std::string error;
    if (!BuildLayout(c, validatedLayout, error)) {
        JST_ERROR("[LFT3_RECEIVER] {}.", error);
        return Result::ERROR;
    }
    if (c.dataType != "CF32" && c.dataType != "CI8") {
        JST_ERROR("[LFT3_RECEIVER] dataType must be CF32 or CI8.");
        return Result::ERROR;
    }
    if (c.fftShiftSign && c.dataType != "CF32") {
        JST_ERROR("[LFT3_RECEIVER] fftShiftSign requires dataType CF32.");
        return Result::ERROR;
    }
    if (c.boardId > 7 || c.udpPort == 0 || c.udpPort > 65535) {
        JST_ERROR("[LFT3_RECEIVER] boardId must be 0..7 and udpPort 1..65535.");
        return Result::ERROR;
    }
    if (c.packetsPerBurst == 0 || c.maxConcurrentBursts < 2) {
        JST_ERROR("[LFT3_RECEIVER] packetsPerBurst must be > 0 and maxConcurrentBursts >= 2.");
        return Result::ERROR;
    }
    if (c.maxOpenBlocks == 0 || c.outputPoolSize < c.maxOpenBlocks + 2) {
        JST_ERROR("[LFT3_RECEIVER] outputPoolSize must be at least maxOpenBlocks + 2.");
        return Result::ERROR;
    }
    if (c.source != "network" && c.source != "synthetic" && c.source != "file") {
        JST_ERROR("[LFT3_RECEIVER] source must be 'network', 'synthetic' or 'file'.");
        return Result::ERROR;
    }
    if (c.source == "file" && c.sourceFile.empty()) {
        JST_ERROR("[LFT3_RECEIVER] source 'file' needs sourceFile (a pcap of the channel stream).");
        return Result::ERROR;
    }
    if (c.source == "synthetic" &&
        (c.syntheticToneChannel >= Lft3::kTotalChannels || c.syntheticToneInput >= Lft3::kInputs ||
         !(c.syntheticToneFrequency > -0.5 && c.syntheticToneFrequency < 0.5) ||
         !(c.syntheticDropFraction >= 0.0 && c.syntheticDropFraction < 1.0))) {
        JST_ERROR("[LFT3_RECEIVER] Invalid synthetic tone/drop settings.");
        return Result::ERROR;
    }
    if (c.interfaceAddress.empty()) {
        JST_ERROR("[LFT3_RECEIVER] interfaceAddress is empty.");
        return Result::ERROR;
    }
    return Result::SUCCESS;
}

Result Lft3ReceiverImpl::define() {
    JST_CHECK(defineTaint(Module::Taint::CLEAN));
    JST_CHECK(defineInterfaceOutput("output"));
    return Result::SUCCESS;
}

Result Lft3ReceiverImpl::create() {
    layout = validatedLayout;

    const Shape shape = {layout.aspects(), layout.channelCount, layout.fftLength, Lft3::Layout::kPols};
    JST_CHECK(outputTensor.create(device(), NameToDataType(dataType), shape));
    JST_CHECK(SetSignalAxes(outputTensor, {.sample = Index{2}, .batch = std::nullopt, .channel = Index{1}}));

    // Static attributes: describe the layout so downstream blocks (the FBH5 writer) can compute the
    // frequency and time axes without duplicating configuration.
    JST_CHECK(outputTensor.setAttribute("timestamp", U64{0}));
    JST_CHECK(outputTensor.setAttribute("lft3.channelStart", layout.channelStart));
    JST_CHECK(outputTensor.setAttribute("lft3.channelCount", layout.channelCount));
    JST_CHECK(outputTensor.setAttribute("lft3.fftLength", layout.fftLength));
    JST_CHECK(outputTensor.setAttribute("lft3.segments", layout.segments));
    JST_CHECK(outputTensor.setAttribute("lft3.spectraPerBlock", layout.spectraPerBlock()));
    JST_CHECK(outputTensor.setAttribute("lft3.fftShiftSign", fftShiftSign));
    JST_CHECK(outputTensor.setAttribute("lft3.boardId", boardId));
    for (int i = 0; i < 4; i++) {
        JST_CHECK(outputTensor.setAttribute(jst::fmt::format("lft3.nco{}", i), I64{0}));
        JST_CHECK(outputTensor.setAttribute(jst::fmt::format("lft3.pfbLsb{}", i), U64{0}));
    }
    JST_CHECK(outputTensor.setAttribute("lft3.blockMissingPackets", U64{0}));
    JST_CHECK(outputTensor.setAttribute("lft3.totalMissingPackets", U64{0}));
    JST_CHECK(outputTensor.setAttribute("lft3.totalExpectedPackets", U64{0}));
    JST_CHECK(outputTensor.setAttribute("lft3.unixTimeAtT0", F64{0.0}));

    outputs()["output"].produced(name(), "output", outputTensor);
    return Result::SUCCESS;
}

Result Lft3ReceiverImpl::reconfigure() {
    return Result::RECREATE;
}

}  // namespace Jetstream::Modules
