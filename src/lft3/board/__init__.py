"""RFSoC 4x2 monitoring and control (docs/monitoring_control.md).

  registers   register map of the running firmware, with kind/semantics per register
  control     bring-up and configuration (writes, under a per-board lock)
  telemetry   read-only snapshots, counter rates, health checks
  sensors     rail voltages/currents and temperatures from the board's sysfs over SSH
"""
from .control import BoardBusy, BoardError, Rfsoc4x2, arp_index  # noqa: F401
