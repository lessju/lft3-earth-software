import dataclasses
import socket
import threading
import time

import numpy as np
import pytest

from conftest import FakeFpga
from lft3 import packets as L
from lft3.board import capture
from lft3.board.station import Station, StationError
from lft3.config import StationConfig


@pytest.fixture
def two_boards(cfg):
    b1 = dataclasses.replace(cfg, name="rfsocA", host="10.0.0.1", board_id=0)
    b2 = dataclasses.replace(cfg, name="rfsocB", host="10.0.0.2", board_id=1)
    fakes = {"10.0.0.1": FakeFpga(), "10.0.0.2": FakeFpga()}
    return StationConfig("test", [b1, b2], "/tmp"), fakes


def test_station_runs_every_board_in_parallel(two_boards, tmp_path):
    scfg, fakes = two_boards
    st = Station(scfg, connect=lambda host: fakes[host], lock_dir=tmp_path, sleep=lambda s: None)
    started = []

    def slow(board):
        started.append(time.monotonic())
        time.sleep(0.2)
        return board.cfg.board_id

    t0 = time.monotonic()
    res = st.each(slow)
    assert time.monotonic() - t0 < 0.35                       # ran concurrently, not 0.4 s
    assert {n: r.value for n, r in res.items()} == {"rfsocA": 0, "rfsocB": 1}


def test_station_start_initialises_then_syncs_all(two_boards, tmp_path):
    scfg, fakes = two_boards
    st = Station(scfg, connect=lambda host: fakes[host], lock_dir=tmp_path, sleep=lambda s: None)
    st.start()
    for f in fakes.values():
        writes = [args for op, args, _ in f.log if op == "write_int"]
        assert writes[-2:] == [("sync", 1), ("sync", 0)]
        assert ("expected_clk_period", 156250000) in writes
    fids = {h: [a for op, a, _ in f.log if op == "write_int" and a[0] == "packetizer_fid"] for h, f in fakes.items()}
    assert fids == {"10.0.0.1": [("packetizer_fid", 0)], "10.0.0.2": [("packetizer_fid", 1)]}


def test_station_reports_failures_per_board(two_boards, tmp_path):
    scfg, fakes = two_boards
    fakes["10.0.0.2"].adcs["rfdc"].mts_ok = False
    st = Station(scfg, connect=lambda host: fakes[host], lock_dir=tmp_path, sleep=lambda s: None)
    with pytest.raises(StationError) as err:
        st.start()
    assert err.value.results["rfsocA"].ok and not err.value.results["rfsocB"].ok
    # the healthy board was initialised, but nothing was synced
    assert ("sync", 1) not in [a for op, a, _ in fakes["10.0.0.1"].log if op == "write_int"]


def test_fft_lane_decoder():
    rng = np.random.default_rng(1)
    re = rng.integers(-2 ** 23, 2 ** 23, 8192)
    im = rng.integers(-2 ** 23, 2 ** 23, 8192)
    words = ((re & 0xFFFFFF).astype(np.uint64) << np.uint64(24)) | (im & 0xFFFFFF).astype(np.uint64)
    lanes = [words[lane::8] for lane in range(8)]               # bin = word * 8 + lane
    out = capture.decode_fft_lanes(lanes)
    np.testing.assert_array_equal(out, re + 1j * im)


def test_adc_bram_decoder():
    n_words = 16
    samples = (np.arange(n_words * 8) - 50) + 1j * (100 - np.arange(n_words * 8))
    brams = []
    for q in range(2):                                          # sample = word*8 + bram*4 + k
        s = samples.reshape(n_words, 2, 4)[:, q, :]
        iq = np.stack([s.real, s.imag], -1).astype(">i2")
        brams.append(iq.tobytes())
    np.testing.assert_array_equal(capture.decode_adc_brams(brams), samples.astype(np.complex64))


def _send_dump(port, fid, n_packets, drop, values):
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    order = list(range(n_packets))[::-1]                        # out of order on purpose
    for s in order:
        if s == drop:
            continue
        x = np.full((2048, 4), values[fid] + 1j * (-values[fid]))
        tx.sendto(L.make_adc_packet(s, 12345, fid, 1, x, adc_lsb=(6, 6, 6, 6), ddr_size=n_packets * 128 - 1),
                  ("127.0.0.1", port))
        time.sleep(0.0005)
    tx.close()


def test_dump_receiver_separates_boards_and_orders_packets():
    port = 41000 + np.random.default_rng().integers(0, 1000)
    values = {0: 3, 1: -5}

    def send():
        for fid in (0, 1):
            threading.Thread(target=_send_dump, args=(port, fid, 20, 7 if fid == 1 else -1, values)).start()

    payloads, received, _ = capture.receive_dumps(int(port), {0: 20, 1: 20}, timeout=10, idle=1.0,
                                                  started=lambda: threading.Timer(0.2, send).start())
    assert received[0].all()
    assert received[1].sum() == 19 and not received[1][7]
    h = L.parse_adc_headers(payloads[1][received[1]])
    assert np.all(np.diff(h["s"].astype(int)) > 0)             # reassembled in sequence order
    x = L.unpack_adc_payload(payloads[0])
    assert np.all(x == 3 - 3j)
    assert np.all(L.unpack_adc_payload(payloads[1][7:8]) == 0)  # missing packet left as zeros
