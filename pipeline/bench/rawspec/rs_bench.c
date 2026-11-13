// Times librawspec on LFT3 blocks (one beam: 6144 channels x 1024 spectra x 2 pols, int8 complex).
//   rs_bench NFFT NINT ITERS DUMP.raw
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include "rawspec.h"

static int dumps = 0;
static void on_dump(rawspec_context *ctx, int product, int cb) { (void)ctx; (void)product; if (cb == RAWSPEC_CALLBACK_POST_DUMP) dumps++; }
static double now(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec + t.tv_nsec * 1e-9; }

int main(int argc, char **argv) {
    if (argc < 5) return 2;
    rawspec_context ctx; memset(&ctx, 0, sizeof(ctx));
    ctx.No = 1; ctx.Np = 2; ctx.Nant = 1; ctx.Nc = 6144; ctx.Ntpb = 1024; ctx.Nbps = 8; ctx.Nbc = (unsigned)(getenv("NBC") ? atoi(getenv("NBC")) : 1);
    ctx.Nts[0] = (unsigned)atoi(argv[1]); ctx.Nas[0] = (unsigned)atoi(argv[2]); ctx.Npolout[0] = 1;
    ctx.dump_callback = on_dump;
    if (rawspec_initialize(&ctx)) { fprintf(stderr, "init failed\n"); return 1; }
    const size_t bs = (size_t)6144 * 1024 * 2 * 2;
    FILE *f = fopen(argv[4], "rb");
    // fill each host block from the dump: skip each block's header (find END card)
    for (unsigned b = 0; b < ctx.Nb_host; b++) {
        char card[81] = {0};
        do { if (fread(card, 1, 80, f) != 80) { rewind(f); fread(card, 1, 80, f); } } while (strncmp(card, "END", 3) != 0);
        if (fread(ctx.h_blkbufs[b], 1, bs, f) != bs) { rewind(f); b--; continue; }
    }
    fclose(f);
    const int iters = atoi(argv[3]);
    double tcopy = 0, tproc = 0;
    for (int i = 0; i < iters; i++) {
        double t0 = now();
        rawspec_copy_blocks_to_gpu(&ctx, 0, 0, ctx.Nb);
        rawspec_wait_for_completion(&ctx);   // copies are on the compute stream; wait gives copy time
        double t1 = now();
        rawspec_start_processing(&ctx, RAWSPEC_FORWARD_FFT);
        rawspec_wait_for_completion(&ctx);
        double t2 = now();
        if (i > 0) { tcopy += t1 - t0; tproc += t2 - t1; }   // skip warm-up
    }
    const double blocks = (double)ctx.Nb * (iters - 1);
    printf("Nbc %u, Nfft %u, Nint %u, Nb %u: per 1024-spectrum block of one beam: copy %.3f ms + fft/detect/integrate %.3f ms "
           "(real time 6.711 ms); dumps %d\n", ctx.Nbc, ctx.Nts[0], ctx.Nas[0], ctx.Nb, 1e3 * tcopy / blocks,
           1e3 * tproc / blocks, dumps);
    rawspec_cleanup(&ctx);
    return 0;
}
