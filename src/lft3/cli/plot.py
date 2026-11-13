"""lft3-plot: inspect and plot the files the system writes.

  lft3-plot info FILE...                                      what is in a file (spectra or capture)
  lft3-plot spectrum FILE [--fmin MHz --fmax MHz] [--tmin s --tmax s] [--max-channels 8192] [--flatten] [-o out.png]
  lft3-plot waterfall FILE [same selection] [--vmin dB --vmax dB] [-o out.png]
  lft3-plot capture FILE [-o out.png]                         board capture (FFT/ADC snapshot, DDR dump)
  lft3-plot latest [--kind spectra|captures] [--board rfsoc0]   newest file in the data directory

FILE is an FBH5 spectrum file from the pipeline (<data_dir>/spectra/<board>/*.h5) or a board capture
(<data_dir>/captures/*.h5). Plots are PNGs; by default written next to the file.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from lft3.config import load_station_config


def _out(path: str, suffix: str, out: str | None) -> Path:
    return Path(out) if out else Path(path).with_name(Path(path).stem + suffix + ".png")


def cmd_info(paths: list[str]) -> int:
    from lft3.products import captures, fbh5
    for p in paths:
        if captures.is_capture(p):
            c = captures.load(p)
            print(f"{p}: {c.kind} from {c.board}, {c.attrs.get('utc')}, data {c.data.shape} {c.data.dtype}")
            for k, v in sorted(c.attrs.items()):
                if k != "received_mask":
                    print(f"  {k}: {v}")
        else:
            i = fbh5.info(p)
            h = i["header"]
            print(f"{p}: FBH5 spectra {i['shape']} (time, if, channel)")
            print(f"  {i['fmin_mhz']:.6f} - {i['fmax_mhz']:.6f} MHz, foff {h['foff'] * 1e6:.3f} Hz, "
                  f"nfpc {h.get('nfpc')}, tsamp {h['tsamp']:.6f} s, {i['duration_s']:.1f} s total")
            print(f"  source {h.get('source_name')}, tstart MJD {h['tstart']:.8f}")
            for k, v in sorted(i["lft3"].items()):
                print(f"  {k}: {v}")
    return 0


def _latest(kind: str, board: str | None) -> Path | None:
    root = Path(load_station_config().data_dir) / kind
    files = sorted(root.rglob(f"{board or ''}*.h5"), key=lambda f: f.stat().st_mtime)
    return files[-1] if files else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="lft3-plot", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("info")
    p.add_argument("files", nargs="+")
    for name in ("spectrum", "waterfall"):
        p = sub.add_parser(name)
        p.add_argument("file")
        p.add_argument("--fmin", type=float)
        p.add_argument("--fmax", type=float)
        p.add_argument("--tmin", type=float, default=0.0)
        p.add_argument("--tmax", type=float)
        p.add_argument("--max-channels", type=int, default=8192 if name == "spectrum" else 2048,
                       help="average adjacent channels down to at most this many")
        p.add_argument("--flatten", action="store_true",
                       help="divide out the coarse-channel response (removes the 152.6 kHz ripple)")
        p.add_argument("-o", "--output")
        if name == "waterfall":
            p.add_argument("--vmin", type=float)
            p.add_argument("--vmax", type=float)
    p = sub.add_parser("capture")
    p.add_argument("file")
    p.add_argument("-o", "--output")
    p = sub.add_parser("latest", help="print the newest file")
    p.add_argument("--kind", choices=["spectra", "captures"], default="spectra")
    p.add_argument("--board")
    a = ap.parse_args(argv)

    if a.cmd == "info":
        return cmd_info(a.files)
    if a.cmd == "latest":
        f = _latest(a.kind, a.board)
        if f is None:
            print("no files", file=sys.stderr)
            return 1
        print(f)
        return 0

    from lft3.products import plot
    if a.cmd == "capture":
        from lft3.products import captures
        print(plot.capture(captures.load(a.file), _out(a.file, "", a.output)))
        return 0
    from lft3.products import fbh5
    s = fbh5.read_spectra(a.file, a.fmin, a.fmax, a.tmin, a.tmax, max_channels=a.max_channels, flatten=a.flatten)
    if a.cmd == "spectrum":
        print(plot.spectrum(s, _out(a.file, "_spectrum", a.output)))
    else:
        print(plot.waterfall(s, _out(a.file, "_waterfall", a.output), vmin=a.vmin, vmax=a.vmax))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
