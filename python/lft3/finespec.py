"""
Reference (golden-model) two-stage spectrometer for LFT3-Earth channel data.

Stage 1 is the RFSoC 4-tap PFB (8192-pt, 6144 channels kept, 152.587890625 kHz).
Stage 2 (here) FFTs each coarse channel's time series with length Nf, squares,
and integrates Na spectra -- the same maths rawspec performs on the GPU.

This is the correctness reference for the production GPU path, not the
production path itself. It runs with NumPy, or CuPy if installed (xp=cupy),
and processes coarse channels in batches to bound memory.

Frequency convention: output fine channel index  i = k*Nf + j  (k coarse 0..6143,
j fine 0..Nf-1 after fftshift) is monotonic in frequency:
  f_offset(i) = BASEBAND_OFFSET_HZ[k] + (j - Nf//2) * CHAN_BW_HZ / Nf
relative to the NCO centre of the beam (sign/flip of the RF mapping still TBC).
"""
from __future__ import annotations
import numpy as np
from .packets import CHAN_BW_HZ, BASEBAND_OFFSET_HZ, NCHAN

def fine_freq_axis(nf: int, k_sel=None) -> np.ndarray:
    k = np.arange(NCHAN) if k_sel is None else np.asarray(k_sel)
    j = np.arange(nf) - nf // 2
    return (BASEBAND_OFFSET_HZ[k][:, None] + j[None, :] * (CHAN_BW_HZ / nf)).ravel()

def fine_spectrometer(x, nf: int, na: int, window: str | None = None, chan_batch: int = 256, xp=np):
    """
    x : complex array (T, n_beam, n_chan) of coarse-channel voltages in time order
        (e.g. from packets.assemble_spectra(...)[1].transpose(0,1,2)).
    nf: fine FFT length (2**17 -> 1.164 Hz). na: FFTs integrated per output.
    Returns float32 power (n_int, n_beam, n_chan*nf), frequency-ordered.
    """
    T, nb, nc = x.shape
    nfft_total = T // nf
    n_int = nfft_total // na
    if n_int == 0:
        raise ValueError(f"need at least nf*na = {nf*na} time samples, got {T}")
    w = None
    if window == "hann":
        w = xp.asarray(np.hanning(nf).astype(np.float32))[None, :, None, None]
    out = np.empty((n_int, nb, nc * nf), np.float32)
    for c0 in range(0, nc, chan_batch):
        c1 = min(nc, c0 + chan_batch)
        blk = xp.asarray(x[: n_int * na * nf, :, c0:c1]).astype(xp.complex64)
        blk = blk.reshape(n_int * na, nf, nb, c1 - c0)          # (fft#, time, beam, chan)
        if w is not None:
            blk = blk * w
        F = xp.fft.fftshift(xp.fft.fft(blk, axis=1), axes=1)     # fine FFT along time
        P = (F.real ** 2 + F.imag ** 2).reshape(n_int, na, nf, nb, c1 - c0).sum(1)
        P = P.transpose(0, 2, 3, 1).reshape(n_int, nb, (c1 - c0) * nf)  # (int, beam, chan*fine)
        out[:, :, c0 * nf:c1 * nf] = P.get() if hasattr(P, "get") else P
    return out

def coarse_spectrometer(x, na: int, xp=np):
    """Detect + integrate at native 152.6 kHz resolution: (n_int, n_beam, n_chan)."""
    T = (x.shape[0] // na) * na
    p = xp.asarray(x[:T]).astype(xp.complex64)
    p = (p.real ** 2 + p.imag ** 2).reshape(T // na, na, *x.shape[1:]).sum(1)
    return p.get() if hasattr(p, "get") else p
