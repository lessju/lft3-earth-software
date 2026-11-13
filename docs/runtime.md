# Real-time runtime: implementation guide

How the LFT3 real-time runtime in `pipeline/` works, why it is built this way, and what it can do on one DGX
Spark. The operator's view (commands, files, troubleshooting) is in `docs/user_guide.md`.

Status (2026-11-13): implemented, unit-tested, running on the live board. Live result: 60.8 Gb/s, zero loss,
1 s fine spectra, quick-look and triggered raw-voltage dumps. It replaced the CyberEther plugin.

## 1. The picture

```
              one process per 100 GbE link ("stream" = <board>-<link>)
             ┌──────────────── lft3-capture (C++/CUDA, privileged for DAQIRI) ────────────────┐
 100 GbE ──► │ DAQIRI RX worker (busy-polls 1 core)                                            │
             │ main thread: parse headers → Assembler (order, gaps, restarts) → scatter kernel │
             │              → seal block (zero-fill) → publish when its kernels completed      │
             └──────────────────────────────────┬───────────────────────────────────────────────┘
                                                │ never waits for readers
     control plane  /dev/shm/lft3-<stream>       │   (head, per-slot metadata, reader cursors, statistics)
     data plane     CUDA VMM allocation, S blocks × [beam][channel][1024 t][pol] int8 (50.3 MB per block)
                    shared by fd over /dev/shm/lft3-<stream>.sock
                                                │ readers attach and leave at any time
        ┌───────────────────────────┬───────────┴──────────────┬────────────────────────────┐
        ▼                           ▼                          ▼                            ▼
 lft3-spectrometer (fine)     lft3-dump                  lft3-spectrometer (coarse)   your reader (C++ or
 FFT 128 → |X|²+|Y|² → 1 s    trigger → copy window      optional, fast quick-look    Python/CuPy via lft3.ring)
 → FBH5 fine + coarse         → GUPPI RAW                → latest snapshot
 + latest snapshot
```

`lft3-pipeline` (Python) plans and launches these processes as containers (image `lft3-runtime`), one set
per link of the board, and reads the control planes for status and Prometheus metrics.

## 2. How we got here: the decisions and the evidence

In the order they were made. Each one is backed by a measurement on this machine.

1. **First implementation: a CyberEther plugin** (receiver → fused fine spectrometer → FBH5 writer). It worked:
   first light at 60.8 Gb/s with zero loss, a 3.2× real-time synthetic chain, and the frequency axis confirmed by
   the board's 125 MHz spurs. But CyberEther runs every block in lock-step on one scheduler thread, so a slow
   consumer stalls the receiver. The receiver then runs out of buffers and drops packets. Every planned addition
   (hyperseti, heimdall, trigger dumps, quick-look) runs at its own pace, and CyberEther's own DSP blocks were too
   slow (0.59× real time), so most of the value was in the UI, which is not needed.
2. **Holoscan 5.0 evaluated** (pip on the Spark, Python benchmark at the real 6.7 ms block cadence).
   * Isolation works: a slow branch on its own partition with a drop-oldest queue left the fast path untouched
     (0 drops, p99 tick 6.97 ms).
   * But 5.0 was three weeks old and a ground-up rewrite. Tensor and queue APIs are Beta, the manual partitions a
     developer preview, and DAQIRI's integration still targets 4.x. A blocking branch also made fan-out emits fail
     for all branches.
   * **Rejected for now**, to be revisited when 5.x settles.
3. **hashpipe compared.** Its building blocks are the same (shared-memory blocks, pinned threads, GPU reading
   pinned memory). But its buffers are semaphore handshakes with one consumer each, which back-pressures the
   network thread: the behaviour we were leaving. Its conventions are kept: GUPPI RAW for dumps,
   hpguppi-style observation keys.
4. **Own runtime: a single-writer ring with independent readers** (the original design note).
   * The writer never waits.
   * A reader that falls behind loses blocks and counts them (seqlock overruns).
   * Readers are separate processes: a crash in a search cannot stop the capture.
