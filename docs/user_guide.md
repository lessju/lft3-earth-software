# LFT3-Earth on the DGX Spark: user guide

How to build, run, watch and use the real-time system. How it works inside is in `docs/runtime.md`; board
commands in detail are in `docs/operations.md` and `docs/monitoring_control.md`.

## 1. What runs

For every 100 GbE link from a board (a "stream", named `<board>-<link>`, e.g. `rfsoc0-link0`):

| Process | Container | Does | Writes |
|---|---|---|---|
| capture | `lft3-<stream>-capture` | receives the link (DAQIRI), assembles 6.7 ms blocks, keeps the last ~2 s of raw voltages in GPU memory (the "ring") | nothing (statistics in /dev/shm) |
| spectrometer | `lft3-<stream>-spectrometer` | 128-point FFT per board channel (1.19 kHz), Stokes I, 1 s integrations | fine FBH5, coarse FBH5, latest coarse spectrum |
| dump | `lft3-<stream>-dump` | waits for triggers, saves a window of raw voltages | GUPPI RAW |
| quicklook (optional) | `lft3-<stream>-quicklook` | coarse power at a faster cadence | latest snapshot only |
| live search | host process (search venv) | hyperseti on the spectrometer's spectra in GPU memory, a window every 32 s | hits CSV, per-window record; raw-voltage triggers when enabled |

Each process can fail or be restarted without affecting the capture. If a reader is too slow it loses
blocks (counted); the capture never loses data because of a reader.

## 2. One-time setup

```bash
make install                 # Python tools into /opt/lft3arth/lft3arth_venv
make pipeline                # builds the lft3-runtime image (first time) and pipeline/build/lft3-{capture,spectrometer,dump}
make viewer                  # lft3-view; linked into the venv's bin/ like the other commands
export PATH=/opt/lft3arth/lft3arth_venv/bin:$PATH   # all lft3-* commands (put it in ~/.profile)
make pipeline-test           # C++/CUDA unit tests on the GPU
make test && make lint       # Python tests
```

* You need docker-group membership; nothing else needs root. The capture container gets raw NIC access
  (`CAP_NET_RAW`, `CAP_IPC_LOCK`, `/dev/infiniband`; set `LFT3_CAPTURE_PRIVILEGED=1` to use `--privileged`
  instead), and readers run as you.
* The data directory (`data_dir` in `config/station.yaml`, `/opt/lft3arth/data`) must be writable by you or one
  of your groups.
* Keep the Spark for the pipeline: one link needs ~23 GB of the 128 GB unified memory and up to half the GPU
  at 2 links. Large GPU services (LLM servers) will cause overruns.

## 3. Configuration

Three kinds of file, all in git: `config/station.yaml` (the boards, the data directory, the observation in use),
`config/observations/<name>.yaml` (how the data is processed) and `config/boards/<board>.yaml` (one board and the
host side of its link).

### Observation config: how the data is processed

`config/observations/<name>.yaml` holds every processing choice; `config/station.yaml` names the one in use
(`observation: default`). `lft3-pipeline run` and `lft3-search` both read it.

```yaml
spectrometer:
  fine_channels_per_coarse: 128     # or resolution_hz: 1192 (nearest power of two); <= 1024 for now
  integration_s: 1.0
  coarse_channels: [0, 6144]
  record_fine: true
  coarse_product: true
  spectra_per_file: 3600
search:
  live: true                        # search the spectra in GPU memory during the run
  window_spectra: 64                # spectra per search -> drift step foff / 64 s = 18.6 Hz/s
  window_step_spectra: 32           # a search every 32 spectra (windows overlap by half)
  max_drift_hz_s: 200.0
  snr_threshold: 20.0
  dedoppler_plan: optimal
  drop_zero_drift: true             # stationary hits = local RFI
  flatten_coarse_ripple: true
  max_missing_fraction: 0.01        # skip windows with more packet loss than this
  ring_spectra: 64                  # GPU spectra kept for the search (6.3 MB each)
  hyperseti: {}                     # raw hyperseti overrides
triggering:                         # live hits -> raw-voltage dumps; off until the rules are agreed
  enabled: false
  min_snr: 50.0
  min_abs_drift_hz_s: 0.0           # set to >= one drift step before enabling (see §9)
  single_beam: true                 # not on hits both beams see
  skip_rfi_labels: true
  min_interval_s: 10.0
  pre_s: 0.5
  post_s: 0.5
dump: {enabled: true, max_window_s: 1.0}
ring: {seconds: 2.0}
```

