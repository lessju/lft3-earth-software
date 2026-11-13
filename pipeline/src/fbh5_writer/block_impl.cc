#include <lft3/fbh5_writer/block.hh>
#include <lft3/fbh5_writer/module.hh>
#include <jetstream/detail/block_impl.hh>

#include "module_impl.hh"

namespace Jetstream::Blocks {

struct Lft3Fbh5WriterImpl : public Block::Impl, public DynamicConfig<Blocks::Lft3Fbh5Writer> {
    Result configure() override;
    Result define() override;
    Result create() override;

 protected:
    std::shared_ptr<Modules::Lft3Fbh5Writer> moduleConfig = std::make_shared<Modules::Lft3Fbh5Writer>();
    Modules::Lft3Fbh5WriterImpl* moduleImpl = nullptr;
};

Result Lft3Fbh5WriterImpl::configure() {
    moduleConfig->outputDirectory = outputDirectory;
    moduleConfig->filePrefix = filePrefix;
    moduleConfig->recording = recording;
    moduleConfig->ncoMHz = ncoMHz;
    moduleConfig->syncTimeUnix = syncTimeUnix;
    moduleConfig->spectraPerFile = spectraPerFile;
    moduleConfig->sourceName = sourceName;
    moduleConfig->telescopeId = telescopeId;
    moduleConfig->machineId = machineId;
    moduleConfig->srcRajHours = srcRajHours;
    moduleConfig->srcDejDegrees = srcDejDegrees;
    moduleConfig->azStart = azStart;
    moduleConfig->zaStart = zaStart;
    return Result::SUCCESS;
}

Result Lft3Fbh5WriterImpl::define() {
    JST_CHECK(defineInterfaceInput("input", "Input", "Integrated power [beams, fine channels, 1, 1] from lft3_receiver."));

    JST_CHECK(defineInterfaceConfig("outputDirectory", "Directory", "Where files are written (created if needed).",
                                    {{"type", "text"}}));
    JST_CHECK(defineInterfaceConfig("filePrefix", "File Prefix", "Files are <prefix>_<UTC>_beam<N>.h5.", {{"type", "text"}}));
    JST_CHECK(defineInterfaceConfig("recording", "Recording", "Write files.", {{"type", "bool"}}));
    JST_CHECK(defineInterfaceConfig("ncoMHz", "NCO", "Exact NCO frequency as programmed (e.g. -2031.25); 0 = header value.",
                                    {{"type", "float"}, {"unit", "MHz"}, {"precision", 6}}));
    JST_CHECK(defineInterfaceConfig("syncTimeUnix", "Sync Time", "UNIX time of t = 0; 0 = estimate from packets.",
                                    {{"type", "float"}, {"unit", "s"}, {"precision", 6}}));
    JST_CHECK(defineInterfaceConfig("spectraPerFile", "Spectra Per File", "Rotate files after this many spectra (0 = never).",
                                    {{"type", "uint"}}));
    JST_CHECK(defineInterfaceConfig("sourceName", "Source", "SIGPROC source_name.", {{"type", "text"}}));
    JST_CHECK(defineInterfaceConfig("telescopeId", "Telescope ID", "SIGPROC telescope_id.", {{"type", "uint"}}));
    JST_CHECK(defineInterfaceConfig("machineId", "Machine ID", "SIGPROC machine_id.", {{"type", "uint"}}));
    JST_CHECK(defineInterfaceConfig("srcRajHours", "RA", "Source RA (J2000).", {{"type", "float"}, {"unit", "h"}, {"precision", 6}}));
    JST_CHECK(defineInterfaceConfig("srcDejDegrees", "Dec", "Source Dec (J2000).", {{"type", "float"}, {"unit", "deg"}, {"precision", 6}}));
    JST_CHECK(defineInterfaceConfig("azStart", "Azimuth", "Azimuth at start.", {{"type", "float"}, {"unit", "deg"}}));
    JST_CHECK(defineInterfaceConfig("zaStart", "Zenith Angle", "Zenith angle at start.", {{"type", "float"}, {"unit", "deg"}}));

    JST_CHECK(defineInterfaceMetric("spectraWritten", "Spectra Written", "Integrations written (all beams).",
                                    {{"type", "label"}}, [this]() -> std::any {
        return std::any(jst::fmt::format("{}", moduleImpl ? moduleImpl->getSpectraWritten() : U64{0}));
    }));
    JST_CHECK(defineInterfaceMetric("tsamp", "Integration", "Measured integration time (tsamp).",
                                    {{"type", "label"}}, [this]() -> std::any {
        return std::any(jst::fmt::format("{:.4f} s", moduleImpl ? moduleImpl->getTsamp() : 0.0));
    }));
    JST_CHECK(defineInterfaceMetric("discontinuities", "Discontinuities", "Time-base breaks (new files started).",
                                    {{"type", "label"}}, [this]() -> std::any {
        return std::any(jst::fmt::format("{}", moduleImpl ? moduleImpl->getDiscontinuities() : U64{0}));
    }));
    JST_CHECK(defineInterfaceMetric("currentFile", "File", "Current beam-0 file.",
                                    {{"type", "label"}}, [this]() -> std::any {
        return std::any(moduleImpl ? moduleImpl->getCurrentFile() : std::string());
    }));
    return Result::SUCCESS;
}

Result Lft3Fbh5WriterImpl::create() {
    JST_CHECK(moduleCreate("lft3_fbh5_writer", moduleConfig, {{"input", inputs().at("input")}}));
    moduleImpl = moduleHandle("lft3_fbh5_writer")->getImpl<Modules::Lft3Fbh5WriterImpl>();
    return Result::SUCCESS;
}

JST_REGISTER_BLOCK(Lft3Fbh5WriterImpl, {"lft3_fbh5_writer"});

}  // namespace Jetstream::Blocks
