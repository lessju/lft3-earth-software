#!/usr/bin/env python3
"""Cost of the whole chain against fine resolution and integration time (docs/runtime.md, "Resolution benchmark").

Part 1, end to end (``--matrix``): for each (fine channels per board channel Nf, integration time) runs
``lft3-pipeline run --source synthetic --random-noise --fast --lossless`` with a drifting tone and the live
search on (window/step/drift range from the observation config), until ``--windows`` windows were searched.
Lossless + fast means the chain runs as fast as its slowest stage, so the measured speed is the capacity of one
stream in units of real time (>= 1: keeps up). Per stage:

* capture: real-time factor of the whole chain (ring head over time, after the first search window);
* spectrometer: GPU busy time per 6.71 ms block -> capacity = 6.71 ms / busy;
* search: time per window (both beams) -> capacity = step x tsamp / time;
* writer: FBH5 bytes per second of data; GPU utilisation (nvidia-smi);
* tone: fraction of windows whose strongest beam-0 hit has the injected drift (within one drift step).

Part 2, search only (``--search-only``): the Searcher on one 2^17-channel gulp of synthetic spectra for Nf up to
2^17 (beyond the fused spectrometer's 1024), scaled to a full-band stream (2 beams x 6144 x Nf channels). The
drift range is the observation's (200 Hz/s) up to Nf 1024 and ``--hz-max-drift`` (4 Hz/s, as in docs/sizing.md)
above; integrations shorter than one FFT (Nf / 152588 s) are skipped.

Run on the host with nothing else on the GPU (the matrix starts and stops pipeline runs itself):

    python3 pipeline/bench/resolution/resolution.py --matrix --out /opt/lft3arth/data/bench/resolution
    /opt/lft3arth/search_venv/bin/python pipeline/bench/resolution/resolution.py --search-only \\
        --out /opt/lft3arth/data/bench/resolution
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "src"))
PIPELINE = "/opt/lft3arth/lft3arth_venv/bin/lft3-pipeline"
RATE = 152587.890625
BLOCK_S = 1024 / RATE


def gpu_sampler(stop: threading.Event, out: list) -> None:
    p = subprocess.Popen(["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits", "-lms", "1000"],
                         stdout=subprocess.PIPE, text=True)
    for line in p.stdout:
        if stop.is_set():
            break
        try:
            out.append(float(line.strip()))
        except ValueError:
            pass
    p.terminate()


def run_one(nf: int, tint: float, out: Path, windows: int, timeout: float, drift: float,
            no_record: bool = False) -> dict:
    from lft3.config import load_observation_config

    obs = load_observation_config("default")
    tsamp = max(1, round(tint * RATE / 1024)) * BLOCK_S
    step_hz_s = RATE / nf / (obs.search.window_spectra * tsamp)
    data = out / f"nf{nf}_t{tint:g}"
    shutil.rmtree(data, ignore_errors=True)
    data.mkdir(parents=True)
    station = data / "station.yaml"
    station.write_text(f"name: bench\nboards: [rfsoc0]\ndata_dir: {data}\nobservation: default\n")
    cmd = [PIPELINE, "-s", str(station), "run", "--source", "synthetic", "--random-noise", "--fast", "--lossless",
           "--no-dump", "--fft-length", str(nf), "--integration", f"{tint:g}", "--tone-channel", "4000",
           "--tone-frequency", "0.1", "--tone-drift", f"{drift:g}", "--tone-amplitude", "1.0",
           "--status-interval", "5", "--duration", str(int(timeout) + 60)] + (["--no-record"] if no_record else [])
    gpu, stop = [], threading.Event()
    threading.Thread(target=gpu_sampler, args=(stop, gpu), daemon=True).start()
    t0 = time.time()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            stdin=subprocess.DEVNULL)
    log = []
    threading.Thread(target=lambda: log.extend(proc.stdout), daemon=True).start()
    wpath = None
    while time.time() - t0 < timeout and proc.poll() is None:
        time.sleep(5)
        found = glob.glob(str(data / "hits" / "rfsoc0" / "live_*.windows.jsonl"))
        if found:
            wpath = found[0]
            if sum(1 for _ in open(wpath)) >= windows:
                break
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=120)
    except subprocess.TimeoutExpired:
        proc.kill()
    stop.set()
    wall = time.time() - t0
    res = {"nf": nf, "integration_s": tint, "tsamp_s": tsamp, "resolution_hz": RATE / nf,
           "drift_step_hz_s": step_hz_s, "wall_s": round(wall, 1)}
    runs = sorted((data / "runs").glob("*"))
    if not runs or not wpath:
        res["error"] = "no run / no search windows (see log)"
        (data / "bench.log").write_text("".join(log))
        return res
    run = runs[-1]
    recs = [json.loads(x) for x in open(wpath)]
    searched = [r for r in recs if r.get("searched")]
    res["windows"] = len(searched)
    st = [json.loads(x) for x in open(run / "status.jsonl")]
    volt = [s for s in st if s.get("kind", "voltages") == "voltages"]
    # steady state: skip the first quarter of the status samples (start-up, CuPy/hyperseti kernel compilation)
    steady = volt[len(volt) // 4:]
    if len(steady) >= 2:
        a, b = steady[0], steady[-1]
        res["speed_x_real_time"] = round((b["head"] - a["head"]) * BLOCK_S / (b["time"] - a["time"]), 3)
    spec_busy = [r["busy_ms"] for s in steady for r in s["readers"] if r["name"] == "spectrometer" and r["busy_ms"] > 0]
    if spec_busy:
        busy = sorted(spec_busy)[len(spec_busy) // 2]
        res["spectrometer_busy_ms"] = round(busy, 3)
        res["spectrometer_capacity_x"] = round(BLOCK_S * 1e3 / busy, 1)
    times = sorted(r["search_s"] for r in searched[1:] or searched)
    med = times[len(times) // 2]
    res["search_s_median"] = round(med, 3)
    res["search_s_max"] = round(max(times), 3)
    step_s = obs.search.window_step_spectra * tsamp
    res["search_capacity_x"] = round(step_s / med, 2)
    nbytes = sum(f.stat().st_size for f in (data / "spectra").rglob("*.h5"))
    res["fbh5_mb_per_s"] = round(nbytes / 1e6 / max(1.0, (volt[-1]["head"] * BLOCK_S if volt else 1.0)), 1)
    res["gpu_util_median"] = sorted(gpu)[len(gpu) // 2] if gpu else None
    # tone: strongest beam-0 hit per window
    import csv
    hits = list(csv.DictReader(open(wpath.replace(".windows.jsonl", ".csv"))))
    ok = 0
    best = {}
    for h in hits:
        if h["beam"] != "0":
            continue
        k = h["window_index"]
        if k not in best or float(h["snr"]) > float(best[k]["snr"]):
            best[k] = h
    for h in best.values():
        ok += abs(float(h["drift_rate_hz_s"]) - drift) <= step_hz_s * 1.01
    res["tone_windows_ok"] = f"{ok}/{len(searched)}"
    res["tone_snr_median"] = round(sorted(float(h["snr"]) for h in best.values())[len(best) // 2], 1) if best else 0
    res["hits_per_window"] = round(len(hits) / max(1, len(searched)), 1)
    (data / "bench.log").write_text("".join(log))
    for f in (data / "spectra").rglob("*.h5"):   # keep the disk free; the numbers are in results
        f.unlink()
    return res


def matrix(a) -> None:
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    results_path = out / "matrix.jsonl"
    done = set()
    if results_path.exists():
        for line in open(results_path):
            r = json.loads(line)
            done.add((r["nf"], r["integration_s"]))
    for nf in a.nf:
        for tint in a.integration:
            if (nf, tint) in done:
                continue
            print(f"--- Nf {nf}, integration {tint:g} s", flush=True)
            r = run_one(nf, tint, out, a.windows, a.timeout, a.drift, a.no_record)
            print(json.dumps(r), flush=True)
            with open(results_path, "a") as f:
                f.write(json.dumps(r) + "\n")


def search_only(a) -> None:
    import cupy as cp
    import numpy as np

    from lft3.config import load_observation_config
    from lft3.search.core import Searcher, Window

    obs = load_observation_config("default").search
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    gulp = obs.gulp_channels
    rows = []
    for nf in a.nf_search:
        for tint in a.integration:
            tsamp = max(1, round(tint * RATE / 1024)) * BLOCK_S
            foff = RATE / nf
            n = obs.window_spectra
            max_dd = obs.max_drift_hz_s if nf <= 1024 else a.hz_max_drift
            if tsamp < nf / RATE:
                rows.append({"nf": nf, "integration_s": tint, "skipped": "integration shorter than one FFT"})
                continue
            if max_dd * n * tsamp < foff:
                rows.append({"nf": nf, "integration_s": tint, "skipped": "drift range < one channel per window"})
                continue
            cfg = obs.hyperseti_config()
            cfg["dedoppler"]["max_dd"] = max_dd
            se = Searcher(cfg, gulp, obs.flatten_coarse_ripple)
            rng = np.random.default_rng(nf)
            x = rng.chisquare(64, size=(n, gulp)).astype(np.float32)
            d = cp.asarray(x)
            w = Window(0, 0, 2000.0, foff * 1e-6, tsamp, 1.79e9, min(nf, gulp))
            se.search_beam(d, w)                         # compile
            cp.cuda.runtime.deviceSynchronize()
            t0 = time.perf_counter()
            for _ in range(a.repeat):
                se.search_beam(d, w)
            cp.cuda.runtime.deviceSynchronize()
            dt = (time.perf_counter() - t0) / a.repeat
            channels = 2 * 6144 * nf
            per_window = dt * channels / gulp
            step_s = obs.window_step_spectra * tsamp
            trials = 2 * int(max_dd / (foff / (n * tsamp))) + 1
            r = {"nf": nf, "integration_s": tint, "resolution_hz": round(foff, 3), "max_drift_hz_s": max_dd,
                 "drift_trials": trials,
                 "gulp_s": round(dt, 4), "stream_search_s_per_window": round(per_window, 2),
                 "search_capacity_x": round(step_s / per_window, 3), "mchan_per_s": round(gulp / dt / 1e6, 2)}
            print(json.dumps(r), flush=True)
            rows.append(r)
    (out / "search_only.json").write_text(json.dumps(rows, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--matrix", action="store_true")
    ap.add_argument("--search-only", action="store_true")
    ap.add_argument("--out", default="/opt/lft3arth/data/bench/resolution")
    ap.add_argument("--nf", type=int, nargs="+", default=[128, 256, 512, 1024])
    ap.add_argument("--nf-search", type=int, nargs="+", default=[128, 1024, 8192, 65536, 131072])
    ap.add_argument("--integration", type=float, nargs="+", default=[1.0, 0.5, 0.25, 0.1])
    ap.add_argument("--windows", type=int, default=5, help="search windows per matrix run")
    ap.add_argument("--timeout", type=float, default=480.0, help="seconds per matrix run")
    ap.add_argument("--drift", type=float, default=50.0, help="tone drift, Hz/s")
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--hz-max-drift", type=float, default=4.0, help="search-only: drift range above Nf 1024, Hz/s")
    ap.add_argument("--no-record", action="store_true", help="matrix: do not write FBH5 (chain without the writer)")
    a = ap.parse_args()
    if a.matrix:
        matrix(a)
    if a.search_only:
        search_only(a)
    if not (a.matrix or a.search_only):
        ap.error("--matrix and/or --search-only")


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    main()
