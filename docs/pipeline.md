# LFT3 receive + fine-spectrometer pipeline (CyberEther plugin)

How the first working pipeline is built, why it is built that way, and how to run it. Code:
`pipeline/` in this repository. Status (2026-11-12): unit-tested; end-to-end tested with a synthetic
packet source at full rate; **running on the live board**: 60.8 Gb/s, 1.00x real time, no packet loss
after start-up, spurs at exact 125 MHz harmonics (§7).

```
RFSoC 4x2 ──100GbE──► ConnectX-7 ──► DAQIRI (raw ibverbs, kernel bypass)
  60.7 Gb/s            flow rule:        RX queue on workerCore, bursts of 1024 packets
  915.5 kpps           UDP dst 60000     header segment (106 B) ─► CPU     payload (8192 B) ─► stays put
                                                   │                               │ (pinned host memory,
                                                   ▼                               │  read in place by GPU)
                            lft3_receiver thread (processingCore)                  │
                            parse t/bindex/fid ─► Assembler: which block, which slot│
                            stage ScatterJob{payload ptr, block ptr, spectrum, chan}│
                            launch scatter kernel ─────────────────────────────────►┘
                            seal finished blocks (zero-fill gaps) ─► ready queue
                                                   │
             CyberEther scheduler ─────────────────┘ computeSubmit(): swap ready block into output tensor
                                                   ▼
             lft3_finespec   one kernel: 128-pt FFT per coarse channel, |X|²+|Y|², sum segments + 149 blocks
                                                   ▼  (every ~1 s; SKIP downstream otherwise)
             lft3_fbh5_writer  GPU→host copy, frequency/time axes from receiver metadata ─► FBH5 per beam
```

## 1. Background you need

### 1.1 Raw Ethernet with ibverbs (what DAQIRI uses underneath)

`libibverbs` is the userspace API of RDMA NICs (ConnectX). Besides RDMA, it can do **raw Ethernet**
receive with the kernel network stack completely bypassed. The pieces:

| Concept | What it is | Analogy if you know DPDK / packet sockets |
|---|---|---|
| Protection domain + **memory region (MR)** | Memory you *register* with the NIC: pinned, translated once, so the NIC can DMA into it directly. | DPDK mempool on hugepages |
| **Receive queue (RQ)** | A ring of descriptors ("WQEs") pointing into registered memory. The NIC writes each arriving frame into the next posted buffer. | RX ring |
| **Completion queue (CQ)** | The NIC posts an entry per received frame (length, flags). Software polls it, no interrupts. | polling `rte_eth_rx_burst` |
| **Flow steering rule** | A hardware match (e.g. IPv4/UDP dst port 60000) that sends matching frames to a given RQ. With *flow isolation*, nothing else reaches it. | `rte_flow` |
| **Striding RQ / MPRQ** | One WQE covers a large buffer split into fixed strides, so one descriptor receives many packets. | |
| **Header-data split** | Each frame is scattered across two buffers: the first N bytes into one MR, the rest into another. | |

Why root (or `CAP_NET_RAW` + `CAP_IPC_LOCK`): creating raw-packet queue pairs and flow rules needs
`CAP_NET_RAW`, and pinning gigabytes of receive buffers exceeds the default `memlock` limit.

On the DGX Spark there is **no GPUDirect RDMA**: the NIC cannot DMA into `cudaMalloc` memory. But the GB10
GPU shares DRAM with the CPU (unified memory), so the receive buffers are pinned *host* memory
(`cudaHostAlloc`) that the GPU reads in place. It is still zero-copy.

### 1.2 DAQIRI

NVIDIA's receive library (Holoscan family) wraps all of the above behind a config and a small API:

* **Config** (built in code in `pipeline/src/receiver/detail/daqiri_config.cc`, or YAML for the benchmarks):
  memory regions (`host_pinned`/`device`/`huge`, buffer size, count), interfaces (PCI address), RX queues
  (CPU core of the polling worker, burst size, which MRs), flows (match → queue).
* DAQIRI starts its own **worker thread** per queue (pinned to `workerCore`) that polls the CQ, reposts
  buffers and groups packets into **bursts**.
