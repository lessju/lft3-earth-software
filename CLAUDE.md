# CLAUDE.md

Host software for LFT3-Earth: receives RFSoC 4x2 F-engine data on an NVIDIA DGX Spark (GB10, Arm64),
channelises it further (152.6 kHz board channels → 1.2 kHz now, ~Hz later), writes spectra, and monitors /
controls the boards. The real-time pipeline (`pipeline/`) runs on the live board; board M&C (`lft3-board`) reads
the live board and its control path is tested against a fake board.

## Repos on this machine

| Path | What |
|---|---|
| `/opt/lft3arth/repos/lft3-earth-software` | this repo |
| `/opt/lft3arth/repos/lft3-earth-main` | firmware (Simulink/CASPER) + docs. **Not a git repo.** Read-only reference for us |
| `/opt/lft3arth/repos/casperfpga` | casperfpga source (installed into the venv below) |
| `/opt/lft3arth/pipeline` | Working area for the external stack being evaluated: `daqiri/` (built on host, `build/`), `stelline/`, `CyberEther/`, `blade/`; `.venv` has meson/ninja |
| `/opt/lft3arth/lft3arth_venv` | Python 3.8 venv: casperfpga, numpy, pyyaml, pytest, ruff, and `lft3` installed editable (`lft3-board`, `lft3-pcap` in its bin/). No cupy. **Use this for everything** |

Key firmware-side files:
* `lft3-earth-main/README.md`: firmware notes (register-semantics gotchas; see below).
* `lft3-earth-main/host/scripts/listener.py`: the firmware author's working packet parser + live plotter.
  **Treat it as ground truth** for packet layout over anything marked ASSUMED in `docs/packet_format.md`.
* `lft3-earth-main/designs/spectrometer_8way/spectrometer_8way_4x/outputs/spectrometer_8way_4x.fpg`:
  the current bitstream. The 12/16-way and 8-way 2x designs are outdated; don't use them.
* `lft3-earth-main/designs/spectrometer_8way/scripts/mts_ddr_8way.ipynb`: bring-up notebook that
  `scripts/start_firmware.py` was copied from.
* `lft3-earth-main/clockfiles/`: LMK/LMX files.
* `lft3-earth-main/docs/`: design review, NRSM, sync memo, `data_rates_packetizer_v2.xlsx` (packet spec), diagrams.

## Layout (standard src layout; see README.md)

* `src/lft3/`: Python package, **Python >= 3.8** (shares the casperfpga venv). Subpackages follow the
  subsystems of `docs/full_architecture.pdf`; add new ones there (`observation`, `triggering`, `products`, ...).
  * `packets.py`: packet formats, pcap I/O, synthetic packets. `dsp/finespec.py`: NumPy/CuPy **golden model**.
  * `board/`: RFSoC M&C. `station.py` (all boards in parallel, like a TPM station), `registers.py` (register map +
    semantics), `control.py` (bring-up = initialise + sync, per-board lock), `telemetry.py` (snapshots, rates,
    health), `capture.py` (FFT/ADC snapshots, DDR dump receiver), `sensors.py` (rails/temps over SSH).
  * `pipeline/runtime.py`: plans a run (per link: capture + readers as `docker run` commands; `lft3-pipeline`).
  * `ring.py`: Python mirror of the runtime's ring control block (status, Prometheus, dump commands, CuPy access).
  * `search/`: `core.py` (window grid, ripple flattening, hyperseti `Searcher` with the dedoppler rounding fix),
    `live.py` (live search on the spectra ring), `hyperseti_search.py` (`lft3-search`: files or `--live`).
  * `products/`: `fbh5.py` (read spectra by freq/time range, `flatten` coarse-channel ripple), `captures.py`
    (capture HDF5), `plot.py` (Agg PNGs; `lft3-plot`).
  * `monitoring/metrics.py`: Prometheus text + JSON lines; `config.py`: station + board configs; `cli/`: entry points.
* `tests/`: pytest with a fake casperfpga (`conftest.py` records every write). `tests/hardware/`: marker
  `hardware`, deselected by default, read-only against the live board.
* `config/station.yaml` (boards, data_dir, `observation:`) + `config/boards/<name>.yaml` (incl. `receiver:` host side,
  `timing:`) + `config/observations/<name>.yaml` (**processing source of truth**: fine channels/resolution,
  integration, products, search window/drift/S/N, dump, ring; `lft3.config.load_observation_config`). CLI options
  override single values per run; the effective config goes to `<run_dir>/observation.yaml`.
