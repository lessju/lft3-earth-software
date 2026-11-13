"""lft3-pipeline: run and watch the real-time runtime (docs/runtime_guide.md, docs/operations.md).

  lft3-pipeline run [-b rfsoc0] [--links link0,link1] [--source network|synthetic|file] [--file cap.pcap]
                    [--loop] [--fast] [--lossless] [--fft-length 128] [--integration 1.0]
                    [--channels START:COUNT] [--quicklook SECONDS] [--no-coarse] [--no-dump] [--no-record]
                    [--ring-seconds 2] [--dump-seconds 1] [--duration S] [--metrics-port 9110] [--dry-run]
  lft3-pipeline status [--json]                        every ring on this host: rates, loss, readers
  lft3-pipeline trigger [-b rfsoc0] [--pre 0.5] [--post 0.5] [--label TEXT]   raw-voltage dump (GUPPI RAW)
  lft3-pipeline stop [-b rfsoc0]                       stop a run's containers (readers first, capture last)

Per link: lft3-capture -> ring <board>-<link> -> lft3-spectrometer (fine FBH5 in <data_dir>/spectra/<board>/,
derived coarse FBH5 in .../quicklook/, latest coarse spectrum in <run_dir>/latest/) and lft3-dump
(<data_dir>/dumps/<board>/); --quicklook S adds a coarse reader at a faster cadence (<run_dir>/latest_fast/).
Each run gets <data_dir>/runs/<UTC>_<board>_<source>/ with one log per process, plan.txt (the exact
commands), nic-<link>.csv (network source) and status.jsonl.

Network sources run the capture container privileged (DAQIRI raw ibverbs): docker-group membership is enough.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path

from lft3.config import ConfigError, load_station_config
from lft3.monitoring.metrics import MetricsServer
from lft3.monitoring.nic import NicLogger, netdev_for_pci
from lft3.pipeline.runtime import BUILD, SOURCES, Process, RunOptions, plan_run
from lft3.ring import Ring, list_streams, send_dump_command, to_prometheus

STOP_TIMEOUT_S = 40
ECHO = ("WARN", "ERROR", "writing ", "stopped", "ready:", "trigger ", "dump ", "capture stopped", "restart")


def _running_containers(prefix: str) -> list[str]:
    out = subprocess.run(["docker", "ps", "--format", "{{.Names}}", "--filter", f"name=^{prefix}"],
                         capture_output=True, text=True).stdout
    return [n for n in out.split() if n.startswith(prefix)]


def _stop(names: list[str], reason: str) -> None:
    if not names:
        return
    print(f"stopping {', '.join(names)} ({reason})", flush=True)
    subprocess.run(["docker", "stop", "-t", str(STOP_TIMEOUT_S), *names], capture_output=True)


def _stop_board(board: str, reason: str) -> None:
    names = _running_containers(f"lft3-{board}-")
    readers = [n for n in names if not n.endswith("-capture")]
    _stop(readers, reason)          # readers first: they flush their files while the ring still exists
    _stop([n for n in names if n.endswith("-capture")], reason)


def _summary(st) -> str:
    s = st.stats
    rd = " ".join(f"{r.name}(lag {r.lag_blocks}, over {r.overruns}, out {r.outputs})" for r in st.readers)
    return (f"{st.stream}: {st.state} {st.rate_gbps:6.2f} Gb/s | blocks {st.head} (partial {s['blocks_partial']}, "
            f"skipped {s['blocks_skipped']}) | missing packets {s['packets_missing']} late {s['packets_late']} | {rd}")


def _status_json(st) -> dict:
    d = asdict(st)
    d["time"] = time.time()
    return d


def _pump(proc: subprocess.Popen, log_path: str, role: str) -> None:
    with open(log_path, "a") as log:
        for line in proc.stdout:
            log.write(line)
            log.flush()
            if any(k in line for k in ECHO):
                sys.stdout.write(f"[{role}] {line}")
                sys.stdout.flush()


def _wait_for_ring(stream: str, timeout: float, proc: subprocess.Popen) -> bool:
    t_end = time.time() + timeout
    while time.time() < t_end:
        if proc.poll() is not None:
            return False
        if stream in list_streams():
            return True
        time.sleep(0.2)
    return False


def cmd_run(a, st, board) -> int:
    start, count = 0, 6144
    if a.channels:
        s, _, c = a.channels.partition(":")
        start, count = int(s), int(c)
    opts = RunOptions(source=a.source, file=a.file, loop=a.loop, fast=a.fast, lossless=a.lossless,
                      links=a.links.split(",") if a.links else None, fft_length=a.fft_length,
                      integration_s=a.integration, channel_start=start, channel_count=count,
                      record=not a.no_record, coarse=not a.no_coarse, quicklook_s=a.quicklook or 0.0,
                      dump=not a.no_dump,
                      ring_seconds=a.ring_seconds, dump_max_s=a.dump_seconds, obs_id=a.obs_id or "")
    stamp = _dt.datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    run_dir = Path(st.data_dir) / "runs" / f"{stamp}_{board.name}_{opts.source}"
    try:
        plan = plan_run(board, opts, st.data_dir, str(run_dir), stamp)
    except (ConfigError, ValueError) as exc:
        print(f"lft3-pipeline: {exc}", file=sys.stderr)
        return 1
    text = "\n\n".join(" ".join(p.docker_argv()) for p in plan.processes)
    print(f"{board.name}: {opts.source} source, links {', '.join(plan.streams())}, {opts.fft_length}-point FFT, "
          f"{opts.tsamp:.4f} s spectra, obs {plan.obs_id}")
    if a.dry_run:
        print(text)
        return 0
    if not (BUILD / "lft3-capture").exists():
        print(f"lft3-pipeline: {BUILD}/lft3-capture missing: run `make pipeline` first", file=sys.stderr)
        return 1
    running = _running_containers(f"lft3-{board.name}-")
    if running:
        print(f"lft3-pipeline: already running: {', '.join(running)} (lft3-pipeline stop -b {board.name})",
              file=sys.stderr)
        return 1
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "plan.txt").write_text(text + "\n")
    for sub in ("spectra", "dumps"):
        (Path(st.data_dir) / sub / board.name / ("quicklook" if sub == "spectra" else "")).mkdir(parents=True,
                                                                                               exist_ok=True)
    print(f"run directory {run_dir}")

    nics = []
    if opts.source == "network":
        for lk in board.links:
            if opts.links and lk.name not in opts.links:
                continue
            netdev = netdev_for_pci(lk.interface)
            if netdev:
                nic = NicLogger(netdev, run_dir / f"nic-{lk.name}.csv", alert=lambda m: print(m, flush=True))
                nic.start()
                nics.append(nic)
    metrics = MetricsServer(a.metrics_port) if a.metrics_port else None

    procs: list[tuple[Process, subprocess.Popen]] = []
    stopping = threading.Event()

    def launch(p: Process) -> subprocess.Popen:
        # stdin=DEVNULL: never read the terminal (a background `lft3-pipeline run &` would get SIGTTIN).
        proc = subprocess.Popen(p.docker_argv(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        threading.Thread(target=_pump, args=(proc, p.log, f"{p.stream}:{p.role}"), daemon=True).start()
        procs.append((p, proc))
        return proc

    rc = 0
    try:
        for p in plan.processes:          # capture first, then its readers once the ring exists
            proc = launch(p)
            if p.role == "capture" and not _wait_for_ring(p.stream, 60, proc):
                print(f"lft3-pipeline: {p.stream}: the capture did not create its ring (see {p.log})", file=sys.stderr)
                rc = 1
                break
        t_end = time.time() + a.duration if a.duration else None
        last = 0.0
        with open(run_dir / "status.jsonl", "a") as status_log:
            while rc == 0 and not stopping.is_set():
                time.sleep(1)
                if t_end and time.time() >= t_end:
                    print(f"--duration {a.duration:g} s reached", flush=True)
                    break
                caps = [proc for p, proc in procs if p.role == "capture"]
                if all(proc.poll() is not None for proc in caps):
                    print("all captures ended", flush=True)
                    break
                dead = [p for p, proc in procs if p.role != "capture" and proc.poll() not in (None, 0)]
                if dead:
                    print(f"lft3-pipeline: {', '.join(p.container for p in dead)} failed (see logs)", flush=True)
                    rc = 1
                    break
                statuses = []
                for s in plan.streams():
                    try:
                        with Ring(s) as r:
                            statuses.append(r.status())
                    except (OSError, ValueError):
                        pass
                if metrics:
                    metrics.update(to_prometheus(statuses))
                if time.time() - last >= a.status_interval:
                    last = time.time()
                    for s in statuses:
                        print(_summary(s), flush=True)
                        status_log.write(json.dumps(_status_json(s)) + "\n")
                    status_log.flush()
    except KeyboardInterrupt:
        print("interrupted", flush=True)
    finally:
        _stop_board(board.name, "run ending")
        for _p, proc in procs:
            try:
                proc.wait(timeout=STOP_TIMEOUT_S + 15)
            except subprocess.TimeoutExpired:
                proc.kill()
        for nic in nics:
            nic.stop()
        if metrics:
            metrics.close()
    for p, proc in procs:
        if proc.returncode not in (0, None, 130, 143) and rc == 0:
            print(f"{p.container} exited with {proc.returncode} (see {p.log})")
            rc = 1
    return rc


def cmd_status(a) -> int:
    statuses = []
    for s in list_streams():
        try:
            with Ring(s) as r:
                statuses.append(r.status())
        except (OSError, ValueError) as exc:
            print(f"{s}: {exc}", file=sys.stderr)
    if a.json:
        print(json.dumps([_status_json(s) for s in statuses], indent=1))
    elif not statuses:
        print("no rings on this host (nothing running)")
    for s in statuses if not a.json else []:
        print(_summary(s))
        print(f"    obs {s.obs_id}, {s.channel_count} channels from {s.channel_start}, ring {s.slots} blocks "
              f"({s.ring_seconds:.2f} s, {s.ring_bytes / 1e9:.1f} GB), "
              f"capture heartbeat {s.writer_heartbeat_age_s:.1f} s ago")
    return 0


def cmd_trigger(a, st, board) -> int:
    streams = [s for s in list_streams() if s.startswith(board.name + "-")]
    if not streams:
        print(f"lft3-pipeline: no running ring for {board.name}", file=sys.stderr)
        return 1
    now = time.time()
    label = a.label or "manual"
    rc = 0
    for s in streams:
        try:
            reply = send_dump_command(s, f"trigger {now:.6f} {a.pre:g} {a.post:g} {label}")
        except OSError as exc:
            reply = f"error {exc} (is lft3-dump running?)"
        print(f"{s}: {reply}")
        rc |= 0 if reply.startswith("ok") else 1
    stamp = _dt.datetime.fromtimestamp(now, _dt.timezone.utc).isoformat(timespec="milliseconds")
    with open(Path(st.data_dir) / "triggers.log", "a") as f:
        f.write(f"{stamp} dump {board.name} pre={a.pre:g} post={a.post:g} {label}\n")
    return rc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="lft3-pipeline", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-s", "--station", default="station")
    ap.add_argument("-b", "--board", default=None, help="board (default: the station's first board)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--source", choices=SOURCES, default="network")
    p.add_argument("--links", help="comma-separated link names (default: all links of the board)")
    p.add_argument("--file", help="source=file: capture to replay (classic pcap)")
    p.add_argument("--loop", action="store_true", help="source=file: replay forever")
    p.add_argument("--fast", action="store_true", help="synthetic/file: as fast as possible, not real time")
    p.add_argument("--lossless", action="store_true", help="synthetic/file: capture waits for the spectrometers")
    p.add_argument("--fft-length", type=int, default=128, help="fine FFT length (128 = 1.19 kHz channels)")
    p.add_argument("--integration", type=float, default=1.0, help="seconds per spectrum")
    p.add_argument("--channels", help="coarse channel range START:COUNT (multiples of 1024)")
    p.add_argument("--quicklook", type=float, help="extra coarse reader at this cadence (s), e.g. 0.1")
    p.add_argument("--no-coarse", action="store_true", help="no derived coarse product / latest snapshot")
    p.add_argument("--no-dump", action="store_true")
    p.add_argument("--no-record", action="store_true", help="do not write FBH5 files")
    p.add_argument("--ring-seconds", type=float, default=2.0)
    p.add_argument("--dump-seconds", type=float, default=1.0, help="largest raw-voltage dump window")
    p.add_argument("--obs-id")
    p.add_argument("--duration", type=float, help="stop after this many seconds")
    p.add_argument("--metrics-port", type=int, help="serve Prometheus metrics on this port")
    p.add_argument("--status-interval", type=float, default=10.0)
    p.add_argument("--dry-run", action="store_true", help="print the commands only")
    p = sub.add_parser("status")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("trigger")
    p.add_argument("--pre", type=float, default=0.5)
    p.add_argument("--post", type=float, default=0.5)
    p.add_argument("--label")
    sub.add_parser("stop")
    a = ap.parse_args(argv)

    if a.cmd == "status":
        return cmd_status(a)
    try:
        st = load_station_config(a.station)
        board = st.board(a.board) if a.board else st.boards[0]
    except (ConfigError, ValueError) as exc:
        print(f"lft3-pipeline: {exc}", file=sys.stderr)
        return 1
    if a.cmd == "run":
        return cmd_run(a, st, board)
    if a.cmd == "trigger":
        return cmd_trigger(a, st, board)
    _stop_board(board.name, "lft3-pipeline stop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
