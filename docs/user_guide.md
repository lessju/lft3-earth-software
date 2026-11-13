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

Each process can fail or be restarted without affecting the capture. If a reader is too slow it loses
blocks (counted); the capture never loses data because of a reader.

## 2. One-time setup

```bash
make install                 # Python tools into /opt/lft3arth/lft3arth_venv
make pipeline                # builds the lft3-runtime image (first time) and pipeline/build/lft3-{capture,spectrometer,dump}
make pipeline-test           # C++/CUDA unit tests on the GPU
make test && make lint       # Python tests
```

* You need docker-group membership; nothing else needs root. The capture container runs privileged
  (raw NIC access) and readers run as you.
* The data directory (`data_dir` in `config/station.yaml`, `/opt/lft3arth/data`) must be writable by you or one
  of your groups.
* Keep the Spark for the pipeline: one link needs ~23 GB of the 128 GB unified memory and up to half the GPU
  at 2 links. Large GPU services (LLM servers) will cause overruns.

## 3. Configuration

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
      reader_cpus: '8,9,0-4'        # cpuset for this link's spectrometer / dump
    - name: link1                   # the second link, when cabled
      interface: '0000:01:00.1'     # enp1s0f1np1: check with `ls /sys/bus/pci/devices/*/net`
      master_core: 15
      worker_core: 16
      processing_core: 17
      beam_offset: 2                # beams 2, 3
      reader_cpus: '18,19,10-14'
```

Each link needs its own three cores. The Cortex-X925 cores are 5–9 and 15–19. Optional per link: `udp_port`
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

**Stop** with Ctrl-C or `--duration`, or from another terminal with `lft3-pipeline stop`. Readers stop first
and close their files, then the capture. Only one run per board at a time.

**Healthy** looks like this. A status line every 10 s per link:

```
rfsoc0-link0: running  60.84 Gb/s | blocks 4600 (partial 0, skipped 0) | missing packets 0 late 0 |
              spectrometer(lag 0, over 0, out 29) dump(lag 0, over 0, out 0)
```

* `60.8 Gb/s` = full band.
* `partial` / `skipped` / `missing packets` flat = no loss.
* `lag` near 0 and `over` (overruns) 0 = readers keep up.
* `out` = spectra written (spectrometer) or dumps done.

## 5. Watching a run

```bash
lft3-pipeline status            # every ring on this host: rate, loss, readers, ring size, obs id
lft3-pipeline status --json     # the same as JSON
```

Each run has `<data_dir>/runs/<UTC>_<board>_<source>/`:

| File | Contents |
|---|---|
| `plan.txt` | the exact `docker run` commands |
| `<stream>-capture.log` | capture log; a summary line every 5 s (rate, blocks, missing, late, no-slot, readers) |
| `<stream>-spectrometer.log`, `-dump.log` | reader logs (files opened/closed, overruns, dumps) |
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
tools/viewer/build/lft3-view                           # pan/zoom in a browser (make viewer once)
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

See `docs/runtime.md` §5 for a complete reader loop (C++ or Python).

## 9. Troubleshooting

| Symptom | Likely cause / what to do |
|---|---|
| `already running: lft3-rfsoc0-link0-…` | a previous run is still up: `lft3-pipeline stop -b rfsoc0` |
| `the capture did not create its ring` | see `<stream>-capture.log`: DAQIRI failed (wrong `interface`, NIC used by another process, no privileged container) or the binaries are missing (`make pipeline`) |
| rate 0 Gb/s, blocks not increasing | board not streaming: `lft3-board status`; destination MAC: `lft3-board set-dest-mac`; link: `ip link` |
| rate ≈ 60.8 Gb/s but `missing packets` rising | if `nic-<link>.csv` shows `rx_out_of_buffer` / `rx_discards_phy`, the host lost them; if those are flat, the packets never arrived (board or link) |
| reader `over` (overruns) rising, `lag` large | the GPU or memory is overloaded: other GPU jobs, too many links (§10), or a slow disk under the writer; the capture itself is unaffected |
| `Permission denied` in a reader log | the data directory is not writable by you or your groups |
| `no ring … within 30 s` (reader) | the capture died or uses another stream name; check the capture log |
| dump reply `error no data in the ring yet` | trigger sent before the first block; wait a second |
| missing first spectrum | integrations are aligned to whole seconds of the board's time base; the first (partial) second is skipped by design |

## 10. Capacity of one Spark

Measured (`pipeline/bench/capacity/`; details in `docs/runtime.md` §6):

| Streams (links) | Beams | Fits? | GPU used |
|---|---|---|---|
| 2 (one board, both links) | 4 | yes, with room for searches | 46 % |
| 4 (two boards) | 8 | compute yes (91 %), needs 4 network links into the Spark (switch, unverified) | 91 % |
| 5 | 10 | at the compute limit | 96 % |
| 6 | 12 | no | — |

For 5 boards with 2 links each, plan **one Spark per board (5 Sparks)**. Three are enough only if each Spark can
take 4 links and the searches run elsewhere.
