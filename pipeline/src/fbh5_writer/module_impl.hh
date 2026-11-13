#ifndef LFT3_FBH5_WRITER_MODULE_IMPL_HH
#define LFT3_FBH5_WRITER_MODULE_IMPL_HH

#include <memory>
#include <string>
#include <vector>

#include <lft3/fbh5_writer/module.hh>
#include <jetstream/detail/module_impl.hh>
#include <jetstream/tools/snapshot.hh>

#include "detail/fbh5_file.hh"
#include "detail/timeline.hh"

namespace Jetstream::Modules {

struct Lft3Fbh5WriterImpl : public Module::Impl, public DynamicConfig<Lft3Fbh5Writer> {
 public:
    Result validate() override;
    Result define() override;
    Result create() override;
    Result destroy() override;
    Result reconfigure() override;

    U64 getSpectraWritten() const { return spectraWritten.get(); }
    U64 getDiscontinuities() const { return discontinuities.get(); }
    F64 getTsamp() const { return tsampSeconds.get(); }
    std::string getCurrentFile() const { return currentFile.get(); }

 protected:
    // Called by the device implementation with the spectra already in host memory:
    // `host` is [beams, channels] (stride `elementStride` floats, real part first).
    Result consume(const float* host, U64 elementStride);

    Result openFiles(U64 startT);
    void closeFiles();
    U64 readU64(const std::string& key) const;
    I64 readI64(const std::string& key) const;
    F64 readF64(const std::string& key) const;
    bool readBool(const std::string& key) const;

    Tensor input;
    U64 beams = 0;
    U64 channels = 0;
    U64 spectraPerBlock = 0;
    U64 channelStart = 0;
    U64 fftLength = 0;

    std::unique_ptr<Lft3::IntegrationTimeline> timeline;
    std::vector<std::unique_ptr<Lft3::Fbh5File>> files;
    std::vector<float> pending;          // first spectrum of a run, held until tsamp is known
    U64 pendingMissing = 0;
    std::vector<float> row;              // one beam's spectrum, contiguous

    Tools::Snapshot<U64> spectraWritten{0};
    Tools::Snapshot<U64> discontinuities{0};
    Tools::Snapshot<F64> tsampSeconds{0.0};
    Tools::Snapshot<std::string> currentFile{std::string()};
};

}  // namespace Jetstream::Modules

#endif  // LFT3_FBH5_WRITER_MODULE_IMPL_HH