5. **Ring layout: blocks, not long rows** (`pipeline/bench/ringbench/`).
   * The first idea was one time-contiguous row per channel across the whole ring. It would let any FFT length
     read contiguous samples.
   * Measured: the scatter (every burst writes 12 288 rows) thrashes the GPU TLB. Write throughput fell from
     82 to **6 GB/s** (7.5 needed) once the ring passed ~6 GB.
   * A ring of 50 MB blocks scatters at **83 GB/s at any ring size**. Long FFTs gather across blocks at
     232 GB/s, so blocks cost long FFTs almost nothing.
6. **Memory type: CUDA VMM, shared by file descriptor.**
   * `cudaHostRegister`'d shared memory (shm, malloc, THP) reads ~30 % slower from the GPU: 172 vs 237 GB/s.
   * A VMM allocation (`cuMemCreate`, 2 MiB pages) exported as a POSIX fd gives the full 237 GB/s in another
     process. `cudaIpc` through CuPy gave 168–205.
   * So voltages live in VMM memory, passed over a unix socket. The small control plane stays in POSIX shm
     (CPU atomics, futex). Hugepages turned out to be unnecessary.
7. **Parity with CyberEther** (same deterministic synthetic input, lossless): identical timestamps, axes and
   attributes over 127 spectra. 92 % of values are bit-identical, the rest within 4.5×10⁻⁶ (float rounding from
   compiling for sm_121 instead of the old plugin's JIT target).
8. **Bug found by the parity run: backpressure margin.**
   * Readers treat blocks older than `head − (slots − 8)` as unsafe. The writer's backpressure must therefore
     stay 8 slots behind the lossless reader (`kReadMargin`), not 0.
   * With 0, the lossless reader skipped ahead and lost 4 217 blocks.
   * Fixed; a unit test covers it.
9. **Profiling → quick-look derived from the fine spectrum.**
   * The separate coarse quick-look reader read every block a second time: 0.23 ms per block and 7.5 GB/s per
     stream.
   * The spectrometer now computes the coarse power per board channel from its own fine spectrum (sum of a
     channel's 128 bins / 128, Parseval). It agrees with the direct computation to 2.6×10⁻⁶ and costs no GPU time.
   * The coarse reader remains as an option for a faster cadence (`--quicklook 0.1`).
10. **FFT kernel experiments.** A shared-memory twiddle table (bit-identical results) was *slower* (543 vs 504 µs
    per block). 4-byte loads alone were neutral (501 µs). The fused kernel is bound by latency and
    `__syncthreads` (64-thread CUDA blocks, 56 synchronised stages per block), not by arithmetic. The next step
    for more capacity is a warp-shuffle radix-4 FFT with several channels per CUDA block (see §7).
11. **Capacity measured** (§6): 4 streams with margin and 5 at the limit of compute. The network allows 2 links
    per Spark with direct cables.

## 3. Code map

| Path | What |
|---|---|
| `pipeline/src/common/packet.hh` | LFT3 channel-packet constants and big-endian header parser |
| `pipeline/src/common/layout.hh` | Ring block layout `[beam][channel][t][pol]`, packet slots, channel selection |
| `pipeline/src/common/ring.{hh,cc}` | Control block (fixed binary layout, static_asserts), `RingWriter`, `RingReader`, VMM + fd passing, futex |
| `pipeline/src/common/util.hh` | Logging (UTC timestamps), options, clocks, pinning, stop signals |
| `pipeline/src/capture/assembler.{hh,cc}` | Packet bookkeeping: blocks, duplicates, late packets, sealing in order, zero-fill lists, restarts |
| `pipeline/src/capture/scatter.{hh,cu}` | Tiled scatter kernel (32 jobs × 32 channels through shared memory) + CPU job sort |
| `pipeline/src/capture/daqiri_config.{hh,cc}` | DAQIRI raw-ibverbs RX config: one queue, header/data split, flow rule |
| `pipeline/src/capture/pcap.{hh,cc}` | Classic pcap reader for the file source |
| `pipeline/src/capture/main.cc` | `lft3-capture`: sources (network/synthetic/file), batches, sealing, publishing, statistics |
| `pipeline/src/spectrometer/finespec.{hh,cu}` | Fused fine spectrometer kernel (shared-memory FFT ≤ 2048, Stokes I, segment sum) and coarse power kernel |
| `pipeline/src/spectrometer/fbh5_file.{hh,cc}`, `axes.hh` | FBH5 writer (filterbankc99; chunked, shuffle + deflate), frequency axes |
| `pipeline/src/spectrometer/main.cc` | `lft3-spectrometer`: integration windows, writer thread, fine/coarse/latest products |
| `pipeline/src/dump/{guppi,main}` | `lft3-dump`: trigger socket, window copy, GUPPI RAW writer |
| `pipeline/tests/` | catch2: packet, layout, assembler, scatter vs CPU, ring protocol, finespec vs DFT, FBH5, GUPPI |
| `pipeline/bench/ringbench/` | Memory-type and layout benchmarks behind decisions 5 and 6 |
| `pipeline/bench/capacity/` | Multi-stream capacity benchmark + NIC-DMA memory-load stand-in |
| `pipeline/docker/Dockerfile` | `lft3-runtime` image: CUDA 13 devel + rdma-core + meson/cmake (also builds `tools/viewer`) |
| `src/lft3/ring.py` | Python mirror of the control block: status, slot metadata, Prometheus text, dump commands, CuPy access |
| `src/lft3/pipeline/runtime.py` | Run planner: board config + options → per-process `docker run` commands |
| `src/lft3/cli/pipeline.py` | `lft3-pipeline run / status / trigger / stop` |

## 4. The data path in detail

### 4.1 Capture (`lft3-capture`)

1. DAQIRI splits each frame into two segments:
   * the header segment (Ethernet/IP/UDP + LFT3 header, 106 B) in pinned host memory;
   * the 8192-B payload in pinned host memory, which the integrated GPU reads in place. The Spark has no
     GPUDirect RDMA and needs none.
2. Per burst (≤ 1024 packets), the main thread parses each header. It filters on board id and channel selection.
   It discards everything before the first block boundary, so the first block is complete. Each packet then goes
   to the **Assembler**:
   * `(t, bindex)` → block `t / 1024`, slot `(t mod 1024) × packets_per_spectrum + bindex`;
   * duplicates and late packets are rejected;
   * at most `max_open_blocks` (4) are open;
   * blocks are sealed strictly in time order: when complete, or when more than `max_block_lag` (2) blocks behind
     the newest;
   * if t jumps back more than 1024 blocks, the stream has restarted (re-sync).
3. A new block gets the **next ring slot in allocation order**: `RingWriter::acquireSlot`. Slots are not tied to
   block numbers, because a restart would otherwise collide with blocks still being sealed. Acquiring invalidates
   the slot (`seq = invalid`) before any GPU write.
4. Jobs (payload pointer, block, t, channel offset) are sorted by channel group on the CPU. That makes each tile of
   32 jobs consecutive in t. One **scatter kernel** per burst then writes `[beam][channel][t][pol]` with 128-byte
   runs. The burst is returned to DAQIRI only after its kernel has finished (CUDA event).
5. Sealed blocks with missing packets are zero-filled by the same kernel with null sources. An event is recorded.
   When it has completed, the block is **published** in time order:
   * the slot metadata is written: block index, t0, missing packets, restart count, header NCO and pfb_lsb,
     estimated UNIX time of t = 0;
   * `slot.seq = n`, `seqToSlot[n % 4096] = slot`, `head = n + 1`;
   * futex waiters are woken.
6. Statistics go straight into the control block: packets received, accepted, filtered, duplicate, late, no-slot
   and missing; blocks complete, partial and skipped; restarts; Gb/s. Capture also logs a summary every 5 s.

Sources: `network` (DAQIRI), `synthetic` (deterministic noise + tone, bit-identical to the old generator) and
`file` (replays whole blocks of a pcap, optionally looped). Synthetic and file sources can run `--fast` and with
`--backpressure` / `--wait-readers N`, so tests and benchmarks are lossless.

### 4.2 The ring protocol (`common/ring.hh`)

* **Readers** follow these steps:
  1. `attach`: open the control shm, receive the VMM fd, map it **read-only**, register an entry
     (name, cursor = head, heartbeat).
  2. `wait(n)`: futex until `head > n`.
  3. `begin(n)`: `slot = seqToSlot[n % 4096]`, check `slot.seq == n`, copy the metadata.
  4. Run the GPU work on `blockPtr(slot)` and synchronise.
  5. `stillValid(n)`: `slot.seq == n` again; if not, the slot was reused while being read → **overrun**, discard.
  6. `consumed(n + 1)`: cursor, lag, heartbeat.
* **Lag rule**: blocks older than `head − (slots − kReadMargin)` (margin 8) are unsafe. A reader behind that
  jumps forward and counts the skipped blocks as overruns.
* **Backpressure** (synthetic/file only): before reusing a slot, the writer waits until every *lossless* reader
  with a fresh heartbeat (< 5 s) has a cursor beyond that slot's last sequence + `kReadMargin`.
* **Lifecycle**: the capture creates the control block and VMM allocation at start and sets `state = running`. On
  exit it sets `stopped`, wakes the readers, and unlinks the shm and socket. Attached readers keep their mappings,
  so they can drain what was published.
* **Layout contract**: the control block has fixed offsets, `static_assert`ed in `ring.cc` and mirrored in
  `src/lft3/ring.py` (`tests/test_ring.py` builds one byte by byte). Change both together and bump `kRingVersion`.

### 4.3 Spectrometer (`lft3-spectrometer`)

* **Integration windows are aligned to absolute block numbers**: window w = blocks `[wN, (w+1)N)`, N = round(1 s ×
  rate / 1024) = 149 → 0.99992 s. Every reader and every link therefore shares one time grid. A reader that
  attaches mid-window starts at the next boundary. `lft3_timestamp` is the window's first spectrum t.
* **Fine mode**: per block, one fused kernel launch over (channel, beam). It loads 128 dual-pol samples per
  segment, does a radix-2 FFT in shared memory, adds |X|²+|Y|² over the 8 segments of the block, then adds the
  result into the accumulator. At the window end the accumulator is copied to the host and handed to the writer
  thread, which uses a 4-buffer pool.
* **Coarse product** (fine mode with `--coarse-dir` / `--latest`): per board channel, the sum of its fine bins
  divided by the FFT length. This equals the coarse-mode power computed from the voltages.
* **Missing data** per spectrum = zero-filled packets + blocks the capture skipped + blocks this reader lost,
  in packets. A window whose data was overwritten during the read is discarded. A gap of whole windows, a restart,
  or t going backwards starts new files. Files rotate every 3600 spectra; `lft3_timestamp` stays continuous across
  rotation.
* **FBH5 output**: unchanged from the CyberEther version.
  * File names: `<prefix>_<UTC>_beam<N>.h5`, N = link beam offset + 0/1.
  * `/data` is chunked per 64 Ki channels with shuffle + deflate 1; `/mask` uses deflate 4.
  * Also present: `/lft3_timestamp` and `/lft3_missing_packets`; the SIGPROC attributes (`fch1`, `foff`,
    `tsamp`, `tstart`, `nfpc`, …); `lft3_*` root attributes (NCO, pfb_lsb, layout, time reference).
  * New attributes: `lft3_stream`, `lft3_obs_id`, `lft3_config_hash`, `lft3_mode`, `lft3_beam_offset`.
* **Storage and volume**: chunks are one spectrum × 65 536 channels, so a frequency range decodes only its chunks.
  `/data` shuffle + deflate 1 makes real spectra 1.43× smaller; the all-zero `/mask` shrinks ~1000× with
  deflate 4. That needs the h5dsc99 patch `shuffle_deflate.diff`. Fine spectra are 3.1 MB/s per beam before
  compression and ≈ 2.2 MB/s on disk: ~16 GB/hour per link (2 beams). The coarse product is ~1/128 of that.

### 4.4 Dump (`lft3-dump`)

* **Trigger**: `trigger <now|UNIX> [pre] [post] [label]` on `/dev/shm/lft3-<stream>-dump.sock`. `lft3-pipeline
  trigger` sends the same UNIX time to every link and appends to `<data_dir>/triggers.log`.
* **Window**: `[t − pre, t + post)` in whole blocks, located through the capture's UNIX time of t = 0. Blocks
  still in the ring, or arriving later, are copied by the GPU into a pinned host buffer. The buffer is allocated at
  start (7.5 GB per second of window).
* **Output**: GUPPI RAW, one file per beam. The header (80-character cards) carries `BLOCSIZE`, `OBSNCHAN`,
  `NPOL=4`, `NBITS=8`, `OBSFREQ`, `OBSBW`, `CHAN_BW`, `TBIN`, `PKTIDX` (= t0), `STT_*`, plus `LFT3*` / `TRIG*`
  cards. The data is exactly one beam of a ring block. A sidecar JSON lists the trigger and any missing blocks.
* Measured live: a 1 s window (150 blocks, 7.5 GB) was collected in 0.51 s and written in 2.8 s.

### 4.5 Orchestration (`lft3-pipeline`, `lft3.pipeline.runtime`)

* `plan_run(board, RunOptions)` → for each link: a capture (privileged with `--net host` for the network source,
  pinned to the link's 3 cores) and readers (unprivileged, `--net none`, the caller's uid and groups, on the
  link's `reader_cpus`). All containers share the host IPC namespace.
* `run` starts each capture, waits for its ring, then starts its readers. It pumps each process's output to
  `<run_dir>/<stream>-<role>.log`, prints a status line from the rings, appends `status.jsonl`, logs NIC counters
  per link, and optionally serves Prometheus metrics.
* Stopping (Ctrl-C, `--duration`, `stop`): readers first (they close files), then captures.

## 5. Adding a reader

**C++**: link `lft3_common`, then follow the reader steps of §4.2:

```cpp
Lft3::RingReader ring;
ring.attach("rfsoc0-link0", "mysearch", /*lossless=*/false, 30.0, error);
std::uint64_t seq = ring.entry().cursor.load();
while (running) {
    if (ring.wait(seq, 200) != Lft3::RingReader::Wait::Ready) continue;
    if (seq < ring.oldestReadable()) { seq = ring.oldestReadable(); continue; }       // fell behind
    Lft3::RingSlotMeta m; const std::uint8_t* block;
    if (!ring.begin(seq, m, block)) { ++seq; continue; }                               // overwritten
    launch_my_kernel(block, ring.layout(), stream); cudaStreamSynchronize(stream);
    if (!ring.stillValid(seq)) discard_result();
    ring.consumed(++seq);
}
```

**Python** (e.g. hyperseti on CuPy; any Python ≥ 3.8, CuPy needed only for the voltages):

```python
from lft3.ring import Ring
r = Ring("rfsoc0-link0")
v = r.voltages()                       # CuPy uint8 [slots, block_bytes], read-only VMM mapping
seq = r.head - 1
m = r.slot_meta(seq); s = r.slot_index(seq)
blk = v[s].view("int8").reshape(2, 6144, 1024, 2, 2)    # [beam, channel, t, pol, re/im]
... ; assert r.slot_meta(seq) is not None               # still valid after reading
```

The Python reader does not register a cursor: it is lossy and invisible to backpressure. Register through the
C++ API if a reader needs that.

## 6. Performance and capacity

**Per stream (one link, full band, 128-point FFT), measured with Nsight Systems:**

| Work | GPU time per 6.71 ms block | Notes |
|---|---|---|
| Scatter (6 launches of ≤ 1024 packets) | 0.50 ms (7.4 %) | ~200 GB/s: memory-bound |
| Fine spectrometer | 0.50 ms (7.5 %) | ~100 GB/s: latency/sync-bound |
| Coarse quick-look (now derived on the CPU) | 0 (was 0.23 ms) | |
| CPU | 1 core busy-polling (DAQIRI worker), ~4 % processing thread, ~18 % spectrometer | |
| Memory | ~23 GB by default: ring 2 s = 15 GB VMM, dump window 1 s = 7.5 GB pinned, DAQIRI 0.5 GB | |

**Capacity on one Spark** (`pipeline/bench/capacity/capacity.py`):
* N real-time synthetic streams, each with a recording spectrometer (fine FBH5 + derived coarse FBH5 + latest
  snapshot).
* Plus N × 7.6 GB/s of host-memory writes standing in for the NICs' DMA.
* Pass = every capture at real time, no reader overrun, lag < 25 % of the ring.

| Streams (2 beams each) | Gb/s | Result | GPU utilisation | worst reader time per block |
|---|---|---|---|---|
| 1 | 60.8 | pass | 17 % | |
| 2 | 121.6 | pass | 46 % | |
| 3 | 182.4 | pass | 71 % | |
| 4 | 243.2 | pass | 91 % | |
| 5 | 304.0 | pass, no margin | 96 % | 4.4 ms of 6.7 |
| 6 | 364.8 | **fail**: spectrometers overrun (captures still at real time, no packet loss) | 96 % | 9.2 ms |

Utilisation grows faster than linearly because the streams compete for the shared 273 GB/s LPDDR5x bus, which
slows every kernel. At 6 streams the isolation worked as designed: every capture stayed at real time, and only
the readers lost blocks.

**Network limits** (from `docs/architecture.md` and DAQIRI's Spark report; not measurable here with one board):
* Each of the Spark's two QSFP ports is two ~100G PCIe halves.
* One 100G cable from a board lands on one half.
* DAQIRI measured 199 Gb/s loss-free on two links.

**Answer for 5 boards × 2 streams (10 streams, 20 beams, 608 Gb/s):**

* **One Spark per board** (2 direct-attached links = 2 streams, 4 beams, 122 Gb/s) is the configuration that is
  known to work. It uses 46 % of the GPU, which leaves room for the planned searches. **5 Sparks.**
* **Two boards per Spark** (4 streams) fits the compute (91 % GPU) but leaves no GPU for searches. It needs
  4 links into one Spark, i.e. a switch or breakout into its two 200G ports with flows steered to all four PCIe
  halves (unverified), and ~76 GB of memory (ring 1.5 s, dump 1 s per link). **3 Sparks** (4 + 4 + 2 streams).
* 5 streams per Spark is the compute limit with no margin. 6 or more do not fit.

## 7. Open items and next steps

* **Faster FFT kernel** (warp-shuffle radix-4, several channels per CUDA block). It would raise compute capacity
  and leave GPU for searches at 4 streams. Fine spectrometer time is the binding cost under contention.
* **Hz resolution** (Nf = 2^16–2^17): cuFFT with load callbacks gathering 64–128 blocks per channel row (232 GB/s
  measured gather); ring ≥ 2 × FFT length (12.9 GB at 2^17). See `docs/architecture.md` for output volume.
* **Searches**: hyperseti as a Python reader on the fine spectra or voltages; heimdall fed from coarse
  detection.
* **Second link**: config entry ready (commented in `config/boards/rfsoc0.yaml`). Confirm its PCI function, UDP
  port and source IP when it is cabled.
* `CAP_NET_RAW` instead of a privileged container; time reference from a PPS-aligned sync
  (`docs/firmware_requests.md`).
* The two rare 13–30 ms gaps at DDR dumps (2 in 121) are still unattributed. `nic-<link>.csv` is logged on every
  network run to settle it.
