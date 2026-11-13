#include "module_impl.hh"

#include <any>
#include <cmath>
#include <filesystem>
#include <map>

#include "lft3_config.hh"
#include "detail/axes.hh"
#include "../receiver/detail/packet.hh"

namespace Jetstream::Modules {

namespace {
const char* kRequiredAttributes[] = {
    "timestamp", "lft3.channelStart", "lft3.fftLength", "lft3.spectraPerBlock",
    "lft3.totalMissingPackets", "lft3.unixTimeAtT0", "lft3.nco0",
};
}  // namespace

U64 Lft3Fbh5WriterImpl::readU64(const std::string& key) const {
    const std::any v = input.attribute(key);
    if (const auto* p = std::any_cast<U64>(&v)) return *p;
    return 0;
}

I64 Lft3Fbh5WriterImpl::readI64(const std::string& key) const {
    const std::any v = input.attribute(key);
    if (const auto* p = std::any_cast<I64>(&v)) return *p;
    return 0;
}

bool Lft3Fbh5WriterImpl::readBool(const std::string& key) const {
    const std::any v = input.attribute(key);
    if (const auto* p = std::any_cast<bool>(&v)) return *p;
    return false;
}

F64 Lft3Fbh5WriterImpl::readF64(const std::string& key) const {
    const std::any v = input.attribute(key);
    if (const auto* p = std::any_cast<F64>(&v)) return *p;
    return 0.0;
}

Result Lft3Fbh5WriterImpl::validate() {
    if (!inputs().contains("input")) {
        return Result::SUCCESS;
    }
    const Tensor& in = inputs().at("input").tensor;
    if (!in.validShape() || in.size() == 0) {
        return Result::SUCCESS;
    }
    if (in.rank() != 4 || in.shape(2) != 1 || in.shape(3) != 1) {
        JST_ERROR("[LFT3_FBH5_WRITER] Expected input [beams, channels, 1, 1], got {}.", in.shape());
        return Result::ERROR;
    }
    if (in.dtype() != DataType::F32 && in.dtype() != DataType::CF32) {
        JST_ERROR("[LFT3_FBH5_WRITER] Expected F32 or CF32 input, got {}.", in.dtype());
        return Result::ERROR;
    }
    for (const char* key : kRequiredAttributes) {
        if (!in.hasAttribute(key)) {
            JST_ERROR("[LFT3_FBH5_WRITER] Input lacks attribute '{}': it must come from an lft3_receiver "
                      "through blocks that propagate attributes.", key);
            return Result::ERROR;
        }
    }
    const auto& c = *candidate();
    if (c.filePrefix.empty() || c.filePrefix.find('/') != std::string::npos) {
        JST_ERROR("[LFT3_FBH5_WRITER] filePrefix must be a plain name.");
        return Result::ERROR;
    }
    return Result::SUCCESS;
}

Result Lft3Fbh5WriterImpl::define() {
    JST_CHECK(defineInterfaceInput("input"));
    return Result::SUCCESS;
}

Result Lft3Fbh5WriterImpl::create() {
    input = inputs().at("input").tensor;
    beams = input.shape(0);
    channels = input.shape(1);
    spectraPerBlock = readU64("lft3.spectraPerBlock");
    channelStart = readU64("lft3.channelStart");
    fftLength = readU64("lft3.fftLength");
    if (spectraPerBlock == 0 || fftLength == 0 || channels % fftLength != 0) {
        JST_ERROR("[LFT3_FBH5_WRITER] Inconsistent receiver attributes (spectraPerBlock {}, fftLength {}, channels {}).",
                  spectraPerBlock, fftLength, channels);
        return Result::ERROR;
    }
    if (recording) {
        std::error_code ec;
        std::filesystem::create_directories(outputDirectory, ec);
        if (ec) {
            JST_ERROR("[LFT3_FBH5_WRITER] Cannot create '{}': {}.", outputDirectory, ec.message());
            return Result::ERROR;
        }
    }
    timeline = std::make_unique<Lft3::IntegrationTimeline>(spectraPerBlock);
    pending.clear();
    row.resize(channels);
    files.clear();
    return Result::SUCCESS;
}

Result Lft3Fbh5WriterImpl::destroy() {
    if (!pending.empty() && files.empty()) {
        JST_WARN("[LFT3_FBH5_WRITER] Stopped after a single spectrum; it was not written (tsamp unknown).");
    }
    closeFiles();
    timeline.reset();
    return Result::SUCCESS;
}

Result Lft3Fbh5WriterImpl::reconfigure() {
    return Result::RECREATE;
}

void Lft3Fbh5WriterImpl::closeFiles() {
    for (auto& f : files) {
        if (f && f->isOpen()) {
            JST_INFO("[LFT3_FBH5_WRITER] Closed '{}' ({} spectra).", f->path(), f->spectraWritten());
        }
    }
    files.clear();
    currentFile.publish(std::string());
}

Result Lft3Fbh5WriterImpl::openFiles(U64 startT) {
    closeFiles();

    // Exact NCO if configured, otherwise the (rounded, whole-MHz) value from the packet header.
    F64 nco = ncoMHz;
    if (nco == 0.0) {
        nco = static_cast<F64>(readI64("lft3.nco0"));
        JST_WARN("[LFT3_FBH5_WRITER] ncoMHz not set: using the header NCO {} MHz, which the firmware rounds "
                 "to whole MHz. Set ncoMHz for an exact frequency axis.", nco);
    }
    const F64 t0 = syncTimeUnix > 0.0 ? syncTimeUnix : readF64("lft3.unixTimeAtT0");
    const F64 startUnix = t0 + static_cast<F64>(startT) / Lft3::kSpectraPerSecond;
    const auto axes = Lft3::ComputeFineAxes(nco, channelStart, channels / fftLength, fftLength);

    Lft3::Fbh5Header h;
    h.sourceName = sourceName;
    h.fch1MHz = axes.fch1MHz;
    h.foffMHz = axes.foffMHz;
    h.nchans = axes.nchans;
    h.tstartMjd = Lft3::UnixToMjd(startUnix);
    h.tsampSeconds = static_cast<F64>(timeline->interval()) / Lft3::kSpectraPerSecond;
    h.telescopeId = static_cast<int>(telescopeId);
    h.machineId = static_cast<int>(machineId);
    h.nbeams = static_cast<int>(beams);
    h.srcRajHours = srcRajHours;
    h.srcDejDegrees = srcDejDegrees;
    h.azStart = azStart;
    h.zaStart = zaStart;
    h.fineChannelsPerCoarse = static_cast<int>(fftLength);
    tsampSeconds.publish(h.tsampSeconds);

    std::map<std::string, double> num = {
        {"lft3_nco_mhz", nco},
        {"lft3_unix_time_at_t0", t0},
        {"lft3_start_t", static_cast<double>(startT)},
        {"lft3_channel_start", static_cast<double>(channelStart)},
        {"lft3_fft_length", static_cast<double>(fftLength)},
        {"lft3_spectra_per_integration", static_cast<double>(timeline->interval())},
        {"lft3_spectra_per_block", static_cast<double>(spectraPerBlock)},
        {"lft3_board_id", static_cast<double>(readU64("lft3.boardId"))},
        {"lft3_fft_shift_sign", readBool("lft3.fftShiftSign") ? 1.0 : 0.0},
    };
    for (int i = 0; i < 4; i++) {
        num[jst::fmt::format("lft3_header_nco{}", i)] = static_cast<double>(readI64(jst::fmt::format("lft3.nco{}", i)));
        num[jst::fmt::format("lft3_pfb_lsb{}", i)] = static_cast<double>(readU64(jst::fmt::format("lft3.pfbLsb{}", i)));
    }
    const std::map<std::string, std::string> str = {
        {"lft3_software", std::string("lft3-earth-software plugin ") + LFT3_PLUGIN_VERSION},
        {"lft3_time_reference", syncTimeUnix > 0.0 ? "configured syncTimeUnix"
                                                   : "estimated from packet arrival (~ms accuracy)"},
        {"lft3_frequency_convention", "RF = -nco + baseband offset; frequency ascends with channel index"},
    };

    const std::string stamp = Lft3::UtcStamp(startUnix);
    for (U64 b = 0; b < beams; b++) {
        h.ibeam = static_cast<int>(b + 1);
        const std::string path = jst::fmt::format("{}/{}_{}_beam{}.h5", outputDirectory, filePrefix, stamp, b);
        auto file = std::make_unique<Lft3::Fbh5File>();
        std::string error;
        if (!file->open(path, h, num, str, error)) {
            JST_ERROR("[LFT3_FBH5_WRITER] {}.", error);
            return Result::ERROR;
        }
        JST_INFO("[LFT3_FBH5_WRITER] Writing '{}': fch1 {:.6f} MHz, foff {:.6f} kHz, {} channels, tsamp {:.4f} s.",
                 path, h.fch1MHz, h.foffMHz * 1e3, h.nchans, h.tsampSeconds);
        files.push_back(std::move(file));
    }
    currentFile.publish(files.empty() ? std::string() : files.front()->path());
    return Result::SUCCESS;
}

Result Lft3Fbh5WriterImpl::consume(const float* host, U64 stride) {
    if (!recording) {
        return Result::SUCCESS;
    }

    const auto writeAll = [&](const float* data, U64 startT, U64 missing) -> Result {
        if (!files.empty() && spectraPerFile > 0 && files.front()->spectraWritten() >= spectraPerFile) {
            closeFiles();
        }
        if (files.empty()) {
            JST_CHECK(openFiles(startT));
        }
        for (U64 b = 0; b < beams; b++) {
            for (U64 c = 0; c < channels; c++) {
                row[c] = data[(b * channels + c) * stride];
            }
            std::string error;
            if (!files[b]->append(row.data(), startT, missing, error)) {
                JST_ERROR("[LFT3_FBH5_WRITER] {}.", error);
                return Result::ERROR;
            }
        }
        spectraWritten.publish(spectraWritten.get() + 1);
        return Result::SUCCESS;
    };

    Lft3::IntegrationTimeline::Spectrum current;
    const auto step = timeline->push(readU64("timestamp"), readU64("lft3.totalMissingPackets"), current);
    const U64 elements = beams * channels * stride;

    switch (step) {
        case Lft3::IntegrationTimeline::Step::Discontinuity:
            if (!files.empty() || !pending.empty()) {
                JST_WARN("[LFT3_FBH5_WRITER] Time discontinuity (re-sync or lost blocks): starting new files.");
                discontinuities.publish(discontinuities.get() + 1);
            }
            closeFiles();
            [[fallthrough]];
        case Lft3::IntegrationTimeline::Step::First:
            pending.assign(host, host + elements);
            return Result::SUCCESS;
        case Lft3::IntegrationTimeline::Step::Next:
            if (!pending.empty()) {
                const auto& first = timeline->firstSpectrum();
                JST_CHECK(writeAll(pending.data(), first.startT, first.missing));
                pending.clear();
            }
            return writeAll(host, current.startT, current.missing);
    }
    return Result::SUCCESS;
}

}  // namespace Jetstream::Modules
