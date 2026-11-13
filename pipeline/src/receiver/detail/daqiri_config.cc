#include "daqiri_config.hh"

#include <arpa/inet.h>

#include "packet.hh"

namespace Lft3 {

namespace {
// A burst is handed to the application when it holds packetsPerBurst packets or after this timeout.
constexpr std::uint64_t kRxBurstTimeoutUs = 1000;
}  // namespace

bool BuildDaqiriRxConfig(const DaqiriRxParams& params, daqiri::NetworkConfig& cfg, std::string& error) {
    if (params.interfaceAddress.empty()) {
        error = "interfaceAddress is empty";
        return false;
    }
    if (params.packetsPerBurst == 0 || params.maxConcurrentBursts == 0) {
        error = "packetsPerBurst and maxConcurrentBursts must be positive";
        return false;
    }

    in_addr_t sourceIp = INADDR_ANY;
    if (!params.sourceAddress.empty()) {
        in_addr parsed{};
        if (inet_pton(AF_INET, params.sourceAddress.c_str(), &parsed) != 1) {
            error = "sourceAddress '" + params.sourceAddress + "' is not an IPv4 address";
            return false;
        }
        sourceIp = parsed.s_addr;
    }

    const std::size_t numBufs = params.packetsPerBurst * params.maxConcurrentBursts;

    cfg.log_level_ = daqiri::LogLevel::INFO;
    cfg.common_.version = 1;
    cfg.common_.master_core_ = static_cast<int>(params.masterCore);
    cfg.common_.dir = daqiri::Direction::RX;
    cfg.common_.loopback_ = daqiri::LoopbackType::DISABLED;
    cfg.common_.stream_type = daqiri::StreamType::RAW;
    cfg.common_.engine = daqiri::EngineType::IBVERBS;

    // Segment 0: Ethernet/IP/UDP + LFT3 header (106 B), read by the CPU.
    daqiri::MemoryRegionConfig header = {};
    header.name_ = "LFT3_RX_HEADER";
    header.kind_ = daqiri::MemoryKind::HOST_PINNED;
    header.affinity_ = 0;
    header.buf_size_ = kHeaderSegmentBytes;
    header.num_bufs_ = numBufs;
    header.access_ = daqiri::MEM_ACCESS_LOCAL;
    header.owned_ = true;
    cfg.mrs_.emplace(header.name_, header);

    // Segment 1: the 8192-B payload, read by the GPU scatter kernel. On DGX Spark (unified memory, no
    // GPUDirect RDMA) this is pinned host memory that the integrated GPU reads in place.
    daqiri::MemoryRegionConfig data = {};
    data.name_ = "LFT3_RX_DATA";
    data.kind_ = params.payloadInDeviceMemory ? daqiri::MemoryKind::DEVICE : daqiri::MemoryKind::HOST_PINNED;
    data.affinity_ = 0;
    data.buf_size_ = kPayloadBytes;
    data.num_bufs_ = numBufs;
    data.access_ = daqiri::MEM_ACCESS_LOCAL;
    data.owned_ = true;
    cfg.mrs_.emplace(data.name_, data);

    daqiri::InterfaceConfig intf = {};
    intf.name_ = "lft3";
    intf.address_ = params.interfaceAddress;
    intf.rx_.flow_isolation_ = true;   // only packets matching our flow reach the queue

    daqiri::RxQueueConfig queue = {};
    queue.common_.name_ = "lft3-chan";
    queue.common_.id_ = 0;
    queue.common_.batch_size_ = static_cast<int>(params.packetsPerBurst);
    queue.common_.cpu_core_ = std::to_string(params.workerCore);
    queue.common_.mrs_ = {header.name_, data.name_};
    queue.timeout_us_ = kRxBurstTimeoutUs;
    intf.rx_.queues_.push_back(queue);

    daqiri::FlowConfig flow = {};
    flow.name_ = "lft3-chan";
    flow.id_ = 1;   // must be non-zero for the ibverbs engine
    flow.action_.type_ = daqiri::FlowType::QUEUE;
    flow.action_.id_ = 0;
    flow.match_.type_ = daqiri::FlowMatchType::IPV4_UDP;
    flow.match_.udp_src_ = 0;
    flow.match_.udp_dst_ = params.udpPort;
    flow.match_.ipv4_len_ = 0;
    flow.match_.ipv4_src_ = sourceIp;
    flow.match_.ipv4_dst_ = INADDR_ANY;
    intf.rx_.flows_.push_back(flow);

    cfg.ifs_.push_back(intf);
    return true;
}

}  // namespace Lft3
