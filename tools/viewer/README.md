# lft3-view: fast browser viewer for FBH5 spectra

A pan/zoom waterfall for the pipeline's filterbank-HDF5 files, served from the Spark and viewed in a browser.
Full-band files are 786 432 channels × thousands of spectra (13 GB per hour), so all data work happens in C++
on the Spark and the browser only receives a screen-sized tile.

```bash
make viewer                                   # builds in the lft3-runtime container (no root)
tools/viewer/build/lft3-view                  # prints http://<host>:8687/?token=...; open that URL
tools/viewer/build/lft3-view --data-dir /opt/lft3arth/data --port 8687 --bind 127.0.0.1   # local only
```

Options: `--data-dir` (default `/opt/lft3arth/data`), `--scan DIR` (repeatable, relative to the data dir; default
`spectra`, searched recursively for `*.h5`, so `bench/` and `reference/` are not listed; `--scan reference` or
`--scan .` to see those), `--bind` (default
`0.0.0.0`), `--port` (8687), `--threads` (default: all cores), `--triggers-log` (default
`<data-dir>/triggers.log`), `--rfi-file` (default `config/known_rfi.json` in this repo), `--no-token`, `--web-dir`.
The binary runs directly on the host (HDF5 is linked statically).

## Using it

* **Runs, not files.** The pipeline rotates files (`<prefix>_<UTC>_beam<N>.h5`). Files of one beam whose
  `lft3_timestamp` continues exactly, one integration after the previous file's last spectrum, are shown as
  one run with one timeline. Dotted lines mark where the next file starts, and the readout names the file under
  the cursor. A re-sync or lost blocks makes the writer start new files after a jump, and those become a new run.
  A `?file=` link may name the run or any of its files.
* The run list shows each run's directory under `spectra/` and its channel width. That separates the fine
  spectra (`spectra/<board>/`, 1.2 kHz) from the coarse quick-look product the new spectrometer writes alongside
  (`spectra/<board>/quicklook/`, 6144 channels at 152.6 kHz, `nfpc` = 1 so flatten does nothing there).
* **Drag** pans. The **wheel** zooms both axes. **Shift+wheel** zooms frequency only and **alt+wheel** time only.
  Double-click or `r` resets the view.
* **Reduce: max** keeps narrowband tones visible when many channels share a pixel, so use it for finding
  signals. **Mean** shows the average power level.
* **Flatten** divides out the 152.6 kHz coarse-channel response, as `lft3-plot --flatten` does
  (the median shape over coarse channels of the first 8 spectra).
* Levels are dB of the file's power units, auto-scaled to the 2–99.8 % range of each tile unless **lock levels** is on.
  Typing a min or max locks them.
* **Normalise per channel** divides every channel by its median over the spectra in the tile, computed in the
  browser, so toggling is instant. Steady spurs and the bandpass flatten out, and anything that changes in
  time stands out (intermittent RFI, tones switching on or off, drifting signals). Values are then dB
  relative to the channel median.
* **Strips.** The bottom strip is the spectrum of the row under the cursor, in blue, over the max of the view in
  grey. The right strip is the time series of the column under the cursor, in blue, over the mean power of
  the view per row in grey. Without the cursor on the image they show only the max and the mean.
* **Drift.** Ctrl/⌘+click two points. The readout bar gives Δf, Δt, the drift in Hz/s and in channels per
  spectrum. Esc clears it. Zoom in first: a point is only as precise as a screen pixel.
* **Marks** (checkbox):
  * trigger times from `triggers.log` (`lft3-board trigger` writes it) as dashed orange lines;
  * known spurs and RFI from `config/known_rfi.json`: 125 MHz PL-clock harmonics and the Spark's Wi-Fi channel 1
    for now. They are ticks above the waterfall, shaded in the spectrum strip, and named in the readout under the cursor.
    Add entries there (`combs`, `lines`, `bands`). The file is the start of the static spur/RFI mask.
* Red ticks on the left edge mark spectra with lost packets (`lft3_missing_packets`).
* Time increases downward (row 0 at the top), as in `lft3-plot` and blimpy, so drifting tones slant the same way
  in every tool. **Newest at top** flips the axis like a live spectrum analyser. The choice is remembered per browser.
* **Follow live** polls every 5 s. It adds new spectra, including rotations into the next file, and keeps the view
  at the newest. When a newer run of the same beam appears (after a re-sync), it switches to that run. Locking
  is not needed: files are opened without HDF5 file locks.
* Newest-at-top, normalise and marks are remembered per browser.

## How it is fast

Measured on the Spark (cold page cache, 20 threads; `lft3-view-bench FILE` reproduces these):

