"""Board captures in HDF5: one file per capture.

    /data            [input, sample-or-bin]  complex64 (ADC) or float64 power (FFT snapshot)
    /received_mask   [packet] bool            (DDR dumps only: which dump packets arrived)
    root attributes: lft3_kind, board, time (UNIX), UTC, and the capture's own attributes
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

import h5py
import numpy as np

from lft3.board.capture import Capture

KINDS = ("fft_snapshot", "adc_snapshot", "adc_dump")


def default_path(data_dir: str, cap: Capture) -> Path:
    stamp = _dt.datetime.utcfromtimestamp(cap.time).strftime("%Y%m%dT%H%M%S")
    return Path(data_dir) / "captures" / f"{cap.board}_{cap.kind}_{stamp}.h5"


def save(cap: Capture, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    attrs = dict(cap.attrs)
    mask = attrs.pop("received_mask", None)
    with h5py.File(path, "w") as h:
        h.create_dataset("data", data=cap.data, compression="gzip", compression_opts=1)
        if mask is not None:
            h.create_dataset("received_mask", data=np.asarray(mask, bool))
        h.attrs["lft3_kind"] = cap.kind
        h.attrs["board"] = cap.board
        h.attrs["time"] = cap.time
        h.attrs["utc"] = _dt.datetime.utcfromtimestamp(cap.time).isoformat() + "Z"
        for k, v in attrs.items():
            h.attrs[k] = json.dumps(v) if isinstance(v, (list, dict)) else v
    return path


def load(path: str | Path) -> Capture:
    with h5py.File(path, "r") as h:
        kind = str(h.attrs["lft3_kind"])
        attrs = {}
        for k, v in h.attrs.items():
            if k in ("lft3_kind", "board", "time"):
                continue
            if isinstance(v, bytes):
                v = v.decode()
            if isinstance(v, str) and v[:1] in "[{":
                try:
                    v = json.loads(v)
                except ValueError:
                    pass
            attrs[k] = v.item() if isinstance(v, np.generic) else v
        if "received_mask" in h:
            attrs["received_mask"] = h["received_mask"][()]
        return Capture(kind, str(h.attrs["board"]), float(h.attrs["time"]), h["data"][()], attrs)


def is_capture(path: str | Path) -> bool:
    try:
        with h5py.File(path, "r") as h:
            return "lft3_kind" in h.attrs
    except OSError:
        return False
