"""Plan a run of the real-time runtime: which processes, with which arguments, in which containers.

For every link of a board (``receiver.links`` in the board configuration) a run consists of:

* ``capture``       lft3-capture: the link (or a synthetic / pcap source) -> the ring ``<board>-<link>``; for the
                    network source with CAP_NET_RAW + CAP_IPC_LOCK and /dev/infiniband (not --privileged)
* ``spectrometer``  lft3-spectrometer fine mode -> FBH5 in <data_dir>/spectra/<board>/, plus the coarse power per
                    board channel derived from it (Parseval, no second pass over the ring) -> FBH5 in
                    <data_dir>/spectra/<board>/quicklook/ and <run_dir>/latest/<board>_latest.{json,f32}
* ``quicklook``     optional (``quicklook_s``): a second reader in coarse mode for a faster cadence than the fine
                    integration -> <run_dir>/latest_fast/ (costs one more full read of every block)
* ``dump``          lft3-dump: GUPPI RAW windows on trigger -> <data_dir>/dumps/<board>/

Each process runs in its own container (image ``lft3-runtime``) so it can be restarted on its own and a crash
in a reader never touches the capture. Capture of a network source gets NIC access (DAQIRI raw ibverbs); readers
run as the invoking user. All share the host IPC namespace (/dev/shm: ring control blocks and sockets).

    plan = plan_run(board, RunOptions(source="synthetic"), data_dir="/opt/lft3arth/data", run_dir=...)
    for p in plan.processes: print(p.docker_argv())
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

from lft3.config import BoardConfig, LinkConfig
from lft3.packets import CH_PER_PKT, NCHAN, SPECTRUM_RATE

SOURCES = ("network", "synthetic", "file")
IMAGE = os.environ.get("LFT3_IMAGE", "lft3-runtime")
REPO = Path(__file__).resolve().parents[3]
BUILD = REPO / "pipeline" / "build"


@dataclass
class RunOptions:
    source: str = "network"
    file: str | None = None               # source=file: pcap path
    loop: bool = False                    # source=file: replay forever
    fast: bool = False                    # synthetic/file: not paced to real time
    lossless: bool = False                # synthetic/file: capture waits for the spectrometers (benchmarks, tests)
    links: list[str] | None = None        # None = every link of the board
    fft_length: int = 128                 # fine FFT length (128 -> 1.19 kHz channels)
    integration_s: float = 1.0
    channel_start: int = 0                # coarse channels, multiples of 1024
    channel_count: int = NCHAN
    record: bool = True
    spectra_per_file: int = 3600
    coarse: bool = True                   # derived coarse product (FBH5 + latest snapshot) from the spectrometer
    quicklook_s: float = 0.0              # > 0: extra coarse-mode reader at this cadence (seconds)
    dump: bool = True
    dump_max_s: float = 1.0               # largest trigger window (pinned host memory: 7.5 GB per second per link)
    ring_seconds: float = 2.0             # ring per link: 7.5 GB per second at full band
    obs_id: str = ""
    tone_channel: int = 4000              # synthetic source
    tone_input: int = 1
    drop_fraction: float = 0.0

    def validate(self) -> None:
        if self.source not in SOURCES:
            raise ValueError(f"source must be one of {SOURCES}")
        if self.source == "file" and not self.file:
            raise ValueError("source=file needs a capture file")
        if self.fft_length < 2 or self.fft_length & (self.fft_length - 1) or self.fft_length > 1024:
            raise ValueError("fft_length must be a power of two, 2..1024 (fused spectrometer within one block)")
        if self.channel_start % CH_PER_PKT or self.channel_count % CH_PER_PKT or self.channel_count <= 0 \
                or self.channel_start + self.channel_count > NCHAN:
            raise ValueError("channels must be whole packets: start/count multiples of 1024 within 0..6144")
        if self.integration_s <= 0 or self.quicklook_s < 0:
            raise ValueError("integration times must be positive")
        if self.ring_seconds < 0.2 or (self.dump and self.dump_max_s + 0.2 > self.ring_seconds):
            raise ValueError("ring_seconds must be >= 0.2 and longer than the dump window + 0.2 s")
        if self.lossless and self.source == "network":
            raise ValueError("lossless (backpressure) is only possible with synthetic or file sources")

    @property
    def tsamp(self) -> float:
        n = max(1, round(self.integration_s * SPECTRUM_RATE / 1024))
        return n * 1024 / SPECTRUM_RATE


@dataclass
class Process:
    role: str                    # capture | spectrometer | quicklook | dump
    stream: str
    container: str
    argv: list[str]              # inside the container
    privileged: bool = False
    cpuset: str = ""
    mounts: list[str] = field(default_factory=list)
    log: str = ""

    def docker_argv(self, uid: int | None = None, gid: int | None = None) -> list[str]:
        uid = os.getuid() if uid is None else uid
        gid = os.getgid() if gid is None else gid
        cmd = ["docker", "run", "--rm", "--name", self.container, "--gpus", "all", "--ipc=host",
               "--ulimit", "memlock=-1", "--stop-timeout", "30", "-e", "HOME=/tmp", "-w", str(REPO)]
        if self.privileged:
            # DAQIRI raw ibverbs needs the RDMA devices, CAP_NET_RAW (raw packet queues) and CAP_IPC_LOCK (pinned
            # buffers) -- verified live 2026-11-13; a fully privileged container is not needed.
            # LFT3_CAPTURE_PRIVILEGED=1 falls back to --privileged.
            if os.environ.get("LFT3_CAPTURE_PRIVILEGED") == "1":
                cmd += ["--privileged", "--net", "host"]
            else:
                cmd += ["--cap-add", "NET_RAW", "--cap-add", "IPC_LOCK", "--device", "/dev/infiniband", "--net", "host"]
        else:
            cmd += ["--user", f"{uid}:{gid}", "--net", "none"]
            for g in sorted(set(os.getgroups()) - {gid}):   # e.g. the data directory's group (lft3eath)
                cmd += ["--group-add", str(g)]
        if self.cpuset:
            cmd += ["--cpuset-cpus", self.cpuset]
        for m in [str(REPO)] + self.mounts:
            cmd += ["-v", f"{m}:{m}"]
        return cmd + [IMAGE] + self.argv


@dataclass
class RunPlan:
    board: str
    run_dir: str
    obs_id: str
    config_hash: str
    processes: list[Process]

    def streams(self) -> list[str]:
        return sorted({p.stream for p in self.processes})


def config_hash(board: BoardConfig, opts: RunOptions) -> str:
    h = hashlib.sha1()
    if board.path and Path(board.path).exists():
        h.update(Path(board.path).read_bytes())
    h.update(repr(sorted(vars(opts).items())).encode())
    return h.hexdigest()[:16]


def plan_run(board: BoardConfig, opts: RunOptions, data_dir: str, run_dir: str, stamp: str = "") -> RunPlan:
    opts.validate()
    links: list[LinkConfig] = [board.link(n) for n in opts.links] if opts.links else list(board.links)
    obs_id = opts.obs_id or f"{board.name}-{stamp or Path(run_dir).name}"
    chash = config_hash(board, opts)
    spectra_dir = str(Path(data_dir) / "spectra" / board.name)
    quick_dir = str(Path(data_dir) / "spectra" / board.name / "quicklook")
    dump_dir = str(Path(data_dir) / "dumps" / board.name)
    latest_dir = str(Path(run_dir) / "latest")
    mounts = [str(Path(data_dir))]
    if opts.file:
        mounts.append(str(Path(opts.file).resolve().parent))
    beams = ",".join(str(i.beam) for i in board.inputs)
    pols = ",".join("0" if i.pol == "X" else "1" for i in board.inputs)
    procs: list[Process] = []
    for lk in links:
        stream = board.stream(lk)
        prefix = board.name   # files are <board>_<UTC>_beam<N>.h5 with N = link.beam_offset + 0/1
        cap = [str(BUILD / "lft3-capture"), "--stream", stream, "--source", opts.source,
               "--board-id", str(board.board_id), "--udp-port", str(lk.udp_port),
               "--master-core", str(lk.master_core), "--worker-core", str(lk.worker_core),
               "--processing-core", str(lk.processing_core),
               "--channels", f"{opts.channel_start}:{opts.channel_count}",
               "--input-beam", beams, "--input-pol", pols, "--ring-seconds", f"{opts.ring_seconds:g}",
               "--nco-mhz", f"{board.nco_mhz:g}", "--obs-id", obs_id, "--config-hash", chash]
        if opts.source == "network":
            cap += ["--interface", lk.interface]
            if lk.source_ip:
                cap += ["--source-ip", lk.source_ip]
        else:
            if opts.fast:
                cap.append("--fast")
            if opts.lossless:
                n_lossless = 1 + (1 if opts.quicklook_s > 0 else 0)
                cap += ["--backpressure", "--wait-readers", str(n_lossless)]
            if opts.source == "synthetic":
                cap += ["--tone-channel", str(opts.tone_channel), "--tone-input", str(opts.tone_input),
                        "--drop-fraction", f"{opts.drop_fraction:g}"]
            else:
                cap += ["--file", str(Path(opts.file).resolve())]
                if opts.loop:
                    cap.append("--loop")
        cap_cpus = ",".join(str(c) for c in (lk.master_core, lk.worker_core, lk.processing_core))
        procs.append(Process("capture", stream, f"lft3-{stream}-capture", cap,
                             privileged=opts.source == "network", cpuset=cap_cpus, mounts=mounts,
                             log=str(Path(run_dir) / f"{stream}-capture.log")))
        common = ["--stream", stream, "--prefix", prefix, "--beam-offset", str(lk.beam_offset),
                  "--nco-mhz", f"{board.nco_mhz:g}", "--source-name", board.name]
        spec = [str(BUILD / "lft3-spectrometer"), *common, "--mode", "fine", "--fft-length", str(opts.fft_length),
                "--integration", f"{opts.integration_s:g}", "--output-dir", spectra_dir,
                "--spectra-per-file", str(opts.spectra_per_file)]
        if not opts.record:
            spec.append("--no-record")
        if opts.coarse:
            spec += ["--latest", latest_dir] + (["--coarse-dir", quick_dir] if opts.record else [])
        if opts.lossless:
            spec.append("--lossless")
        procs.append(Process("spectrometer", stream, f"lft3-{stream}-spectrometer", spec, cpuset=lk.reader_cpus,
                             mounts=mounts, log=str(Path(run_dir) / f"{stream}-spectrometer.log")))
        if opts.quicklook_s > 0:
            ql = [str(BUILD / "lft3-spectrometer"), *common, "--mode", "coarse", "--name", "quicklook",
                  "--integration", f"{opts.quicklook_s:g}", "--no-record", "--latest", latest_dir + "_fast"]
            if opts.lossless:
                ql.append("--lossless")
            procs.append(Process("quicklook", stream, f"lft3-{stream}-quicklook", ql, cpuset=lk.reader_cpus,
                                 mounts=mounts, log=str(Path(run_dir) / f"{stream}-quicklook.log")))
        if opts.dump:
            dump = [str(BUILD / "lft3-dump"), "--stream", stream, "--prefix", prefix,
                    "--beam-offset", str(lk.beam_offset), "--output-dir", dump_dir,
                    "--max-seconds", f"{opts.dump_max_s:g}", "--nco-mhz", f"{board.nco_mhz:g}",
                    "--source-name", board.name, "--obs-id", obs_id]
            procs.append(Process("dump", stream, f"lft3-{stream}-dump", dump, cpuset=lk.reader_cpus,
                                 mounts=mounts, log=str(Path(run_dir) / f"{stream}-dump.log")))
    return RunPlan(board.name, run_dir, obs_id, chash, procs)
