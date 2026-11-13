#ifndef LFT3_RECEIVER_MODULE_IMPL_HH
#define LFT3_RECEIVER_MODULE_IMPL_HH

#include <lft3/receiver/module.hh>
#include <jetstream/detail/module_impl.hh>
#include <jetstream/memory/tensor.hh>
#include <jetstream/tools/snapshot.hh>

#include "detail/layout.hh"

namespace Jetstream::Modules {

// Metrics published by the receive thread and read by the UI / block metrics (lock-free snapshots).
struct Lft3ReceiverMetrics {
    Tools::Snapshot<U64> packetsReceived{0};     // all packets pulled from DAQIRI
    Tools::Snapshot<U64> packetsAccepted{0};     // placed into a block
    Tools::Snapshot<U64> packetsFiltered{0};     // wrong board id / outside the channel selection
    Tools::Snapshot<U64> packetsDuplicate{0};
    Tools::Snapshot<U64> packetsLate{0};
    Tools::Snapshot<U64> packetsNoBuffer{0};
    Tools::Snapshot<U64> packetsMissing{0};      // zero-filled or in skipped/dropped blocks
    Tools::Snapshot<U64> blocksComplete{0};
    Tools::Snapshot<U64> blocksPartial{0};
    Tools::Snapshot<U64> blocksDropped{0};
    Tools::Snapshot<U64> blocksSkipped{0};
    Tools::Snapshot<U64> blocksEmitted{0};
    Tools::Snapshot<U64> restarts{0};
    Tools::Snapshot<U64> latestTimestamp{0};
    Tools::Snapshot<U64> openBlocks{0};
    Tools::Snapshot<U64> readyBlocks{0};
    Tools::Snapshot<U64> freeBuffers{0};
    Tools::Snapshot<F64> throughputGbps{0.0};
};

struct Lft3ReceiverImpl : public Module::Impl, public DynamicConfig<Lft3Receiver> {
 public:
    Result validate() override;
    Result define() override;
    Result create() override;
    Result reconfigure() override;

    const Lft3ReceiverMetrics& metrics() const { return stats; }
    const Lft3::Layout& layoutInUse() const { return layout; }

 protected:
    // Builds the layout from a config (shared by validate and create, and by tests).
    static bool BuildLayout(const Lft3Receiver& config, Lft3::Layout& out, std::string& error);

    Lft3::Layout validatedLayout;
    Lft3::Layout layout;
    Tensor outputTensor;
    Lft3ReceiverMetrics stats;
};

}  // namespace Jetstream::Modules

#endif  // LFT3_RECEIVER_MODULE_IMPL_HH
