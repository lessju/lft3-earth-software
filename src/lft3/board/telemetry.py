"""Read-only board telemetry and health checks.

A Snapshot is one pass over the registers (casperfpga read_uint). Two snapshots give rates. Health
checks turn snapshot + rates + config into OK / WARN / FAIL lines for operators and alerting.
Nothing here writes to the board (reading the ARP entry is optional: it uses the core's read-address
registers, as casperfpga's get_arp_details does).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from lft3.config import BoardConfig

from .registers import FPGA_CLOCK_HZ, GMAC, REGISTERS

WRAP = 2 ** 32


@dataclass
class Snapshot:
    time: float
    values: dict[str, int | None]
    destination_mac: str | None = None
    errors: dict[str, str] = field(default_factory=dict)


@dataclass
class Check:
    name: str
    status: str          # OK, INFO, UNKNOWN, WARN, FAIL
    detail: str


def read_snapshot(fpga: Any, names: list[str] | None = None, clock=time.time) -> Snapshot:
    names = names or list(REGISTERS)
    values: dict[str, int | None] = {}
    errors: dict[str, str] = {}
    t = clock()
    for name in names:
        if REGISTERS.get(name) is not None and REGISTERS[name].kind == "control":
            continue
        try:
            values[name] = int(fpga.read_uint(name))
        except Exception as exc:  # missing register in another design, katcp timeout, ...
            values[name] = None
            errors[name] = str(exc)
    return Snapshot(t, values, errors=errors)


def counter_delta(a: int | None, b: int | None) -> int | None:
    if a is None or b is None:
        return None
    return (b - a) % WRAP


def rates(prev: Snapshot, cur: Snapshot) -> dict[str, float | None]:
    dt = cur.time - prev.time
    out: dict[str, float | None] = {}
    if dt <= 0:
        return out
    for name, reg in REGISTERS.items():
        if reg.kind != "counter" or name not in cur.values:
            continue
        d = counter_delta(prev.values.get(name), cur.values.get(name))
        out[name] = None if d is None else d / dt
    return out


def _signed32(v: int) -> int:
    return v - (1 << 32) if v & 0x80000000 else v


def health(cfg: BoardConfig, cur: Snapshot, prev: Snapshot | None = None) -> list[Check]:
    v = cur.values
    r = rates(prev, cur) if prev else {}
    checks: list[Check] = []

    def add(name: str, ok: bool | None, detail: str, warn_only: bool = False, info_only: bool = False) -> None:
        if ok is None:
            status = "UNKNOWN"
        elif ok:
            status = "OK"
        else:
            status = "INFO" if info_only else ("WARN" if warn_only else "FAIL")
        checks.append(Check(name, status, detail))

    pps_is_reference = cfg.pps == "reference"
    pps_note = "" if pps_is_reference else f" [PPS is '{cfg.pps}': informational]"

    rate = v.get(GMAC + "tx_packet_rate")
    if rate is not None:
        exp = cfg.expected_packet_rate * bin(cfg.packet_mask).count("1") / 6
        add("tx_packet_rate", abs(rate - exp) <= 0.001 * exp, f"{rate}/s (expected {exp:.0f})")
    else:
        add("tx_packet_rate", None, "unreadable")

    for name in ("packetizer_fifo_full_cnt", "packetizer_blocked_word_cnt"):
        if prev is not None and r.get(name) is not None:
            add(name, r[name] == 0, f"{r[name]:.1f}/s (must stay flat)")
        elif v.get(name) is not None:
            add(name, v[name] == 0, f"total {v[name]}", warn_only=True)

    if prev is not None and r.get("pps_cnt") is not None:
        # whole edges counted over a ~1 s window read as 0, 1 or 2: only flag clearly missing/doubled PPS
        add("pps", 0.4 <= r["pps_cnt"] <= 2.1, f"{r['pps_cnt']:.2f} edges/s{pps_note}", warn_only=True,
            info_only=cfg.pps == "none")
    cyc = v.get("raw_cycles_per_pps")
    if cyc:
        # FPGA clock vs PPS. Sky frequencies scale with the sampling clock, so an offset of N ppm moves
        # a 2 GHz line by 2000*N Hz. Needs a disciplined (GPS/10 MHz) reference to be ~0.
        ppm = (cyc - FPGA_CLOCK_HZ) / FPGA_CLOCK_HZ * 1e6
        add("sample_clock_vs_pps", abs(ppm) < 1.0,
            f"{cyc} cycles/PPS ({ppm:+.2f} ppm = {2000 * ppm / 1e3:+.1f} kHz at 2 GHz){pps_note}",
            warn_only=True, info_only=not pps_is_reference)
    exp_period = v.get("expected_clk_period")
    if exp_period is not None:
        add("expected_clk_period", exp_period == cfg.expected_clk_period,
            f"{exp_period} (config {cfg.expected_clk_period}; the design default 208333332 is wrong)", warn_only=True)

    for i in range(4):
        n = v.get(f"packetizer_hdr_nco{i}")
        if n is not None:
            add(f"header_nco{i}", _signed32(n) == cfg.header_nco, f"{_signed32(n)} MHz (config {cfg.header_nco})")
        lsb = v.get(f"f_engine{i}_ch_lsb_select")
        if lsb is not None:
            add(f"ch_lsb{i}", lsb == cfg.ch_lsb, f"{lsb} (config {cfg.ch_lsb})", warn_only=True)

    for name, want in (("packetizer_fid", cfg.board_id), ("packetizer_packet_mask", cfg.packet_mask)):
        if v.get(name) is not None:
            add(name, v[name] == want, f"{v[name]} (config {want})")

    if cur.destination_mac is not None:
        add("destination_mac", cur.destination_mac == cfg.destination_mac.lower(),
            f"{cur.destination_mac} (config {cfg.destination_mac}; ff:..:ff = broadcast)")
    return checks


def worst(checks: list[Check]) -> str:
    order = {"OK": 0, "INFO": 1, "UNKNOWN": 2, "WARN": 3, "FAIL": 4}
    return max((c.status for c in checks), key=lambda s: order[s], default="UNKNOWN")
