"""ConnectX NIC counters (``ethtool -S``, unprivileged), sampled during pipeline runs.

The drop counters tell host-side loss (``rx_out_of_buffer``: an RX queue, e.g. DAQIRI's, had no buffer;
``rx_discards_phy``: the port buffer overflowed) apart from loss before the NIC (board or link): if the
receiver reports missing packets while these stay flat, the packets never arrived.
"""
from __future__ import annotations

import datetime as _dt
import subprocess
import threading
from pathlib import Path
from typing import Callable

#: counters logged every interval; the first group are losses / congestion and are reported when they move
DROP_COUNTERS = ("rx_out_of_buffer", "rx_discards_phy", "rx_crc_errors_phy", "tx_global_pause",
                 "rx_buffer_passed_thres_phy")
RATE_COUNTERS = ("rx_packets_phy", "rx_bytes_phy", "rx_vport_unicast_packets", "rx_vport_broadcast_packets")


def netdev_for_pci(pci: str) -> str | None:
    """Netdev name of a PCI function (``0000:01:00.0`` → ``enp1s0f0np0``), None if it has none."""
    d = Path("/sys/bus/pci/devices") / pci / "net"
    names = sorted(p.name for p in d.iterdir()) if d.is_dir() else []
    return names[0] if names else None


def parse_ethtool(text: str) -> dict[str, int]:
    out = {}
    for line in text.splitlines():
        key, sep, val = line.strip().partition(":")
        if sep:
            try:
                out[key.strip()] = int(val)
            except ValueError:
                pass
    return out


def read_counters(netdev: str) -> dict[str, int]:
    res = subprocess.run(["ethtool", "-S", netdev], capture_output=True, text=True, check=True)
    return parse_ethtool(res.stdout)


class NicLogger(threading.Thread):
    """Append one CSV row per interval (UTC time, then counter deltas) and call ``alert(msg)`` whenever a
    drop counter increases."""

    def __init__(self, netdev: str, path: Path, interval: float = 5.0,
                 alert: Callable[[str], None] | None = None,
                 read: Callable[[str], dict[str, int]] = read_counters):
        super().__init__(daemon=True, name="nic-logger")
        self.netdev, self.path, self.interval, self.alert, self.read = netdev, Path(path), interval, alert, read
        self.keys = DROP_COUNTERS + RATE_COUNTERS
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def sample(self, prev: dict[str, int]) -> dict[str, int]:
        cur = self.read(self.netdev)
        now = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds")
        delta = {k: cur.get(k, 0) - prev.get(k, 0) for k in self.keys}
        with open(self.path, "a") as f:
            f.write(",".join([now] + [str(delta[k]) for k in self.keys]) + "\n")
        moved = [f"{k} +{delta[k]}" for k in DROP_COUNTERS if delta[k] > 0]
        if moved and self.alert:
            self.alert(f"{now} NIC {self.netdev}: " + ", ".join(moved))
        return cur

    def run(self) -> None:
        try:
            prev = self.read(self.netdev)
        except (OSError, subprocess.CalledProcessError) as exc:
            if self.alert:
                self.alert(f"NIC counters unavailable for {self.netdev}: {exc}")
            return
        with open(self.path, "w") as f:
            f.write(",".join(("utc",) + self.keys) + "\n")
        while not self._stop.wait(self.interval):
            try:
                prev = self.sample(prev)
            except (OSError, subprocess.CalledProcessError):
                pass