Choosing resolution and integration (one stream on the Spark, measured; docs/runtime.md §6):

* Everything keeps real time **while recording** up to 1024 fine channels (149 Hz) at 0.25 s, or 256 at 0.1 s:
  the FBH5 writer manages ~160 MB/s (4 threads), so keep fine channels per board channel / integration time below
  ~4000 per second. Without recording (`record_fine: false`) everything up to 1024 at 0.1 s keeps up.
* The drift step is channel width / window length, and a signal drifting less than one step is dropped as
  stationary. With short integrations keep the window ~64 s long (`window_spectra` = 64 s / integration).
* Search sensitivity grows as √(fine channels × window duration).
* More than 1024 fine channels needs the Hz-resolution path (not built yet) and, for the search, longer
  integrations and `dedoppler_plan: stepped`.

Another observation: copy the file, then `lft3-pipeline run --observation NAME`. Options such as
`--fft-length`, `--integration`, `--no-search` override single values for one run. The run directory gets the
effective `observation.yaml`. Invalid combinations are refused at start (for example a drift range that never
crosses one channel within a window, or an FFT length the spectrometer cannot do yet).

### Board config: the host side of a link

`config/boards/<board>.yaml`, section `receiver`:

```yaml
receiver:
  links:
    - name: link0
      interface: '0000:01:00.0'     # ConnectX PCI function (enp1s0f0np0)
      master_core: 5                # DAQIRI master
      worker_core: 6                # DAQIRI RX worker (busy-polls this core at 100 %)
      processing_core: 7            # capture main thread
      beam_offset: 0                # global beam numbers of this link: 0, 1
      reader_cpus: '8,9,0-4'        # spectrometer / dump cpuset; the search uses its non-X925 cores
    - name: link1                   # a second output: only for a future two-link firmware
      interface: '0000:01:00.1'     # enp1s0f1np1: check with `ls /sys/bus/pci/devices/*/net`
      master_core: 15
      worker_core: 16
      processing_core: 17
      beam_offset: 2                # beams 2, 3
      reader_cpus: '18,19,10-14'
```

The station has one link per board; a second board on the same Spark uses the other QSFP port, with its own
`receiver.links` in its own YAML. Each link needs its own three cores. The Cortex-X925 cores are 5–9 and 15–19. Optional per link: `udp_port`
(default `network.channel_port`) and `source_ip` (default `network.source_ip`). The input → beam/pol mapping is
`adc.inputs`, the exact NCO `adc.nco_mhz`.

## 4. Running

Start the board first (`lft3-board start`; see `docs/operations.md`), then:

```bash
lft3-pipeline run                                   # all links of the station's first board, until Ctrl-C
lft3-pipeline run -b rfsoc0 --links link0           # one link only
lft3-pipeline run --duration 3600                   # one hour
lft3-pipeline run --integration 10                  # 10 s spectra
lft3-pipeline run --fft-length 1024 --channels 2048:1024   # 149 Hz channels on board channels 2048-3071
lft3-pipeline run --quicklook 0.1                   # + 10 Hz coarse snapshots in <run_dir>/latest_fast/
lft3-pipeline run --metrics-port 9110               # + Prometheus metrics at http://<spark>:9110/metrics
lft3-pipeline run --no-dump --no-record             # nothing written (link test)
lft3-pipeline run --dry-run                         # print the docker commands only
```

Without the board:

```bash
lft3-pipeline run --source synthetic                # noise + a tone (board channel 4000, beam 0 Y), real time
lft3-pipeline run --source synthetic --fast --lossless --duration 60   # as fast as possible, no data lost
lft3-pipeline run --source file --file cap.pcap --loop                 # replay a tcpdump capture
```

**Stop** with Ctrl-C or `--duration`, or from another terminal with `lft3-pipeline stop`. The search stops first,
then the readers, which close their files, then the capture. Only one run per board at a time.

**Healthy** looks like this. A status line every 10 s per link:

```
rfsoc0-link0: running  60.84 Gb/s | blocks 4600 (partial 0, skipped 0) | missing packets 0 late 0 |
              spectrometer(lag 0, over 0, out 29) dump(lag 0, over 0, out 0)
rfsoc0-link0-spectra: running spectra ring | spectra 29 | search(lag 0, over 0, windows 0)
```

* `60.8 Gb/s` = full band.
* `partial` / `skipped` / `missing packets` flat = no loss.
* `lag` near 0 and `over` (overruns) 0 = readers keep up.
* `out` = spectra written (spectrometer) or dumps done; `windows` = search windows searched (one every 32 s once
  the first 64 spectra are in).

## 5. Watching a run

```bash
lft3-pipeline status            # every ring on this host: rate, loss, readers, ring size, obs id
lft3-pipeline status --json     # the same as JSON
```

Each run has `<data_dir>/runs/<UTC>_<board>_<source>/`:

| File | Contents |
|---|---|
| `plan.txt` | the exact commands (`docker run …`, and the search's host command) |
| `observation.yaml` | the observation config of this run, with command-line overrides applied |
| `<stream>-capture.log` | capture log; a summary line every 5 s (rate, blocks, missing, late, no-slot, readers) |
| `<stream>-spectrometer.log`, `-dump.log` | reader logs (files opened/closed, overruns, dumps) |
| `<stream>-search.log` | live search: windows, hits, triggers, every 30 s a summary |
| `status.jsonl` | the status lines as JSON (time series of all counters) |
| `nic-<link>.csv` | NIC counters every 5 s (network source); drops / pauses are also printed |
| `latest/<board>_latest.{json,f32}` | the newest 1 s coarse spectrum, float32 `[beam][6144]`, axes in the JSON |

Metrics (`--metrics-port`) include `lft3_capture_rate_gbps`, `lft3_capture_packets_missing_total`,
`lft3_reader_lag_blocks`, `lft3_reader_overruns_total`, … labelled by stream and reader.

## 6. Triggered raw-voltage dumps

```bash
lft3-pipeline trigger                       # now, 0.5 s before + 0.5 s after, every link of the board
lft3-pipeline trigger --pre 0.8 --post 0.2 --label "FRB candidate"
```

* Writes `<data_dir>/dumps/<board>/<board>_<UTC>_beam<N>.0000.raw` (GUPPI RAW: 6144 channels, 2 pols,
  8-bit complex, 6.5536 µs samples) plus a `.json` summary, and appends a line to `<data_dir>/triggers.log`.
* The live search can send the same triggers itself when enabled (§9).
* The window must still be in the ring: about 2 s back, set by `--ring-seconds`. The longest window is set by
  `--dump-seconds` (default 1 s = 7.5 GB per link, ~3 s to write).
* One dump at a time; at most about one every 10 s.
* Read the files with blimpy (`GuppiRaw`), rawspec or `numpy.fromfile` (header = 80-byte cards up to `END`, then
  `BLOCSIZE` bytes `[channel][time][pol][re,im]`).

## 7. The products

| Product | Where | Shape | Resolution |
|---|---|---|---|
| Fine spectra | `<data_dir>/spectra/<board>/<board>_<UTC>_beam<N>.h5` | `[time, 1, 786432]` per beam | 1.192 kHz, 1 s |
| Coarse spectra | `<data_dir>/spectra/<board>/quicklook/…_beam<N>.h5` | `[time, 1, 6144]` per beam | 152.6 kHz, 1 s |
| Raw voltages | `<data_dir>/dumps/<board>/…_beam<N>.0000.raw` | GUPPI RAW | 6.5536 µs |

The FBH5 files open with `lft3-plot`, `lft3-view` (browser), blimpy, turboSETI and hyperseti.

```bash
lft3-plot latest                                       # newest file
lft3-plot waterfall FILE --fmin 2400 --fmax 2425 --flatten
lft3-view                                              # pan/zoom in a browser (make viewer once)
```

Important attributes (on `/data` and the root):
* `fch1`, `foff`, `tsamp`, `tstart` (MJD), `nfpc`;
* `/lft3_timestamp`: the first spectrum counter of each integration;
* `/lft3_missing_packets`: packets missing within each integration (zero-filled);
* `lft3_obs_id`, `lft3_stream`, `lft3_config_hash`, `lft3_software`;
* `lft3_unix_time_at_t0` and `lft3_time_reference`: the time is estimated from packet arrival (~ms) until the
  firmware has a PPS-aligned sync.

Files rotate every hour (3600 spectra). A board re-sync, or a gap of whole integrations, starts new files.
Disk use: fine spectra ≈ 16 GB/hour per link (compressed); coarse spectra ~1/128 of that; each 1 s dump ≈ 7.5 GB.

## 8. Reading the live data from your own code

Python (any version ≥ 3.8; CuPy for the GPU data):

```python
from lft3.ring import Ring, list_streams
print(list_streams())                      # ['rfsoc0-link0']
r = Ring("rfsoc0-link0")
print(r.status())                          # rates, counters, readers
v = r.voltages()                           # CuPy view of the raw-voltage ring (read-only)
```

`lft3.ring.RingReader` registers a Python reader in a ring's reader table, so it shows in `lft3-pipeline status`
and can be lossless; the live search uses it on the spectra ring (`Ring("rfsoc0-link0-spectra").spectra()`).
See `docs/runtime.md` §5 for complete reader loops (C++ and Python).

## 9. Searching the spectra (hyperseti)

The narrowband drift search uses hyperseti in its own Python environment (CuPy for CUDA 13 needs Python ≥ 3.10):
`scripts/setup_search_env.sh` once (→ `/opt/lft3arth/search_venv`; `make link-tools` puts `lft3-search` on the
venv's PATH). Settings come from the observation config (`search:`).

**Live (default during a run).** With `search.live: true`, `lft3-pipeline run` also starts a live search per
link. It reads the 1 s spectra straight from GPU memory (the spectrometer's spectra ring), so it adds almost no
memory traffic, and searches the last 64 spectra every 32 s:

* Hits: `<data_dir>/hits/<board>/live_<stream>_<UTC>.csv`: window, start time, beam, frequency, drift (Hz/s),
  S/N, channel, `label`, obs id, stream. Written within a second of a window's last spectrum.
* Per window: `live_<stream>_<UTC>.windows.jsonl` (searched or skipped and why, hits, search time, latency).
* `lft3-pipeline status` shows it as `search(lag, over, windows)` on the `<stream>-spectra` ring; its log is
  `<run_dir>/<stream>-search.log`. `--no-search` turns it off for one run.
* Windows with more than 1 % missing packets are skipped. A gap in the spectra restarts the window.
* Hits with zero drift (stationary, i.e. local interference) are dropped; the window record counts them.
* **Triggers** (off by default; `--search-triggers` for one run, or `triggering.enabled` in the observation): the
  strongest new hit with S/N ≥ 50 that is not labelled RFI and not seen by both beams triggers a raw-voltage dump
  of its board (`lft3-dump`), at most every 10 s. The dump covers the moment the hit is found, not the
  window itself (the voltage ring holds ~2 s), so it records signals that are still there. Triggers are in
  `<data_dir>/triggers.log` with the hit in the label, and in the window record.
  On live data the strongest hits left after the zero-drift drop are stationary signals measured one step off
  zero (±18.6 Hz/s), so set `min_abs_drift_hz_s` to at least one step before enabling triggers.
* Drift resolution = channel width / window length (18.6 Hz/s for 1.19 kHz and 64 s), and reported drifts are
  good to about one step. S/N ≥ 20 by default.

**Files.** `lft3-search` searches finished FBH5 files in the same windows (identical hits for the same spectra):

```bash
lft3-search FILE.h5                   # one file (one beam): hits in <dir>/hits/<file>.csv
lft3-search --watch [--once]          # every finished file in <data_dir>/spectra/<board>/
lft3-search FILE.h5 --whole-file      # the whole file as one window (more sensitive to slow drifts)
lft3-search --live --stream rfsoc0-link0   # what lft3-pipeline starts
```

* Hits within two fine channels of a board spur (every 125 MHz) or in Wi-Fi channel 1 are labelled `rfi:...`,
  not removed.
* A file search reads ~1 GB files and loads the memory bus: don't run `--watch` or `--whole-file` on the capture
  Spark during a live capture, where it can make the NIC drop packets. The live search does not have this problem.

## 10. Troubleshooting

| Symptom | Likely cause / what to do |
|---|---|
| `already running: lft3-rfsoc0-link0-…` | a previous run is still up: `lft3-pipeline stop -b rfsoc0` |
| `the capture did not create its ring` | see `<stream>-capture.log`: DAQIRI failed (wrong `interface`, NIC used by another process, RDMA devices not passed in: try `LFT3_CAPTURE_PRIVILEGED=1`) or the binaries are missing (`make pipeline`) |
| rate 0 Gb/s, blocks not increasing | board not streaming: `lft3-board status`; destination MAC: `lft3-board set-dest-mac`; link: `ip link` |
| rate ≈ 60.8 Gb/s but `missing packets` rising | if `nic-<link>.csv` shows `rx_out_of_buffer` / `rx_discards_phy`, the host lost them; if those are flat, the packets never arrived (board or link) |
| `missing packets` rising with `rx_discards_phy` in `nic-<link>.csv` | the NIC could not write to host memory fast enough: another heavy job (search, large copies) is loading the memory bus; stop it |
| reader `over` (overruns) rising, `lag` large | the GPU or memory is overloaded: other GPU jobs, too many links (§11), or a slow disk under the writer; the capture itself is unaffected |
| `WARNING live search … exited` | see `<stream>-search.log`; usually the search venv is missing (`scripts/setup_search_env.sh`). The run goes on without the search |
| search `lag` rising, windows late | a window takes longer than 32 s to search: finer channels or shorter integrations than the search can handle (docs/runtime.md §6); use a longer window step or turn off the search |
| `Permission denied` in a reader log | the data directory is not writable by you or your groups |
| `no ring … within 30 s` (reader) | the capture died or uses another stream name; check the capture log |
| dump reply `error no data in the ring yet` | trigger sent before the first block; wait a second |
| missing first spectrum | integrations are aligned to whole seconds of the board's time base; the first (partial) second is skipped by design |

## 11. Capacity of one Spark

Measured (`pipeline/bench/capacity/`; details in `docs/runtime.md` §6):

| Streams (links) | Beams | Fits? | GPU used |
|---|---|---|---|
| 2 (two boards) | 4 | yes, with room for the live search | 46 % |
| 4 (four boards) | 8 | compute yes, but needs 4 network links into the Spark (switch, unverified) | 87 % |
| 5 | 10 | passes the synthetic test, no headroom for the NICs | 96 % |
| 6 | 12 | no | — |

The station is 6 boards with **one link each** (6 streams). On Sparks that is **one Spark per 2 boards (3 Sparks)**,
using both QSFP ports. Production servers (discrete GPUs) are sized in `docs/sizing.md`.

Two cautions:
* Other heavy jobs on the capture host can make the NIC drop packets through memory-bus contention, even when the
  GPU is not full (§9).
* At ~1 Hz resolution (Nf = 2^17), one Spark can process about one full-band stream; see `docs/runtime.md` §2
  (rawspec comparison).
