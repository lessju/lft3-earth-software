"""Narrowband drift search with hyperseti on the runtime's fine FBH5 spectra (``lft3-search``).

    lft3-search FILE.h5 [FILE.h5 ...]                  search files, write hits next to <data_dir>/hits/
    lft3-search --watch [-b rfsoc0] [--once]           search every finished file in <data_dir>/spectra/<board>/

Runs in the search environment (``scripts/setup_search_env.sh``: Python >= 3.10, CuPy for CUDA 13, hyperseti),
not in the casperfpga venv. The module itself is Python 3.8 syntax and imports hyperseti lazily.

A file is *finished* when a newer file of the same beam exists (the spectrometer rotated) or it has not been
modified for ``--idle`` seconds (the run ended). Files being written are never opened: HDF5 without SWMR is not
safe to read concurrently. Each searched file gets ``<data_dir>/hits/<file stem>.csv``; ``hits/searched.txt``
records what is done, so ``--watch`` resumes.

Defaults for 1.19 kHz channels and 1 s spectra (tested on live data 2026-11-13):
* no spectral-kurtosis flagging: hyperseti derives the accumulation count from tsamp x foff, which does not
  account for the Stokes-I sum of two polarisations, and then flags 100 % of the data;
* drift up to 200 Hz/s: a signal must cross at least one 1.19 kHz channel within the file to be a drift trial
  (at 10 Hz/s it would not, and hyperseti stops with "No steps!");
* S/N threshold 20.
Hits within 2 fine channels of a known board spur (every 125 MHz) or in the Spark's Wi-Fi channel 1 are labelled
``rfi`` (not removed).
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
import warnings
from pathlib import Path

DEFAULT_CONFIG = {
    "preprocess": {"sk_flag": False, "normalize": True, "blank_edges": {"n_chan": 8},
                   "blank_extrema": {"threshold": 10000}},
    "dedoppler": {"kernel": "dedoppler", "max_dd": 200.0, "apply_smearing_corr": True, "plan": "stepped"},
    "hitsearch": {"threshold": 20},
    "pipeline": {"merge_boxcar_trials": True},
}
SPUR_SPACING_MHZ = 125.0
WIFI_CH1_MHZ = (2401.0, 2423.0)


def rfi_label(f_mhz: float, foff_mhz: float) -> str:
    nearest = round(f_mhz / SPUR_SPACING_MHZ) * SPUR_SPACING_MHZ
    if abs(f_mhz - nearest) <= 2 * abs(foff_mhz):
        return f"rfi:board-spur-{nearest:.0f}MHz"
    if WIFI_CH1_MHZ[0] <= f_mhz <= WIFI_CH1_MHZ[1]:
        return "rfi:wifi-ch1"
    return ""


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


def search_file(path: Path, out_dir: Path, config: dict, gulp: int) -> int:
    """Run hyperseti on one file; returns the number of hits written to <out_dir>/<stem>.csv."""
    warnings.filterwarnings("ignore")
    import h5py
    from hyperseti.pipeline import find_et

    with h5py.File(path, "r") as h:
        d = h["data"]
        n_time, foff = d.shape[0], float(d.attrs["foff"])
        attrs = {k: (v.decode() if isinstance(v, bytes) else v) for k, v in h.attrs.items()
                 if k in ("lft3_obs_id", "lft3_stream", "lft3_beam_offset")}
    if n_time < 4:
        print(f"{path.name}: only {n_time} spectra, skipped", flush=True)
        return 0
    t0 = time.time()
    res = find_et(str(path), config, gulp_size=gulp)
    tab = res.hit_table if hasattr(res, "hit_table") else res
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{path.stem}.csv"
    cols = [c for c in ("f_start", "drift_rate", "snr", "channel_idx", "beam_idx", "boxcar_size") if c in tab.columns]
    rows = tab.sort_values("snr", ascending=False) if len(tab) else tab
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["file", *cols, "label", "obs_id", "stream"])
        for _, r in rows.iterrows():
            w.writerow([path.name, *[r[c] for c in cols], rfi_label(float(r["f_start"]), foff),
                        attrs.get("lft3_obs_id", ""), attrs.get("lft3_stream", "")])
    n_rfi = sum(1 for _, r in rows.iterrows() if rfi_label(float(r["f_start"]), foff))
    print(f"{path.name}: {n_time} spectra searched in {time.time() - t0:.1f} s, {len(tab)} hits "
          f"({n_rfi} known RFI) -> {out}", flush=True)
    return len(tab)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="lft3-search", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", type=Path)
    ap.add_argument("--watch", action="store_true", help="search finished files in the board's spectra directory")
    ap.add_argument("--once", action="store_true", help="with --watch: one pass, then exit")
    ap.add_argument("-s", "--station", default="station")
    ap.add_argument("-b", "--board")
    ap.add_argument("--out", type=Path, help="hits directory (default <data_dir>/hits/<board>)")
    ap.add_argument("--idle", type=float, default=120.0, help="seconds without writes before a file counts as done")
    ap.add_argument("--max-drift", type=float, default=DEFAULT_CONFIG["dedoppler"]["max_dd"], help="Hz/s")
    ap.add_argument("--snr", type=float, default=DEFAULT_CONFIG["hitsearch"]["threshold"])
    ap.add_argument("--gulp", type=int, default=2 ** 17, help="channels per GPU gulp")
    a = ap.parse_args(argv)
    config = {k: dict(v) for k, v in DEFAULT_CONFIG.items()}
    config["dedoppler"]["max_dd"] = a.max_drift
    config["hitsearch"]["threshold"] = a.snr

    if a.files:
        out = a.out or a.files[0].resolve().parent / "hits"
        for f in a.files:
            search_file(f, out, config, a.gulp)
        return 0
    if not a.watch:
        ap.error("give files or --watch")
    from lft3.config import load_station_config
    st = load_station_config(a.station)
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
                search_file(f, out, config, a.gulp)
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
