"""Narrowband drift search with hyperseti on the runtime's fine FBH5 spectra (``lft3-search``).

    lft3-search FILE.h5 [FILE.h5 ...]                  search files, write hits to <dir of file>/hits/
    lft3-search --watch [-b rfsoc0] [--once]           search every finished file in <data_dir>/spectra/<board>/
    lft3-search --live --stream rfsoc0-link0           live search on the spectra ring (lft3.search.live)

Runs in the search environment (``scripts/setup_search_env.sh``: Python >= 3.10, CuPy for CUDA 13, hyperseti),
not in the casperfpga venv. The module itself is Python 3.8 syntax and imports hyperseti lazily.

Settings (window, drift range, S/N, flattening, ...) come from the observation config
(config/observations/<name>.yaml, ``search:``; the station's by default, ``--observation`` for another).
Files are searched in the same windows as the live search (lft3.search.core): window k = integrations
[k * step, k * step + N) of the stream, only windows that lie wholly in one file. ``--whole-file`` searches each
file in one piece instead (hyperseti find_et, the behaviour before the live search).

A file is *finished* when a newer file of the same beam exists (the spectrometer rotated) or it has not been
modified for ``--idle`` seconds (the run ended). Files being written are never opened: HDF5 without SWMR is not
safe to read concurrently. Each searched file gets ``<data_dir>/hits/<file stem>.csv``; ``hits/searched.txt``
records what is done, so ``--watch`` resumes.

Notes for 1.19 kHz channels and 1 s spectra (tested on live data 2026-11-13):
* no spectral-kurtosis flagging: hyperseti derives the accumulation count from tsamp x foff, which does not
  account for the Stokes-I sum of two polarisations, and then flags 100 % of the data;
* a drift is only a trial if the signal crosses at least one channel within the window (hyperseti stops with
  "No steps!" otherwise): max_drift_hz_s x window length must exceed one channel width.
Hits within 2 fine channels of a known board spur (every 125 MHz) or in the Spark's Wi-Fi channel 1 are labelled
``rfi`` (not removed).
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
from pathlib import Path

from lft3.search.core import HIT_COLUMNS, Searcher, Window, hit_rows, rfi_label, searcher_for, window_ends_at  # noqa: F401

SPECTRUM_RATE = 152587.890625


def finished_files(spectra_dir: Path, idle_s: float) -> list[Path]:
    """Fine FBH5 files that are no longer being written."""
    files = sorted(p for p in spectra_dir.glob("*_beam*.h5"))
    by_beam: dict[str, list[Path]] = {}
    for p in files:
        by_beam.setdefault(p.stem.rsplit("_beam", 1)[1], []).append(p)
    now = time.time()
    done = []
    for beam_files in by_beam.values():
        beam_files.sort()
        for i, p in enumerate(beam_files):
            newer_exists = i + 1 < len(beam_files)
            if newer_exists or now - p.stat().st_mtime > idle_s:
                done.append(p)
    return sorted(done)


def file_windows(h, n: int, step: int):
    """(window index, first row) of every window of the grid that lies wholly in an open FBH5 file."""
    spi = int(h.attrs["lft3_spectra_per_integration"])
    ts = h["lft3_timestamp"][:]
    integ = [int(t) // spi for t in ts]
    out = []
    for row in range(len(integ)):
        k = window_ends_at(integ[row], n, step)
        first = row - n + 1
        if k is None or first < 0 or integ[row] - integ[first] != n - 1:
            continue
        out.append((k, first))
    return out


def search_file(path: Path, out_dir: Path, settings, searcher=None, whole_file: bool = False) -> int:
    """Search one FBH5 file (one beam) in the observation's windows; returns the number of hits written to
    <out_dir>/<stem>.csv."""
    import h5py

    with h5py.File(path, "r") as h:
        d = h["data"]
        n_time, foff, fch1 = d.shape[0], float(d.attrs["foff"]), float(d.attrs["fch1"])
        attrs = {k: (v.decode() if isinstance(v, bytes) else v) for k, v in h.attrs.items()}
    m = re.search(r"_beam(\d+)\.h5$", path.name)
    beam = int(m.group(1)) if m else 0
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{path.stem}.csv"
    if whole_file:
        return _search_whole_file(path, out, settings, n_time, foff, attrs, beam)
    if searcher is None:
        searcher = searcher_for(settings)
    import cupy as cp

    t0 = time.time()
    rows_written = n_windows = skipped = 0
    with h5py.File(path, "r") as h, open(out, "w", newline="") as fo:
        w_csv = csv.writer(fo)
        w_csv.writerow(HIT_COLUMNS)
        d = h["data"]
        fine_per_coarse = int(d.attrs.get("nfpc", attrs.get("lft3_fft_length", 1)))
        spi = int(attrs["lft3_spectra_per_integration"])
        packets_per_integration = spi * (d.shape[2] // fine_per_coarse // 1024)
        unix_t0 = float(attrs.get("lft3_unix_time_at_t0", 0.0))
        tsamp = float(d.attrs["tsamp"])
        ts = h["lft3_timestamp"][:]
        missing = h["lft3_missing_packets"][:] if "lft3_missing_packets" in h else None
        n, step = settings.window_spectra, settings.window_step_spectra
        for k, first in file_windows(h, n, step):
            miss = float(missing[first:first + n].sum()) / (n * packets_per_integration) if missing is not None else 0.0
            if miss > settings.max_missing_fraction:
                skipped += 1
                continue
            w = Window(k, int(ts[first]) // spi, fch1, foff, tsamp, unix_t0 + float(ts[first]) / SPECTRUM_RATE,
                       fine_per_coarse, miss)
            data = cp.asarray(d[first:first + n, 0, :])
            hits = searcher.search_beam(data, w)
            rows = hit_rows(hits, w, beam, path.name, str(attrs.get("lft3_obs_id", "")),
                            str(attrs.get("lft3_stream", "")))
            w_csv.writerows(rows)
            rows_written += len(rows)
            n_windows += 1
    print(f"{path.name}: {n_windows} windows of {settings.window_spectra} spectra searched ({skipped} skipped for "
          f"missing data) in {time.time() - t0:.1f} s, {rows_written} hits -> {out}", flush=True)
    return rows_written


def _search_whole_file(path: Path, out: Path, settings, n_time: int, foff: float, attrs: dict, beam: int) -> int:
    from hyperseti.pipeline import find_et

    if n_time < 4:
        print(f"{path.name}: only {n_time} spectra, skipped", flush=True)
        return 0
    t0 = time.time()
    res = find_et(str(path), settings.hyperseti_config(), gulp_size=settings.gulp_channels)
    tab = res.hit_table if hasattr(res, "hit_table") else res
    if settings.drop_zero_drift and len(tab):
        tab = tab[tab["drift_rate"] != 0]
    rows = tab.sort_values("snr", ascending=False) if len(tab) else tab
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["file", "f_start", "drift_rate", "snr", "channel_idx", "beam", "boxcar_size", "label", "obs_id",
                    "stream"])
        for _, r in rows.iterrows():
            w.writerow([path.name, r["f_start"], r["drift_rate"], r["snr"], r["channel_idx"], beam,
                        r.get("boxcar_size", 1), rfi_label(float(r["f_start"]), foff), attrs.get("lft3_obs_id", ""),
                        attrs.get("lft3_stream", "")])
    print(f"{path.name}: {n_time} spectra searched whole in {time.time() - t0:.1f} s, {len(tab)} hits -> {out}",
          flush=True)
    return len(tab)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--live" in argv:
        argv.remove("--live")
        from lft3.search.live import main as live_main
        return live_main(argv)
    ap = argparse.ArgumentParser(prog="lft3-search", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", type=Path)
    ap.add_argument("--watch", action="store_true", help="search finished files in the board's spectra directory")
    ap.add_argument("--once", action="store_true", help="with --watch: one pass, then exit")
    ap.add_argument("-s", "--station", default="station")
    ap.add_argument("-b", "--board")
    ap.add_argument("--observation", help="observation config (default: the station's)")
    ap.add_argument("--out", type=Path, help="hits directory (default <data_dir>/hits/<board>)")
    ap.add_argument("--idle", type=float, default=120.0, help="seconds without writes before a file counts as done")
    ap.add_argument("--whole-file", action="store_true", help="search each file in one piece (find_et)")
    ap.add_argument("--max-drift", type=float, help="Hz/s (default: observation config)")
    ap.add_argument("--snr", type=float, help="S/N threshold (default: observation config)")
    ap.add_argument("--gulp", type=int, help="channels per GPU gulp (default: observation config)")
    a = ap.parse_args(argv)
    from lft3.config import load_observation_config, load_station_config

    st = None
    if a.observation:
        obs = load_observation_config(a.observation)
    else:
        st = load_station_config(a.station)
        obs = st.observation_config()
    settings = obs.search
    if a.max_drift is not None:
        settings.max_drift_hz_s = a.max_drift
    if a.snr is not None:
        settings.snr_threshold = a.snr
    if a.gulp is not None:
        settings.gulp_channels = a.gulp
    searcher = None if a.whole_file else searcher_for(settings)

    if a.files:
        out = a.out or a.files[0].resolve().parent / "hits"
        for f in a.files:
            search_file(f, out, settings, searcher, a.whole_file)
        return 0
    if not a.watch:
        ap.error("give files, --watch or --live")
    st = st or load_station_config(a.station)
    board = st.board(a.board) if a.board else st.boards[0]
    spectra = Path(st.data_dir) / "spectra" / board.name
    out = a.out or Path(st.data_dir) / "hits" / board.name
    out.mkdir(parents=True, exist_ok=True)
    ledger = out / "searched.txt"
    done = set(ledger.read_text().split()) if ledger.exists() else set()
    print(f"watching {spectra} -> {out}", flush=True)
    while True:
        for f in finished_files(spectra, a.idle):
            if f.name in done:
                continue
            try:
                search_file(f, out, settings, searcher, a.whole_file)
            except Exception as exc:  # noqa: BLE001 - keep watching; the ledger marks the file as tried
                print(f"{f.name}: search failed: {exc}", file=sys.stderr, flush=True)
            done.add(f.name)
            with open(ledger, "a") as fh:
                fh.write(f.name + os.linesep)
        if a.once:
            return 0
        time.sleep(30)


if __name__ == "__main__":
    raise SystemExit(main())
