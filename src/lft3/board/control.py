"""RFSoC 4x2 control: bring-up and configuration of the spectrometer_8way_4x firmware over casperfpga.

The bring-up sequence is the one in lft3-earth-main/designs/spectrometer_8way/scripts/mts_ddr_8way.ipynb
(previously scripts/start_firmware.py), driven by a BoardConfig. Every operation that changes the board
holds a per-board lock file, so two operators (or a script and an operator) cannot reconfigure the same
board at once.

    board = Rfsoc4x2(load_board_config("rfsoc0"))
    board.start()                 # program, clocks, MTS, NCO, packetizer, destination MAC, sync
"""
from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Iterator

from lft3.config import BoardConfig

from .registers import GBE

log = logging.getLogger(__name__)

LOCK_DIR = Path(os.environ.get("LFT3_LOCK_DIR", "/tmp"))


class BoardError(RuntimeError):
    pass


class BoardBusy(BoardError):
    pass


def arp_index(ip: str) -> int:
    """ARP-table entry the 100GbE core uses for a destination IP (casperfpga: (octet3*256 + octet4) % 512)."""
    o = [int(x) for x in ip.split(".")]
    return (o[2] * 256 + o[3]) % 512


def mac_to_int(mac: str) -> int:
    return int(mac.replace(":", "").replace("-", ""), 16)


def mac_to_str(mac: int) -> str:
    return ":".join("%02x" % ((mac >> s) & 0xFF) for s in range(40, -8, -8))


@contextlib.contextmanager
def board_lock(name: str, lock_dir: Path | None = None) -> Iterator[None]:
    path = (lock_dir or LOCK_DIR) / f"lft3-board-{name}.lock"
    fh = open(path, "a+")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.seek(0)
            holder = fh.read().strip() or "unknown"
            raise BoardBusy(f"board '{name}' is being reconfigured by {holder} (lock {path})") from None
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid {os.getpid()} user {os.environ.get('USER', '?')} since {time.ctime()}\n")
        fh.flush()
        yield
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def _default_connect(host: str) -> Any:
    import casperfpga  # only needed when talking to real hardware
    return casperfpga.CasperFpga(host)


