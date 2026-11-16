#!/usr/bin/env python3
"""Cost of a hyperseti narrowband drift search at Hz-like resolution (docs/sizing.md, "Narrowband search").

Builds a synthetic Stokes-I spectrogram (Gaussian noise + one tone drifting at 0.05 Hz/s), puts it on the GPU and
times hyperseti's brute-force dedoppler kernel (stepped plan, smearing correction), then the full ``find_et``
pipeline from a host array. Prints fine channels searched per second; scale to a stream with
channels = beams x 6144 x Nf and the window length nt x tsamp.

Run in the search venv (scripts/setup_search_env.sh), with nothing capturing on the same host:

    /opt/lft3arth/search_venv/bin/python -I pipeline/bench/search/bench_hyperseti.py \\
        --nt 32 --tsamp 10 --df 1.164 --nchan 1048576 --max-dd 4

GB10 (2026-11-14), nchan 2^20, df 1.164 Hz, tsamp 10 s, max_dd 4 Hz/s: dedoppler 6.2 / 2.4 / 0.7 Mchan/s at
nt 16 / 32 / 64; at nt 32, max_dd 1 / 4 / 10 Hz/s: 3.5 / 2.4 / 2.1 Mchan/s. 2^21 channels in one call fails
(CUDA_ERROR_INVALID_VALUE), so keep gulps at 2^20.
"""
from __future__ import annotations

import argparse
import copy
import logging
import time
import warnings


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--nt", type=int, default=32, help="spectra per search window")
    ap.add_argument("--tsamp", type=float, default=10.0, help="seconds per spectrum")
    ap.add_argument("--df", type=float, default=1.164, help="fine channel width, Hz")
    ap.add_argument("--nchan", type=int, default=2 ** 20, help="channels (<= 2^20 per call)")
    ap.add_argument("--max-dd", type=float, default=4.0, help="max drift, Hz/s")
    ap.add_argument("--repeat", type=int, default=3)
    a = ap.parse_args()

    warnings.filterwarnings("ignore")
    logging.disable(logging.WARNING)
    import astropy.time
    import cupy as cp
    import numpy as np
    from astropy import units as u
    from hyperseti.data_array import from_metadata
    from hyperseti.dedoppler import dedoppler
    from hyperseti.pipeline import find_et

    rng = np.random.default_rng(1)
    d = rng.standard_normal((a.nt, 1, a.nchan), dtype=np.float32) + 10
    for t in range(a.nt):
        d[t, 0, a.nchan // 3 + int(0.05 * t * a.tsamp / a.df)] += 5
    md = {"frequency_start": 1000.0 * u.MHz, "frequency_step": a.df / 1e6, "time_step": a.tsamp * u.s,
          "beam_id_start": 0, "beam_id_step": 1, "source_name": "synth",
          "time_start": astropy.time.Time(60000, format="mjd")}
    dims = ("time", "beam_id", "frequency")

    da = from_metadata(cp.asarray(d), copy.deepcopy(md), dims=dims, units="counts")
    best = float("inf")
    for _ in range(a.repeat):
        cp.cuda.Device().synchronize()
        t0 = time.perf_counter()
        dd = dedoppler(da, max_dd=a.max_dd, apply_smearing_corr=True, plan="stepped")
        cp.cuda.Device().synchronize()
        best = min(best, time.perf_counter() - t0)
    print(f"nt={a.nt} tsamp={a.tsamp}s df={a.df}Hz nchan={a.nchan} max_dd={a.max_dd}Hz/s "
          f"drift trials={dd.data.shape[0]}: dedoppler {best:.3f} s = {a.nchan / best / 1e6:.2f} Mchan/s")

    cfg = {"preprocess": {"sk_flag": False, "normalize": True},
           "dedoppler": {"kernel": "dedoppler", "max_dd": a.max_dd, "apply_smearing_corr": True, "plan": "stepped"},
           "hitsearch": {"threshold": 20}, "pipeline": {"merge_boxcar_trials": True}}
    host = from_metadata(d, copy.deepcopy(md), dims=dims, units="counts")
    t0 = time.perf_counter()
    res = find_et(host, cfg, gulp_size=min(a.nchan, 2 ** 20))
    el = time.perf_counter() - t0
    tab = res.hit_table if hasattr(res, "hit_table") else res
    top = tab.sort_values("snr").iloc[-1]["drift_rate"] if len(tab) else None
    print(f"find_et from host: {el:.2f} s = {a.nchan / el / 1e6:.2f} Mchan/s; hits {len(tab)} "
          f"(injected 0.05 Hz/s, strongest {top})")


if __name__ == "__main__":
    main()
