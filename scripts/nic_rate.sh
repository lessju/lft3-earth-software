#!/usr/bin/env bash
# Live NIC-level receive rate and drop counters (no application needed).
# Usage: scripts/nic_rate.sh <netdev> [interval_s]
# rx_*_phy counters are what arrived on the wire; rx_out_of_buffer counts packets
# dropped because the receive queue had no posted buffers (application too slow).
set -euo pipefail
IF=${1:?netdev, e.g. enp1s0f0np0 (see ibdev2netdev)}; DT=${2:-1}
get() { ethtool -S "$IF" | awk -v k="$1" '$1==k":"{print $2}'; }
pb=$(get rx_bytes_phy); pp=$(get rx_packets_phy)
printf "%8s %10s %10s %12s %12s %10s\n" time Gb/s Mpps rx_discards out_of_buf crc_err
while sleep "$DT"; do
  b=$(get rx_bytes_phy); p=$(get rx_packets_phy)
  printf "%8s %10.2f %10.3f %12s %12s %10s\n" "$(date +%T)" \
    "$(echo "($b-$pb)*8/$DT/1e9" | bc -l)" "$(echo "($p-$pp)/$DT/1e6" | bc -l)" \
    "$(get rx_discards_phy)" "$(get rx_out_of_buffer)" "$(get rx_crc_errors_phy)"
  pb=$b; pp=$p
done
