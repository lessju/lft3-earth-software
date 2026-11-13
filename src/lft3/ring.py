"""Read the real-time runtime's voltage rings from Python (status, metrics, triggers, and GPU data access).

Each lft3-capture process owns one ring (docs/runtime.md, docs/runtime_guide.md):

* control plane ``/dev/shm/lft3-<stream>``: a fixed binary layout (``RingControl`` in
  ``pipeline/src/common/ring.hh``) with the layout, publication head, per-slot metadata, reader cursors and
  capture statistics. This module mirrors the offsets; ``kRingVersion`` guards them.
* data plane: a CUDA VMM allocation handed out as a file descriptor over ``/dev/shm/lft3-<stream>.sock``.
  :meth:`Ring.voltages` imports it through the CUDA driver API (ctypes, no extra packages) and wraps it as a
  CuPy array when CuPy is installed: the hook for GPU searches (hyperseti) written in Python.

    from lft3.ring import Ring, list_streams
    for name in list_streams():
        with Ring(name) as r:
            print(r.status())

Python 3.8 compatible; status/metrics need only the standard library.
"""
from __future__ import annotations

import ctypes
import mmap
import os
import socket
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path

SHM_DIR = Path("/dev/shm")
MAGIC = 0x4C46543352494E47
VERSION = 1
MAX_READERS = 16
MAX_SLOTS = 4096
CONTROL_BYTES = 576 + MAX_READERS * 128 + MAX_SLOTS * 8 + MAX_SLOTS * 128
OFF_READERS = 576
OFF_SEQ_TO_SLOT = OFF_READERS + MAX_READERS * 128
OFF_SLOTS = OFF_SEQ_TO_SLOT + MAX_SLOTS * 8
STATES = {0: "starting", 1: "running", 2: "stopped"}
STAT_NAMES = ("packets_received", "packets_accepted", "packets_filtered", "packets_duplicate", "packets_late",
              "packets_no_buffer", "packets_missing", "blocks_complete", "blocks_partial", "blocks_skipped",
              "blocks_published", "restarts", "backpressure_ns", "latest_t", "rate_milli_gbps", "slots_unavailable")
INVALID_SEQ = 2 ** 64 - 1


def shm_path(stream: str) -> Path:
    return SHM_DIR / f"lft3-{stream}"


def socket_path(stream: str) -> Path:
    return SHM_DIR / f"lft3-{stream}.sock"


def dump_socket_path(stream: str) -> Path:
    return SHM_DIR / f"lft3-{stream}-dump.sock"


def list_streams() -> list[str]:
    """Streams with a control block in /dev/shm (running or recently stopped captures)."""
    out = []
    for p in sorted(SHM_DIR.glob("lft3-*")):
        if p.suffix == ".sock" or not p.is_file():
            continue
        try:
            if p.stat().st_size == CONTROL_BYTES:
                out.append(p.name[len("lft3-"):])
        except OSError:
            pass
    return out


def _cstr(b: bytes) -> str:
    return b.split(b"\0", 1)[0].decode(errors="replace")


@dataclass
class ReaderStatus:
    name: str
    lossless: bool
    cursor: int
    lag_blocks: int
    blocks_read: int
    overruns: int
    busy_ms: float
    outputs: int
    heartbeat_age_s: float


@dataclass
class SlotMeta:
    seq: int
    block_index: int
    t0: int
    missing_packets: int
    restarts: int
    nco: tuple
    pfb_lsb: tuple
    fid: int
    flags: int
    unix_time_at_t0: float
    publish_unix: float


@dataclass
class RingStatus:
    stream: str
    state: str
    obs_id: str
    software: str
    board_id: int
    nco_mhz: float
    channel_start: int
    channel_count: int
    spectra_per_block: int
    slots: int
    block_bytes: int
    ring_bytes: int
    head: int
    unix_time_at_t0: float
    start_unix: float
    writer_heartbeat_age_s: float
    stats: dict = field(default_factory=dict)
    readers: list = field(default_factory=list)

    @property
    def ring_seconds(self) -> float:
        return self.slots * self.spectra_per_block / 152587.890625

    @property
    def rate_gbps(self) -> float:
        return self.stats.get("rate_milli_gbps", 0) / 1e3


def _monotonic_ns() -> int:
    return time.clock_gettime_ns(time.CLOCK_MONOTONIC)


