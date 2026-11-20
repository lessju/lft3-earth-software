"""Read the real-time runtime's rings from Python (status, metrics, triggers, and GPU data access).

Each lft3-capture process owns one voltage ring ``<board>-<link>``; a spectrometer started with
``--spectra-ring N`` owns a spectra ring ``<board>-<link>-spectra`` (one integrated fine spectrum per slot,
float32 [beam][channel], for the live search). Both use the same layout (docs/runtime.md):

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
VERSION = 2
KIND_VOLTAGES, KIND_SPECTRA = 0, 1
KINDS = {KIND_VOLTAGES: "voltages", KIND_SPECTRA: "spectra"}
FLAG_PARTIAL, FLAG_NEW_RUN = 1, 2       # slot flags (spectra ring: FLAG_NEW_RUN = discontinuity before this one)
READER_TIMEOUT_NS = 5_000_000_000
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


def list_streams(kind: str | None = None) -> list[str]:
    """Rings with a control block in /dev/shm (running or recently stopped). kind: "voltages" | "spectra" | None."""
    out = []
    for p in sorted(SHM_DIR.glob("lft3-*")):
        if p.suffix in (".sock", ".pid") or not p.is_file():
            continue
        try:
            if p.stat().st_size != CONTROL_BYTES:
                continue
            if kind is not None:
                with open(p, "rb") as fh:
                    head = fh.read(560)
                if len(head) < 532 or KINDS.get(struct.unpack_from("<I", head, 528)[0]) != kind:
                    continue
            out.append(p.name[len("lft3-"):])
        except OSError:
            pass
    return out


def spectra_stream(stream: str) -> str:
    """Name of the spectra ring of a voltage stream."""
    return f"{stream}-spectra"


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
    kind: str = "voltages"
    fine_channels_per_coarse: int = 0     # spectra ring: FFT length
    fch1_mhz: float = 0.0                 # spectra ring: frequency axis of the published spectra
    foff_mhz: float = 0.0
    tsamp_s: float = 0.0                  # spectra ring: integration time per slot

    @property
    def ring_seconds(self) -> float:
        """Time covered by the ring (spectra ring: spectra_per_block = spectra per integration)."""
        return self.slots * self.spectra_per_block / 152587.890625

    @property
    def channels_out(self) -> int:
        """Values per beam in one slot (spectra ring)."""
        return self.block_bytes // (4 * 2) if self.kind == "spectra" else 0

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
        # Version 1 (before the spectra ring) differs only in bytes 528..575, which were reserved (zero = voltages):
        # still readable, so status works against a run started with older binaries.
        if magic != MAGIC or version not in (1, VERSION) or size != CONTROL_BYTES:
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
        kind, nfine, fch1, foff, tsamp = struct.unpack_from("<IIddd", m, 528)
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
                          bb, rb, head, t0, start, max(0.0, (now - hb) / 1e9), stats, readers,
                          KINDS.get(kind, str(kind)), nfine, fch1, foff, tsamp)

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

    def spectra(self):
        """A spectra ring as a read-only CuPy float32 array of shape (slots, beams, channels)."""
        st = self.status()
        if st.kind != "spectra":
            raise ValueError(f"{self.stream} is a {st.kind} ring")
        return self.voltages().view("float32").reshape(st.slots, 2, st.channels_out)

    def slot_index(self, seq: int) -> int | None:
        meta = self.slot_meta(seq)
        return None if meta is None else self._u64(OFF_SEQ_TO_SLOT + (seq % MAX_SLOTS) * 8)


class RingReader:
    """A registered reader of a ring from Python: it appears in the ring's reader table (status, lag, overruns)
    and, with lossless=True, the writer waits for it when backpressure is on (synthetic / file sources).

        with RingReader("rfsoc0-link0-spectra", "search") as rd:
            while True:
                seq = rd.cursor
                if not rd.wait(seq, 1.0): continue
                meta, slot = rd.begin(seq)        # None if overwritten (overrun)
                ... read rd.ring.spectra()[slot] on the GPU, synchronise ...
                ok = rd.still_valid(seq)
                rd.consumed(seq + 1)

    The entry is claimed with an atomic compare-and-swap (libatomic) like the C++ readers. A thread refreshes
    the heartbeat every second, so a long GPU search does not make the writer treat this reader as dead.
    """

    def __init__(self, stream: str, name: str, lossless: bool = False, timeout: float = 30.0):
        t_end = time.time() + timeout
        while True:
            try:
                self.ring = Ring(stream)
                break
            except (OSError, ValueError):
                if time.time() > t_end:
                    raise
                time.sleep(0.2)
        fd = os.open(str(shm_path(stream)), os.O_RDWR)
        try:
            self._mm = mmap.mmap(fd, CONTROL_BYTES, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
        finally:
            os.close(fd)
        self._buf = (ctypes.c_char * CONTROL_BYTES).from_buffer(self._mm)
        self._base = ctypes.addressof(self._buf)
        self.entry = -1
        self.name = name
        me = os.getpid() | (1 << 63)
        now = _monotonic_ns()
        for i in range(MAX_READERS):
            o = OFF_READERS + i * 128
            cur = self._load(o)
            if cur != 0 and now - self._load(o + 16) < READER_TIMEOUT_NS:
                continue
            if _cas_u64(self._base + o, cur, me):
                self._store(o + 8, self.ring.head)          # cursor
                self._store(o + 16, now)                    # heartbeat
                struct.pack_into("<I", self._mm, o + 24, 1 if lossless else 0)
                raw = name.encode()[:31]
                self._mm[o + 32:o + 64] = raw + b"\0" * (32 - len(raw))
                for k in range(5):                           # blocksRead, overruns, lag, busy, outputs
                    self._store(o + 64 + 8 * k, 0)
                self.entry = i
                break
        if self.entry < 0:
            self.close()
            raise RuntimeError(f"{stream}: all {MAX_READERS} reader entries are in use")
        self._o = OFF_READERS + self.entry * 128
        self._stop = False
        import threading
        self._beat = threading.Thread(target=self._heartbeat_loop, name="ring-heartbeat", daemon=True)
        self._beat.start()

    # 8-byte aligned loads/stores through ctypes are single instructions on aarch64 / x86-64.
    def _load(self, off: int) -> int:
        return ctypes.c_uint64.from_address(self._base + off).value

    def _store(self, off: int, value: int) -> None:
        ctypes.c_uint64.from_address(self._base + off).value = value

    def _heartbeat_loop(self) -> None:
        while not self._stop:
            self._store(self._o + 16, _monotonic_ns())
            time.sleep(1.0)

    @property
    def cursor(self) -> int:
        return self._load(self._o + 8)

    def wait(self, seq: int, timeout: float) -> bool:
        """True once block/spectrum `seq` is published (polls; the cadence of spectra is ~1 s)."""
        t_end = time.time() + timeout
        while self.ring.head <= seq:
            if time.time() >= t_end or self.ring.status().state == "stopped":
                return self.ring.head > seq
            time.sleep(0.01)
        return True

    def oldest_readable(self, margin: int = 8) -> int:
        st = self.ring.status()
        keep = st.slots - margin if st.slots > margin else 1
        return max(0, st.head - keep)

    def begin(self, seq: int):
        """(SlotMeta, slot index) of published `seq`, or None if it was already overwritten."""
        meta = self.ring.slot_meta(seq)
        if meta is None:
            return None
        slot = self.ring._u64(OFF_SEQ_TO_SLOT + (seq % MAX_SLOTS) * 8)
        return meta, slot

    def still_valid(self, seq: int) -> bool:
        return self.ring.slot_meta(seq) is not None

    def consumed(self, next_seq: int, busy_ns: int = 0, outputs: int | None = None) -> None:
        self._store(self._o + 8, next_seq)
        head = self.ring.head
        self._store(self._o + 64 + 16, head - next_seq if head > next_seq else 0)   # lagBlocks
        if busy_ns:
            self._store(self._o + 64 + 24, busy_ns)
        if outputs is not None:
            self._store(self._o + 64 + 32, outputs)
        self._store(self._o + 16, _monotonic_ns())

    def add(self, field_index: int, n: int) -> None:
        """Add to blocksRead (0) or overruns (1)."""
        off = self._o + 64 + 8 * field_index
        self._store(off, self._load(off) + n)

    def close(self) -> None:
        self._stop = True
        if getattr(self, "entry", -1) >= 0:
            struct.pack_into("<I", self._mm, self._o + 24, 0)
            self._store(self._o, 0)
            self.entry = -1
        if getattr(self, "_buf", None) is not None:
            del self._buf
            self._buf = None
            self._mm.close()
        if getattr(self, "ring", None) is not None:
            self.ring.close()
            self.ring = None

    def __enter__(self) -> RingReader:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


_libatomic = None


def _cas_u64(address: int, expected: int, desired: int) -> bool:
    """Atomic 64-bit compare-and-swap at `address` (libatomic's __atomic_compare_exchange_8)."""
    global _libatomic
    if _libatomic is None:
        _libatomic = ctypes.CDLL("libatomic.so.1")
        _libatomic.__atomic_compare_exchange_8.restype = ctypes.c_bool
        _libatomic.__atomic_compare_exchange_8.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint64,
                                                           ctypes.c_bool, ctypes.c_int, ctypes.c_int]
    exp = ctypes.c_uint64(expected)
    return bool(_libatomic.__atomic_compare_exchange_8(address, ctypes.addressof(exp), desired, False, 5, 5))


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
        if st.kind == "spectra":
            g("spectra_ring_head", st.head, stream=s)
            for r in st.readers:
                g("reader_lag_spectra", r.lag_blocks, stream=s, reader=r.name)
                g("reader_overruns_total", r.overruns, stream=s, reader=r.name)
                g("reader_outputs_total", r.outputs, stream=s, reader=r.name)
                g("reader_busy_ms", r.busy_ms, stream=s, reader=r.name)
                g("reader_heartbeat_age_seconds", r.heartbeat_age_s, stream=s, reader=r.name)
            continue
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