* `pipeline/`: C++20/CUDA real-time runtime (meson, catch2): `lft3-capture`, `lft3-spectrometer`, `lft3-dump`;
  builds in the `lft3-runtime` container (`pipeline/docker/Dockerfile`), binaries in `pipeline/build/`.
* `scripts/`: host shell scripts (link setup, NIC counters). `docs/`: see README.md table.

## Commands

```bash
make install         # pip install -e '.[dev]' into /opt/lft3arth/lft3arth_venv (done; console scripts in its bin/)
make link-tools      # symlinks lft3-view(-bench) and lft3-search into that bin/ too (run by install and viewer)
make test            # Python tests (no hardware);  make lint  (ruff, target py38)
make test-hardware   # read-only, live board
make pipeline        # C++/CUDA runtime build in the lft3-runtime container;  make pipeline-test  (+ GPU tests)

# full list in docs/operations.md
lft3-board status | start | snapshot fft|adc [--plot] | capture [--ddr-size 0xFFFFF] | sync | trigger
lft3-pipeline run [--source network|synthetic|file --file X.pcap --loop] [--links link0] [--duration S]
lft3-pipeline status | trigger [--pre --post --label] | stop
lft3-search FILE.h5 | --watch [--once] | --live --stream S   # hyperseti (search venv); file modes not during live capture
lft3-plot info|spectrum|waterfall|capture FILE [--fmin --fmax --flatten];  lft3-plot latest
lft3-pcap check cap.pcap | synth out.pcap
scripts/clean_data.sh [--yes] [--older-than DAYS] [--only spectra|hits|runs|dumps|captures]   # dry run without --yes

# capture (needs root; Claude has no passwordless sudo, so ask the user to run it)
sudo tcpdump -i enp1s0f0np0 -s 0 -B 1048576 -c 20000 -w cap.pcap udp
```

Keep `src/lft3` Python 3.8-compatible: `from __future__ import annotations` in every module (then `list[str]`,
`X | None` in annotations are fine), no `match`, no 3.9+ stdlib APIs at runtime.

## Hardware / data facts (confirmed from firmware, override docs where they differ)

* ADC 5 GSPS real → 1250 MSPS complex after decimation. FPGA clock 156.25 MHz (`t_trig` counts these cycles).
* PFB: 8192-pt, **8 taps** (the firmware README; design review and our docs say 4, which is wrong).
  152.587890625 kHz channels, 6144 kept per beam. Packets b=0..2 carry FFT bins 5120–8191, b=3..5 carry bins 0–3071.
* 4 beams/ADCs per board. ADC map (tile, block): `[(0,0), (0,1), (2,0), (2,1)]`, tile mask 0x5.
* **UDP ports: channel stream = 60000, ADC/DDR dump = 40000.** Board 10.17.16.60 (MAC 00:00:00:00:12:34), src port 60000.
* **Destination IP 10.17.16.10 is compiled into the firmware** (packetizer `Concat1` = Constant51 `10.17.16` ‖ Constant43 `10`),
  as are the ports (Constant58/59). Changing them needs a firmware rebuild. The destination **MAC** comes from the 100GbE
  core's ARP table entry 10, which defaults to broadcast and is set at runtime by `lft3-board set-dest-mac` (and by `lft3-board start`).
  The ARP table is lost on reprogram.
  Board control (casperfpga/katcp): **192.168.2.100**, reached from this host's 1 GbE `enP7s7` (192.168.2.101).
* All header words are **big-endian**. Channel payload: per channel, 4 beams × (I int8, Q int8), beam 0 first,
  I first. This matches the `packets.py` defaults.
