import threading

import pytest

from lft3.board.control import BoardBusy, BoardError, Rfsoc4x2, arp_index, board_lock, mac_to_int, mac_to_str


def writes(log):
    return [args for op, args, _ in log if op == "write_int"]


def test_arp_index_and_mac_helpers():
    assert arp_index("10.17.16.10") == 10
    assert arp_index("10.0.1.2") == 258
    assert mac_to_str(mac_to_int("4c:bb:47:2b:97:28")) == "4c:bb:47:2b:97:28"


def test_start_follows_the_bringup_notebook(cfg, fake_fpga, fake_connect, tmp_path):
    board = Rfsoc4x2(cfg, connect=fake_connect, lock_dir=tmp_path, sleep=lambda s: None)
    board.start()
    ops = [op for op, _, _ in fake_fpga.log]

    # Order of the major steps (mts_ddr_8way.ipynb / the old scripts/start_firmware.py).
    order = ["program", "gbe.set_single_arp_entry", "rfdc.init", "rfdc.progpll", "rfdc.run_mts",
             "rfdc.set_mixer_event_source", "rfdc.set_fine_mixer_freq"]
    positions = [ops.index(o) for o in order]
    assert positions == sorted(positions)

    progpll = [args for op, args, _ in fake_fpga.log if op == "rfdc.progpll"]
    assert progpll == [("lmk", cfg.lmk_file), ("lmx", cfg.lmx_file)]
    mts = [(args, kw) for op, args, kw in fake_fpga.log if op == "rfdc.run_mts"]
    assert mts == [((0,), (("tile_mask", 0x5),))]

    # NCO: header register gets round(nco) as uint32, the mixer gets the exact value.
    w = writes(fake_fpga.log)
    assert ("packetizer_hdr_nco0", (-2031) & 0xFFFFFFFF) in w
    mixers = [args for op, args, _ in fake_fpga.log if op == "rfdc.set_fine_mixer_freq"]
    assert [m[:2] for m in mixers] == [(0, 0), (0, 1), (2, 0), (2, 1)]
    assert all(m[3] == -2031.25 for m in mixers)

    # Packetizer configuration, then a sync pulse as the last writes; no trigger by default.
    assert ("packetizer_ddr_size", 2 ** 26 - 1) in w
    assert ("f_engine3_adc_lsb_select", 6) in w and ("f_engine3_ch_lsb_select", 0) in w
    assert ("packetizer_packet_mask", 0x3F) in w
    assert ("expected_clk_period", 156250000) in w
    assert w[-2:] == [("sync", 1), ("sync", 0)]
    assert ("manual_trig", 1) not in w

    # Destination MAC landed in ARP entry 10.
    assert board.read_destination_mac() == "4c:bb:47:2b:97:28"


def test_start_with_trigger_rearms(cfg, fake_fpga, fake_connect, tmp_path):
    Rfsoc4x2(cfg, connect=fake_connect, lock_dir=tmp_path, sleep=lambda s: None).start(trigger=True)
    assert writes(fake_fpga.log)[-4:] == [("manual_trig", 1), ("manual_trig", 0), ("trig_rst", 1), ("trig_rst", 0)]


def test_start_without_programming_reads_design(cfg, fake_fpga, fake_connect, tmp_path):
    Rfsoc4x2(cfg, connect=fake_connect, lock_dir=tmp_path, sleep=lambda s: None).start(program=False)
    ops = [op for op, _, _ in fake_fpga.log]
    assert "program" not in ops and ops[0] == "get_system_information"


def test_mts_failure_is_an_error(cfg, fake_fpga, fake_connect, tmp_path):
    fake_fpga.adcs["rfdc"].mts_ok = False
    with pytest.raises(BoardError):
        Rfsoc4x2(cfg, connect=fake_connect, lock_dir=tmp_path, sleep=lambda s: None).start()


def test_lock_prevents_concurrent_reconfiguration(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    def holder():
        with board_lock("rfsocX", tmp_path):
            entered.set()
            release.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    entered.wait(5)
    # flock is per open file description, so a second open in this process conflicts too
    with pytest.raises(BoardBusy):
        with board_lock("rfsocX", tmp_path):
            pass
    release.set()
    t.join()
    with board_lock("rfsocX", tmp_path):
        pass
