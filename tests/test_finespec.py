import numpy as np
import pytest

from lft3 import packets as L
from lft3.dsp import finespec as F

rng = np.random.default_rng(42)


def test_fine_spectrometer_tone():
    nf, na, T = 64, 2, 256
    k, beam, frac = 1234, 2, 0.203125            # frac*nf = 13 fine bins above centre
    x = (rng.normal(0, 1, (T, 4, L.NCHAN)) + 1j * rng.normal(0, 1, (T, 4, L.NCHAN))).astype(np.complex64)
    x[:, beam, k] += 20 * np.exp(2j * np.pi * frac * np.arange(T))
    P = F.fine_spectrometer(x, nf, na)
    assert P.shape == (T // nf // na, 4, L.NCHAN * nf)
    f = F.fine_freq_axis(nf)
    i = int(P[0, beam].argmax())
    assert i == k * nf + nf // 2 + 13
    assert f[i] == pytest.approx(L.BASEBAND_OFFSET_HZ[k] + frac * L.CHAN_BW_HZ)
    assert np.all(np.diff(f) > 0)

def test_coarse_spectrometer():
    x = np.ones((100, 4, L.NCHAN), np.complex64) * (3 + 4j)
    P = F.coarse_spectrometer(x, 10)
    assert P.shape == (10, 4, L.NCHAN) and np.allclose(P, 250)
