"""lft3-board: monitor and control the station's RFSoC 4x2 boards.

Boards: every board in config/station.yaml, or the ones given with -b (repeatable; a name from
config/boards/ or a path). Operations run on all selected boards in parallel.

Read-only:
  lft3-board list                                       boards in the station
  lft3-board status  [--arp] [--sensors] [--json]       health of every board (-v: every check)
  lft3-board monitor [--interval 5] [--prometheus-port 9101] [--jsonl FILE]

Data from the boards (saved under <data_dir>/captures/ as HDF5; --plot also writes a PNG):
  lft3-board snapshot fft [--plot]                      full 8192-bin PFB power spectrum per input
  lft3-board snapshot adc [--samples 8192] [--plot]     16-bit complex ADC samples per input
  lft3-board capture [--ddr-size 0xFFFFF] [--plot]      trigger the DDR ring and receive the 4-bit dump
                                                        (default size = the configured ring, 4 GiB)

Changes the boards (per-board locks):
  lft3-board start [--no-program] [--trigger]           initialise all boards in parallel, then sync together
  lft3-board set-dest-mac [--mac MAC] [--show]          100GbE destination (default from each board's config)
  lft3-board sync | trigger                             sync pulse on all boards / manual DDR dump + re-arm
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

from lft3.board.control import BoardError
from lft3.board.station import Station
from lft3.board.telemetry import read_snapshot, worst
from lft3.config import ConfigError, load_station_config


def _station(a) -> Station:
    return Station(load_station_config(a.station, a.board or None))


def _sensors(board, wanted: bool):
    if not (wanted or board.cfg.sensors_enabled):
        return None
    from lft3.board.sensors import read_sensors
    try:
        return read_sensors(board.cfg.host, board.cfg.ssh_user)
    except Exception as exc:
        logging.warning("%s: sensors unavailable (%s): needs an SSH key for %s@%s",
                        board.cfg.name, exc, board.cfg.ssh_user, board.cfg.host)
        return None


def cmd_status(st: Station, a) -> int:
    from lft3.monitoring.metrics import to_json
    results = st.status(a.interval, a.arp)
    any_fail = False
    for name, res in results.items():
        board = st.boards[name]
        if res is None:
            print(f"{name} ({board.cfg.host}): UNREACHABLE")
            any_fail = True
            continue
        snap, checks = res
        sensors = _sensors(board, a.sensors)
        if a.json:
            print(to_json(name, snap, {}, checks, sensors))
            continue
        level = worst(checks)
        any_fail |= level == "FAIL"
        print(f"{name} ({board.cfg.host}): {level}")
        for c in checks:
            if a.verbose or c.status != "OK":
                print(f"  {c.status:7s} {c.name:22s} {c.detail}")
        ok = sum(c.status == "OK" for c in checks)
        print(f"  {ok}/{len(checks)} checks OK" + ("" if a.verbose else " (-v for all)"))
        if sensors is not None:
            for rail in sensors.rails:
                print(f"    {rail.name:20s} {rail.volts:6.3f} V {rail.amps:6.2f} A ({100 * rail.utilisation:.0f}%)")
            for k, t in sorted(sensors.temperatures_c.items()):
                print(f"    {k:30s} {t:6.1f} C")
    return 2 if any_fail else 0


def cmd_monitor(st: Station, a) -> int:
    from lft3.board.telemetry import health, rates
    from lft3.monitoring.metrics import MetricsServer, to_json, to_prometheus
    server = MetricsServer(a.prometheus_port) if a.prometheus_port else None
    if server:
        logging.info("serving metrics on http://0.0.0.0:%d/metrics", server.port)
    jsonl = open(a.jsonl, "a") if a.jsonl else None
    prev = {n: r.value for n, r in st.each(lambda b: read_snapshot(b.fpga)).items() if r.ok}
    try:
        while True:
            time.sleep(a.interval)
            cur = {n: r.value for n, r in st.each(lambda b: read_snapshot(b.fpga)).items() if r.ok}
            text, summary = [], []
            for name, snap in cur.items():
                board = st.boards[name]
                checks = health(board.cfg, snap, prev.get(name))
                r = rates(prev[name], snap) if name in prev else {}
                sensors = _sensors(board, a.sensors)
                text.append(to_prometheus(name, snap, r, checks, sensors))
                if jsonl:
                    jsonl.write(to_json(name, snap, r, checks, sensors) + "\n")
                bad = [c.name for c in checks if c.status in ("WARN", "FAIL")]
                summary.append(f"{name} {worst(checks)}" + (f" ({', '.join(bad)})" if bad else ""))
            for name in set(st.boards) - set(cur):
                summary.append(f"{name} UNREACHABLE")
            if server:
                server.update("".join(text))
            if jsonl:
                jsonl.flush()
            logging.info(" | ".join(summary))
            prev = cur
    except KeyboardInterrupt:
        return 0
    finally:
        if server:
            server.close()
        if jsonl:
            jsonl.close()


def _save_captures(st: Station, caps, plot: bool) -> None:
    from lft3.products import captures as store
    for cap in caps:
        path = store.save(cap, store.default_path(st.cfg.data_dir, cap))
        line = f"{cap.board}: {cap.kind} {cap.data.shape} -> {path}"
        if plot:
            from lft3.products import plot as plots
            line += f"  (plot {plots.capture(cap, path.with_suffix('.png'))})"
        print(line)


def cmd_snapshot(st: Station, a) -> int:
    from lft3.board import capture
    fn = capture.fft_snapshot if a.kind == "fft" else (lambda b: capture.adc_snapshot(b, a.samples))
    results = st.each(fn)
    _save_captures(st, [r.value for r in results.values() if r.ok], a.plot)
    for name, r in results.items():
        if not r.ok:
            print(f"{name}: FAILED {r.error}", file=sys.stderr)
    return 0 if all(r.ok for r in results.values()) else 1


def cmd_capture(st: Station, a) -> int:
    from lft3.board import capture
    with st.lock():
        caps = capture.capture_dump(list(st.boards.values()), ddr_size=a.ddr_size, timeout=a.timeout)
    _save_captures(st, caps, a.plot)
    for c in caps:
        if c.attrs["packets_received"] < c.attrs["packets_expected"]:
            print(f"{c.board}: received {c.attrs['packets_received']}/{c.attrs['packets_expected']} packets "
                  "(missing samples are zero)", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="lft3-board", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("-s", "--station", default="station", help="station config name or path")
    ap.add_argument("-b", "--board", action="append", help="only these boards (repeatable)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="boards in the station")
    p = sub.add_parser("status", help="health of every board (read-only)")
    p.add_argument("--interval", type=float, default=1.0, help="seconds between the two snapshots (rates)")
    p.add_argument("--arp", action="store_true", help="also read the 100GbE destination MAC")
    p.add_argument("--sensors", action="store_true", help="also read rails/temperatures over SSH")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("monitor", help="periodic telemetry (read-only)")
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--prometheus-port", type=int, default=0, help="serve /metrics on this port (0 = off)")
    p.add_argument("--jsonl", help="append one JSON line per board per interval to this file")
    p.add_argument("--sensors", action="store_true")
    p = sub.add_parser("snapshot", help="FFT or ADC snapshot from every board")
    p.add_argument("kind", choices=["fft", "adc"])
    p.add_argument("--samples", type=int, default=8192, help="adc: samples per input (max 8192)")
    p.add_argument("--plot", action="store_true", help="also write a PNG next to each file")
    p = sub.add_parser("capture", help="trigger and receive the DDR (ADC) dump of every board")
    p.add_argument("--ddr-size", type=lambda v: int(v, 0), default=None,
                   help="temporary ring size: last 512-bit word address, e.g. 0xFFFFF = 64 MiB (default: config)")
    p.add_argument("--timeout", type=float, default=15.0)
    p.add_argument("--plot", action="store_true")
    p = sub.add_parser("start", help="initialise all boards, then sync them together")
    p.add_argument("--no-program", action="store_true", help="reconfigure the running design, don't reprogram")
    p.add_argument("--trigger", action="store_true", help="fire a manual DDR dump at the end")
    p = sub.add_parser("set-dest-mac", help="show or set the 100GbE destination MAC")
    p.add_argument("--mac", help="MAC to set on every selected board (default: each board's config)")
    p.add_argument("--show", action="store_true", help="only show the current entries")
    sub.add_parser("sync", help="sync pulse on all boards")
    sub.add_parser("trigger", help="manual DDR dump + re-arm on all boards")

    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for noisy in ("casperfpga", "katcp", "tornado", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger().handlers[0].addFilter(
        lambda rec: not rec.getMessage().startswith(("Client connect loop", "Stopping ioloop", "Managed tornado")))
    try:
        st = _station(a)
        if a.cmd == "list":
            for b in st.cfg.boards:
                print(f"{b.name:10s} fid {b.board_id}  {b.host:15s} -> {b.destination_ip} {b.destination_mac}  "
                      f"NCO {b.nco_mhz} MHz")
            return 0
        if a.cmd == "status":
            return cmd_status(st, a)
        if a.cmd == "monitor":
            return cmd_monitor(st, a)
        if a.cmd == "snapshot":
            return cmd_snapshot(st, a)
        if a.cmd == "capture":
            return cmd_capture(st, a)
        if a.cmd == "start":
            for name, r in st.start(program=not a.no_program, trigger=a.trigger).items():
                print(f"{name}: initialised in {r.seconds:.1f} s")
            return 0
        if a.cmd == "set-dest-mac":
            if a.show:
                for name, r in st.each(lambda b: (b.describe_design(), b.read_destination_mac())[1]).items():
                    print(f"{name}: {r.value if r.ok else 'FAILED ' + str(r.error)}")
                return 0
            if a.mac:
                with st.lock():
                    res = st.each(lambda b: (b.describe_design(), b.set_destination_mac(a.mac))[1],
                                  raise_on_error=True)
            else:
                res = st.set_destination_macs()
            for name, r in res.items():
                print(f"{name}: {r.value}")
            return 0
        if a.cmd == "sync":
            with st.lock():
                st.sync()
            return 0
        if a.cmd == "trigger":
            t0 = time.time()
            res = st.trigger()
            _log_trigger(st.cfg.data_dir, t0, res)
            return 0
    except (ConfigError, BoardError) as exc:
        print(f"lft3-board: {exc}", file=sys.stderr)
        return 1
    return 1


def _log_trigger(data_dir: str, t0: float, res) -> None:
    """Append the trigger time to <data_dir>/triggers.log so loss in a pipeline run can be matched to dumps."""
    import datetime as _dt
    from pathlib import Path
    stamp = _dt.datetime.fromtimestamp(t0, _dt.timezone.utc).isoformat(timespec="milliseconds")
    boards = " ".join(f"{n}={'ok' if r.ok else 'FAILED'}" for n, r in res.items())
    print(f"{stamp} trigger {boards}")
    try:
        with open(Path(data_dir) / "triggers.log", "a") as f:
            f.write(f"{stamp} trigger {boards}\n")
    except OSError as exc:
        print(f"lft3-board: could not write triggers.log: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
