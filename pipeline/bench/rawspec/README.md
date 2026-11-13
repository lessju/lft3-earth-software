# rawspec comparison (docs/runtime.md §2, decision 14)

`rs_bench.c` times librawspec on LFT3 blocks (one beam: 6144 channels × 1024 spectra × 2 pols, int8),
filled from a GUPPI RAW dump written by `lft3-dump`.

Build rawspec (UCBerkeleySETI/rawspec @ a5cc3b0) for GB10 with CUDA 13:

1. Delete the `CUFFT_INCOMPLETE_PARAMETER_LIST`, `CUFFT_PARSE_ERROR` and `CUFFT_LICENSE_ERROR` cases from
   `cufft_error_name.h` (these enums no longer exist in CUDA 13).
2. For Nf = 2^17: `#define LOAD_TEXTURE_WIDTH_POWER 17` in `rawspec_gpu.cu` (the default 15 exceeds the
   maximum 2D texture height with a 3.2 GB input buffer).
3. Build in a container with HDF5 headers: `lft3-runtime` + `apt-get install libhdf5-dev`, then
   `make SMS=121 CUDA_ROOT=/usr/local/cuda`.

```bash
gcc -O2 -o rs_bench rs_bench.c -I$RAWSPEC -L$RAWSPEC -lrawspec -L/usr/local/cuda/lib64 -lcudart
NBC=64 ./rs_bench 128 1192 6 /opt/lft3arth/data/dumps/rfsoc0/<dump>_beam1.0000.raw     # 1.19 kHz, 1 s
NBC=0  ./rs_bench 131072 1 3 /opt/lft3arth/data/dumps/rfsoc0/<dump>_beam1.0000.raw     # 1.16 Hz
```

Results on the Spark (2026-11-13), per 6.71 ms block of one beam:

| Nf | rawspec copy + compute | ours |
|---|---|---|
| 128 (Nbc 64, best) | 0.63 + 9.2 ms | 0.15 ms (warp kernel, both beams 0.30 ms) |
| 131072 | 5.2 + 13.2 ms (cannot keep up) | — (plain cuFFT floor: 3.1× real time per beam, FFT only) |

`rawspec` (the file tool, `-f 128 -t 1192`) gives the same spectra as ours after its 1/127 sample scaling: median
relative difference 7.6×10⁻⁸, same channel order. nsys profiling of rawspec failed (pinned-buffer copies error
under nsys), hence this harness.
