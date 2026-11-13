"""lft3-pipeline: run the real-time pipeline (receiver → fine spectrometer → FBH5) for one board.

  lft3-pipeline run [-b rfsoc0] [--source network|synthetic|file] [--file cap.pcap] [--loop] [--fast]
                    [--fft-length 128] [--integration 1.0] [--channels START:COUNT]
                    [--monitor [--remote]] [--duration SECONDS] [--no-record] [--dry-run]
  lft3-pipeline flowgraph ... -o flowgraph.yml          only write the flowgraph

Each run gets a directory <data_dir>/runs/<UTC>_<board>_<source>/ with the flowgraph and a log;
spectra go to <data_dir>/spectra/<board>/. Read them with lft3-plot.

Sources: network = the live stream (the container runs privileged as root for DAQIRI; membership of the
docker group is enough, no sudo), synthetic = generated noise + tone, file = replay a tcpdump capture
(`--fast` replays as fast as possible instead of in real time).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import subprocess
import sys
import threading
from pathlib import Path

from lft3.config import ConfigError, load_station_config
from lft3.monitoring.nic import NicLogger, netdev_for_pci
from lft3.pipeline.flowgraph import SOURCES, PipelineOptions, build_flowgraph

REPO = Path(__file__).resolve().parents[3]
RUN_SH = REPO / "pipeline" / "scripts" / "run.sh"
STOP_TIMEOUT_S = 60  # docker stop: SIGINT, then SIGKILL after run.sh's --stop-timeout


def _container_running(name: str) -> bool:
    out = subprocess.run(["docker", "ps", "-q", "--filter", f"name=^{name}$"],
                         capture_output=True, text=True).stdout
    return bool(out.strip())


def _stop_container(name: str, reason: str) -> None:
    """Stop the pipeline container (SIGINT → graceful shutdown, files closed). Signalling the docker client
    is not enough: it does not reliably reach CyberEther, and killing the client leaves the container running."""
    if _container_running(name):
        print(f"stopping container {name} ({reason})", flush=True)
        subprocess.run(["docker", "stop", "-t", str(STOP_TIMEOUT_S), name], capture_output=True)


def _options(a) -> PipelineOptions:
    start, count = 0, 6144
    if a.channels:
        s, _, c = a.channels.partition(":")
        start, count = int(s), int(c)
    return PipelineOptions(
        source=a.source, file=str(Path(a.file).resolve()) if a.file else None, loop=a.loop, real_time=not a.fast,
        fft_length=a.fft_length, integration_s=a.integration, channel_start=start, channel_count=count,
        monitor=a.monitor, record=not a.no_record,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="lft3-pipeline", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-s", "--station", default="station")
    ap.add_argument("-b", "--board", default=None, help="board (default: the station's first board)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "flowgraph"):
        p = sub.add_parser(name)
        p.add_argument("--source", choices=SOURCES, default="network")
        p.add_argument("--file", help="source=file: capture to replay (classic pcap)")
        p.add_argument("--loop", action="store_true", help="source=file: replay forever")
        p.add_argument("--fast", action="store_true", help="synthetic/file: as fast as possible, not real time")
        p.add_argument("--fft-length", type=int, default=128, help="fine FFT length (128 = 1.19 kHz channels)")
        p.add_argument("--integration", type=float, default=1.0, help="seconds per spectrum")
        p.add_argument("--channels", help="coarse channel range START:COUNT (multiples of 1024)")
        p.add_argument("--monitor", action="store_true", help="add the live lineplot/waterfall branch")
        p.add_argument("--no-record", action="store_true", help="do not write FBH5 files")
        if name == "run":
            p.add_argument("--remote", action="store_true", help="stream the UI to a browser (port 8686)")
            p.add_argument("--duration", type=float, help="stop after this many seconds")
            p.add_argument("--dry-run", action="store_true", help="print the flowgraph and command only")
        else:
            p.add_argument("-o", "--output", required=True)
    a = ap.parse_args(argv)

    try:
        st = load_station_config(a.station)
        board = st.board(a.board) if a.board else st.boards[0]
        opts = _options(a)
        stamp = _dt.datetime.utcnow().strftime("%Y%m%dT%H%M%S")
        spectra_dir = Path(st.data_dir) / "spectra" / board.name
        text = build_flowgraph(board, opts, str(spectra_dir))
    except (ConfigError, ValueError) as exc:
        print(f"lft3-pipeline: {exc}", file=sys.stderr)
        return 1

    if a.cmd == "flowgraph":
        Path(a.output).write_text(text)
        print(a.output)
        return 0

    run_dir = Path(st.data_dir) / "runs" / f"{stamp}_{board.name}_{opts.source}"
    cmd = [str(RUN_SH)]
    if a.remote:
        cmd.append("--remote")
    if opts.file:
        cmd += ["--mount", str(Path(opts.file).parent)]
    cmd.append(str(run_dir / "flowgraph.yml"))
    print(f"{board.name}: {opts.source} source, {opts.fft_length}-point FFT, {opts.tsamp:.4f} s spectra"
          f"{', monitor' if opts.monitor else ''} -> {spectra_dir if opts.record else 'not recorded'}")
    if a.dry_run:
        print(text)
        print(" ".join(cmd))
        return 0
    run_dir.mkdir(parents=True, exist_ok=True)
    spectra_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "flowgraph.yml").write_text(text)
    print(f"run directory {run_dir}  (log: {run_dir / 'pipeline.log'})")
    container = os.environ.get("LFT3_CONTAINER", "lft3-pipeline")
    env = dict(os.environ, DATA_DIR=st.data_dir, LFT3_CONTAINER=container)
    timer = None
    nic = None
    if opts.source == "network":
        netdev = netdev_for_pci(board.receiver_interface)
        if netdev:
            print(f"NIC counters of {netdev} -> {run_dir / 'nic.csv'} (drops are reported here)")
            nic = NicLogger(netdev, run_dir / "nic.csv", alert=lambda m: print(m, flush=True))
            nic.start()
    with open(run_dir / "pipeline.log", "w") as log:
        # stdin=DEVNULL: the container must never read the terminal. With `lft3-pipeline run ... &` a read
        # from the tty sends SIGTTIN to the whole job and freezes this wrapper (log, NIC logger, --duration).
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, env=env)
        if a.duration:
            timer = threading.Timer(a.duration, _stop_container, (container, f"--duration {a.duration:g} s reached"))
            timer.daemon = True
            timer.start()
        try:
            for line in proc.stdout:
                log.write(_dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z " + line)
                log.flush()
                if any(k in line for k in ("LFT3_", "ERROR", "Error", "Join URL", "FATAL", "Exception", "missing:",
                                           "CYBERETHER")):
                    sys.stdout.write(line)
                    sys.stdout.flush()
        except KeyboardInterrupt:
            _stop_container(container, "interrupted")
        finally:
            if timer is not None:
                timer.cancel()
            if nic is not None:
                nic.stop()
            if proc.poll() is None:
                try:
                    proc.wait(timeout=STOP_TIMEOUT_S + 15)
                except subprocess.TimeoutExpired:
                    proc.kill()
            if _container_running(container):  # never leave a receiver behind on the NIC
                _stop_container(container, "client exited")
        return proc.wait()


if __name__ == "__main__":
    raise SystemExit(main())
