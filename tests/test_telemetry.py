from conftest import FakeFpga
from lft3.board.registers import GMAC, REGISTERS
from lft3.board.telemetry import Snapshot, counter_delta, health, rates, read_snapshot, worst


def healthy_registers(cfg, t0=0):
    regs = {
        GMAC + "tx_packet_rate": 915527,
        "packetizer_fifo_full_cnt": 0,
        "packetizer_blocked_word_cnt": 0,
        "pps_cnt": 100 + t0,
        "raw_cycles_per_pps": 156250000,
        "expected_clk_period": 156250000,
        "pps_drift_err_cnt": 0,
        "packetizer_fid": cfg.board_id,
        "packetizer_packet_mask": cfg.packet_mask,
        GMAC + "tx_packet_count": (t0 * 915527) % 2 ** 32,
    }
    for i in range(4):
        regs[f"packetizer_hdr_nco{i}"] = cfg.header_nco & 0xFFFFFFFF
        regs[f"f_engine{i}_ch_lsb_select"] = cfg.ch_lsb
    return regs


def test_counter_delta_handles_wrap():
    assert counter_delta(2 ** 32 - 10, 5) == 15
    assert counter_delta(None, 5) is None


def test_snapshot_skips_controls_and_records_missing():
    fpga = FakeFpga({"pps_cnt": 7})
    snap = read_snapshot(fpga, clock=lambda: 1.0)
    assert snap.values["pps_cnt"] == 7
    assert "sync" not in snap.values                      # control registers are never read
    assert snap.values["trig_cnt"] is None and "trig_cnt" in snap.errors


def test_rates_and_healthy_board(cfg):
    a = Snapshot(0.0, healthy_registers(cfg, 0))
    b = Snapshot(2.0, healthy_registers(cfg, 2))
    r = rates(a, b)
    assert abs(r[GMAC + "tx_packet_count"] - 915527) < 1
    assert r["pps_cnt"] == 1.0
    checks = health(cfg, b, a)
    assert worst(checks) == "OK", [c for c in checks if c.status != "OK"]


def test_health_flags_problems(cfg):
    a = Snapshot(0.0, healthy_registers(cfg, 0))
    regs = healthy_registers(cfg, 1)
    regs[GMAC + "tx_packet_rate"] = 0                     # stream stopped
    regs["packetizer_fifo_full_cnt"] = 3                  # board-side loss
    regs["packetizer_hdr_nco2"] = (-2000) & 0xFFFFFFFF    # NCO not what the config says
    regs["raw_cycles_per_pps"] = 156246153                # free-running sample clock, -24.6 ppm
    regs["expected_clk_period"] = 208333332               # drift check configured for another clock
    b = Snapshot(1.0, regs, destination_mac="ff:ff:ff:ff:ff:ff")
    status = {c.name: c.status for c in health(cfg, b, a)}
    assert status["tx_packet_rate"] == "FAIL"
    assert status["packetizer_fifo_full_cnt"] == "FAIL"
    assert status["header_nco2"] == "FAIL"
    assert status["destination_mac"] == "FAIL"
    assert status["header_nco0"] == "OK"
    assert status["sample_clock_vs_pps"] == "INFO"       # rfsoc0's PPS is "temporary"
    assert status["expected_clk_period"] == "WARN"


def test_clock_check_warns_only_with_a_reference_pps(cfg):
    import dataclasses
    regs = healthy_registers(cfg, 1)
    regs["raw_cycles_per_pps"] = 156246153
    b = Snapshot(1.0, regs)
    a = Snapshot(0.0, healthy_registers(cfg, 0))
    ref = dataclasses.replace(cfg, pps="reference")
    assert {c.name: c.status for c in health(ref, b, a)}["sample_clock_vs_pps"] == "WARN"
    assert worst(health(cfg, b, a)) == "INFO"           # temporary PPS: clock offset only informational


def test_unreliable_counters_are_marked():
    assert not REGISTERS[GMAC + "tx_overflow_count"].reliable
