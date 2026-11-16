#!/usr/bin/env python3
"""Station sizing vs fine resolution and drift search (docs/sizing.md §9). Same model as sizing_calculator.html.

    /opt/lft3arth/lft3arth_venv/bin/python docs/sizing_figures/sizing_model.py   # writes the PNGs + prints the table

All costs are in GB10-equivalents (fraction of one DGX Spark GPU, 273 GB/s) per 60.8 Gb/s stream and are moved to
other GPUs by memory bandwidth. Inputs: measured on the GB10 2026-11-13/14 (runtime capacity benchmark, cuFFT
length sweep, pipeline/bench/search/bench_hyperseti.py) except where marked estimate.
"""
from __future__ import annotations

import math
from pathlib import Path

COARSE_HZ, NCH, BEAMS, RAW_GBS = 152587.890625, 6144, 2, 7.5
# cuFFT C2C fp32, FFT only, GE per stream (3.75 GS/s), measured on GB10 per FFT length 2^p
FFT_ONLY = {7: .283, 8: .278, 9: .280, 10: .274, 11: .273, 12: .273, 13: .272, 14: .276,
            15: .641, 16: .646, 17: .627, 18: .643, 19: .629, 20: .629}
WARP_GE = 0.045       # measured: warp-shuffle kernel (<= 256 points), int8 in, fused detect/accumulate
CALLBACK_GE = 0.10    # estimate: detect + accumulate traffic of cuFFT callbacks
CAPTURE_GE = 0.15     # measured: capture, scatter, 1.2 kHz spectra, quick-look
RFI_GE = 0.05         # estimate
WIDE_GE = 0.30        # estimate (0.1-1): wideband detect + dedispersion
KCELL = [(16, 5.28e-11), (32, 3.25e-11), (64, 2.78e-11)]  # hyperseti s per (channel x spectrum x trial), GB10
RTX6000 = dict(name="RTX PRO 6000 Blackwell Server", bw=1597.0, mem=96.0)


def kcell(nt: int) -> float:
    x = math.log2(nt)
    pts = [(math.log2(n), math.log(k)) for n, k in KCELL]
    i = 0 if x <= pts[1][0] else 1
    (x0, y0), (x1, y1) = pts[i], pts[i + 1]
    t = min(max((x - x0) / (x1 - x0), -1.0), 2.0)
    return math.exp(y0 + t * (y1 - y0))


def trials(nt: int, tsamp: float, df: float, dd: float) -> int:
    """hyperseti 'stepped' plan: all shifts up to nt channels, then doubling (fits the measured counts to ~5 %)."""
    up = dd * nt * tsamp / df
    if up < 1:
        return 1
    side = up if up <= nt else nt * (1 + math.log2(up / nt))
    return 2 * round(side) + 1


def stream_cost(p: int, tsamp: float = 10.0, nt: int = 32, dd: float = 4.0, frac: float = 1.0,
                kf: float = 1.0) -> dict:
    nf = 2 ** p
    df, tfft = COARSE_HZ / nf, nf / COARSE_HZ
    tsamp = max(tsamp, tfft)
    nch = BEAMS * NCH * nf * frac
    chan = WARP_GE if p <= 8 else FFT_ONLY[p] + CALLBACK_GE
    ge = dict(capture=CAPTURE_GE, chan=chan * frac, search=kcell(nt) * nch * trials(nt, tsamp, df, dd) / tsamp / kf,
              wide=WIDE_GE, rfi=RFI_GE * frac)
    uhf = sum(ge.values())
    vhf = CAPTURE_GE + 0.5 * (uhf - CAPTURE_GE)
    gmem = max(2.0, 2 * tfft) * RAW_GBS + 2 * nch * 4 / 1e9 + 3
    return dict(df=df, tsamp=tsamp, ge=ge, uhf=uhf, vhf=vhf, gmem=gmem)


def gpus(c: dict, gpu: dict = RTX6000, util: float = 0.7) -> int:
    cap = gpu["bw"] / 273 * util
    usable = gpu["mem"] * 0.9
    spg = min(math.floor(cap / c["uhf"]), math.floor(usable / c["gmem"]))
    if spg >= 1:
        return math.ceil((5 + c["vhf"] / c["uhf"]) / spg)
    per = max(math.ceil(c["uhf"] / cap), math.ceil(c["gmem"] / usable))
    return 5 * per + max(math.ceil(c["vhf"] / cap), math.ceil(c["gmem"] / usable))


