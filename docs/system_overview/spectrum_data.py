"""Extract the small data set behind the 'real spectra' figure (f17) from a soak-test FBH5 file.

    /opt/lft3arth/lft3arth_venv/bin/python spectrum_data.py [FILE.h5] [-o data/soak_spectrum.npz]

The source file (one hour, ~8 GB) is not in the repository; the reduced arrays (~1 MB) are, so build.py and
figures.py run without it. Default source: the first hour of the 2-hour soak of 2026-11-13, beam 0.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np

from lft3.products.fbh5 import read_spectra

DEFAULT = "/opt/lft3arth/data/spectra/rfsoc0/rfsoc0_20261113T134029_beam0.h5"
FULL_PIXELS = 3072            # full-band plot: one pixel = 256 fine channels = 2 coarse channels
FULL_SECONDS = 300            # average of the first 5 minutes
SPUR_MHZ, SPUR_HALF = 2125.0, 0.40
WIFI = (2398.0, 2428.0)
WATERFALL_PIXELS, WATERFALL_ROW_S = 600, 10


def reduce(x: np.ndarray, n: int, how) -> np.ndarray:
    k = x.shape[-1] // n
    return how(x[..., : k * n].reshape(*x.shape[:-1], n, k), axis=-1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("file", nargs="?", default=DEFAULT)
    ap.add_argument("-o", "--out", default=str(Path(__file__).parent / "data" / "soak_spectrum.npz"))
    a = ap.parse_args()

    # full band, time average, coarse-channel response divided out; read in frequency slices to bound memory
    s0 = read_spectra(a.file, fmax=1600, tmax=1)
    fch1, foff = s0.header["fch1"], s0.header["foff"]
    nchan = int(s0.header["nchans"])
    edges = np.linspace(0, nchan, 9).astype(int)
    mean_parts, max_parts, f_parts = [], [], []
    for c0, c1 in zip(edges[:-1], edges[1:]):
        s = read_spectra(a.file, fmin=fch1 + foff * c0, fmax=fch1 + foff * (c1 - 1), tmax=FULL_SECONDS, flatten=True)
        m = s.data.mean(0)
        npx = FULL_PIXELS * (c1 - c0) // nchan
        mean_parts.append(reduce(m, npx, np.mean))
        max_parts.append(reduce(m, npx, np.max))
        f_parts.append(reduce(s.freqs_mhz, npx, np.mean))
    full_f, full_mean, full_max = (np.concatenate(p) for p in (f_parts, mean_parts, max_parts))

    # zoom around one board spur at full fine resolution (not flattened: shows the coarse-channel shape)
    z = read_spectra(a.file, fmin=SPUR_MHZ - SPUR_HALF, fmax=SPUR_MHZ + SPUR_HALF, tmax=FULL_SECONDS)
    zoom_f, zoom_p = z.freqs_mhz, z.data.mean(0)

    # waterfall of the Wi-Fi channel over the whole file, 10 s rows
    w = read_spectra(a.file, fmin=WIFI[0], fmax=WIFI[1], flatten=True)
    rows = w.data.shape[0] // WATERFALL_ROW_S
    wf = w.data[: rows * WATERFALL_ROW_S].reshape(rows, WATERFALL_ROW_S, -1).mean(1)
    wf = reduce(wf, WATERFALL_PIXELS, np.mean)
    wf_f = reduce(w.freqs_mhz, WATERFALL_PIXELS, np.mean)

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out, full_f=full_f, full_mean=full_mean, full_max=full_max, zoom_f=zoom_f, zoom_p=zoom_p,
                        wf=wf.astype(np.float32), wf_f=wf_f, wf_row_s=WATERFALL_ROW_S, foff_mhz=foff,
                        full_seconds=FULL_SECONDS, source=Path(a.file).name, obs_id=s0.lft3.get("lft3_obs_id", ""))
    print(f"wrote {out} ({out.stat().st_size / 1e6:.2f} MB)")


if __name__ == "__main__":
    main()