class Ring:
    """A read-only view of one ring's control block (plus optional GPU access to the voltages)."""

    def __init__(self, stream: str):
        self.stream = stream
        path = shm_path(stream)
        fd = os.open(str(path), os.O_RDONLY)
        try:
            if os.fstat(fd).st_size < CONTROL_BYTES:
                raise ValueError(f"{path} is not an lft3 ring control block")
            self._mm = mmap.mmap(fd, CONTROL_BYTES, mmap.MAP_SHARED, mmap.PROT_READ)
        finally:
            os.close(fd)
        magic, version, size = struct.unpack_from("<QII", self._mm, 0)
        if magic != MAGIC or version != VERSION or size != CONTROL_BYTES:
            self._mm.close()
            raise ValueError(f"{path}: magic/version {magic:#x}/{version} not supported (want version {VERSION})")
        self._cuda = None

    def close(self) -> None:
        if self._cuda is not None:
            self._cuda.release()
            self._cuda = None
        self._mm.close()

    def __enter__(self) -> Ring:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _u64(self, off: int) -> int:
        return struct.unpack_from("<Q", self._mm, off)[0]

    @property
    def head(self) -> int:
        return self._u64(368)

    def status(self) -> RingStatus:
        m = self._mm
        stream, obs, software, chash = (_cstr(m[o:o + n]) for o, n in ((24, 48), (72, 64), (136, 64), (200, 64)))
        cs, cc, spb, beams, slots, bb, rb = struct.unpack_from("<7Q", m, 264)
        board_id, state = struct.unpack_from("<II", m, 328)
        nco, _sps = struct.unpack_from("<dd", m, 336)
        t0 = struct.unpack_from("<d", m, 352)[0]
        head, _pid, hb = struct.unpack_from("<QQQ", m, 368)
        start = struct.unpack_from("<d", m, 392)[0]
        stats = dict(zip(STAT_NAMES, struct.unpack_from("<16Q", m, 400)))
        now = _monotonic_ns()
        readers = []
        for i in range(MAX_READERS):
            o = OFF_READERS + i * 128
            pid, cursor, rhb, lossless = struct.unpack_from("<QQQI", m, o)
            if not pid:
                continue
            name = _cstr(m[o + 32:o + 64])
            read, over, lag, busy, outputs = struct.unpack_from("<5Q", m, o + 64)
            readers.append(ReaderStatus(name, bool(lossless), cursor, lag, read, over, busy / 1e6, outputs,
                                        max(0.0, (now - rhb) / 1e9)))
        return RingStatus(stream, STATES.get(state, str(state)), obs, software, board_id, nco, cs, cc, spb, slots,
                          bb, rb, head, t0, start, max(0.0, (now - hb) / 1e9), stats, readers)

    def slot_meta(self, seq: int) -> SlotMeta | None:
        """Metadata of published block `seq`, or None if it is no longer (or not yet) in the ring."""
        slot = self._u64(OFF_SEQ_TO_SLOT + (seq % MAX_SLOTS) * 8)
        if slot >= MAX_SLOTS:
            return None
        o = OFF_SLOTS + slot * 128
        raw = struct.unpack_from("<5Q4i4HIIdd", self._mm, o)
        if raw[0] != seq:
            return None
        meta = SlotMeta(raw[0], raw[1], raw[2], raw[3], raw[4], raw[5:9], raw[9:13], raw[13], raw[14], raw[15],
                        raw[16])
        return meta if self._u64(o) == seq else None

    def voltages(self):
        """The whole ring as a read-only CuPy uint8 array of shape (slots, block_bytes) (needs CuPy and a GPU).

        Reshape a slot to [beam, channel, t, pol, re/im] int8. Use slot_meta() before and after reading a block:
        if its seq changed, the writer reused the slot meanwhile (an overrun)."""
        import cupy  # optional dependency

        if self._cuda is None:
            self._cuda = _VmmImport(self.stream)
        st = self.status()
        mem = cupy.cuda.UnownedMemory(self._cuda.ptr, self._cuda.size, self)
        arr = cupy.ndarray((st.slots, st.block_bytes), cupy.uint8, cupy.cuda.MemoryPointer(mem, 0))
        return arr

    def slot_index(self, seq: int) -> int | None:
        meta = self.slot_meta(seq)
        return None if meta is None else self._u64(OFF_SEQ_TO_SLOT + (seq % MAX_SLOTS) * 8)