| Request | 1-hour file, old layout (3600 × 786 432, 11.3 GB) | 862 rows, old layout (2.7 GB) | same 862 rows, compressed layout (1.9 GB on disk) |
|---|---|---|---|
| Whole-file overview to 1024 × 2048 | 1.7 s (6.8 GB/s) | 0.44 s | 0.46 s |
| 1 % of the band, all spectra | 68 ms | 25 ms | 59 ms |
| 2048 channels, all spectra | 49 ms | 14 ms | 49 ms |
| 64 spectra, full band | 34 ms | 37 ms | 39 ms |

For comparison, h5py reads these files at about 0.5 GB/s.

* HDF5 (the library) is only used to read metadata and the chunk index (one `H5Dchunk_iter` pass: 6 ms for a
  562-spectrum file, so a file that is being written can be re-indexed every few seconds). The spectra are
  read with `pread()` on all cores. The library's own read path is single-threaded.
* Current files (writer since 2026-11-13) use chunks of 65 536 channels with byte shuffle + deflate 1.
  A view decodes (inflate, then unshuffle) only the chunks it covers, one chunk per task on all cores. Each
  thread keeps its last decoded chunk.
* Older files have deflate at level 0, which zlib emits as "stored" blocks: 65 535-byte payloads, each behind a
  5-byte header. The byte positions are therefore known, so a frequency zoom reads only the wanted bytes of
  each spectrum, with nothing to decode. Every block header touched is validated. Unfiltered chunks are read directly.
* The reduction to the screen (max and mean per pixel, optional flatten) is vectorised and runs in the same
  pass as the reads.
* Spark → browser: the page asks for `format=u16db`. That is dB quantised to 16 bits over the tile's own range
  (steps of ~0.1–0.6 millidecibel; the decode error is at most half a step). It is sent as two byte planes:
  the high bytes are run-length deflated and the low bytes, which are noise, are stored. The body is gzip, so
  the browser inflates it natively. A full-screen tile is 2.2–2.7× smaller than float32 (e.g. 6.2 MB → 2.3 MB)
  for ~10–25 ms of encoding. `X-Tile` reports `wire_bytes` and `f32_bytes`, and the status line shows both.
* `lft3-view-bench` also checks the reads bit for bit against `H5Dread`, and checks the reducer against a naive implementation.

## Layout

| File | What |
|---|---|
| `src/fbh5_file.{hh,cc}` | File index, metadata, parallel ranged row reads (`RowSource` interface) |
| `src/fbh5_sequence.{hh,cc}` | A run of contiguous files as one `RowSource`; the rules for chaining |
| `src/reduce.{hh,cc}` | Thread pool, max/mean reduction to a tile, coarse-channel response |
| `src/server.cc` | `lft3-view`: HTTP server (cpp-httplib), run catalog, API |
| `src/bench.cc` | `lft3-view-bench`: correctness checks + timings |
| `web/index.html` | The page: canvas waterfall, axes, strips, marks, drift, readout; no external libraries. `window.__lft3` exposes read-only state for UI tests |
| `../../config/known_rfi.json` | Known spurs/RFI drawn as marks |

API (token as `?token=` or `X-Token` header):
`GET /api/files` (runs), `GET /api/meta?file=RUN_OR_FILE`, `GET /api/overlays` (trigger lines, known RFI),
`GET /api/tile?file=RUN_OR_FILE&t0&t1&c0&c1&w&h[&ifno][&stat=max|mean][&flatten=1][&format=f32|u16db]`, which returns
float32 `[rows][cols]` (default) or u16db planes, with the geometry (and `db_lo`/`db_hi` for u16db) in the `X-Tile`
header. Both formats are gzip-encoded when the client sends `Accept-Encoding: gzip`.

## Not done yet

Planned (agreed 2026-11-13, not started):

* **Shareable view links and export.** Keep run, view, levels and options in the URL so a link opens exactly
  that view. Save the view as PNG, or as data (`.npy`) for Python.
* **Both beams side by side with linked zoom**, plus a difference or ratio view. This is the visual check of the
  input → beam/pol mapping with an injected tone.

Other gaps:

* There are no hyperseti hit overlays yet (there are no hits yet).
* **Hz-resolution files.** At Nf = 2^17 one spectrum is ~800 M channels (~3.2 GB) per beam, so an hour-long
  overview would read terabytes. The coarse product now exists (`quicklook/`, same names and timestamps). What
  is missing is the viewer using it for wide views and switching to the fine run when zoomed in.
* Normalisation uses the median over the rows of the current tile, i.e. of the reduced pixels, not of every
  spectrum.
* The tile encoding is not entropy coded: the low byte of each pixel is sent as is. Coding the noise properly
  would gain at most another ~1.8×.
