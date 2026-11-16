# DGX Spark receive + spectrometer architecture (one RFSoC 4x2 board, one 100G link)

> **Status (2026-11-13):** implemented as the ring-buffer runtime in `pipeline/` (`docs/runtime.md`): DAQIRI capture →
> GPU voltage ring → fused fine spectrometer (1.19 kHz, interim) → FBH5, plus GUPPI RAW dumps. The rawspec
> design and sizing below remain the plan for Hz-level resolution, which needs cuFFT-sized FFTs. The
> "corner turn" here is the receiver's scatter kernel. The 4 inputs are 2 dual-pol beams, Stokes I formed on the GPU.

```
RFSoC 4x2 ──100GbE──► ConnectX-7 (one PCIe Gen5 x4 half, ~95 Gb/s usable)
                         │ hardware flow steering by UDP dst port
          ┌──────────────┴──────────────┐
   channel stream (60.5 Gb/s)     ADC dump (10 Gb/s, triggered)
          │ DAQIRI raw-ibverbs RX queue 0          │ DAQIRI RX queue 1
          ▼ host_pinned buffers (unified memory)   ▼
   corner-turn kernel (GPU)                   raw writer → NVMe (GUPPI-like or raw packets)
   (t, b, ch, beam) → [beam][k][t] blocks
          ▼
   rawspec (librawspec, cuFFT with load/store callbacks)
     product 0: fine,   Nt = 2^17 → 1.164 Hz,   Na ≈ 12 (≈10 s)
     product 1: coarse, small Nt, ms integrations (monitoring / RFI / quick-look)
          ▼ dump callback
   FBH5/SIGPROC files, and/or reduced spectra → lft3-view or another viewer
```

## Why these components

**Receiver: NVIDIA DAQIRI** (Apache-2.0, part of the Holoscan family).
* Lists DGX Spark as supported hardware.
* Has raw-ibverbs and DPDK engines, hardware flow steering, CPU/GPU memory regions, C++ and Python APIs.
* Its DGX Spark report shows 199 Gb/s loss-free on two links with 8000-B payloads, with one CPU core polling both queues.
* The same report shows a plain UDP socket tops out at ≈25 Gb/s per core at 8 KB, so sockets are not an option at 60–70 Gb/s.
* GPUDirect RDMA is not supported on Spark. Use `host_pinned` regions, which the iGPU reads in place.

Alternative: **hashpipe + hpguppi_daq ibverbs capture** (Breakthrough Listen / ATA / COSMIC heritage, plain C).
Choose this if you want the standard radio-astronomy ring-buffer framework. DAQIRI is the
NVIDIA-supported path on this exact hardware.

**Fine spectrometer: rawspec** (UC Berkeley SETI, the Breakthrough Listen high-spectral-resolution product, e.g. 2.79 Hz).
Its source confirms:
* A library API (`rawspec_initialize`, `rawspec_copy_blocks_to_gpu`, `rawspec_start_processing`), with client-managed host block buffers.
* `Np` = 1..2, `Nant` antennas (the 4 beams map to Nant = 4), and up to 4 simultaneous (Nt, Na) output products.
* Coarse-channel batching (`Nbc`) to bound GPU memory.
* cuFFT **load callback** (8-bit → float) and **store callback** (detect + accumulate). These fused passes are what
  keep it inside Spark's memory bandwidth (see below).
* Output to SIGPROC/FBH5 or UDP.

## rawspec mapping for one board

| rawspec field | Value | Note |
|---|---|---|
| Nant | 4 | one "antenna" per beam |
| Nc | 24 576 | 4 beams × 6144 channels, beam-major |
| Np | 1 | |
| Nbps | 8 | |
| Ntpb | 16 384 | block = 24 576 × 16 384 × 2 B = 805 MB (≈107 ms) |
| Nts[0], Nas[0] | 131 072, 12 | 1.164 Hz, 10.3 s per dump |
| Nb | 8 | blocks per 2^17 FFT (6.4 GB input buffer) |

Block layout required by rawspec (GUPPI RAW order): `[beam][k][t]`. The packets arrive as `(t, b, ch, beam)`, so a
**corner-turn kernel** has to sit between DAQIRI and rawspec. It is the main piece of new code. It runs on the GPU,
reads the DAQIRI buffers in place, zero-fills missing packets, and counts them per block.

## Sizing: full band, 4 beams (3.75 G complex samples/s per board)

| Fine FFT | Δf | FFT length in time | int8 input per FFT | Fine channels | One spectrum | Output at ≈10 s integration |
|---|---|---|---|---|---|---|
| 2^16 | 2.33 Hz | 0.43 s | 3.2 GB | 1.6 G | 6.4 GB | 0.65 GB/s |
| **2^17** | **1.16 Hz** | **0.86 s** | **6.4 GB** | **3.2 G** | **12.9 GB** | **1.25 GB/s** |
| 2^18 | 0.58 Hz | 1.72 s | 12.9 GB | 6.4 G | 25.8 GB | 2.5 GB/s |
| 2^19 | 0.29 Hz | 3.44 s | 25.8 GB | 12.9 G | 51.5 GB | 5.0 GB/s |

* **Compute** is not the limit: about 320 GFLOP/s of FFT for 2^17.
* **Memory bandwidth** is the limit. GB10 has 273 GB/s, shared by CPU, GPU and NIC.
  * A fused implementation (callbacks) moves ≈120 GB/s.
  * A naive one (separate convert, FFT, detect and accumulate passes) needs ≈300 GB/s, which is over budget.
  * Both are estimates. Benchmark rawspec on the Spark first.
* **Memory footprint** at 2^17 is roughly 6.4 GB input + 6.4 GB FFT work + 2 × 12.9 GB power buffers ≈ 40 GB of the 128 GB.
* **Output volume is the real constraint.** Full-band Hz-resolution spectra for 4 beams at 10 s integration are
  1.25 GB/s, i.e. 4.5 TB/hour, which fills the Spark's internal NVMe in under an hour. Decide one or more of:
  * restrict the fine product to selected beams or coarse channels (cost scales linearly);
  * integrate longer;
  * run a narrowband hit search on the GPU and keep only hits plus a short ring of full spectra;
  * store reduced precision (8- or 16-bit).

## DGX Spark specifics

* No GPUDirect RDMA. Use host-pinned (`cudaHostAlloc`) or DAQIRI `host_pinned` buffers. On unified memory these cost no copy.
* Each ConnectX-7 port is two ~100G logical interfaces, one per PCIe Gen5 x4 half. A single 100G link from the RFSoC
  lands on one half, so expect ~92–97 Gb/s usable. 60.95 Gb/s continuous, and 71 Gb/s peak during a dump, fits.
* GB10 is Arm64 (10 Cortex-X925 + 10 Cortex-A725). Pin the polling thread and corner-turn launcher to X925 cores.
* rawspec's default `SMS` list stops at sm_75. Build with the GB10 compute capability (`nvidia-smi --query-gpu=compute_cap`).

## Where the viewer fits

Live views and browsing use the reduced products (coarse spectra at ~1 s, zoomed fine-resolution windows):
`lft3-view`, `lft3-plot`, or a reader of the latest snapshot. Nothing interactive sits in the 60 Gb/s path.
CyberEther was used for the first implementation. It was replaced because its lock-step scheduler lets a slow
consumer stall the receiver (`docs/runtime.md` §2).
