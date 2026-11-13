"""Data from the boards: firmware snapshots and triggered ADC (DDR) dumps.

* FFT snapshot  (`fft_snapshot`)   one full 8192-point PFB output per input, before requantisation
                                   (24-bit I/Q per bin, 8 parallel lanes); returned as power, FFT bin order.
* ADC snapshot  (`adc_snapshot`)   up to 8192 consecutive complex 16-bit samples per input (1250 MSPS).
* DDR dump      (`capture_dump`)   trigger the 4 GiB ring buffer (or a smaller ring, `ddr_size`) and receive
                                   the 4-bit I/Q dump packets on UDP `adc_port` with a plain socket. Several
                                   boards can dump at once: packets are separated by the header fid.

Snapshot readout follows lft3-earth-main/host/scripts/listener.py. The word decoders are pure functions
(tested without hardware).
"""
from __future__ import annotations

import logging
import socket
import time
from dataclasses import dataclass, field

import numpy as np

from lft3 import packets as L

from .control import Rfsoc4x2

log = logging.getLogger(__name__)

NLANE = 8                     # parallel FFT lanes per F-engine
FFT_SIZE = 8192
ADC_SNAPSHOTS_PER_FENG = 2
ADC_SAMPLES_PER_WORD = 4      # per 128-bit snapshot word: 4 x (I int16, Q int16)
ADC_SNAPSHOT_WORDS = 1024
DDR_WORDS_PER_PACKET = 128
RCVBUF_WANTED = 256 * 1024 * 1024   # a 64 MiB dump fits entirely; larger dumps need the reader to keep up


# -- pure decoders ------------------------------------------------------------------------------------
def _signed(value: np.ndarray, bits: int) -> np.ndarray:
    value = value.astype(np.int64)
    return np.where(value & (1 << (bits - 1)), value - (1 << bits), value)