class Rfsoc4x2:
    """One board. `connect` is injectable so tests can use a fake casperfpga object."""

    def __init__(self, config: BoardConfig, connect: Callable[[str], Any] | None = None,
                 lock_dir: Path | None = None, sleep: Callable[[float], None] = time.sleep):
        self.cfg = config
        self._connect = connect
        self._lock_dir = lock_dir
        self._sleep = sleep
        self._fpga: Any = None

    # -- connection ---------------------------------------------------------------------------------
    @property
    def fpga(self) -> Any:
        if self._fpga is None:
            self._fpga = (self._connect or _default_connect)(self.cfg.host)
        return self._fpga

    def describe_design(self) -> None:
        """Populate casperfpga's view of the design (gbes, registers) without reprogramming."""
        self.fpga.get_system_information(self.cfg.bitstream)

    def lock(self):
        return board_lock(self.cfg.name, self._lock_dir)

    # -- individual steps (call inside lock()) ----------------------------------------------------
    def program(self) -> None:
        log.info("%s: programming %s", self.cfg.name, self.cfg.bitstream)
        self.fpga.upload_to_ram_and_program(self.cfg.bitstream)

    def init_clocks(self) -> str:
        rfdc = self.fpga.adcs["rfdc"]
        rfdc.init()
        rfdc.progpll("lmk", self.cfg.lmk_file)
        rfdc.progpll("lmx", self.cfg.lmx_file)
        self._sleep(0.1)
        return str(rfdc.status())

    def run_mts(self) -> None:
        f, rfdc = self.fpga, self.fpga.adcs["rfdc"]
        rfdc.sysref_en(1)
        f.write_int("sysref_gate_reg", 0)
        report = rfdc.run_mts(rfdc.ADC_TILE, tile_mask=self.cfg.tile_mask)
        rfdc.sysref_en(0)
        f.write_int("sysref_gate_reg", 0xF)
        if not report:
            raise BoardError("ADC multi-tile synchronisation failed")
        for tile, block in self.cfg.adc_map:
            rfdc.set_mixer_event_source(tile, block, rfdc.ADC_TILE, rfdc.EVNT_SRC_SYSREF)

    def set_nco(self, nco_mhz: float | None = None) -> None:
        """Set every ADC's fine mixer and the header NCO fields; applied on a SYSREF pulse (MTS first)."""
        nco = self.cfg.nco_mhz if nco_mhz is None else nco_mhz
        f, rfdc = self.fpga, self.fpga.adcs["rfdc"]
        f.write_int("sysref_gate_reg", 0xF)
        header = int(round(nco)) & 0xFFFFFFFF
        for adc, (tile, block) in enumerate(self.cfg.adc_map):
            f.write_int(f"packetizer_hdr_nco{adc}", header)
            rfdc.set_fine_mixer_freq(tile, block, rfdc.ADC_TILE, nco, 0.0, force=0)
        rfdc.sysref_en(1)
        f.write_int("sysref_gate_reg", 0)
        rfdc.sysref_en(0)
        f.write_int("sysref_gate_reg", 0xF)

    def configure_packetizer(self) -> None:
        f, c = self.fpga, self.cfg
        f.write_int("packetizer_ddr_size", c.ddr_size)
        f.write_int("packetizer_ddr_read_delay", c.ddr_read_delay)
        for i in range(4):
            f.write_int(f"f_engine{i}_adc_lsb_select", c.adc_lsb)
            f.write_int(f"f_engine{i}_ch_lsb_select", c.ch_lsb)
        f.write_int("packetizer_fid", c.board_id)
        f.write_int("packetizer_packet_mask", c.packet_mask)

    def configure_timing(self) -> None:
        """PPS drift check period (the design's default is for a different clock)."""
        self.fpga.write_int("expected_clk_period", self.cfg.expected_clk_period)

    def set_destination_mac(self, mac: str | None = None) -> str:
        """Point the 100GbE stream at one host interface. The ARP table is lost on reprogram."""
        mac = mac or self.cfg.destination_mac
        self.fpga.gbes[GBE].set_single_arp_entry(self.cfg.destination_ip, mac_to_int(mac))
        return self.read_destination_mac()

    def read_destination_mac(self) -> str:
        idx = arp_index(self.cfg.destination_ip)
        entry = self.fpga.gbes[GBE].get_arp_details(N=idx + 1)[idx]
        value = entry if isinstance(entry, int) else getattr(entry, "mac_int", None)
        if value is None:
            value = mac_to_int(str(entry))
        return mac_to_str(value)

    def sync(self) -> None:
        """Global sync: latches packet_mask, fid and the pfb_lsb header fields; resets t."""
        self.fpga.write_int("sync", 1)
        self.fpga.write_int("sync", 0)

    def trigger_dump(self) -> None:
        """Manual DDR dump, then re-arm the trigger."""
        self.fpga.write_int("manual_trig", 1)
        self.fpga.write_int("manual_trig", 0)
        self.fpga.write_int("trig_rst", 1)
        self.fpga.write_int("trig_rst", 0)

    # -- full sequences --------------------------------------------------------------------------
    def initialise(self, program: bool = True) -> None:
        """Everything up to (not including) the sync. Call inside lock(); the Station runs this on all
        boards in parallel and then syncs them together."""
        if program:
            self.program()
        else:
            self.describe_design()
        mac = self.set_destination_mac()
        log.info("%s: 100GbE destination %s -> %s", self.cfg.name, self.cfg.destination_ip, mac)
        log.info("%s: RFDC %s", self.cfg.name, self.init_clocks())
        self.run_mts()
        self.set_nco()
        self.configure_packetizer()
        self.configure_timing()

    def start(self, program: bool = True, trigger: bool = False) -> None:
        """Full bring-up of this board alone. With program=False the running design is reconfigured."""
        with self.lock():
            self.initialise(program)
            self.sync()
            if trigger:
                self.trigger_dump()
            log.info("%s: started (NCO %.4f MHz, fid %d)", self.cfg.name, self.cfg.nco_mhz, self.cfg.board_id)
