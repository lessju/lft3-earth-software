#include <cuda_runtime.h>

#include <lft3/finespec/module.hh>
#include <jetstream/detail/module_impl.hh>
#include <jetstream/memory/axis.hh>
#include <jetstream/registry.hh>
#include <jetstream/runtime_context_native_cuda.hh>
#include <jetstream/scheduler_context.hh>

#include "detail/kernel.hh"

namespace Jetstream::Modules {

struct Lft3FineSpecImplNativeCuda : public Module::Impl,
                                    public DynamicConfig<Lft3FineSpec>,
                                    public NativeCudaRuntimeContext,
                                    public Scheduler::Context {
 public:
    Result validate() override;
    Result define() override;
    Result create() override;
    Result reconfigure() override { return Result::RECREATE; }
    Result computeSubmit(const cudaStream_t& stream) override;

 private:
    Tensor input;
    Tensor output;
    Lft3::FineSpecGeometry geometry = {};
    U64 blockInIntegration = 0;
};

Result Lft3FineSpecImplNativeCuda::validate() {
    const auto& c = *candidate();
    if (c.mode != "fine" && c.mode != "coarse") {
        JST_ERROR("[LFT3_FINESPEC] mode must be 'fine' or 'coarse'.");
        return Result::ERROR;
    }
    if (c.beams == 0 || c.integrationBlocks == 0 || c.binsPerOutput == 0) {
        JST_ERROR("[LFT3_FINESPEC] beams, integrationBlocks and binsPerOutput must be positive.");
        return Result::ERROR;
    }
    if (!inputs().contains("buffer")) {
        return Result::SUCCESS;
    }
    const Tensor& in = inputs().at("buffer").tensor;
    if (!in.validShape() || in.size() == 0) {
        return Result::SUCCESS;
    }
    if (in.rank() != 4 || in.shape(3) != 2 || in.shape(0) % c.beams != 0) {
        JST_ERROR("[LFT3_FINESPEC] Expected [beams*segments, channels, fftLength, 2], got {}.", in.shape());
        return Result::ERROR;
    }
    const U64 S = in.shape(2);
    if (c.mode == "coarse") {
        if (in.shape(1) % c.binsPerOutput != 0) {
            JST_ERROR("[LFT3_FINESPEC] Coarse mode: binsPerOutput {} must divide the {} channels.", c.binsPerOutput, in.shape(1));
            return Result::ERROR;
        }
    } else if (S < 2 || S > Lft3::kFineSpecMaxFftLength || (S & (S - 1)) != 0) {
        JST_ERROR("[LFT3_FINESPEC] fftLength {} must be a power of two in 2..{}.", S, Lft3::kFineSpecMaxFftLength);
        return Result::ERROR;
    }
    if (in.dtype() != DataType::CI8 && in.dtype() != DataType::CF32) {
        JST_ERROR("[LFT3_FINESPEC] Input must be CI8 or CF32, got {}.", in.dtype());
        return Result::ERROR;
    }
    if (c.mode == "fine" && (in.shape(1) * S) % c.binsPerOutput != 0) {
        JST_ERROR("[LFT3_FINESPEC] binsPerOutput {} must divide channels * fftLength = {}.", c.binsPerOutput, in.shape(1) * S);
        return Result::ERROR;
    }
    if (in.shape(1) > 65535 * 1024ULL) {
        JST_ERROR("[LFT3_FINESPEC] Too many channels.");
        return Result::ERROR;
    }
    return Result::SUCCESS;
}

Result Lft3FineSpecImplNativeCuda::define() {
    JST_CHECK(defineInterfaceInput("buffer"));
    JST_CHECK(defineInterfaceOutput("buffer"));
    return Result::SUCCESS;
}

Result Lft3FineSpecImplNativeCuda::create() {
    input = inputs().at("buffer").tensor;
    geometry.beams = static_cast<std::uint32_t>(beams);
    geometry.segments = static_cast<std::uint32_t>(input.shape(0) / beams);
    geometry.channels = static_cast<std::uint32_t>(input.shape(1));
    geometry.fftLength = static_cast<std::uint32_t>(input.shape(2));
    geometry.binsPerOutput = static_cast<std::uint32_t>(binsPerOutput);

    const U64 channelsOut = (mode == "coarse") ? input.shape(1) / binsPerOutput
                                               : input.shape(1) * input.shape(2) / binsPerOutput;
    JST_CHECK(output.create(device(), DataType::F32, {beams, channelsOut, 1, 1}));
    JST_CHECK(SetSignalAxes(output, {.sample = Index{2}, .batch = std::nullopt, .channel = Index{1}}));
    // Downstream (the FBH5 writer) reads the receiver's lft3.* attributes through this tensor.
    JST_CHECK(output.propagateAttributes(input));
    outputs()["buffer"].produced(name(), "buffer", output);
    blockInIntegration = 0;
    return Result::SUCCESS;
}

Result Lft3FineSpecImplNativeCuda::computeSubmit(const cudaStream_t& stream) {
    if (blockInIntegration == 0) {
        // Start of an integration. Stream order guarantees the previous result was consumed downstream.
        if (cudaMemsetAsync(output.data(), 0, output.sizeBytes(), stream) != cudaSuccess) {
            JST_ERROR("[LFT3_FINESPEC] cudaMemsetAsync failed.");
            return Result::ERROR;
        }
    }
    const bool cf32 = input.dtype() == DataType::CF32;
    auto* out = static_cast<float*>(output.data());
    const cudaError_t err = (mode == "coarse") ? Lft3::LaunchCoarseDetect(input.data(), cf32, out, geometry, stream)
                                               : Lft3::LaunchFineSpec(input.data(), cf32, out, geometry, stream);
    if (err != cudaSuccess) {
        JST_ERROR("[LFT3_FINESPEC] Kernel launch failed: {}.", cudaGetErrorString(err));
        return Result::ERROR;
    }
    blockInIntegration = (blockInIntegration + 1) % integrationBlocks;
    // SKIP tells the scheduler not to run downstream modules this cycle (integration not finished).
    return blockInIntegration == 0 ? Result::SUCCESS : Result::SKIP;
}

JST_REGISTER_MODULE(Lft3FineSpecImplNativeCuda, DeviceType::CUDA, RuntimeType::NATIVE, "generic");

}  // namespace Jetstream::Modules
