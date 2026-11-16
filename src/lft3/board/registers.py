"""Register map of the spectrometer_8way_4x firmware, as used by the host.

Names come from the .fpg register list; semantics from lft3-earth-main/README.md, listener.py and our own
measurements. This is the start of the "register map" asked for in docs/firmware_requests.md #9.

Each Register says how the host should treat it:
  kind     "counter" (monotonic, 32-bit wrap; rates are meaningful), "gauge" (instantaneous value),
           "config" (written by the host), "flag", "control" (pulse)
  latched  True if the value only takes effect after a sync pulse
"""
from __future__ import annotations

from dataclasses import dataclass

NUM_FENGINES = 4
GBE = "packetizer_onehundred_gbe"
GMAC = GBE + "_gmac_reg_"
FPGA_CLOCK_HZ = 156.25e6


@dataclass(frozen=True)
class Register:
    name: str
    kind: str
    description: str
    latched: bool = False
    reliable: bool = True       # False: known to report nonsense (see notes)


_REGS: list[Register] = [
    # Timing / sync
    Register("pps_cnt", "counter", "PPS edges seen (monitoring only; PPS does not drive sync)"),
    Register("clk_cycles_per_pps", "gauge",
             "Varies randomly between reads (looks like a running count, not a latched period); use raw_cycles_per_pps",
             reliable=False),
    Register("raw_cycles_per_pps", "gauge", "FPGA clock cycles between the last two PPS edges (156.25e6 nominal)"),
    Register("expected_clk_period", "config",
             "Cycles per PPS the drift check expects. Plain software register; design default 208333332 is "
             "wrong for 156.25 MHz, so `lft3-board start` writes timing.expected_clk_period"),
    Register("pps_drift_err_cnt", "counter",
             "PPS intervals outside tolerance of expected_clk_period (counts every PPS while that is wrong)"),
    Register("trig_cnt", "counter", "Trigger events since the last sync"),
    Register("sys_clkcounter", "counter", "Free-running FPGA clock counter (CASPER sys block)"),
    # Packetizer (channel stream + DDR dump)
    Register("packetizer_accepted_word_cnt", "counter", "512-bit words accepted by the packetizer"),
    Register("packetizer_blocked_word_cnt", "counter", "Words blocked (back-pressure): should stay 0"),
    Register("packetizer_eof_cnt", "counter", "End-of-frame markers emitted"),
    Register("packetizer_fifo_full_cnt", "counter", "Channel FIFO full events: data loss on the board"),
    Register("packetizer_fifo_ddr_full_cnt", "counter", "DDR-dump FIFO full events"),
    Register("packetizer_out_vld_ddr_cnt", "counter", "DDR words read out for dumps"),
    Register("packetizer_rd_fifo_rdy_cnt", "counter", "DDR read FIFO ready cycles"),
    Register("packetizer_user_wr_rdy_cnt", "counter", "DDR write-ready cycles"),
    Register("packetizer_gbe_ovflow_reg", "flag", "100GbE overflow flag (appears sticky; meaning unconfirmed)",
             reliable=False),
    # 100GbE core (CASPER gmac)
    Register(GMAC + "tx_packet_rate", "gauge", "Transmitted packets in the last second (915527 nominal)"),
    Register(GMAC + "tx_packet_count", "counter", "Transmitted packets"),
    Register(GMAC + "tx_valid_count", "counter", "Valid transmit words"),
    Register(GMAC + "tx_overflow_count", "counter", "TX overflow: rises ~0.7e9/s with zero real loss, unreliable",
             reliable=False),
    Register(GMAC + "tx_almost_full_count", "counter", "TX almost full: rises ~2.2e9/s, unreliable", reliable=False),
    Register(GMAC + "rx_packet_count", "counter", "Received packets (ARP etc.)"),
    Register(GMAC + "rx_bad_packet_count", "counter", "Received bad packets"),
    # Configuration written by the host
    Register("packetizer_fid", "config", "Board id in packet headers (0..7)", latched=True),
    Register("packetizer_packet_mask", "config", "Which bindex packets are sent", latched=True),
    Register("packetizer_ddr_size", "config", "Last DDR word address of the ring buffer"),
    Register("packetizer_ddr_read_delay", "config", "DDR words written after a trigger before readout"),
    Register("sysref_gate_reg", "config", "SYSREF gate for MTS/NCO cells (releasing it emits a sync)"),
] + [Register(f"packetizer_hdr_nco{i}", "config", f"Header NCO of input {i} (signed whole MHz)")
     for i in range(NUM_FENGINES)] + [
    Register(f"f_engine{i}_ch_lsb_select", "config",
             f"Channel requantiser bit select, input {i} (takes effect immediately; header pfb_lsb after sync)")
    for i in range(NUM_FENGINES)] + [
    Register(f"f_engine{i}_adc_lsb_select", "config", f"ADC (DDR) requantiser bit select, input {i}")
    for i in range(NUM_FENGINES)] + [
    Register("sync", "control", "Write 1 then 0: global sync (latches packet_mask, fid, pfb_lsb headers)"),
    Register("manual_trig", "control", "Write 1 then 0: manual DDR dump trigger"),
    Register("trig_rst", "control", "Write 1 then 0: re-arm the trigger after every dump"),
]

REGISTERS: dict[str, Register] = {r.name: r for r in _REGS}


def by_kind(*kinds: str) -> list[Register]:
    return [r for r in _REGS if r.kind in kinds]
