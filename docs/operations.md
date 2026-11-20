# Operating the system

Everyday commands, in the order you would normally use them. All tools read `config/station.yaml` (which
boards make up the station, where data goes) and `config/boards/<name>.yaml` (one per board). Run them
from the casperfpga venv (`/opt/lft3arth/lft3arth_venv/bin`, or after `make install`).

## 1. Boards (`lft3-board`)

Every command acts on **all boards of the station in parallel**. `-b NAME` (repeatable) selects boards.

```bash
lft3-board list                       # boards, fids, control IPs, stream destinations, NCOs
lft3-board start                      # program + initialise every board in parallel, then sync them together
lft3-board start --no-program         # re-apply the config to the running designs
lft3-board status                     # health: only non-OK checks are listed (-v: all); exit 2 if anything FAILs
lft3-board monitor --prometheus-port 9101 --jsonl /opt/lft3arth/data/monitor.jsonl
lft3-board set-dest-mac [--show]      # 100GbE destination MAC from config (start does this too)
lft3-board sync | trigger
```

Multi-board sync: the sync pulses are released from a barrier, so boards sync within ~ms of each other.
Sample-accurate alignment needs a PPS-armed sync in firmware (`docs/firmware_requests.md` #4).

## 2. Data from the boards

Saved as HDF5 under `<data_dir>/captures/<board>_<kind>_<UTC>.h5`; `--plot` writes a PNG next to it.

| Command | What you get |
|---|---|
| `lft3-board snapshot fft --plot` | One full 8192-bin PFB output per input (power, before requantisation). Covers 1250 MHz, including the 2 × 156 MHz that are not streamed |
| `lft3-board snapshot adc --plot` | 8192 consecutive complex 16-bit ADC samples per input (6.6 µs) |
| `lft3-board capture --ddr-size 0xFFFFF --plot` | Triggers the DDR ring buffer and receives the 4-bit dump on UDP 40000: here a 64 MiB ring (8192 packets, 13.4 ms per input). Without `--ddr-size`: the whole 4 GiB ring (859 ms) |

The DDR dump is received with an ordinary UDP socket, so this host needs the boards' destination IP
(10.17.16.10 on `enp1s0f0np0`) and a large socket buffer. **Once, as root:**

```bash
sudo sysctl -w net.core.rmem_max=268435456     # persist in /etc/sysctl.d/90-lft3.conf
```

With the default 212 kB limit about half of a 64 MiB dump is dropped; the tool warns and fills the missing
packets with zeros (`received_mask` in the file says which). Several boards can dump at once: packets are
separated by their fid.

The 4-bit dump uses `requantisation.adc_lsb` from the board config (6). With nothing connected the inputs
are only ADC noise (~12 counts RMS), so the dump is mostly 0 and ±1. For noise-only tests use ~2.

## 3. The real-time pipeline (`lft3-pipeline`)

Full guide: `docs/user_guide.md` (how it works inside: `docs/runtime.md`). Per link of a board: lft3-capture →
GPU ring → lft3-spectrometer (fine + coarse FBH5) and lft3-dump (GUPPI RAW on trigger), each in its own container.
With the observation's `search.live` (default) a live hyperseti search reads the spectra from GPU memory and writes
hits to `<data_dir>/hits/<board>/live_<stream>_<UTC>.csv`. Resolution, integration, products and search
settings are in `config/observations/<name>.yaml`, named by `config/station.yaml`.

```bash
lft3-pipeline run                                   # live stream, 1.19 kHz channels, 1 s spectra, until Ctrl-C
lft3-pipeline run --duration 600 --integration 10   # 10 minutes of 10 s spectra
lft3-pipeline run --fft-length 1024 --channels 2048:1024    # 149 Hz channels on coarse channels 2048-3071
lft3-pipeline run --source file --file cap.pcap --loop      # replay a tcpdump capture (no board needed)
lft3-pipeline run --source synthetic --no-record    # generated noise + tone (no board needed)
lft3-pipeline run --source synthetic --random-noise --tone-drift 50 --lossless   # drifting tone for the live search
lft3-pipeline run --observation NAME                # another config/observations/NAME.yaml
lft3-pipeline run --no-search                       # without the live drift search
lft3-pipeline run --dry-run                         # show the docker commands only
lft3-pipeline status                                # every ring on this host: rate, loss, readers
lft3-pipeline trigger --label test                  # raw-voltage dump of every link (GUPPI RAW)
lft3-pipeline stop                                  # stop a run from another terminal
```

Stopping: Ctrl-C, `--duration` or `lft3-pipeline stop` stop the readers (files closed) and then the captures.
Only one run per board at a time. Run directory `<data_dir>/runs/<UTC>_<board>_<source>/`: one log per process,
`plan.txt`, `status.jsonl`, `nic-<link>.csv` (NIC drop/pause counters, also printed when they move), `latest/`.
`lft3-board trigger` and `lft3-pipeline trigger` append to `<data_dir>/triggers.log`.

Soak test (1 h, with a DDR dump every 5 min):
```bash
lft3-pipeline run --duration 3600 &
for i in $(seq 11); do sleep 300; lft3-board trigger; done
```

Healthy: `60.8 Gb/s`, `missing packets` flat, readers `lag 0, over 0`, the spectra ring's `search(lag 0, over 0,
windows N)` counting up every 32 s. Limits: `--fft-length` ≤ 1024 (one block),
channel ranges in whole packets (multiples of 1024).

## 4. Looking at the files (`lft3-plot`)

```bash
lft3-plot latest                                   # newest spectrum file (--kind captures for captures)
lft3-plot info FILE                                # contents, frequency range, resolution, LFT3 metadata
lft3-plot spectrum FILE                            # time-averaged + max spectrum, full band
lft3-plot waterfall FILE --fmin 2400 --fmax 2425 --flatten
lft3-plot capture /opt/lft3arth/data/captures/rfsoc0_fft_snapshot_....h5
```

`--flatten` divides out the coarse-channel response: each 152.6 kHz board channel rolls off ~3 dB at its
edges, which otherwise shows as a 152.6 kHz ripple in wide zooms. `--max-channels` (default 8192 for
spectra, 2048 for waterfalls) averages adjacent channels; zoom with `--fmin/--fmax` for full resolution.

From Python:

```python
from lft3.products import fbh5, captures
s = fbh5.read_spectra(path, fmin=1999.9, fmax=2000.1, flatten=True)   # s.data [time, chan], s.freqs_mhz, s.times
c = captures.load(capture_path)                                        # c.data [input, sample], c.attrs
```

FBH5 files also open with blimpy, turboSETI and hyperseti.

### Interactive viewer (`lft3-view`)

For browsing whole files (pan/zoom from the full band down to single fine channels):

```bash
make viewer     # once; builds in the lft3-runtime container and links lft3-view into the venv's bin/
lft3-view       # prints http://<spark>:8687/?token=...; open it in a browser
```

An overview of a 1-hour file takes ~1.7 s and zooms take tens of ms. It also follows files that are still being
written. See `tools/viewer/README.md`.

### Removing old data

`scripts/clean_data.sh` removes the files of previous observations and dumps from the station data directory:
spectra, hits, run records, raw-voltage dumps (with `triggers.log`) and DDR captures. `reference/`, `bench/`,
`plots/` and `synthetic/` are never touched. It is a dry run unless given `--yes`, and it refuses to run while
lft3 pipeline containers are up.

```bash
scripts/clean_data.sh                          # what would be removed, per category, with sizes
scripts/clean_data.sh --yes                    # remove all of it
scripts/clean_data.sh --older-than 7 --yes     # only files not modified for 7 days
scripts/clean_data.sh --only dumps --yes       # one category: spectra | hits | runs | dumps | captures
```

## 5. What you should see with nothing connected (2026-11-12)

* Board spurs at 1750, 1875, 2000, 2250 MHz (+ 2500 MHz outside the streamed band), weaker at 1600, 2125,
  2200, 2375 MHz. At 1.19 kHz resolution, the 2000 MHz spur has ±30 kHz sidebands.
* The Spark's own Wi-Fi (channel 1): a 20 MHz OFDM band at 2402–2422 MHz with its centre-carrier null at
  2412 MHz, bursting in time. Other 2.4 GHz devices appear around 2440–2460 MHz.
* `lft3-board status`: everything OK except `sample_clock_vs_pps` (INFO: −24.6 ppm against the temporary PPS).
