#!/usr/bin/env bash
# One-off link bring-up for the RFSoC 4x2 (100GbE, QSFP28) -> DGX Spark ConnectX-7.
# Usage: sudo scripts/link_setup.sh <netdev> [ip/prefix]
set -euo pipefail
IF=${1:?netdev}; ADDR=${2:-}
ibdev2netdev || true                         # map RDMA devices <-> netdevs (4 on a Spark)
ethtool "$IF" | grep -E 'Speed|Link detected|Duplex' || true
ethtool --show-fec "$IF" || true
# If the link stays down, force 100G and RS-FEC to match the RFSoC CMAC configuration:
#   ethtool -s "$IF" speed 100000 autoneg off
#   ethtool --set-fec "$IF" encoding rs        # or 'off' if the CMAC has RS-FEC disabled
ip link set "$IF" mtu 9000 up                # frames are 8298 B; need MTU >= 8284
[ -n "$ADDR" ] && ip addr replace "$ADDR" dev "$IF"
# Larger socket buffers only matter for tcpdump/socket tests; DAQIRI bypasses the kernel.
sysctl -w net.core.rmem_max=268435456 net.core.rmem_default=67108864 >/dev/null
echo "Now: scripts/nic_rate.sh $IF   and   tcpdump -i $IF -s 0 -c 20000 -w cap.pcap"