* The application calls `get_rx_burst(&burst)`, reads packets with
  `get_segment_packet_ptr(burst, segment, i)` (segment 0 = header MR, 1 = payload MR), and must call
  `free_all_packets_and_burst_rx(burst)` when it is done, which returns the buffers to the RQ. If the
  application holds bursts too long, the RQ runs dry and the NIC drops (`rx_out_of_buffer`).
* The benchmark earlier today (`config/daqiri/lft3_bench_rx.yaml`): 915 551 packets/s, 60.8 Gb/s, no
  errors, on one core.

### 1.3 CyberEther, blocks, modules, plugins

CyberEther ("Jetstream" in the code) runs **flowgraphs** (YAML files) of **blocks** on CPU/GPU, with an
optional GUI. Terms:

* **Block**: the user-facing node: type name, config fields, inputs/outputs, metrics shown in the UI.
  It creates one or more **modules**.
* **Module**: the compute part for one device (here CUDA). Lifecycle: `validate()` → `define()` →
  `create()` (allocate, publish output tensors) → repeated `computeSubmit(stream)` → `destroy()`.
* **Scheduler**: per cycle, calls `hasPendingCompute()` on source modules (ours blocks until a block of
  data is ready), then `computeSubmit()` of each module in graph order on one CUDA stream. A module
  returning `Result::SKIP` stops downstream modules for that cycle (used for integration).
* **Tensor**: typed n-d buffer. Outputs are owned by the producing module; consumers keep a handle to
  the same tensor object, so a source can swap new storage into it every cycle (`swapBuffers`).
* **Attributes**: key/value metadata on tensors. `propagateAttributes()` makes a downstream tensor look
  up keys on its source *at read time*. That is how the FBH5 writer reads the receiver's `timestamp`,
  NCO etc. through the spectrometer.
* **Plugin** (`.cep`): a tar.gz bundle with a shared library + example flowgraphs. Blocks and modules
  register themselves statically; `cyberether --plugin x.cep` loads it.

### 1.4 Stelline

A CyberEther plugin (same author) with an ATA telescope receiver, FBH5/UVH5 writers and the former
BLADE DSP blocks (channelizer, detector, integrator, beamformer...). We **reused its design** (receiver
structure, FBH5 via filterbankc99, build/bundling) but not its blocks in the main flowgraph:

* Its receiver is specific to ATA packets and drops incomplete blocks (we zero-fill).
* Its FBH5 writer requires GPUDirect Storage (not on Spark) and writes placeholder metadata.
* Its separate channelizer → detector → integrator chain is **too slow at full band** on the Spark:
  measured **0.59× real time** (see §3.1). Kept as `pipeline/examples/lft3-stelline-chain.yml` for reference.

## 2. lft3_receiver

Files: `pipeline/src/receiver/` (`module_impl_native_cuda.cc` is the main one, `detail/` holds the pure
parts).

### 2.1 Output layout

One output **block** = `fftLength × segments` consecutive spectra (default 128 × 8 = 1024 spectra =
6.71 ms). Tensor shape `[2 × segments, channelCount, fftLength, 2]`:

```
axis 0  aspect  = beam * segments + m        (time segment m folded into the first axis)
axis 1  channel = coarse channel within the selection (k - channelStart)
axis 2  sample  = s                           (spectrum n of the block = m * fftLength + s)
axis 3  pol     = 0 (X) / 1 (Y)
```

Each `[aspect, channel, :, :]` row is one fine-FFT input, contiguous in memory. Folding segments into
axis 0 lets one tensor carry many FFTs while the FFT length stays `fftLength`. Inputs are mapped to
(beam, pol) by `inputBeam`/`inputPolarization` (default: input 0 = beam 0 X, 1 = beam 0 Y, 2 = beam 1 X,
3 = beam 1 Y, i.e. the `ADC_MAP` order; still to be verified with a real signal). `channelStart` /
`channelCount` select whole packets (multiples of 1024 channels): this is the GPU-side subband
selection from the system-design notes.

### 2.2 Per packet

