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
  * `pipeline/flowgraph.py`: builds CyberEther flowgraphs from config + options (`lft3-pipeline`).
  * `products/`: `fbh5.py` (read spectra by freq/time range, `flatten` coarse-channel ripple), `captures.py`
    (capture HDF5), `plot.py` (Agg PNGs; `lft3-plot`).
  * `monitoring/metrics.py`: Prometheus text + JSON lines; `config.py`: station + board configs; `cli/`: entry points.
* `tests/`: pytest with a fake casperfpga (`conftest.py` records every write). `tests/hardware/`: marker
  `hardware`, deselected by default, read-only against the live board.
* `config/station.yaml` (boards, data_dir) + `config/boards/<name>.yaml` (incl. `receiver:` host side, `timing:`).
* `pipeline/`: C++20/CUDA CyberEther plugin (meson, catch2), self-contained; builds in a container.
* `scripts/`: host shell scripts (link setup, NIC counters). `docs/`: see README.md table.

## Commands

```bash
make install         # pip install -e '.[dev]' into /opt/lft3arth/lft3arth_venv (done; console scripts in its bin/)
make test            # Python tests (no hardware);  make lint  (ruff, target py38)
make test-hardware   # read-only, live board
make pipeline-test     # C++/CUDA plugin build + tests in the lft3-cyberether container

# full list in docs/operations.md
lft3-board status | start | snapshot fft|adc [--plot] | capture [--ddr-size 0xFFFFF] | sync | trigger
lft3-pipeline run [--source network|synthetic|file --file X.pcap --loop] [--monitor --remote] [--duration S]
lft3-plot info|spectrum|waterfall|capture FILE [--fmin --fmax --flatten];  lft3-plot latest
lft3-pcap check cap.pcap | synth out.pcap

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
* Stelline (CyberEther plugin, includes BLADE DSP blocks) is built **inside the CyberEther container**: a host build
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

## Pipeline: CyberEther plugin `pipeline/` (details: docs/pipeline.md)

Three blocks in our own CyberEther plugin (`pipeline/build/lft3.cep`), built and run in the CyberEther CUDA container:
* `lft3_receiver`: DAQIRI raw ibverbs (or `source: synthetic`) → header parse on CPU → `Assembler` (pure C++,
  ordered sealing, zero-fill, restart detection) → GPU scatter kernel → output `[2*segments, channels, fftLength, 2]`
  CI8/CF32 blocks + `lft3.*` attributes. Buffer recycling uses CUDA events across the receive and scheduler streams.
* `lft3_finespec`: one fused kernel: shared-memory FFT (≤2048) + |X|²+|Y|² + segment/block integration.
* `lft3_fbh5_writer`: FBH5 via filterbankc99 (POSIX HDF5, not GDS), axes from receiver attributes, tsamp
  measured from timestamp spacing, extra datasets `lft3_timestamp` and `lft3_missing_packets`.
* Measured (synthetic, full band, 1.19 kHz): fused chain 3.2× real time; Stelline's separate
  channelizer→detector→integrator chain only 0.59× (memory-bandwidth bound), kept as `examples/lft3-stelline-chain.yml`.
* Synthetic end-to-end verified: tone at the predicted fine channel and frequency, loss accounting exact, blimpy reads files.
* **First light 2026-11-12 (live, nothing connected):** 60.8 Gb/s, 1.00× real time, zero loss after start-up
  (so the board does not drop packets; the gmac tx_overflow counter is meaningless). Spurs at exact 125 MHz
  multiples (1750/2000/2125/2250/2375 MHz) confirm the frequency axis and its sign. Coarse channel −3 dB at edges.
* Live monitor: `examples/lft3-monitor.yml` (+ quick-look `lft3_finespec mode: coarse` = power per board channel, no
  FFT, 6 channels per point → 1024-point lineplot/waterfall per beam, range 145..180, outputPoolSize 24), viewed with `sudo pipeline/scripts/run.sh --remote ...` → browser at http://<spark>:8686/remote#token.
  NVIDIA Vulkan fails inside the container (works on the host), so the UI uses llvmpipe + software encoder at 10 fps.
* RFI seen with nothing connected: the Spark's own Wi-Fi (channel 1, 2412 MHz) appears in the data.
* Receiver starts on the scheduler's first hasPendingCompute, discards the DAQIRI backlog, aligns to a block boundary.
* Image: `lft3-cyberether` (`pipeline/docker/Dockerfile`) = CyberEther CUDA image + libibverbs/libmlx5. Without them
  DAQIRI silently omits its ibverbs engine ("Engine type 'ibverbs' is not available"); meson.build now errors instead.
* Commands: `pipeline/scripts/build.sh [test]` (no root), `pipeline/scripts/run.sh <flowgraph>` (container as root;
  the network flowgraph needs it, user runs with sudo). Data goes to `/opt/lft3arth/data/`.
* Hz-level resolution needs a cuFFT/rawspec-style path + subbands (docs/architecture.md sizing still applies).
  GPU compute capability 12.1. Memory bandwidth (273 GB/s shared) is the binding constraint.

## Board M&C findings (docs/monitoring_control.md)

* **The lab PPS is temporary, not fixed properly** (`timing.pps: temporary`). The clock measures −24.6 ppm
  against it (`raw_cycles_per_pps` ≈ 156 246 160); unconfirmed until the PPS is fixed. If real: ~49 kHz error at 2 GHz.
* `expected_clk_period` is a plain software register (design default 208 333 332 is wrong); `start` writes 156250000.
  `clk_cycles_per_pps` is garbage, use `raw_cycles_per_pps`. Both in firmware_requests.md #11.
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
* `lft3-pipeline run ... &` froze the wrapper (docker -i read the tty → SIGTTIN); fixed with stdin=DEVNULL.
  `docker logs -t lft3-pipeline` keeps the full log regardless. `tx_global_pause` = 742k cumulative on
  enp1s0f0np0 (host → board pause frames, 802.3x on) predates the test; watch it.
* CyberEther is PID 1 in the container and only handles SIGINT. `run.sh` sets `--stop-signal SIGINT`; `lft3-pipeline`
  stops runs with `docker stop`. Signalling the docker client left containers running.

## Known gaps

* Nothing synthesises ADC packets on port 40000 yet (`lft3-pcap synth` emits channel packets only).
* Frequency sign is confirmed by the 125 MHz spurs; the input → beam/pol mapping still needs an injected tone.

## Working conventions

* `src/lft3/dsp/finespec.py` is the correctness reference. Any GPU/production path must be cross-checked against it.
* Packet-format details that come from firmware must cite where they were confirmed (listener.py, the xlsx, or a capture).
* Never write to the firmware repo. Reprogramming/triggering the board for testing is allowed (user, 2026-11-12),
  but check nothing is consuming the stream first (`docker ps`) and leave the board running with the config.
* New board/firmware knowledge goes into `src/lft3/board/registers.py` (semantics) and `config/boards/` (values).
* Every Python change: `make test && make lint`. Every plugin change: `make pipeline-test`.