class _VmmImport:
    """Imports the ring's VMM allocation through the CUDA driver API (ctypes): read-only mapping."""

    _PROP_FMT = None

    def __init__(self, stream: str):
        self.cu = ctypes.CDLL("libcuda.so.1")
        self.ptr = 0
        self.size = 0
        self.handle = ctypes.c_ulonglong(0)
        self._check(self.cu.cuInit(0), "cuInit")
        dev = ctypes.c_int()
        self._check(self.cu.cuDeviceGet(ctypes.byref(dev), 0), "cuDeviceGet")
        ctx = ctypes.c_void_p()
        self._check(self.cu.cuDevicePrimaryCtxRetain(ctypes.byref(ctx), dev), "cuDevicePrimaryCtxRetain")
        self._check(self.cu.cuCtxSetCurrent(ctx), "cuCtxSetCurrent")
        fd, self.size = _receive_fd(stream)
        try:
            # CU_MEM_HANDLE_TYPE_POSIX_FILE_DESCRIPTOR = 1
            self._check(self.cu.cuMemImportFromShareableHandle(ctypes.byref(self.handle), ctypes.c_void_p(fd), 1),
                        "cuMemImportFromShareableHandle")
        finally:
            os.close(fd)
        ptr = ctypes.c_ulonglong(0)
        self._check(self.cu.cuMemAddressReserve(ctypes.byref(ptr), ctypes.c_size_t(self.size),
                                                ctypes.c_size_t(2 << 20), ctypes.c_ulonglong(0), ctypes.c_ulonglong(0)),
                    "cuMemAddressReserve")
        self.ptr = ptr.value
        self._check(self.cu.cuMemMap(ctypes.c_ulonglong(self.ptr), ctypes.c_size_t(self.size), ctypes.c_size_t(0),
                                     self.handle, ctypes.c_ulonglong(0)), "cuMemMap")

        class Location(ctypes.Structure):
            _fields_ = [("type", ctypes.c_int), ("id", ctypes.c_int)]

        class AccessDesc(ctypes.Structure):
            _fields_ = [("location", Location), ("flags", ctypes.c_int)]

        # CU_MEM_LOCATION_TYPE_DEVICE = 1, CU_MEM_ACCESS_FLAGS_PROT_READ = 1
        desc = AccessDesc(Location(1, 0), 1)
        self._check(self.cu.cuMemSetAccess(ctypes.c_ulonglong(self.ptr), ctypes.c_size_t(self.size),
                                           ctypes.byref(desc), ctypes.c_size_t(1)), "cuMemSetAccess")

    def _check(self, rc: int, what: str) -> None:
        if rc != 0:
            raise RuntimeError(f"{what} failed (CUresult {rc})")

    def release(self) -> None:
        if self.ptr:
            self.cu.cuMemUnmap(ctypes.c_ulonglong(self.ptr), ctypes.c_size_t(self.size))
            self.cu.cuMemAddressFree(ctypes.c_ulonglong(self.ptr), ctypes.c_size_t(self.size))
            self.ptr = 0
        if self.handle.value:
            self.cu.cuMemRelease(self.handle)
            self.handle = ctypes.c_ulonglong(0)


def _receive_fd(stream: str) -> tuple[int, int]:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(str(socket_path(stream)))
        msg, anc, _flags, _addr = s.recvmsg(16, socket.CMSG_LEN(4))
        fds = [struct.unpack("i", data[:4])[0] for level, typ, data in anc
               if level == socket.SOL_SOCKET and typ == socket.SCM_RIGHTS]
        if len(msg) != 16 or not fds:
            raise RuntimeError(f"no ring memory from {socket_path(stream)}")
        _ring_id, size = struct.unpack("<QQ", msg)
        return fds[0], size
    finally:
        s.close()


def send_dump_command(stream: str, command: str, timeout: float = 5.0) -> str:
    """Send one command ("trigger now 0.5 0.5 label", "status") to a stream's lft3-dump; returns its reply."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(dump_socket_path(stream)))
        s.sendall(command.encode() + b"\n")
        return s.recv(512).decode().strip()
    finally:
        s.close()


def to_prometheus(statuses: list[RingStatus]) -> str:
    """Prometheus text exposition of ring, capture and reader statistics."""
    lines = []

    def g(name: str, value: float, **labels: str) -> None:
        lab = ",".join(f'{k}="{v}"' for k, v in labels.items())
        lines.append(f"lft3_{name}{{{lab}}} {value}")

    for st in statuses:
        s = st.stream
        g("ring_running", 1 if st.state == "running" else 0, stream=s)
        g("ring_head_blocks", st.head, stream=s)
        g("ring_slots", st.slots, stream=s)
        g("capture_rate_gbps", st.rate_gbps, stream=s)
        g("capture_heartbeat_age_seconds", st.writer_heartbeat_age_s, stream=s)
        for k, v in st.stats.items():
            if k != "rate_milli_gbps":
                g(f"capture_{k}_total", v, stream=s)
        for r in st.readers:
            g("reader_lag_blocks", r.lag_blocks, stream=s, reader=r.name)
            g("reader_overruns_total", r.overruns, stream=s, reader=r.name)
            g("reader_blocks_read_total", r.blocks_read, stream=s, reader=r.name)
            g("reader_busy_ms", r.busy_ms, stream=s, reader=r.name)
            g("reader_outputs_total", r.outputs, stream=s, reader=r.name)
            g("reader_heartbeat_age_seconds", r.heartbeat_age_s, stream=s, reader=r.name)
    return "\n".join(lines) + "\n"