def fmt_hz(hz: float) -> str:
    return f"{hz / 1000:.2f} kHz" if hz >= 1000 else f"{hz:.1f} Hz" if hz >= 10 else f"{hz:.2f} Hz"


def main() -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    out = Path(__file__).resolve().parent
    ps = list(range(7, 21))
    labels = [fmt_hz(COARSE_HZ / 2 ** p) for p in ps]
    colours = dict(capture="#2a78d6", chan="#eb6834", search="#1baf7a", wide="#eda100", rfi="#e87ba4")
    names = dict(capture="Capture + 1.2 kHz spectra", chan="Fine channeliser", search="Drift search (hyperseti)",
                 wide="Wideband search (estimate)", rfi="RFI flagging (estimate)")

    fig, (a1, a2) = plt.subplots(1, 2, figsize=(13, 4.6), dpi=130)
    costs = [stream_cost(p) for p in ps]
    base = [0.0] * len(ps)
    for k in colours:
        top = [b + c["ge"][k] for b, c in zip(base, costs)]
        a1.fill_between(ps, base, top, color=colours[k], alpha=0.9, lw=0, label=names[k], step=None)
        base = top
    cap = RTX6000["bw"] / 273 * 0.7
    for k in (1, 2):
        a1.axhline(k * cap, color="0.15", ls="--", lw=1.2)
        a1.text(7.1, k * cap + 0.15, f"{k} × RTX PRO 6000 at 70 %", fontsize=8)
    a1.set_ylim(0, 10)
    a1.set_ylabel("GB10-equivalents per UHF stream")
    a1.set_title("Cost per stream (32 × 10 s window, ±4 Hz/s)", fontsize=10)
    a1.legend(fontsize=7.5, loc="upper left", bbox_to_anchor=(0, 0.80), frameon=False)

    for nt, c in ((16, "#2a78d6"), (32, "#eb6834"), (64, "#1baf7a")):
        a2.step(ps, [gpus(stream_cost(p, nt=nt)) for p in ps], where="mid", color=c, lw=2,
                label=f"{nt} spectra × 10 s per window")
    a2.step(ps, [gpus(stream_cost(p, nt=32, frac=0.25)) for p in ps], where="mid", color="#6250d6", lw=2, ls=":",
            label="32 spectra, ¼ of band at fine resolution")
    a2.set_yscale("log", base=2)
    a2.set_yticks([1, 2, 4, 8, 16, 32, 64])
    a2.get_yaxis().set_major_formatter(matplotlib.ticker.ScalarFormatter())
    a2.set_ylabel("RTX PRO 6000 for the station (70 %)")
    a2.set_title("GPUs for 6 streams (±4 Hz/s)", fontsize=10)
    a2.legend(fontsize=7.5, frameon=False)
    for ax in (a1, a2):
        ax.set_xticks(ps[::2], labels[::2], fontsize=8)
        ax.set_xlabel("fine channel width (FFT length 2^7 … 2^20)")
        ax.grid(alpha=0.25)
        ax.set_xlim(7, 20)
    fig.tight_layout()
    fig.savefig(out / "sizing_vs_resolution.png")

    r5000 = dict(name="RTX PRO 5000", bw=1344.0, mem=48.0)
    print("| Fine channel | GE per UHF stream (16 / 32 / 64 spectra) | RTX PRO 6000 | RTX PRO 5000 48 GB "
          "| GPU memory per stream |")
    print("|---|---|---|---|---|")
    for p in (7, 10, 13, 15, 16, 17, 18, 19):
        cs = [stream_cost(p, nt=nt) for nt in (16, 32, 64)]
        ge = " / ".join(f"{c['uhf']:.2f}" for c in cs)
        g6 = " / ".join(str(gpus(c)) for c in cs)
        g5 = " / ".join(str(gpus(c, r5000)) for c in cs)
        print(f"| {fmt_hz(cs[0]['df'])} (2^{p}) | {ge} | {g6} | {g5} | {cs[0]['gmem']:.0f} GB |")

if __name__ == "__main__":
    main()