* ADC payload: 512-bit DDR words, 16 sample-times × 4 ADCs each; byte = I high nibble, Q low nibble (4-bit two's complement).
* Header NCO fields are **signed int32, whole MHz** (`int(round(nco))`). -2031.25 MHz is sent as -2031, so don't
  derive exact sky frequency from the header. `packets.py` decodes them as int32.
* DDR: `ddr_size` is the last 512-bit word address (max 2^26-1 → 4 GiB → 2^19 dump packets).
  The trigger lands in packet `(ddr_size + 1 - ddr_read_delay) // 128`.
* ADC header `src` bits, MSB→LSB: beam0, beam1, beam2, beam3, manual.

## Firmware control gotchas (from lft3-earth-main/README.md)

* `packet_mask`, `fid`, header `pfb_lsb`, and `h6`–`h13_ch` only take effect after a `sync` pulse.
  `ch_lsb_select` changes the data immediately, so sync after changing it or the header `pfb_lsb` will be stale.
* Releasing `sysref_gate_reg` (NCO/MTS cells) also emits a sync.
* `trig_rst` must be pulsed after every trigger to re-arm. `trig_cnt` counts triggers since the last sync.
* PPS only feeds monitoring counters (`pps_cnt`, `clk_cycles_per_pps`, `pps_drift_err_cnt`). It doesn't drive sync.
* Trigger multiplier is UFix17.2 (max 32767.75).
* `rfdc.progpll('lmk', name)` with a bare filename uses a clock file **already on the board**. The LMK/LMX
  files used by start_firmware.py have been copied there. For a new local file, pass the path with `upload=True`.
* NCO setting order: MTS first, then mixer event source = SYSREF, then set fine mixer freq and pulse SYSREF.

## This host's 100G link (DGX Spark)

* The RFSoC QSFP port appears as two netdevs (PCIe halves): `enp1s0f0np0` = 0000:01:00.0 (rocep1s0f0) and
  `enP2p1s0f0np0` = 0002:01:00.0. MTU 9000, no IPv4 assigned. `*_phy` ethtool counters are per port, so both show the same.
* Firmware sends channel packets to the **broadcast MAC** by default. Both halves get a full copy (`rx_vport_broadcast_packets`).
  Fix: `lft3-board set-dest-mac` (config value 4c:bb:47:2b:97:28 = enp1s0f0np0), and on the host
  `sudo ip addr add 10.17.16.10/24 dev enp1s0f0np0` (only needed for kernel tools; DAQIRI flow rules don't need an IP).
  The kernel path accepts only ~0.41 Mpps on one queue.
* CPU cores: Cortex-X925 = 5–9, 15–19; A725 = 0–4, 10–14. Pin DAQIRI master/queue/bench threads to X925 cores.
* (Historical, no longer used by this repo.) Stelline (CyberEther plugin, includes BLADE DSP blocks) is built **inside the CyberEther container**: a host build
  needs X11/Vulkan/glslang dev packages. Image `ghcr.io/luigifcruz/cyberether:ubuntu24-aarch64-cuda` (CUDA 13.0, has
  meson/ninja, no cmake; cmake comes from `/opt/lft3arth/pipeline/.venv/bin`):
  `docker run --rm [--gpus all] --user $(id -u):$(id -g) -e HOME=/tmp -v /opt/lft3arth/pipeline:/opt/lft3arth/pipeline -w /opt/lft3arth/pipeline/stelline <image> bash -c 'export PATH=$PATH:/opt/lft3arth/pipeline/.venv/bin; meson compile -C build-docker'`
  gives `build-docker/stelline.cep`. `meson test -C build-docker` with `--gpus all`: 39/40 pass on GB10 (the failure
  is radiointerferometryc99 `posangle_reference`, which needs astropy).
* **DAQIRI RX result (2026-11-12, 30 s, 1 queue on core 6, host_pinned):** 27 530 240 packets in 30.07 s =
  915 551 pps (spec 915 528), 8298 B/frame, 60.8 Gb/s on the wire. 0 fillers, 0 CQE errors, 0 app-ring drops,
  0 pause frames. So one X925 core keeps up with a full board. This compares counts only; proving zero loss needs a
  (t, b) sequence check in our own receiver. 802.3x pause is enabled on enp1s0f0np0 (DAQIRI warns about it); harmless so far.
* DAQIRI RX benchmark: `config/daqiri/lft3_bench_rx.yaml`, run as root with
  `/opt/lft3arth/pipeline/daqiri/build/examples/daqiri_bench_raw_gpudirect <yaml> --seconds 30`.
* Board TX registers: `..._gmac_reg_tx_packet_rate` = 915527/s (matches spec). Packetizer `fifo_full_cnt`,
  `fifo_ddr_full_cnt` and `blocked_word_cnt` are 0, and `accepted_word_cnt` rises ~129 words × 915.5k/s.
  The gmac `tx_overflow_count` (+0.7e9/s) and `tx_almost_full_count` (+2.2e9/s) climb implausibly fast and look
  meaningless. `packetizer_gbe_ovflow_reg` = 1 (sticky?). Board-side loss can only be ruled out with a lossless receiver.
* Unprivileged checks that work: `ethtool -S <if>` deltas, and casperfpga register reads.

## First capture (2026-11-12, `cap_chan.pcap`, channel stream, kernel tcpdump)

* Format verified: 8256-B payloads, big-endian, b = 0..5, fid 0, nco = −2031 ×4 (signed), pfb_lsb 0, meta words 0.
* tcpdump kept ~31% of packets (runs of ≤62 packets, then gaps); 3091 complete spectra in 70 ms.
* Levels with `ch_lsb_select` = 0: int8 RMS ≈ 11, clip fraction 8e-5, I/Q balanced, no DC. Good.
* **Nothing was connected to the inputs**, so every tone is a board spur. Tones on all beams at baseband −281.2,
  −156.25, −31.3, +218.8 MHz (+15–20 dB), each strongest on a different input. With RF = 2031.25 + offset they are
  exact 125 MHz harmonics (14, 15, 16, 18 × 125 = PL clock). The opposite sign gives odd multiples of 62.5 MHz, which
  matches no clock. This tentatively supports listener.py's convention (k ascending = RF ascending for a negative NCO).
  Confirm with a real injected tone. These spurs belong in the static RFI/spur mask.

## Long-term system design (`docs/full_architecture.pdf`)

A "rumination" doc with open questions, plus KZA's answers in red. It is **not** a spec. Decisions so far (2026-11-12):
* **Authority:** for data rates, channel counts and packet layout, the firmware, `docs/packet_format.md` and listener.py
  win over the rumination doc's numbers (its 80 Gb/s / subband figures don't match the firmware or each other).
* **Stokes is formed in host software, not the FPGA.** The 4 ADC inputs are 2 dual-pol beams. Which inputs pair up is still TBD.
* **Gaps: zero-fill for now.** Flagging/masks (per the doc) are deferred, but keep a per-block loss count so a mask is easy to add.
* **Scope: stop just before the searches.** Build capture → ring buffer → fine channeliser (+ coarse/integrated
  spectra). Keep the later pieces in mind so the design doesn't block them:
  * narrowband drift search: hyperseti, which takes CuPy arrays directly;
  * wideband path: detect + integrate → heimdall / AstroAccelerate, fed from the *coarse* beam data, not the fine channeliser;
  * raw-beam transient buffer dumped on trigger;
  * triggers: FPGA power detector, GPU narrowband, manual, scheduled; at most ~1 per 10 s;
  * products: GUPPI RAW + filterbank-HDF5, with obs ID, config and software version in every product.
* **Fine resolution: interim 1.2 kHz** (Nf = 128 → 1192 Hz on 152.6 kHz channels), integrate in time, write spectra to
  disk "to see what's happening". The final value depends on science (SETI convention is ~1–3 Hz, Nf = 2^16–2^17).
  **Design so Nf is a parameter and nothing breaks at 2^17**: ring/buffer sizing, corner-turn block length,
  GPU memory batching over coarse channels, and output volume.
* **Polarisation pairing (ASSUMED, unverified):** inputs follow the ADC map order (`config/boards/rfsoc0.yaml`):
  input 0 = (tile 0, block 0) beam 1 X, input 1 = (0,1) beam 1 Y, input 2 = (2,0) beam 2 X, input 3 = (2,1) beam 2 Y.
  Verify with a common signal / X–Y cross-correlation once something is connected.
* Ops direction: casperfpga monitoring, containers, Ansible, config in git, CLI-first.

## Real-time runtime `pipeline/` (2026-11-13; docs/runtime.md = implementation + decisions, docs/user_guide.md)

* Replaced the CyberEther plugin (lock-step scheduler: a slow consumer stalls the receiver; the UI is not needed).
  Holoscan 5.0 evaluated and rejected for now (brand-new rewrite, Beta tensor/queue APIs, DAQIRI still on 4.x);
  hashpipe compared (semaphore handshake = back-pressure; its GUPPI RAW / hpguppi conventions are kept).
* Per link ("stream" `<board>-<link>`, `receiver.links` in the board YAML): `lft3-capture` (single writer, DAQIRI) →
  ring of S blocks `[beam][channel][1024 t][pol]` CI8 (50.3 MB, 6.71 ms) in a **CUDA VMM allocation shared by fd**
  (`/dev/shm/lft3-<stream>.sock`) + control block in POSIX shm `/dev/shm/lft3-<stream>` (fixed layout, mirrored in
  `src/lft3/ring.py`, bump `kRingVersion` on change) → readers `lft3-spectrometer` (fine FBH5 + derived coarse
  FBH5 + latest snapshot), `lft3-dump` (GUPPI RAW on `lft3-pipeline trigger`), optional coarse `--quicklook`.
  Readers never back-pressure capture (seqlock + overrun counts); backpressure only for synthetic/file (`--lossless`).
* Measured, these decided the design: a ring of time-contiguous rows thrashes the GPU TLB (scatter 6 GB/s, needs 7.5)
  vs blocks 83 GB/s; `cudaHostRegister`'d memory reads ~30 % slower than VMM/cudaMalloc (172 vs 237 GB/s).
* Parity with the CyberEther output: identical timestamps/axes over 127 spectra, values equal to float rounding.
  Bug found + fixed: backpressure must keep `kReadMargin` (8) slots behind the lossless reader's cursor.
* Live (2026-11-13): 60.84 Gb/s, zero loss, readers lag 0; 1 s dump (7.5 GB) collected 0.51 s, written 2.8 s.
* Per stream: scatter 0.50 ms + finespec 0.50 ms GPU per 6.71 ms block; CPU 1 busy-poll core + ~4 % + ~18 %.
  Quick-look is derived from the fine spectrum (Parseval) instead of a second ring read. Finespec kernel is
  latency/sync-bound (twiddle table: no gain); next step = warp-shuffle radix-4 FFT.
* **Capacity (pipeline/bench/capacity, synthetic + NIC-DMA memload): 4 streams pass with margin (GPU 87 %), 5 pass
  (96 %, readers ≤ 2.5 ms/block with the warp FFT), 6 fail (readers overrun; captures still real time).** Network:
  2 direct 100G links per Spark. **Station = 6 boards × 1 link (user, 2026-11-14; xlsx Final Specs)** → 3 Sparks
  (2 boards each, GPU 46 %). Production sizing (discrete GPUs): docs/sizing.md.
* **Sizing (docs/sizing.md, 2026-11-14):** all stages are memory-bandwidth bound, so cost is in GB10-equivalents
  scaled by GPU bandwidth. hyperseti dedoppler (GB10, 2^20 ch of 1.164 Hz, 10 s spectra, ±4 Hz/s, stepped plan):
  6.2 / 2.5 / 0.7 Mchan/s at 16 / 32 / 64 spectra; drift range matters little (±1/±10: 3.5/2.1). At full band
  2^17 that is ~2.1 GB10 per stream, more than the Hz channeliser (~0.75). Recommendation: 1 RTX PRO 6000
  Blackwell per board (57 %), 3 servers × 2 boards. GeForce excluded (no GPUDirect RDMA, licence).
* **Warp FFT kernel** (64/128/256 points, one warp per FFT, `__shfl_xor`, no shared memory): 0.30 ms vs 0.50 ms per
  block, matches the DFT reference; `LFT3_FINESPEC_KERNEL=shared` forces the old kernel.
* **Soak 2026-11-13 (2 h, 23 DDR + 23 raw dumps): no loss of the system's own making**, 0 reader overruns, DDR-dump
  gaps did not recur (0/23). **But** a hyperseti search running on the Spark made the NIC drop 277 packets
  (`rx_discards_phy`, memory-bus contention; host pause frames didn't help, the board probably ignores 802.3x).
  Don't run heavy GPU/memory jobs on the capture host during a live capture.
* **rawspec** (built sm_121; CUDA 13 cuFFT enum patch; `LOAD_TEXTURE_WIDTH_POWER=17` for 2^17): same spectra as ours
  (after its 1/127 sample scaling), ~60× slower at Nf = 128, and at Nf = 2^17 too slow for even one beam in real time
  (13.2 + 5.2 ms per 6.7 ms block). Plain cuFFT 2^17 = 3.1× real time per beam → Hz resolution ≈ 1 stream per Spark.
* Capture runs with `--cap-add NET_RAW --cap-add IPC_LOCK --device /dev/infiniband` (verified), not --privileged.
* Search env: `scripts/setup_search_env.sh` → /opt/lft3arth/search_venv (Python 3.12, cupy-cuda13x, hyperseti
  @a15f7eb; setuptools<81 for blimpy's pkg_resources; hyperseti pulls the generic `cupy` → replaced by the wheel).
  `lft3-search` defaults: sk_flag off (else 100 % flagged), max_dd 200 Hz/s (10 Hz/s gives "No steps!" at 1.19 kHz).
* Containers: capture `--net host` + NET_RAW/IPC_LOCK/`/dev/infiniband`; readers as the user with `--group-add` (data dir is group
  `lft3eath`), `--net none`; all `--ipc=host`. Binaries also run on the host (handy for nsys:
  `/opt/nvidia/nsight-systems/2025.3.2/bin/nsys`). The old `lft3-cyberether` image is unused (not deleted).
* Earlier CyberEther-era facts still valid: first light 2026-11-12 (zero loss; spurs at exact 125 MHz multiples
  confirm the axis and sign; coarse channel −3 dB at edges); the Spark's Wi-Fi (2412 MHz) appears in the data.
* Reference outputs: `/opt/lft3arth/data/reference/` (CyberEther synthetic run and runtime runs).
* Hz-level resolution needs a cuFFT path gathering blocks (232 GB/s measured); docs/architecture.md sizing applies.

## Live drift search (2026-11-20; docs/runtime.md decisions 16-18, §4.6)

* `lft3-spectrometer --spectra-ring N` copies each 1 s spectrum D2D into GPU ring `<stream>-spectra` (ring
  **version 2**: `kind`, FFT length, fch1/foff/tsamp at offsets 528..559; Python `Ring` still reads v1 rings).
  `lft3.search.live` (host process, search venv, `taskset` to A725 reader cores) registers via
  `lft3.ring.RingReader` (libatomic CAS + heartbeat thread), keeps the last N spectra in its own buffer, searches
  window k = integrations [k·step, k·step+N). Started/stopped by `lft3-pipeline` (pid file
  `/dev/shm/lft3-<stream>-search.pid`); a failing search does not end the run.
* **hyperseti a15f7eb dedoppler truncates** `shift*t/T` (C int division): on short windows trials ±1 equal 0
  (0 Hz/s reported as −74.5). `core.patch_dedoppler_rounding` recompiles it rounded. The `stepped` plan adds
  trials far beyond max_dd on short windows → `optimal`. 16-spectrum windows only give ±75 Hz/s; default 64/32.
* Validated (synthetic `--random-noise`: hashed GPU noise + drifting tone, `capture/synth.cu`): live windows
  bit-identical to FBH5, live hits == `lft3-search` file hits; +50 Hz/s → 37/56 Hz/s at S/N 143 (amp 1),
  −120 → −112/−130 at S/N ~36 (amp 0.5), no false hits in the empty beam; 2 % drops → windows skipped;
  0.38 s per window per 32 s, kept up at 5.5× real time.
* **1 h live soak (2026-11-20) with the search: 0 loss, `rx_discards_phy` unchanged**, 110/110 windows, 0
  overruns. Real data: ~3700 hits/window, almost all zero-drift local RFI (Wi-Fi, spurs, other). hyperseti's
  `merge_hits` took 13.9 s/window → `core.merge_hits` (identical results on soak data, 180× faster, ~0.7 s per
  beam per window now).
* **Resolution benchmark (2026-11-20, pipeline/bench/resolution):** one stream keeps real time while recording
  up to Nf 1024 @ 1 s or Nf 128 @ 0.1 s; the limit is the single-thread FBH5 writer (~50 MB/s compressed). Without
  recording everything up to Nf 1024 @ 0.1 s keeps up. Search per 2^17-ch gulp of 64 spectra: ~21 ms + 0.15 ms per
  drift trial; at 1.16 Hz with 1 s spectra ~30 GB10/stream → Hz needs 10 s spectra + stepped plan (sizing basis).
* The old periodic synthetic generator repeats every block → integrated spectra never fluctuate: useless for
  searches (fixed pattern adds coherently). Use `--random-noise` for anything search-related.

## Board M&C findings (docs/monitoring_control.md)

* **The lab PPS is temporary, not fixed properly** (`timing.pps: temporary`). The clock measures −24.6 ppm
  against it (`raw_cycles_per_pps` ≈ 156 246 160); unconfirmed until the PPS is fixed. If real: ~49 kHz error at 2 GHz.
* `expected_clk_period` is a plain software register (design default 208 333 332 is wrong); `start` writes 156250000.
  `clk_cycles_per_pps` is garbage, use `raw_cycles_per_pps`. Both in firmware_requests.md #10.
* All `lft3-board` write commands verified live 2026-11-12 (start 9.6 s; trigger dumps 4 GiB at ~10 Gb/s for ~3.5 s).
* **The user allows reprogramming the board and testing write functions** (2026-11-12). Still say what you're doing.
* Board sensors (rails, temps) are only in the board's sysfs; katcp has no sensors. SSH as casper needs a key.

## Captures and pipeline runs (2026-11-12)

* DDR dump via UDP socket loses ~55% while `net.core.rmem_max` is the default 212 kB (socket RcvbufErrors);
  needs `sudo sysctl -w net.core.rmem_max=268435456` (not done yet; Claude has no sudo).
* `adc_lsb: 6` makes noise-only 4-bit dumps ~0/±1; fine for real signals presumably, ~2 for noise tests.
* The pipeline container needs only docker-group membership (it runs privileged as root); Claude can run it.
* Spectra show a 152.6 kHz ripple (coarse channel −3 dB edges): `lft3-plot --flatten` / `read_spectra(flatten=True)`.
* Wi-Fi channel 1 of the Spark (2412 MHz OFDM, centre null) is clearly visible: an RFI source next to the board.

## Soak test (2026-11-12, 1 h live, 11 DDR dumps every 5 min)

* 0 loss except **one 29.6 ms gap (27 090 packets) exactly at the 17:14:36 UTC trigger**; 10 other triggers in the run
  and 10 rapid test triggers (with NIC counters watched) were clean: no `rx_out_of_buffer`, `rx_discards_phy` or
  pause frames, and each dump arrived complete (2^19 packets). Cause (board mux vs host) unknown; ~1 in 21 dumps.
  Runs now log `nic.csv` and `triggers.log` to settle it next time.
* 2nd soak (2 h, 50 dumps every 140 s): again 1 gap, 12 332 packets (13.5 ms), within 3 s after the 19:48:41 UTC
  trigger; port counters (`rx_discards_phy`, pause, CRC) unchanged over the run. So 2 gaps in 71 dumps, always at a
  dump. Kernel IRQs for dump packets land on core 13, not the DAQIRI worker (6). Still open: board vs DAQIRI RQ.
* 3rd soak (2026-11-13 08:32–10:32 UTC, 2 h, 50 dumps, first run with `nic.csv`): **zero loss** (6.59e9 packets,
  1 072 124 complete blocks), all 50 dumps arrived (≈2^19 packets each), no NIC drop/pause counter moved after
  start-up, `--duration` stopped the container cleanly. Tally: 2 gaps in 121 dumps; cause still unattributed.
* `lft3-pipeline run ... &` froze the wrapper (docker -i read the tty → SIGTTIN); fixed with stdin=DEVNULL.
  Per-process logs now live in the run directory. `tx_global_pause` = 742k cumulative on
  enp1s0f0np0 (host → board pause frames, 802.3x on) predates the test; watch it.
* `lft3-pipeline` stops runs with `docker stop` (readers first, then captures); signalling the docker client left
  containers running.

## FBH5 viewer `tools/viewer/` (lft3-view, 2026-11-13)

* C++ server + one-page browser UI (no JS libs): `make viewer`, then `tools/viewer/build/lft3-view` → http://<spark>:8687/?token=…
  Built in the lft3-runtime container (HDF5 static via the same wrap as pipeline/), runs on the host.
* HDF5 lib only for metadata + chunk index; spectra via parallel `pread`. Writer's deflate level 0 = zlib stored
  blocks (65535 B + 5 B header), so frequency ranges are read without inflating (headers validated, zlib fallback).
  Cold: 1-hour file overview 1.7 s (6.8 GB/s NVMe), zooms < 70 ms; h5py manages ~0.5 GB/s.
* `lft3-view-bench FILE` checks reads bit-for-bit vs H5Dread and times typical views. UI tested with headless
  Firefox + Selenium (geckodriver at /snap/bin; Selenium in a scratch venv, not in the project venv).
* The container's system cpp-httplib wants OpenSSL headers it lacks: meson forces the wrap (`force_fallback_for`).
* Runs: rotated files of one beam are chained when `lft3_timestamp` continues exactly (`fbh5_sequence.cc`). Overlays:
  `triggers.log` + `config/known_rfi.json` (JSON so the C++ server and the page need no YAML). Per-channel
  normalisation, strips and drift measurement are client-side. Planned, not started: shareable URL/PNG/npy export,
  and two beams side by side with linked zoom.
* Index via `H5Dchunk_iter` (6 ms vs 150 ms per-chunk lookups); offsets are elements in HDF5 1.14.6.
* New runtime (other session, 2026-11-13) also writes a coarse product to `spectra/<board>/quicklook/` (6144 ch,
  152.6 kHz, nfpc 1, same names/timestamps, root attrs lft3_mode/stream/obs_id/beam_offset). The viewer lists it as
  separate runs; run labels show dir + channel width. Viewer builds in the `lft3-runtime` image now.
* UI tests: Selenium + `/snap/bin/geckodriver` from a scratch venv; page globals are `const`, so use `window.__lft3`.
* Tiles to the browser: `format=u16db` (16-bit dB over the tile range, hi/lo byte planes, gzip with RLE hi + stored
  lo), 2.2–2.7× smaller than f32, ≤0.3 mdB error.
* **FBH5 writer storage since 2026-11-13:** chunks (1, 1, 65536), `/data` shuffle + deflate 1 (real spectra 1.43×),
  `/mask` deflate 4 (all-zero → ~1000×). Files ~45 % smaller. Needs h5dsc99 patch `shuffle_deflate.diff`
  (filter_flag 20..29 = shuffle + deflate level N-20). Note `filter_flag` is `char`, unsigned on aarch64, so
  `H5_FILTER_FLAG_NONE` (-1) never works. Older files: whole-row chunks, deflate 0 = no compression.

## Tone test (2026-11-13, sig-gen 1800.000 MHz CW, −80 dBm, into the SMA the user calls "ADC 0")

* **The SMA called "ADC 0" feeds firmware input 3 = (tile 2, block 1)** (−60 dBm, 10 averaged FFT snapshots:
  +23 dB on input 3 only). Other three connectors not yet mapped. Panel "ADC 0" ≠ firmware input 0.
* At −60 dBm the tone is ~+23 dB per 152 kHz bin over a channel-noise RMS of ≈11 → ~160 peak in int8: clips the
  channel stream (ch_lsb 0). Use ≤ −70 dBm for linear tests at this ch_lsb.
* Appears at 1800.0404 MHz (+16.5 dB in a 1.19 kHz channel after 20 s), mirror 2262.5 MHz absent: **sign confirmed**.
* **+40.4 kHz = +22.5 ppm high**, while the board's own 125 MHz-harmonic spurs land exact (±0.2 kHz, same clock).
  So the board sample clock runs ~22 ppm slow relative to the generator, consistent with −24.6 ppm vs the temporary
  PPS: the board clock (not the PPS) is off. Generator reference accuracy not yet known.
* Single FFT snapshots (one 8192-pt spectrum) cannot see −80 dBm; the live 1 s spectra can.

## Known gaps

* Nothing synthesises ADC packets on port 40000 yet (`lft3-pcap synth` emits channel packets only).
* Live search hits are not yet turned into triggers, clustered across windows/beams, or RFI-filtered beyond labels.
* Frequency sign is confirmed by the 125 MHz spurs; the input → beam/pol mapping still needs an injected tone.

## Working conventions

* `src/lft3/dsp/finespec.py` is the correctness reference. Any GPU/production path must be cross-checked against it.
* Packet-format details that come from firmware must cite where they were confirmed (listener.py, the xlsx, or a capture).
* Never write to the firmware repo. Reprogramming/triggering the board for testing is allowed (user, 2026-11-12),
  but check nothing is consuming the stream first (`docker ps`) and leave the board running with the config.
* New board/firmware knowledge goes into `src/lft3/board/registers.py` (semantics) and `config/boards/` (values).
* Every Python change: `make test && make lint`. Every runtime (pipeline/) change: `make pipeline-test`, and for
  kernel / spectrometer changes a lossless synthetic parity run against the previous output (docs/runtime.md §2).
