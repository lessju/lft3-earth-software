#include <cuda_runtime.h>

#include <jetstream/registry.hh>
#include <jetstream/runtime_context_native_cuda.hh>
#include <jetstream/scheduler_context.hh>

#include "module_impl.hh"

namespace Jetstream::Modules {

// Copies each integrated spectrum to pinned host memory and writes it with HDF5 on the scheduler
// thread. At the interim 1.2 kHz resolution this is ~3 MB per beam per second. At Hz resolution the
// copy and write should move to a separate writer thread with double buffering.
struct Lft3Fbh5WriterImplNativeCuda : public Lft3Fbh5WriterImpl,
                                      public NativeCudaRuntimeContext,
                                      public Scheduler::Context {
 public:
    Result create() final;
    Result destroy() final;
    Result computeSubmit(const cudaStream_t& stream) override;

 private:
    float* hostBuffer = nullptr;
    U64 hostBytes = 0;
};

Result Lft3Fbh5WriterImplNativeCuda::create() {
    JST_CHECK(Lft3Fbh5WriterImpl::create());
    hostBytes = input.sizeBytes();
    if (cudaMallocHost(reinterpret_cast<void**>(&hostBuffer), hostBytes) != cudaSuccess) {
        JST_ERROR("[LFT3_FBH5_WRITER] Cannot allocate {} bytes of pinned host memory.", hostBytes);
        return Result::ERROR;
    }
    return Result::SUCCESS;
}

Result Lft3Fbh5WriterImplNativeCuda::destroy() {
    const Result result = Lft3Fbh5WriterImpl::destroy();
    if (hostBuffer) {
        cudaFreeHost(hostBuffer);
        hostBuffer = nullptr;
    }
    return result;
}

Result Lft3Fbh5WriterImplNativeCuda::computeSubmit(const cudaStream_t& stream) {
    if (!recording) {
        return Result::SUCCESS;
    }
    cudaError_t err = cudaMemcpyAsync(hostBuffer, input.data(), hostBytes, cudaMemcpyDeviceToHost, stream);
    if (err == cudaSuccess) {
        err = cudaStreamSynchronize(stream);
    }
    if (err != cudaSuccess) {
        JST_ERROR("[LFT3_FBH5_WRITER] Copy to host failed: {}.", cudaGetErrorString(err));
        return Result::ERROR;
    }
    const U64 stride = input.dtype() == DataType::CF32 ? 2 : 1;   // CF32: take the real part
    return consume(hostBuffer, stride);
}

JST_REGISTER_MODULE(Lft3Fbh5WriterImplNativeCuda, DeviceType::CUDA, RuntimeType::NATIVE, "generic");

}  // namespace Jetstream::Modules
