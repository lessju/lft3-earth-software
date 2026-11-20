"""The narrowband drift search shared by the live search (spectra in GPU memory) and the file search (FBH5).

Both paths cut the spectra into the same windows and call :func:`search_window`, so a live window and the same
spectra read back from FBH5 give the same hits (docs/runtime.md, live search).

* Window grid: window k covers integrations [k * step, k * step + N) of the stream's absolute integration
  number (spectrum start t / spectra per integration), with N = ``search.window_spectra`` and
  step = ``search.window_step_spectra`` (config/observations/*.yaml).
* Per window and beam: optional coarse-channel ripple flattening (per gulp: divide by the median response of
  the fine bins across board channels), then hyperseti (normalise, blank edges, dedoppler, hit search) per gulp
  of ``gulp_channels``.

Runs in the search venv (CuPy, hyperseti). This module is Python 3.8 syntax and imports them lazily.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

SPUR_SPACING_MHZ = 125.0
COARSE_CHANNEL_MHZ = 1250.0 / 8192.0
HIT_COLUMNS = ("source", "window_index", "window_start_utc", "window_start_unix", "beam", "f_start_mhz",
               "drift_rate_hz_s", "snr", "channel", "boxcar_size", "label", "obs_id", "stream")


def rfi_label(f_mhz: float, foff_mhz: float) -> str:
    """Known RFI near a hit: the board's spurs at multiples of 125 MHz, and the Spark's Wi-Fi channel 1."""
    nearest = round(f_mhz / SPUR_SPACING_MHZ) * SPUR_SPACING_MHZ
    if abs(f_mhz - nearest) <= 2 * abs(foff_mhz):
        return f"rfi:board-spur-{nearest:.0f}MHz"
    if 2401.0 <= f_mhz <= 2423.0:
        return "rfi:wifi-ch1"
    return ""


@dataclass
class Window:
    """A window of N consecutive integrations of one stream, ready to search."""
    index: int                  # k: covers integrations [k * step, k * step + N)
    first_integration: int
    fch1_mhz: float             # centre of channel 0
    foff_mhz: float
    tsamp_s: float
    start_unix: float           # start of the first spectrum
    fine_per_coarse: int        # fine channels per board channel (for flattening)
    missing_fraction: float = 0.0

    @property
    def start_utc(self) -> str:
        return _dt.datetime.fromtimestamp(self.start_unix, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]


def window_ends_at(integration: int, n: int, step: int) -> int | None:
    """If a window of the grid ends with `integration`, its index; else None."""
    first = integration - n + 1
    if first < 0 or first % step:
        return None
    return first // step


def axes_from_nco(nco_mhz: float, channel_start: int, fine_per_coarse: int) -> tuple[float, float]:
    """(fch1, foff) of fine spectra (pipeline/src/spectrometer/axes.hh): RF = -nco + baseband offset."""
    foff = COARSE_CHANNEL_MHZ / fine_per_coarse
    fch1 = -nco_mhz + (channel_start - 3072) * COARSE_CHANNEL_MHZ - (fine_per_coarse // 2) * foff
    return fch1, foff


def flatten_ripple(x, fine_per_coarse: int):
    """Divide [T, F] spectra (CuPy) by the response of the fine bins within a board channel: the median over
    board channels of the time-mean, normalised to mean 1. F must be a multiple of fine_per_coarse."""
    import cupy as cp

    t, f = x.shape
    shape = x.reshape(t, f // fine_per_coarse, fine_per_coarse).mean(axis=0)     # [C, Nf]
    resp = cp.median(shape, axis=0)
    resp /= resp.mean()
    resp = cp.where(resp > 0, resp, 1.0).astype(x.dtype)
    return (x.reshape(t, -1, fine_per_coarse) / resp).reshape(t, f)


_TRUNCATING_SHIFT = "(shift[d] * t / T)"
_ROUNDED_SHIFT = "__float2int_rn((float)(shift[d] * t) / (float)T)"


def patch_dedoppler_rounding() -> bool:
    """Make hyperseti's dedoppler kernel round the per-spectrum shift instead of truncating it.

    hyperseti (a15f7eb) shifts spectrum t of trial d by ``shift[d] * t / T`` channels in C integer division,
    which truncates toward zero. Over hour-long files that does not matter; over a 16-spectrum window it makes
    trials +-1 identical to trial 0 (no spectrum is ever shifted), so a zero-drift signal is reported at
    -74.5 Hz/s, and a +74.5 Hz/s one too (found 2026-11-20 with synthetic drifting tones). Rounding gives
    shift round(d t / T): the drift of trial d is d * foff / (T tsamp), as hyperseti labels it. Same kernel
    source otherwise. Returns True if the patch is in place."""
    import cupy as cp
    import hyperseti.kernels.dedoppler as kd

    if getattr(kd, "_lft3_rounding", False):
        return True
    src = kd.dedoppler_kernel.code
    if _TRUNCATING_SHIFT not in src:
        return False
    kd.dedoppler_kernel = cp.RawKernel(src.replace(_TRUNCATING_SHIFT, _ROUNDED_SHIFT), "dedopplerKernel")
    kd._lft3_rounding = True
    return True


def searcher_for(settings) -> Searcher:
    """The Searcher for a SearchSettings (lft3.config): what the live and the file search both use."""
    return Searcher(settings.hyperseti_config(), settings.gulp_channels, settings.flatten_coarse_ripple,
                    drop_zero_drift=settings.drop_zero_drift)


def merge_hits(drift_idx, channel_idx, boxcar, snr):
    """Indices of the hits kept by hyperseti's merge_hits (hits.py), vectorised.

    Same greedy rule: take the strongest remaining hit, drop every remaining hit within boxcar + 1 drift trials
    and boxcar + 1 channels of it (using either hit's boxcar size), repeat. hyperseti runs one pandas query per
    hit (~3 ms each), which took 11.8 s for a 3136-hit window of live data; this takes milliseconds. Ties in S/N
    keep the first hit in hyperseti's sort order, as a stable sort does here. Returns indices in S/N order."""
    import numpy as np

    d = np.asarray(drift_idx, dtype=np.int64)
    c = np.asarray(channel_idx, dtype=np.int64)
    b = np.asarray(boxcar, dtype=np.int64)
    order = np.argsort(-np.asarray(snr, dtype=np.float64), kind="stable")
    alive = np.ones(len(d), dtype=bool)
    keep = []
    for i in order:
        if not alive[i]:
            continue
        keep.append(int(i))
        dd, dc = np.abs(d - d[i]), np.abs(c - c[i])
        near = ((dd <= b + 1) | (dd <= b[i] + 1)) & ((dc <= b[i] + 1) | (dc <= b + 1))
        alive &= ~near
    return keep


class Searcher:
    """hyperseti on windows; keeps its compiled kernels between windows."""

    def __init__(self, hyperseti_config: dict, gulp_channels: int, flatten: bool, gpu_id: int = 0,
                 drop_zero_drift: bool = False):
        import logging
        import warnings

        warnings.filterwarnings("ignore")
        logging.getLogger("hdf5plugin").setLevel(logging.ERROR)
        from hyperseti.kernels import BlankHitsMan, DedopplerMan, PeakFinderMan, SmearCorrMan
        from hyperseti.log import set_log_level
        from hyperseti.utils import attach_gpu_device

        set_log_level("warning")
        attach_gpu_device(gpu_id)
        if not patch_dedoppler_rounding():
            raise RuntimeError("hyperseti's dedoppler kernel changed: check lft3.search.core.patch_dedoppler_rounding")
        # hyperseti reads merge_boxcar_trials from "hitsearch" (not "pipeline"); its merge is O(hits^2) pandas
        # queries, so it is off there and done by merge_hits() above with the same rule.
        self.config = {k: (dict(v) if isinstance(v, dict) else v) for k, v in hyperseti_config.items()}
        legacy = self.config.get("pipeline", {}).get("merge_boxcar_trials", True)
        self.merge = bool(self.config.get("hitsearch", {}).get("merge_boxcar_trials", legacy))
        self.config.setdefault("hitsearch", {})["merge_boxcar_trials"] = False
        self.gulp = gulp_channels
        self.flatten = flatten
        # Hits whose (merged) drift is exactly 0 are stationary, i.e. local RFI. Dropped after merging, so a
        # stationary signal's weaker off-zero trials, folded into its zero-drift peak, go with it.
        self.drop_zero_drift = drop_zero_drift
        self.dropped = 0                  # zero-drift hits dropped by the last search_beam call
        self.kernels = {"dedoppler": DedopplerMan(), "blank_hits": BlankHitsMan(), "peak_find": PeakFinderMan(),
                        "smear_corr": SmearCorrMan()}

    def search_beam(self, data, w: Window):
        """Hits in one beam's window: data is a CuPy float32 [N, F] array (not modified). Returns a list of dicts
        with f_start_mhz, drift_rate_hz_s, snr, channel (absolute fine channel), boxcar_size."""
        import cupy as cp
        from astropy.coordinates import SkyCoord
        from hyperseti.data_array import DataArray
        from hyperseti.dimension_scale import DimensionScale, TimeScale
        from hyperseti.pipeline import GulpPipeline

        n_time, n_chan = data.shape
        gulp = min(self.gulp, n_chan)
        hits = []
        self.dropped = 0
        tstart_mjd = w.start_unix / 86400.0 + 40587.0
        for c0 in range(0, n_chan, gulp):
            c1 = min(n_chan, c0 + gulp)
            x = data[:, c0:c1].astype(cp.float32, copy=True)
            if self.flatten and (c1 - c0) % w.fine_per_coarse == 0:
                x = flatten_ripple(x, w.fine_per_coarse)
            x = cp.ascontiguousarray(x.reshape(n_time, 1, c1 - c0))
            scales = {
                "time": TimeScale("time", tstart_mjd, w.tsamp_s, n_time, time_format="mjd", time_delta_format="sec"),
                "beam_id": DimensionScale("beam_id", 0, 0, 1, units=""),
                "frequency": DimensionScale("frequency", w.fch1_mhz + c0 * w.foff_mhz, w.foff_mhz, c1 - c0,
                                            units="MHz"),
            }
            attrs = {"name": f"window{w.index}", "source": "LFT3", "sky_coord": SkyCoord(0, 0, unit="deg")}
            arr = DataArray(x, ("time", "beam_id", "frequency"), scales, attrs, units="counts")
            pipe = GulpPipeline(arr, self.config, kernel_managers=self.kernels)
            tab = pipe.run()
            if not len(tab):
                continue
            box = tab["boxcar_size"].to_numpy() if "boxcar_size" in tab else [1] * len(tab)
            idx = merge_hits(tab["driftrate_idx"].to_numpy(), tab["channel_idx"].to_numpy(), box,
                             tab["snr"].to_numpy()) if self.merge else range(len(tab))
            f, dr, sn, ch = (tab[k].to_numpy() for k in ("f_start", "drift_rate", "snr", "channel_idx"))
            for i in idx:
                if self.drop_zero_drift and dr[i] == 0:
                    self.dropped += 1
                    continue
                hits.append({"f_start_mhz": float(f[i]), "drift_rate_hz_s": float(dr[i]), "snr": float(sn[i]),
                             "channel": c0 + int(ch[i]), "boxcar_size": int(box[i])})
        hits.sort(key=lambda h: -h["snr"])
        return hits


def hit_rows(hits: list, w: Window, beam: int, source: str, obs_id: str, stream: str) -> list:
    """CSV rows (HIT_COLUMNS) for one beam's hits in a window."""
    rows = []
    for h in hits:
        rows.append([source, w.index, w.start_utc, f"{w.start_unix:.6f}", beam, f"{h['f_start_mhz']:.9f}",
                     f"{h['drift_rate_hz_s']:.6g}", f"{h['snr']:.4f}", h["channel"], h["boxcar_size"],
                     rfi_label(h["f_start_mhz"], w.foff_mhz), obs_id, stream])
    return rows
