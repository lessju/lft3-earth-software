#!/usr/bin/env python3
"""How many 100 GbE board streams can one DGX Spark process in real time? (docs/runtime_guide.md, "Capacity")

For N = 1, 2, ... runs N real-time synthetic streams at once, each = lft3-capture (full band, 60.8 Gb/s
equivalent, real-time paced) + lft3-spectrometer (128-point fine FFT, 1 s integrations, FBH5 + derived coarse
FBH5 + latest snapshot, recording to NVMe), plus memload writing N x 7.6 GB/s into host memory (what the NICs'
DMA would add). A configuration passes when every capture keeps real time (>= 0.995x), no reader overruns
and the worst reader lag stays below 25 % of the ring.

Not covered by the synthetic source: the NIC/PCIe path itself and DAQIRI's busy-polling worker core (one core
per link, measured separately). Run on the host (binaries from pipeline/build, no root needed):

    python3 pipeline/bench/capacity/capacity.py --max 8 --seconds 60 --out /opt/lft3arth/data/bench/capacity
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "src"))
from lft3.ring import Ring  # noqa: E402

BUILD = REPO / "pipeline" / "build"
X925 = [5, 6, 7, 8, 9, 15, 16, 17, 18, 19]
A725 = [0, 1, 2, 3, 4, 10, 11, 12, 13, 14]
SPECTRA_PER_S = 152587.890625
NIC_GBPS_PER_STREAM = 7.6


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


def run_n(n: int, seconds: float, warmup: float, out_dir: Path, memload: Path, ring_s: float, fft: int) -> dict:
    streams = [f"cap{n}-{i}" for i in range(n)]
    procs = []
    logs = out_dir / f"n{n}"
    shutil.rmtree(logs, ignore_errors=True)
    logs.mkdir(parents=True)
    for i, s in enumerate(streams):
        lf = open(logs / f"{s}-capture.log", "w")
        procs.append(subprocess.Popen([str(BUILD / "lft3-capture"), "--stream", s, "--source", "synthetic",
                                       "--processing-core", str(X925[i % len(X925)]), "--ring-seconds", str(ring_s),
                                       "--nco-mhz", "-2031.25"], stdout=lf, stderr=subprocess.STDOUT))
    time.sleep(2)
    for s in streams:
        lf = open(logs / f"{s}-spectrometer.log", "w")
        d = logs / s
        procs.append(subprocess.Popen([str(BUILD / "lft3-spectrometer"), "--stream", s, "--fft-length", str(fft),
                                       "--output-dir", str(d), "--coarse-dir", str(d / "quicklook"),
                                       "--latest", str(d / "latest"), "--prefix", "bench"],
                                      stdout=lf, stderr=subprocess.STDOUT))
    mem = subprocess.Popen([str(memload), f"{n * NIC_GBPS_PER_STREAM}", str(warmup + seconds + 2),
                            ",".join(str(c) for c in A725)], stdout=subprocess.PIPE, text=True)
    time.sleep(warmup)
    stop = threading.Event()
    gpu: list = []
    threading.Thread(target=gpu_sampler, args=(stop, gpu), daemon=True).start()

    def snap():
        out = {}
        for s in streams:
            with Ring(s) as r:
                out[s] = r.status()
        return out

    t0 = time.time()
    a = snap()
    max_lag = {s: 0 for s in streams}
    while time.time() - t0 < seconds:
        time.sleep(1)
        for s, st in snap().items():
            for rd in st.readers:
                max_lag[s] = max(max_lag[s], rd.lag_blocks)
    t1 = time.time()
    b = snap()
    stop.set()
    for p in procs:
        p.terminate()
    for p in procs:
        try:
            p.wait(timeout=30)
        except subprocess.TimeoutExpired:
            p.kill()
    mem_out = mem.communicate()[0].strip()
    per = []
    for s in streams:
        sa, sb = a[s], b[s]
        blocks = sb.head - sa.head
        speed = blocks * sb.spectra_per_block / SPECTRA_PER_S / (t1 - t0)
        over = sum(r.overruns for r in sb.readers) - sum(r.overruns for r in sa.readers)
        busy = max((r.busy_ms for r in sb.readers), default=0.0)
        per.append({"stream": s, "speed": round(speed, 4), "overruns": over, "max_lag_blocks": max_lag[s],
                    "slots": sb.slots, "reader_busy_ms_per_block": round(busy, 3),
                    "missing_packets": sb.stats["packets_missing"] - sa.stats["packets_missing"]})
    ok = all(p["speed"] >= 0.995 and p["overruns"] == 0 and p["max_lag_blocks"] < 0.25 * p["slots"] for p in per)
    res = {"streams": n, "gbps_equivalent": round(n * 60.8, 1), "ok": ok,
           "min_speed": min(p["speed"] for p in per), "overruns": sum(p["overruns"] for p in per),
           "max_lag_blocks": max(p["max_lag_blocks"] for p in per),
           "gpu_util_mean": round(sum(gpu) / len(gpu), 1) if gpu else None,
           "gpu_util_max": max(gpu) if gpu else None, "memload": mem_out, "per_stream": per}
    shutil.rmtree(logs / "data", ignore_errors=True)
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min", type=int, default=1)
    ap.add_argument("--max", type=int, default=8)
    ap.add_argument("--seconds", type=float, default=60)
    ap.add_argument("--warmup", type=float, default=8)
    ap.add_argument("--ring-seconds", type=float, default=1.0)
    ap.add_argument("--fft-length", type=int, default=128)
    ap.add_argument("--out", default="/opt/lft3arth/data/bench/capacity")
    ap.add_argument("--keep-going", action="store_true", help="continue after the first failing N")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    memload = out / "memload"
    subprocess.run(["g++", "-O2", "-pthread", "-o", str(memload), str(HERE / "memload.cc")], check=True)
    results = []
    for n in range(a.min, a.max + 1):
        r = run_n(n, a.seconds, a.warmup, out, memload, a.ring_seconds, a.fft_length)
        results.append(r)
        print(f"N={n} ({r['gbps_equivalent']} Gb/s): {'PASS' if r['ok'] else 'FAIL'}  min speed {r['min_speed']:.3f}x  "
              f"overruns {r['overruns']}  max lag {r['max_lag_blocks']} blocks  GPU {r['gpu_util_mean']}% "
              f"(max {r['gpu_util_max']})  | {r['memload']}", flush=True)
        (out / "results.json").write_text(json.dumps(results, indent=1))
        if not r["ok"] and not a.keep_going:
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
