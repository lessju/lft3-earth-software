# External libraries (not vendored; clone at a pinned commit)

| Library | Role | Source | Licence |
|---|---|---|---|
| DAQIRI | kernel-bypass receive (raw ibverbs / DPDK), flow steering, HW reorder | https://github.com/NVIDIA/daqiri (release v2026.11.00 at time of writing) | Apache-2.0 |
| rawspec / librawspec | GPU fine channelisation, detection, integration, FBH5/SIGPROC output | https://github.com/UCBerkeleySETI/rawspec | check repo |
| hashpipe (alternative receiver) | ibverbs capture + shared-memory ring buffers | https://github.com/david-macmahon/hashpipe | check repo |

Build notes (DGX Spark, Arm64, CUDA 13):
* DAQIRI: its DGX Spark benchmark was built Release with `DAQIRI_ENGINE="dpdk ibverbs"`.
  The repo ships an onboarding guide (`skills/daqiri-onboarding/`) and config generators/validators.
* rawspec: `make SMS=<GB10 compute capability without dot>`. The Makefile defaults to SMS 35–75.
  rawspec links static cuFFT for its load/store callbacks, which are legacy-style `cufftXtSetCallback`.
  **Risk to check first:** whether the CUDA 13 cuFFT still accepts legacy callbacks. If it does not, port the two callbacks
  to cuFFT LTO callbacks; the rest of the library is unaffected.
