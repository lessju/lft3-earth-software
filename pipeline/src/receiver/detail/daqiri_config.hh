#ifndef LFT3_RECEIVER_DETAIL_DAQIRI_CONFIG_HH
#define LFT3_RECEIVER_DETAIL_DAQIRI_CONFIG_HH

#include <cstdint>
#include <string>

#include <daqiri/daqiri.h>

namespace Lft3 {

struct DaqiriRxParams {
    std::string interfaceAddress;        // PCI address (e.g. 0000:01:00.0) or netdev name
    std::uint64_t masterCore = 5;
    std::uint64_t workerCore = 6;
    std::uint64_t packetsPerBurst = 1024;
    std::uint64_t maxConcurrentBursts = 64;  // receive buffers = packetsPerBurst * maxConcurrentBursts
    std::string sourceAddress;           // board IPv4 to match, empty = any
    std::uint16_t udpPort = 60000;       // destination port to match
    bool payloadInDeviceMemory = false;  // true only with GPUDirect RDMA (discrete GPUs)
};

// One RX queue on one interface, one flow rule steering the board's UDP stream into it, and two
// memory regions so DAQIRI splits every packet into header (CPU) and payload (GPU) segments.
bool BuildDaqiriRxConfig(const DaqiriRxParams& params, daqiri::NetworkConfig& cfg, std::string& error);

}  // namespace Lft3

#endif  // LFT3_RECEIVER_DETAIL_DAQIRI_CONFIG_HH
