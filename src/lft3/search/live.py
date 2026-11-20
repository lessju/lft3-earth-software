"""Live narrowband drift search on the spectra ring (``lft3-search --live STREAM``, started by lft3-pipeline).

lft3-spectrometer ``--spectra-ring N`` copies every integrated fine spectrum into the GPU ring
``<stream>-spectra``. This reader keeps the last ``window_spectra`` spectra in its own GPU buffer (each
spectrum is copied once, device to device) and searches every window of the grid in
:mod:`lft3.search.core` as soon as its last spectrum arrives: no files, no host copies.

* Gaps (a discontinuity flag, a jump in the integration number, or a spectrum lost to lag) restart the buffer;
  a window with more than ``max_missing_fraction`` missing packets is skipped and logged.
* Output in ``--out`` (default <data_dir>/hits/<board>/):
  ``live_<stream>_<UTC>.csv`` hits (core.HIT_COLUMNS) and ``live_<stream>_<UTC>.windows.jsonl``, one line per
  window (searched or skipped, hits, search time, lag): the real-time record.
* ``--lossless`` registers as a lossless reader: with synthetic / file sources and ``lft3-pipeline run
  --lossless`` the spectrometer (and so the capture) waits for the search: every window is searched.
* ``--save-windows DIR --save-every K`` writes every K-th window's spectra (before flattening) as .npy, to
  check them bit for bit against the FBH5 files.
* Zero-drift hits (stationary: local RFI) are dropped after merging (``search.drop_zero_drift``); the window record
  counts them.
* Triggering (observation ``triggering:``, off by default): after each window, the strongest hit with S/N >=
  ``min_snr`` that is not labelled RFI and (``single_beam``) not also seen in the other beam within 2 channels
  sends ``trigger now PRE POST label`` to the stream's lft3-dump, at most once per ``min_interval_s``, and is
  appended to ``<data_dir>/triggers.log``. The dump covers the moment of the decision: the voltage ring holds
  ~2 s, so the window's own voltages are long gone; it catches signals that persist.

Runs in the search venv (Python >= 3.10, CuPy, hyperseti); this module is Python 3.8 syntax.
"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import os
import signal
import sys
import time
from pathlib import Path

from lft3.config import load_observation_config, load_station_config
from lft3.ring import FLAG_NEW_RUN, RingReader, send_dump_command, spectra_stream
from lft3.search.core import HIT_COLUMNS, Window, axes_from_nco, hit_rows, rfi_label, searcher_for, window_ends_at

_stop = False


def _on_signal(signum, frame) -> None:  # noqa: ARG001
    global _stop
    _stop = True


class LiveSearch:
    def __init__(self, stream: str, settings, out_dir: Path, beam_offset: int = 0, lossless: bool = False,
                 save_dir: Path | None = None, save_every: int = 0, attach_timeout: float = 60.0,
                 log=print, trigger=None, board: str = "", triggers_log: Path | None = None):
        import cupy as cp

        self.cp = cp
        self.s = settings
        self.log = log
        self.stream = stream
        self.beam_offset = beam_offset
        self.reader = RingReader(spectra_stream(stream), "search", lossless=lossless, timeout=attach_timeout)
        st = self.reader.ring.status()
        if st.kind != "spectra":
            raise ValueError(f"{spectra_stream(stream)} is not a spectra ring")
        self.st = st
        self.ring = self.reader.ring.spectra()              # CuPy (slots, 2, F) view of the ring
        self.n, self.step = settings.window_spectra, settings.window_step_spectra
        self.channels = st.channels_out
        self.buf = cp.zeros((self.n, 2, self.channels), cp.float32)
        self.meta = {}                                      # integration -> SlotMeta, for spectra in buf
        self.run_len = 0                                    # consecutive integrations in buf
        self.last = None                                    # last integration copied
        self.restarts = None
        self.searcher = searcher_for(settings)
        self.trigger = trigger
        self.board = board or stream.rsplit("-", 1)[0]
        self.triggers_log = triggers_log
        self.last_trigger = 0.0
        packets_per_spectrum = st.channel_count // 1024
        self.packets_per_integration = st.spectra_per_block * packets_per_spectrum
        self.save_dir, self.save_every = save_dir, save_every
        if save_dir:
            save_dir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
        self.hits_path = out_dir / f"live_{stream}_{stamp}.csv"
        self.windows_path = out_dir / f"live_{stream}_{stamp}.windows.jsonl"
        self._hits = open(self.hits_path, "w", newline="")
        self._csv = csv.writer(self._hits)
        self._csv.writerow(HIT_COLUMNS)
        self._hits.flush()
        self._windows = open(self.windows_path, "w")
        self.stats = {"spectra": 0, "windows": 0, "skipped": 0, "hits": 0, "overruns": 0, "restarts": 0,
                      "search_s_max": 0.0, "search_s_last": 0.0, "zero_drift_dropped": 0, "triggers": 0}
        log(f"live search on {spectra_stream(stream)}: {self.channels} channels x 2 beams, foff "
            f"{st.foff_mhz * 1e6:.2f} Hz, tsamp {st.tsamp_s:.4f} s; windows of {self.n} every {self.step}, "
            f"|drift| <= {settings.max_drift_hz_s:g} Hz/s, S/N >= {settings.snr_threshold:g}; ring {st.slots} spectra"
            f"{' (lossless)' if lossless else ''} -> {self.hits_path}")

    def _reset(self, why: str) -> None:
        if self.run_len:
            self.stats["restarts"] += 1
            self.log(f"window buffer restarted: {why}")
        self.run_len = 0
        self.meta.clear()
        self.last = None

    def _axes(self, m) -> tuple[float, float]:
        if self.st.foff_mhz > 0:
            return self.st.fch1_mhz, self.st.foff_mhz
        return axes_from_nco(float(m.nco[0]), self.st.channel_start, self.st.fine_channels_per_coarse)

    def run(self) -> int:
        rd = self.reader
        seq = rd.cursor
        last_log = time.time()
        while not _stop:
            if time.time() - last_log >= 30:
                last_log = time.time()
                s = self.stats
                self.log(f"spectra {s['spectra']}, windows {s['windows']} (skipped {s['skipped']}), hits {s['hits']}, "
                         f"search {s['search_s_last']:.2f} s (max {s['search_s_max']:.2f}), lag "
                         f"{max(0, rd.ring.head - seq)}, overruns {s['overruns']}")
            if not rd.wait(seq, 1.0):
                if rd.ring.status().state == "stopped":
                    self.log("spectrometer stopped")
                    break
                rd.consumed(seq)
                continue
            oldest = rd.oldest_readable()
            if seq < oldest:
                self._overrun(oldest - seq, "fell behind the spectra ring")
                seq = oldest
                continue
            got = rd.begin(seq)
            if got is None:
                self._overrun(1, "spectrum overwritten")
                seq += 1
                continue
            meta, slot = got
            t0 = time.perf_counter()
            k = meta.block_index
            if self.restarts is not None and meta.restarts != self.restarts:
                self._reset("stream restart")
            elif meta.flags & FLAG_NEW_RUN and self.last is not None:
                self._reset("discontinuity before this spectrum")
            elif self.last is not None and k != self.last + 1:
                self._reset(f"integration {self.last} -> {k}")
            self.restarts = meta.restarts
            self.buf[k % self.n] = self.ring[slot]
            self.cp.cuda.runtime.deviceSynchronize()
            if not rd.still_valid(seq):
                self._overrun(1, "spectrum overwritten while copied")
                seq += 1
                continue
            self.meta[k] = meta
            self.meta.pop(k - self.n, None)
            self.last = k
            self.run_len += 1
            self.stats["spectra"] += 1
            rd.add(0, 1)
            w_index = window_ends_at(k, self.n, self.step)
            if w_index is not None and self.run_len >= self.n:
                self._search(w_index, k - self.n + 1, seq)
            seq += 1
            rd.consumed(seq, int((time.perf_counter() - t0) * 1e9), outputs=self.stats["windows"])
        self.close()
        return 0

    def _overrun(self, n: int, why: str) -> None:
        self.stats["overruns"] += n
        self.reader.add(1, n)
        self._reset(why)

    def _search(self, w_index: int, first: int, seq: int) -> None:
        cp = self.cp
        metas = [self.meta[first + i] for i in range(self.n)]
        m0 = metas[0]
        fch1, foff = self._axes(m0)
        missing = sum(m.missing_packets for m in metas) / float(self.n * self.packets_per_integration)
        start_unix = m0.unix_time_at_t0 + m0.t0 / 152587.890625
        w = Window(w_index, first, fch1, foff, self.st.tsamp_s, start_unix, self.st.fine_channels_per_coarse,
                   missing)
        rec = {"window": w_index, "first_integration": first, "start_unix": round(start_unix, 6),
               "start_utc": w.start_utc, "missing_fraction": missing,
               "lag_spectra": max(0, self.reader.ring.head - seq - 1)}
        if missing > self.s.max_missing_fraction:
            self.stats["skipped"] += 1
            rec.update(searched=False, reason="missing data")
            self._record(rec)
            return
        order = [(first + i) % self.n for i in range(self.n)]
        data = self.buf[order]                                   # [N, 2, F] in time order (a copy)
        if self.save_dir and self.save_every and w_index % self.save_every == 0:
            path = self.save_dir / f"{self.stream}_window{w_index}.npy"
            cp.save(str(path), data)
            rec["saved"] = str(path)
        t0 = time.perf_counter()
        n_hits = dropped = 0
        per_beam = []
        for b in range(2):
            hits = self.searcher.search_beam(data[:, b, :], w)
            dropped += self.searcher.dropped
            per_beam.append(hits)
            beam = self.beam_offset + b
            rows = hit_rows(hits, w, beam, "live", self.st.obs_id, self.stream)
            self._csv.writerows(rows)
            n_hits += len(rows)
        self._hits.flush()
        dt = time.perf_counter() - t0
        self.stats["zero_drift_dropped"] += dropped
        rec["zero_drift_dropped"] = dropped
        if self.trigger is not None and self.trigger.enabled:
            fired = self._maybe_trigger(w, per_beam)
            if fired:
                rec["trigger"] = fired
        self.stats["windows"] += 1
        self.stats["hits"] += n_hits
        self.stats["search_s_last"] = dt
        self.stats["search_s_max"] = max(self.stats["search_s_max"], dt)
        rec.update(searched=True, hits=n_hits, search_s=round(dt, 4),
                   latency_s=round(time.time() - (start_unix + self.n * self.st.tsamp_s), 3))
        self._record(rec)

    def trigger_candidate(self, w: Window, per_beam: list):
        """(beam index 0/1, hit) of the strongest hit that passes the trigger rules, or None."""
        tr = self.trigger
        best = None
        for b, hits in enumerate(per_beam):
            other = per_beam[1 - b]
            for h in hits:
                if h["snr"] < tr.min_snr or abs(h["drift_rate_hz_s"]) < tr.min_abs_drift_hz_s:
                    continue
                if tr.skip_rfi_labels and rfi_label(h["f_start_mhz"], w.foff_mhz):
                    continue
                if tr.single_beam and any(abs(o["channel"] - h["channel"]) <= 2 for o in other):
                    continue
                if best is None or h["snr"] > best[1]["snr"]:
                    best = (b, h)
        return best

    def _maybe_trigger(self, w: Window, per_beam: list) -> dict | None:
        cand = self.trigger_candidate(w, per_beam)
        now = time.time()
        if cand is None or now - self.last_trigger < self.trigger.min_interval_s:
            return None
        b, h = cand
        beam = self.beam_offset + b
        label = (f"search:{self.stream}:w{w.index}:beam{beam}:{h['f_start_mhz']:.6f}MHz:"
                 f"{h['drift_rate_hz_s']:.3g}Hz/s:snr{h['snr']:.1f}")
        tr = self.trigger
        try:
            reply = send_dump_command(self.stream, f"trigger {now:.6f} {tr.pre_s:g} {tr.post_s:g} {label}")
        except OSError as exc:
            reply = f"error {exc} (is lft3-dump running?)"
        self.last_trigger = now
        ok = reply.startswith("ok")
        self.stats["triggers"] += ok
        self.log(f"trigger {'sent' if ok else 'FAILED'}: {label} -> {reply}")
        if self.triggers_log:
            stamp = _dt.datetime.fromtimestamp(now, _dt.timezone.utc).isoformat(timespec="milliseconds")
            with open(self.triggers_log, "a") as f:
                f.write(f"{stamp} dump {self.board} pre={tr.pre_s:g} post={tr.post_s:g} {label} -> {reply}\n")
        return {"unix": round(now, 6), "label": label, "reply": reply}

    def _record(self, rec: dict) -> None:
        self._windows.write(json.dumps(rec) + "\n")
        self._windows.flush()

    def close(self) -> None:
        s = self.stats
        self.log(f"done: spectra {s['spectra']}, windows {s['windows']} (skipped {s['skipped']}), hits {s['hits']} "
                 f"({s['zero_drift_dropped']} zero-drift dropped), triggers {s['triggers']}, overruns {s['overruns']}, "
                 f"max search {s['search_s_max']:.2f} s")
        for f in (self._hits, self._windows):
            if not f.closed:
                f.close()
        self.reader.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="lft3-search --live", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stream", required=True, help="voltage stream, e.g. rfsoc0-link0 (reads <stream>-spectra)")
    ap.add_argument("--observation", help="observation config name or path (default: the station's)")
    ap.add_argument("-s", "--station", default="station")
    ap.add_argument("--out", type=Path, help="hits directory (default <data_dir>/hits/<board>)")
    ap.add_argument("--prefix", default="", help="board name (triggers.log)")
    ap.add_argument("--triggers-log", type=Path, help="append triggers here (default <data_dir>/triggers.log)")
    ap.add_argument("--beam-offset", type=int, default=0)
    ap.add_argument("--lossless", action="store_true")
    ap.add_argument("--pid-file")
    ap.add_argument("--attach-timeout", type=float, default=60.0)
    ap.add_argument("--save-windows", type=Path)
    ap.add_argument("--save-every", type=int, default=0)
    a = ap.parse_args(argv)

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    if a.observation:
        obs = load_observation_config(a.observation)
        out = a.out
    else:
        st = load_station_config(a.station)
        obs = st.observation_config()
        out = a.out
    if out is None:
        st = load_station_config(a.station)
        out = Path(st.data_dir) / "hits" / a.stream.split("-")[0]
    if a.pid_file:
        Path(a.pid_file).write_text(f"{os.getpid()}\n")

    def log(msg: str) -> None:
        print(f"{_dt.datetime.utcnow():%Y-%m-%d %H:%M:%S} search:{a.stream} {msg}", flush=True)

    try:
        if obs.trigger.enabled:
            log(f"triggering ON: S/N >= {obs.trigger.min_snr:g}, single beam {obs.trigger.single_beam}, "
                f"every >= {obs.trigger.min_interval_s:g} s, dump -{obs.trigger.pre_s:g}/+{obs.trigger.post_s:g} s")
        tlog = a.triggers_log or out.parents[1] / "triggers.log"
        live = LiveSearch(a.stream, obs.search, out, a.beam_offset, a.lossless, a.save_windows, a.save_every,
                          a.attach_timeout, log, trigger=obs.trigger, board=a.prefix, triggers_log=tlog)
        return live.run()
    except Exception as exc:  # noqa: BLE001 - report and exit non-zero; lft3-pipeline logs it
        log(f"ERROR {type(exc).__name__}: {exc}")
        return 1
    finally:
        if a.pid_file:
            try:
                Path(a.pid_file).unlink()
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
