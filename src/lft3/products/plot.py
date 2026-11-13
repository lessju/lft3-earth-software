"""Plots of spectra (FBH5) and board captures. Headless (Agg): every function writes a PNG.

Board captures: the FFT snapshot is the full 8192-bin PFB output (both edges included, unlike the
6144 channels that are streamed). Its x axis is RF = -NCO + baseband offset, the same convention as the
FBH5 axis.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from lft3.board.capture import Capture  # noqa: E402

from .fbh5 import Spectra  # noqa: E402

FS_MHZ = 1250.0


def _db(x: np.ndarray) -> np.ndarray:
    return 10 * np.log10(np.maximum(x, 1e-30))


def _save(fig, out: str | Path) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=110, bbox_inches="tight")
    plt.close(fig)
    return out


def spectrum(s: Spectra, out: str | Path, title: str | None = None) -> Path:
    """Time-averaged spectrum, plus the max over time (shows intermittent signals)."""
    fig, ax = plt.subplots(figsize=(13, 4.5))
    ax.plot(s.freqs_mhz, _db(s.data.mean(0)), lw=0.7, label="mean")
    if s.data.shape[0] > 1:
        ax.plot(s.freqs_mhz, _db(s.data.max(0)), lw=0.5, alpha=0.6, label="max")
        ax.legend(loc="upper right")
    ax.set_xlabel("RF frequency (MHz)")
    ax.set_ylabel("power (dB, arbitrary)")
    res = abs(s.header.get("foff", 0)) * 1e6 * s.channel_average
    ax.set_title(title or f"{Path(s.path).name}: {s.data.shape[0]} x {s.tsamp:.2f} s, {res:.0f} Hz per point")
    ax.grid(alpha=0.3)
    return _save(fig, out)


def waterfall(s: Spectra, out: str | Path, title: str | None = None, vmin: float | None = None,
              vmax: float | None = None) -> Path:
    db = _db(s.data)
    if vmin is None or vmax is None:
        lo, hi = np.percentile(db, [2, 99.8])
        vmin = lo if vmin is None else vmin
        vmax = hi if vmax is None else vmax
    fig, (ax, axm) = plt.subplots(1, 2, figsize=(14, 7), gridspec_kw={"width_ratios": [12, 1]}, sharey=True)
    elapsed = s.times - s.times[0]
    extent = [s.freqs_mhz[0], s.freqs_mhz[-1], elapsed[-1] + s.tsamp, 0]
    im = ax.imshow(db, aspect="auto", extent=extent, vmin=vmin, vmax=vmax, cmap="viridis", interpolation="nearest")
    ax.set_xlabel("RF frequency (MHz)")
    ax.set_ylabel(f"time since {_dt.datetime.utcfromtimestamp(s.times[0]):%Y-%m-%d %H:%M:%S} UTC (s)")
    ax.set_title(title or Path(s.path).name)
    fig.colorbar(im, ax=axm, label="dB")
    if s.missing_packets is not None:
        axm.plot(s.missing_packets, elapsed + s.tsamp / 2, drawstyle="steps-mid")
        axm.set_xlabel("missing\npackets")
    else:
        axm.axis("off")
    return _save(fig, out)


def capture(cap: Capture, out: str | Path) -> Path:
    inputs = cap.attrs.get("inputs", [f"input{i}" for i in range(cap.data.shape[0])])
    nco = float(cap.attrs.get("nco_mhz", 0.0))
    stamp = _dt.datetime.utcfromtimestamp(cap.time).strftime("%Y-%m-%d %H:%M:%S UTC")
    if cap.kind == "fft_snapshot":
        fig, ax = plt.subplots(figsize=(13, 5))
        n = cap.data.shape[1]
        rf = -nco + np.fft.fftshift(np.fft.fftfreq(n, d=1 / FS_MHZ))
        floor = None
        for i, p in enumerate(cap.data):
            # The snapshot's 24-bit outputs are small integers at low input levels; empty bins (power 0)
            # are drawn at a 0 dB floor instead of -inf.
            db = _db(np.maximum(np.fft.fftshift(p), 1.0))
            floor = db.min() if floor is None else min(floor, db.min())
            ax.plot(rf, db, lw=0.6, label=inputs[i])
        ax.axvspan(rf[0], -nco - 468.75, color="grey", alpha=0.15)
        ax.axvspan(-nco + 468.75, rf[-1], color="grey", alpha=0.15, label="not streamed")
        ax.set_xlabel("RF frequency (MHz)")
        ax.set_ylabel("PFB output power (dB, 0 = empty bin)")
        ax.set_ylim(bottom=floor - 2)
        ax.legend(ncol=5, fontsize=8)
        ax.set_title(f"{cap.board} FFT snapshot, {stamp}")
        ax.grid(alpha=0.3)
        return _save(fig, out)

    # ADC samples (16-bit snapshot or 4-bit DDR dump): time series, histogram, spectrum
    x = cap.data
    mask = cap.attrs.get("received_mask")
    if mask is not None and np.any(mask) and not np.all(mask):
        # statistics and spectra from received packets only (missing ones are zero-filled)
        per = x.shape[1] // len(mask)
        keep = np.repeat(np.asarray(mask, bool), per)
        x = x[:, keep]
    fig, axes = plt.subplots(3, 1, figsize=(13, 11))
    show = min(x.shape[1], 512)
    t_us = np.arange(show) / FS_MHZ
    for i in range(x.shape[0]):
        rms = np.sqrt(np.mean(np.abs(x[i]) ** 2))
        axes[0].plot(t_us, x[i, :show].real, lw=0.6, label=f"{inputs[i]} I (rms {rms:.2f})")
    axes[0].set_xlabel("time (us)")
    axes[0].set_ylabel("I")
    axes[0].legend(ncol=4, fontsize=8)
    lim = np.abs(x.real).max() + 1
    integer = np.all(x.real == np.round(x.real))
    bins = np.arange(-lim - 0.5, lim + 1.5) if integer else 100
    for i in range(x.shape[0]):
        axes[1].hist(x[i].real, bins=bins, histtype="step", label=inputs[i])
    axes[1].set_yscale("log")
    axes[1].set_xlabel("I value")
    axes[1].legend(fontsize=8)
    nfft = 4096 if x.shape[1] >= 4096 else 2 ** int(np.log2(max(x.shape[1], 2)))
    nseg = x.shape[1] // nfft
    rf = -nco + np.fft.fftshift(np.fft.fftfreq(nfft, d=1 / FS_MHZ))
    for i in range(x.shape[0]):
        seg = x[i, : nseg * nfft].reshape(nseg, nfft) * np.hanning(nfft)
        p = (np.abs(np.fft.fftshift(np.fft.fft(seg, axis=1), axes=1)) ** 2).mean(0)
        axes[2].plot(rf, _db(p), lw=0.6, label=inputs[i])
    axes[2].set_xlabel("RF frequency (MHz)")
    axes[2].set_ylabel("power (dB)")
    axes[2].legend(ncol=4, fontsize=8)
    extra = ""
    if cap.kind == "adc_dump":
        extra = f", {cap.attrs.get('packets_received')}/{cap.attrs.get('packets_expected')} packets"
    axes[0].set_title(f"{cap.board} {cap.kind.replace('_', ' ')}, {stamp}, {x.shape[1]} samples{extra}")
    fig.tight_layout()
    return _save(fig, out)