1. DAQIRI worker has DMA'd the frame: bytes 0–105 (Ethernet/IP/UDP + LFT3 header) into the header MR,
   bytes 106–8297 (payload) into the payload MR.
2. Our thread (`processingCore`) parses the header (`detail/packet.hh`): t, bindex, fid, NCO, pfb_lsb.
   Wrong board id or bindex outside the selection: counted as *filtered*.
3. **Assembler** (`detail/assembler.cc`, no CUDA, unit-tested) maps (t, bindex) → block = t / 1024,
   slot = (t mod 1024) × packetsPerSpectrum + (bindex − first). It rejects duplicates and packets for
   blocks already sealed (*late*), allocates a block buffer from the pool for the first packet of a new
   block (*no-buffer* if the pool is empty: downstream too slow).
4. A `ScatterJob {payload pointer, block base pointer, spectrum, channel offset}` is appended to the
   burst's job list (pinned host memory, read by the GPU directly).
5. After the whole burst: one **scatter kernel** launch (`detail/kernel.cu`): one thread per
   (packet, channel), reads 8 bytes (4 inputs × I/Q) from the payload in place, writes 4 samples
   (CI8 or CF32) to their place in the block. The burst is returned to DAQIRI only once that kernel has
   finished (CUDA event), so the payload memory is never recycled under the GPU.

### 2.3 Sealing, gaps, ordering

The assembler keeps up to `maxOpenBlocks` blocks open. It seals strictly in time order:

* the oldest open block is sealed when **complete**, or when it is more than `maxBlockLag` blocks behind
  the newest block seen (**stale**);
* a stale block's missing slots are listed; with `zeroFill: true` (the agreed behaviour for now) the
  same scatter kernel with a null source writes zeros there, and the block is emitted as *partial*;
* blocks for which no packet arrived at all are counted as *skipped* (their packets as missing);
* if t jumps backwards by more than 1024 blocks (firmware re-sync), it **restarts** instead of treating
  everything as late.

Per-block and cumulative missing-packet counts travel with the data as attributes, and end up in the
FBH5 file (§3.2), which is the hook for flagging later.

### 2.4 Buffer lifecycle (the part that needs care)

```
free ──acquire──► open (being scattered into) ──seal──► sealed (kernels queued; event recorded)
  ▲                                                          │ event complete
  │                                                          ▼
  └── release event on scheduler stream ◄── emitted ◄── ready (computeSubmit swaps it in)
```

* `computeSubmit()` (scheduler thread) swaps the ready block's storage into the published output tensor.
  The storage it gets back is the block emitted in the previous cycle, which downstream kernels may
  still be reading on the GPU. So it records a `released` event on the scheduler's stream; when the
  receive thread reuses that buffer it first makes its own stream wait on that event. Neither thread
  ever blocks the CPU waiting for the GPU.
