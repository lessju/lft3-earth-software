"""Read the filterbank-HDF5 (FBH5) spectra written by the pipeline (lft3_fbh5_writer).

A full-band file holds 786 432 fine channels per spectrum, so reading selects a frequency range and a time
range first, and can average channels down to a manageable number:

    s = read_spectra("lft3_..._beam0.h5", fmin=1990, fmax=2010, tmax=60, max_channels=8192)
    s.data          # [time, channel] float32 power
    s.freqs_mhz     # channel centres (MHz, ascending)
    s.times         # UNIX time of each spectrum's start
"""
from __future__ import annotations

from dataclasses import dataclass, field

import h5py
import numpy as np


@dataclass
class Spectra:
    path: str
    data: np.ndarray            # [time, channel] power
    freqs_mhz: np.ndarray       # [channel]
    times: np.ndarray           # [time] UNIX seconds (start of each integration)
    tsamp: float
    missing_packets: np.ndarray | None
    channel_average: int
    header: dict = field(default_factory=dict)      # SIGPROC attributes of /data
    lft3: dict = field(default_factory=dict)        # lft3_* root attributes


def _attrs(obj) -> dict:
    out = {}
    for k, v in obj.attrs.items():
        if isinstance(v, bytes):
            v = v.decode()
        elif isinstance(v, np.generic):
            v = v.item()
        elif isinstance(v, np.ndarray) and v.dtype.kind == "S":
            v = [x.decode() for x in v]
        out[k] = v
    return out


def info(path: str) -> dict:
    with h5py.File(path, "r") as h:
        d = h["data"]
        hdr = _attrs(d)
        nchan = d.shape[2]
        return {
            "path": path, "shape": d.shape, "header": hdr,
            "lft3": {k: v for k, v in _attrs(h).items() if k.startswith("lft3")},
            "fmin_mhz": hdr["fch1"] - 0.5 * hdr["foff"],
            "fmax_mhz": hdr["fch1"] + (nchan - 0.5) * hdr["foff"],
            "duration_s": d.shape[0] * hdr["tsamp"],
        }


def mjd_to_unix(mjd: float) -> float:
    return (mjd - 40587.0) * 86400.0


def coarse_channel_response(path: str, nspectra: int = 8, ifno: int = 0) -> np.ndarray:
    """Average shape of a coarse (board) channel across its fine channels, normalised to mean 1.

    The board's PFB channels roll off ~3 dB at their edges, which shows up as a ripple with a 152.6 kHz
    period in the fine spectra. The median over all coarse channels of a few spectra is robust to the
    narrow spurs. Dividing by it flattens the band (`read_spectra(..., flatten=True)`)."""
    with h5py.File(path, "r") as h:
        d = h["data"]
        nfpc = int(d.attrs.get("nfpc", 0)) or 1
        x = d[: min(nspectra, d.shape[0]), ifno, :].astype(np.float64).mean(0)
    coarse = x[: x.size // nfpc * nfpc].reshape(-1, nfpc)
    shape = np.median(coarse / np.median(coarse, axis=1, keepdims=True), axis=0)
    return shape / shape.mean()


def read_spectra(path: str, fmin: float | None = None, fmax: float | None = None,
                 tmin: float = 0.0, tmax: float | None = None, ifno: int = 0,
                 max_channels: int | None = None, flatten: bool = False) -> Spectra:
    """fmin/fmax in MHz; tmin/tmax in seconds from the file start. max_channels averages groups of adjacent
    channels so at most that many remain. flatten divides out the coarse-channel response."""
    with h5py.File(path, "r") as h:
        d = h["data"]
        hdr = _attrs(d)
        nt, _, nchan = d.shape
        fch1, foff, tsamp = hdr["fch1"], hdr["foff"], hdr["tsamp"]
        # channels whose centre lies in [fmin, fmax] (tolerance for float rounding of the edges)
        eps = 1e-6
        c0 = 0 if fmin is None else int(np.clip(np.ceil((fmin - fch1) / foff - eps), 0, nchan))
        c1 = nchan if fmax is None else int(np.clip(np.floor((fmax - fch1) / foff + eps) + 1, c0, nchan))
        t0 = int(np.clip(np.floor(tmin / tsamp), 0, nt))
        t1 = nt if tmax is None else int(np.clip(np.ceil(tmax / tsamp), t0, nt))
        data = d[t0:t1, ifno, c0:c1].astype(np.float32)
        missing = h["lft3_missing_packets"][t0:t1] if "lft3_missing_packets" in h else None
        lft3 = {k: v for k, v in _attrs(h).items() if k.startswith("lft3")}
    freqs = fch1 + foff * np.arange(c0, c1)
    if flatten and hdr.get("nfpc"):
        response = coarse_channel_response(path, ifno=ifno)
        data = data / response[np.arange(c0, c1) % int(hdr["nfpc"])]
    avg = 1
    if max_channels and data.shape[1] > max_channels:
        avg = int(np.ceil(data.shape[1] / max_channels))
        n = data.shape[1] // avg * avg
        data = data[:, :n].reshape(data.shape[0], -1, avg).mean(2)
        freqs = freqs[:n].reshape(-1, avg).mean(1)
    start = mjd_to_unix(hdr["tstart"])
    times = start + tsamp * np.arange(t0, t1)
    return Spectra(path, data, freqs, times, tsamp, missing, avg, hdr, lft3)