def decode_fft_lanes(lanes: list[np.ndarray]) -> np.ndarray:
    """lanes[l][w] = 48-bit word (24-bit re | 24-bit im) of FFT bin w*8 + l. Returns complex128 [8192]."""
    words = np.stack([np.asarray(x[: FFT_SIZE // NLANE], dtype=np.uint64) for x in lanes])   # [lane, word]
    re = _signed((words >> np.uint64(24)) & np.uint64(0xFFFFFF), 24)
    im = _signed(words & np.uint64(0xFFFFFF), 24)
    return (re + 1j * im).T.reshape(-1)


def decode_adc_brams(brams: list[bytes]) -> np.ndarray:
    """brams = raw bytes of the two async snapshot BRAMs of one input. Returns complex64 samples in time order:
    sample = word * 8 + bram * 4 + k (each 128-bit word holds 4 big-endian int16 I/Q pairs)."""
    quarters = []
    for raw in brams:
        w = np.frombuffer(raw, dtype=">i2").reshape(-1, ADC_SAMPLES_PER_WORD, 2)
        quarters.append(w[:, :, 0] + 1j * w[:, :, 1])
    return np.concatenate(quarters, axis=1).ravel().astype(np.complex64)


# -- snapshots ------------------------------------------------------------------------------------------
@dataclass
class Capture:
    """One capture from one board. `data` is [input, sample-or-bin]."""
    kind: str                         # "fft_snapshot" | "adc_snapshot" | "adc_dump"
    board: str
    time: float
    data: np.ndarray
    attrs: dict = field(default_factory=dict)


def _snapshot_attrs(board: Rfsoc4x2) -> dict:
    c = board.cfg
    return {"board_id": c.board_id, "nco_mhz": c.nco_mhz, "sample_rate_hz": L.FS_DECIM_HZ,
            "adc_lsb": c.adc_lsb, "ch_lsb": c.ch_lsb, "host": c.host,
            "inputs": [f"beam{i.beam}{i.pol}" for i in c.inputs]}


def fft_snapshot(board: Rfsoc4x2) -> Capture:
    """Power of one full PFB output per input (FFT bin order: DC first, negative frequencies from 4096)."""
    board.describe_design()
    f = board.fpga
    names = [[f"f_engine{fe}_snapshot{lane}" for lane in range(NLANE)] for fe in range(4)]
    for row in names:
        for n in row:
            getattr(f.snapshots, n).arm()
    t = time.time()
    f.registers.snapshot_ctrl_fft.write(start="pulse")
    spectra = []
    for row in names:
        lanes = [np.asarray(getattr(f.snapshots, n).read(arm=False, timeout=3)["data"]["data"]) for n in row]
        spectra.append(np.abs(decode_fft_lanes(lanes)) ** 2)
    attrs = _snapshot_attrs(board)
    attrs["bin_hz"] = L.CHAN_BW_HZ
    return Capture("fft_snapshot", board.cfg.name, t, np.array(spectra), attrs)


def adc_snapshot(board: Rfsoc4x2, nsamples: int = 8192) -> Capture:
    """Consecutive complex samples per input from the 16-bit ADC snapshot BRAMs."""
    board.describe_design()
    f = board.fpga
    per_word = ADC_SNAPSHOTS_PER_FENG * ADC_SAMPLES_PER_WORD
    nwords = min(int(np.ceil(nsamples / per_word)), ADC_SNAPSHOT_WORDS)
    names = [[f"f_engine{fe}_adc_snapshot_async{i}_ss" for i in range(ADC_SNAPSHOTS_PER_FENG)] for fe in range(4)]
    for row in names:
        for n in row:
            getattr(f.snapshots, n).arm()
    t = time.time()
    f.registers.snapshot_ctrl_adc.write(start="pulse")
    time.sleep(0.05)
    data = [decode_adc_brams([f.read(n + "_bram", nwords * 16) for n in row])[:nsamples] for row in names]
    return Capture("adc_snapshot", board.cfg.name, t, np.array(data), _snapshot_attrs(board))


# -- DDR dump ---------------------------------------------------------------------------------------------
def receive_dumps(port: int, expected: dict[int, int], timeout: float = 15.0, idle: float = 1.5,
                  started: callable = None) -> tuple[dict[int, np.ndarray], dict[int, np.ndarray], float]:
    """Receive ADC dump packets on UDP `port` until every fid in `expected` ({fid: packets}) is complete,
    `idle` s pass without packets after the first, or `timeout`. `started()` is called once the socket is
    ready (that is when the boards should be triggered).

    Returns ({fid: payloads uint8 [packets, 8256] in sequence order, missing rows zero},
             {fid: bool [packets] received}, seconds)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RCVBUF_WANTED)   # capped by net.core.rmem_max
    granted = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF) // 2   # Linux reports twice the size
    if granted < RCVBUF_WANTED // 2:
        log.warning("UDP receive buffer is only %d kB (net.core.rmem_max): dump packets will be dropped. "
                    "Fix once as root: sysctl -w net.core.rmem_max=%d (persist in /etc/sysctl.d/)",
                    granted // 1024, RCVBUF_WANTED)
    sock.bind(("0.0.0.0", port))
    sock.settimeout(0.2)
    total = sum(expected.values())
    raw = np.zeros((total + 1024, L.PAYLOAD_BYTES), np.uint8)     # landing area, one row per datagram
    view = memoryview(raw.reshape(-1))
    pb = L.PAYLOAD_BYTES
    n = 0
    t0 = time.monotonic()
    last = None
    try:
        if started:
            started()
        while n < raw.shape[0]:
            now = time.monotonic()
            if now - t0 > timeout or (last is not None and now - last > idle):
                break
            try:
                got = sock.recv_into(view[n * pb:(n + 1) * pb], pb)
            except socket.timeout:
                continue
            if got == L.PAYLOAD_BYTES:
                n += 1
                last = time.monotonic()
                if n >= total and _complete(raw[:n], expected):
                    break
    finally:
        sock.close()
    seconds = (last or time.monotonic()) - t0
    return (*_assemble(raw[:n], expected), seconds)


def _headers(p: np.ndarray) -> dict:
    return L.parse_adc_headers(p, "big")


def _complete(p: np.ndarray, expected: dict[int, int]) -> bool:
    h = _headers(p)
    return all(len(np.unique(h["s"][h["fid"] == fid])) >= cnt for fid, cnt in expected.items())


def _assemble(p: np.ndarray, expected: dict[int, int]):
    h = _headers(p) if len(p) else {"fid": np.array([], int), "s": np.array([], np.uint32)}
    out, got = {}, {}
    for fid, cnt in expected.items():
        sel = np.nonzero(h["fid"] == fid)[0]
        rows = np.zeros((cnt, L.PAYLOAD_BYTES), np.uint8)
        have = np.zeros(cnt, bool)
        s = h["s"][sel].astype(np.int64)
        ok = s < cnt
        rows[s[ok]] = p[sel[ok]]
        have[s[ok]] = True
        out[fid], got[fid] = rows, have
    return out, got


def capture_dump(boards: list[Rfsoc4x2], ddr_size: int | None = None, timeout: float = 15.0) -> list[Capture]:
    """Trigger the DDR dump on `boards` and receive it. `ddr_size` (last word address, e.g. 2**20-1 = 64 MiB)
    temporarily shrinks the ring (restored afterwards); None uses each board's configured size.
    Needs the destination IP of the boards (e.g. 10.17.16.10) on a local interface."""
    port = boards[0].cfg.adc_port
    sizes = {b.cfg.board_id: (ddr_size if ddr_size is not None else b.cfg.ddr_size) for b in boards}
    expected = {fid: (size + 1) // DDR_WORDS_PER_PACKET for fid, size in sizes.items()}
    for b in boards:
        if ddr_size is not None:
            b.fpga.write_int("packetizer_ddr_size", ddr_size)
            b.fpga.write_int("packetizer_ddr_read_delay", (ddr_size + 1) // 2)

    def trigger_all():
        for b in boards:
            b.trigger_dump()

    t = time.time()
    try:
        payloads, received, seconds = receive_dumps(port, expected, timeout=timeout, started=trigger_all)
    finally:
        if ddr_size is not None:
            for b in boards:
                b.fpga.write_int("packetizer_ddr_size", b.cfg.ddr_size)
                b.fpga.write_int("packetizer_ddr_read_delay", b.cfg.ddr_read_delay)
    captures = []
    for b in boards:
        fid = b.cfg.board_id
        p, have = payloads[fid], received[fid]
        x = L.unpack_adc_payload(p, "big").reshape(-1, 4).T            # [input, sample]
        h = _headers(p[have][:1]) if have.any() else None
        attrs = _snapshot_attrs(b)
        attrs.update({
            "ddr_size": sizes[fid], "packets_expected": len(have), "packets_received": int(have.sum()),
            "receive_seconds": seconds, "received_mask": have,
        })
        if h is not None:
            attrs.update({"t_trig": int(h["t_trig"][0]), "trigger_src": int(h["src"][0]),
                          "header_adc_lsb": h["adc_lsb"][0].tolist(), "ddr_read_delay": int(h["ddr_read_delay"][0])})
        log.info("%s: dump %d/%d packets in %.2f s", b.cfg.name, have.sum(), len(have), seconds)
        captures.append(Capture("adc_dump", b.cfg.name, t, x, attrs))
    return captures