* Attributes set at swap time: `timestamp` (t of the block's first spectrum), `lft3.nco0..3`,
  `lft3.pfbLsb0..3`, `lft3.blockMissingPackets`, `lft3.totalMissingPackets`,
  `lft3.totalExpectedPackets`, `lft3.unixTimeAtT0`.
* **Time reference**: the firmware's t counts from the last software sync, not from a PPS edge (see
  `docs/firmware_requests.md` #4). The receiver estimates the UNIX time of t = 0 from the first packet's
  arrival (~ms accuracy) and records that this is an estimate; `syncTimeUnix` in the writer overrides it.

### 2.5 Start-up

DAQIRI starts receiving when the block is created, but the rest of the flowgraph is created after it.
So the receive thread only starts on the scheduler's first `hasPendingCompute()`. It then throws away the
backlog DAQIRI queued in the meantime, and ignores packets until the first block boundary
(t multiple of 1024, first bindex), so the first block is complete. In headless runs the first block is
complete. With the UI (`--remote`) the UI is still starting when data flows, and a few blocks at
start-up can be partial or skipped. That is visible in the log and in `lft3_missing_packets`, and harmless.

### 2.6 Synthetic source

`source: synthetic` replaces DAQIRI with an in-memory generator (noise + one complex tone at a chosen
channel/input/offset, optional random packet drops) that goes through the same assembler, scatter and
sealing code. No root, no board. Real-time paced or as fast as possible. Used for every end-to-end test
below.

## 3. lft3_finespec and lft3_fbh5_writer

### 3.1 Fused fine spectrometer (`pipeline/src/finespec/`)

One CUDA thread block per (coarse channel, beam), `fftLength/2` threads:

```
for each segment m:   load 128 X and 128 Y samples (contiguous, coalesced; int8 → float)
                      radix-2 FFT of X and Y in shared memory
                      acc[bin] += |X|² + |Y|²                 (Stokes I)
out[beam][channel*128 + fftshift(bin)] += acc                 (accumulates over integrationBlocks)
```

The output is zeroed at the start of each integration and emitted every `integrationBlocks` blocks
(149 × 1024 spectra = 0.99975 s); in between the module returns SKIP so the writer does not run.
`binsPerOutput` > 1 sums groups of fine channels in the same kernel (atomic adds).

`mode: coarse` skips the FFT entirely: each CUDA block sums |X|²+|Y|² over all samples of one coarse
channel (the board's own 152.6 kHz channels), and `binsPerOutput` then groups *coarse* channels. This is
what the live monitor uses. It is numerically the fine spectrum summed per channel divided by fftLength
(Parseval; unit-tested), without the FFT arithmetic.

Why fused: per 6.7 ms block at full band, the separate-pass chain moves ~1.3 GB (CF32 scatter 201 MB,
then read+write for the sign flip, the FFT and detection, then integration) ≈ 190 GB/s at real time,
on a 273 GB/s memory bus shared with the CPU and NIC. The fused kernel reads the 50 MB of int8 voltages
once and touches 6 MB of output: ~8 GB/s.

| Measured (synthetic, full band, 2 beams, 1.19 kHz) | Stelline chain (CF32) | Fused (CI8) |
|---|---|---|
| As fast as possible | 0.59× real time | **3.2× real time** |
| Real-time paced | drops ~40% of blocks | 1.00×, 0 no-buffer drops |

Limit: `fftLength` ≤ 2048 (a shared-memory FFT). For Hz-level resolution (2^16–2^17) the same block
interface needs a cuFFT path with load/store callbacks (what rawspec does) and channel batching; the
receiver layout already supports `segments = 1, fftLength = 2^17` but not the memory for 6144 channels
at once, so it would also use `channelStart`/`channelCount` subbands.

### 3.2 FBH5 writer (`pipeline/src/fbh5_writer/`)

Writes the Breakthrough Listen filterbank-HDF5 layout via **filterbankc99** (the library behind
rawspec), with the default POSIX HDF5 driver. One file per beam:
`<outputDirectory>/<filePrefix>_<UTC>_beam<N>.h5`, rotated every `spectraPerFile` spectra.

* `/data` float32 `[time, 1, nchans]` with SIGPROC attributes. blimpy reads it (checked).
* Frequency axis: `foff = 152.587890625 kHz / fftLength` (positive, ascending),
  `fch1 = −ncoMHz + (channelStart − 3072) × 152.587890625 kHz − (fftLength/2) × foff`.
  `ncoMHz` should be set to the exact programmed NCO (the packet header only has whole MHz).
* Time axis: `tsamp` is *measured* from the spacing of consecutive integrations' timestamps, so the
  integration length is configured only once (in lft3_finespec). Files open when the second spectrum
  arrives. A change in spacing (re-sync, lost whole blocks) closes the files and starts new ones.
* Extra datasets: `/lft3_timestamp` (t of each integration's first spectrum) and
  `/lft3_missing_packets` (zero-filled packets in each integration).
* Root attributes `lft3_*`: exact and header NCOs, pfb_lsb, layout, time reference and how it was
  obtained, software version.

Output volume at the current settings: 786 432 channels × 4 B per beam per second ≈ 3.1 MB/s per beam,
~22 GB/hour for two beams.

## 4. Live monitor (waterfall in a browser)

`pipeline/examples/lft3-monitor.yml` is the spectrometer plus a display branch. The branch does not change
what is written to files:

```
receiver ──► finespec (fine mode, 1 s, full resolution) ──► FBH5 writer
         └─► quicklook: lft3_finespec mode coarse (no FFT), ~0.1 s, 6 coarse channels per point → [2, 1024]
               └─► per beam: slice → copy to CPU → amplitude (dB) → range → lineplot + waterfall
```

The quick-look reads the voltages once more (~8 GB/s of GPU memory traffic). Both together still run at
1.00× real time. Each display point is 6 coarse channels (0.916 MHz), so every channel reaches some pixel:
the plots are ~1000 px wide, and with all 6144 channels single-channel lines were drawn only when they
landed on a drawn column. The monitor flowgraphs use `outputPoolSize: 24` so the receiver can absorb the
slow first cycles while the display blocks start up. `lft3-monitor-synthetic.yml` is the same with the synthetic source.

CyberEther **Remote** streams the whole UI to a browser: flowgraph, every block's metrics (the
receiver's counters live), lineplots and waterfalls. Start it with `--remote`:

```bash
sudo pipeline/scripts/run.sh --remote pipeline/examples/lft3-monitor.yml
```

The terminal prints `Join URL: http://<address>:8686/remote#<token>`. Open it in a browser, using an address of
the Spark your machine can reach (e.g. `192.168.2.101` on the lab network, not necessarily the one printed).
The browser shows a 6-character code. If it matches the one in the terminal, answer `y`. From another
network, tunnel the broker over SSH (CyberEther docs, "Streaming Over SSH").

What you see:

* x axis: fraction of the band, 0..1 (CyberEther does not know our frequencies): 0 = 1562.5 MHz,
  0.5 = NCO centre 2031.25 MHz, 1 = 2500 MHz; RF = 1562.5 + 937.5 × x MHz (point = 1024 × x, coarse
  channel ≈ 6144 × x).
* Colour / y axis: `amplitude` computes 20·log10 of the summed power, with no normalisation because the
  sliced tensor has no time axis. That is a *doubled* dB scale: a 10 dB power change shows as 20. The
  `range` block maps 145..180 onto 0..1. The live floor is ~152 (≈0.2) and the board spurs (diluted 6×
  by the grouping) ~165–174 (≈0.6–0.8). If the
  waterfall is one flat colour, the range is wrong: change `min`/`max` on `beamN_range` in the UI. The floor
  depends on the requantisation (`ch_lsb_select`).
* One waterfall line per ~0.1 s, 512 lines (~50 s) of history.
* With nothing connected: a flat floor with the board's spurs as vertical lines at 1750, 1875, 2000 and
  2250 MHz (x = 0.200, 0.333, 0.467, 0.733), and a flickering band at x ≈ 0.90–0.91: the Spark's own
  Wi-Fi (wlP9s9, channel 1, 2402–2422 MHz) picked up by the board. Other 2.4 GHz devices show as
  sweeping streaks further right. In the Turbo colour map the top of the scale is dark red, so a strong
  narrow line can look black. A tone injected at an input appears as a new vertical line at its
  frequency, in the beam/polarisation it was connected to.

Rendering: the NVIDIA Vulkan driver does not initialise inside the container (it does on the host;
`vkCreateInstance` fails in the ICD), so the UI renders on the CPU with Mesa llvmpipe and is encoded
in software at 10 fps (~3.5 CPU cores). This does not affect the GPU pipeline. Fixing GPU rendering in the
container (or running CyberEther natively) is a later improvement.

## 5. Running it

Normally with `lft3-pipeline` (docs/operations.md §3), which generates the flowgraph from the board config
(`src/lft3/pipeline/flowgraph.py`) and starts the container. The receiver's `source` can be `network`,
`synthetic` or `file`. `file` replays the whole blocks of a classic pcap through the same assembler and scatter
path (`detail/pcap.cc`), paced in real time or as fast as possible, optionally looped with a continuous time
axis. Synthetic and file sources apply backpressure (wait for free buffers) instead of dropping. The
hand-written flowgraphs in `pipeline/examples/` and `pipeline/scripts/run.sh` still work directly:

```bash
# build + unit tests (no root; runs in the lft3-cyberether container = CyberEther CUDA image + RDMA
# libraries, pipeline/docker/Dockerfile, built automatically on first use)
pipeline/scripts/build.sh test

# end-to-end with synthetic packets (no root, no board): writes /opt/lft3arth/data/synthetic/
pipeline/scripts/run.sh pipeline/examples/lft3-synthetic.yml           # Ctrl-C to stop

# the real stream (root: raw NIC access). Make sure the board is unicasting to enp1s0f0np0 first:
lft3-board set-dest-mac            # destination MAC from config/boards/rfsoc0.yaml
sudo pipeline/scripts/run.sh                                          # pipeline/examples/lft3-spectrometer.yml

# the real stream + live waterfall in a browser (§4)
sudo pipeline/scripts/run.sh --remote pipeline/examples/lft3-monitor.yml
```

Every 5 s the receiver logs one line: rate, blocks emitted and how they relate to real time,
complete / partial / skipped blocks, accepted / missing / late / duplicate / no-buffer packets, queue
depths. Healthy: `1.00x real time`, `no-buffer 0`, `missing` flat. Files written as root are owned by
root.

Check a file:

```python
import h5py, numpy as np
f = h5py.File('/opt/lft3arth/data/spectra/lft3_..._beam0.h5')
d = f['data']; a = d.attrs
freq = a['fch1'] + np.arange(a['nchans']) * a['foff']           # MHz
spec = d[:, 0, :].mean(0)
```

## 6. Validation done

* Unit tests (`pipeline/scripts/build.sh test`, on the GB10): header parse identical to the Python
  decoder; layout arithmetic; assembler ordering / stale / zero-fill / skipped / drop / restart /
  no-buffer; scatter kernel vs CPU reference including a missing packet and a non-default input map;
  fused spectrometer vs a double-precision DFT (S = 2, 16, 128, two-block accumulation) and tone bin;
  FBH5 write + read-back; frequency axis; integration timeline.
* End-to-end (synthetic, real-time paced, 20 s): tone injected at channel 4000, input 1,
  +0.1015625 cycles/sample appears in fine channel 512077 (= coarse 4000, bin 64 + 13) at the predicted
  2172.867060 MHz, 24.7 dB above the median, in beam 0 only; integrations exactly 15 360 spectra apart;
  0.1% injected packet loss reported as such; blimpy opens the files.

## 7. First light (2026-11-12, live board, nothing connected to the inputs)

* 60.8 Gb/s, 1.00× real time for ~1 minute: 50.3 M packets, every block complete after start-up.
  So the board does not drop packets, and its 100GbE `tx_overflow_count` is not a real loss counter.
* Files: tsamp 0.999922 s, integrations exactly 152 576 spectra apart, only the first integration had
  missing packets (the start-up transient, since fixed for headless runs).
* Coarse-channel response: flat to ±0.1 dB over the central half, −3 dB (0.51) at the channel edges.
  No DC spike in the fine FFT.
* Spurs at **1750.0, 2000.0, 2125.0, 2250.0, 2375.0 MHz** (exact 125 MHz multiples, within 0.5 kHz) in both
  beams, plus 1875 MHz at a coarse-channel centre. This confirms the frequency axis and its sign.
  Weaker unexplained lines: 2200.0 MHz (both beams), 1600.84 MHz (beam 0), 2278.125 and 2043.75 MHz (beam 1).

## 8. Known gaps / next steps

1. Inject a known tone to verify the input → beam/polarisation mapping and the absolute frequency.
2. GPU rendering for the remote UI (see §4).
3. **Time**: PPS-aligned sync in firmware (or `syncTimeUnix`) for real timestamps.
4. **Hz resolution**: cuFFT-based spectrometer path + subbands (§3.1), and output-volume decisions.
5. **Flags instead of zero-fill**: the per-integration missing counts are already in the files; a
   per-channel mask (FBH5 `/mask`) is the natural next step.
6. Search paths (hyperseti narrowband, heimdall wideband) and the transient buffer attach to the
   receiver's output tensor or the finespec output; CyberEther lets several blocks consume one output.
