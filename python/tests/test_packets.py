import os, sys
import numpy as np
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lft3 import packets as L
from lft3 import finespec as F

rng = np.random.default_rng(42)

def rand_chan():
    return rng.integers(-127, 128, (1024, 4)) + 1j * rng.integers(-127, 128, (1024, 4))

@pytest.mark.parametrize("endian", ["big", "little"])
def test_channel_roundtrip(endian):
    d = rand_chan()
    pkt = L.make_channel_packet(123456789, 5, 3, d, endian, nco=(11, 22, 33, 44), pfb_lsb=(1, 2, 3, 4))
    assert len(pkt) == L.PAYLOAD_BYTES == 8256
    p = np.frombuffer(pkt, np.uint8)[None]
    h = L.parse_channel_headers(p, endian)
    assert int(h["t"][0]) == 123456789 and h["b"][0] == 5 and h["fid"][0] == 3
    assert h["nco"][0].tolist() == [11, 22, 33, 44] and h["pfb_lsb"][0].tolist() == [1, 2, 3, 4]
    np.testing.assert_array_equal(L.unpack_channel_payload(p, endian)[0], d.astype(np.complex64))

@pytest.mark.parametrize("endian", ["big", "little"])
def test_adc_roundtrip(endian):
    x = rng.integers(-8, 8, (2048, 4)) + 1j * rng.integers(-8, 8, (2048, 4))
    pkt = L.make_adc_packet(77, (1 << 40) + 5, 4, 17, x, endian, nco=(1, 2, 3, 4), adc_lsb=(9, 8, 7, 6),
                            ddr_size=2**30, ddr_read_delay=12)
    assert len(pkt) == 8256
    p = np.frombuffer(pkt, np.uint8)[None]
    h = L.parse_adc_headers(p, endian)
    assert h["s"][0] == 77 and int(h["t_trig"][0]) == (1 << 40) + 5 and h["fid"][0] == 4 and h["src"][0] == 17
    assert h["adc_lsb"][0].tolist() == [9, 8, 7, 6] and h["ddr_size"][0] == 2**30
    np.testing.assert_array_equal(L.unpack_adc_payload(p, endian)[0], x.astype(np.complex64))

def test_channel_mapping():
    assert L.PFB_CHAN[0] == 5120 and L.PFB_CHAN[-1] == 3071 and L.SIGNED_BIN[3072] == 0
    assert np.all(np.diff(L.BASEBAND_OFFSET_HZ) > 0)            # k ascending = frequency ascending
    assert L.BASEBAND_OFFSET_HZ[0] == pytest.approx(-468.75e6)

def test_pcap_gap_detection(tmp_path):
    pk = [L.make_channel_packet(t, b, 1, rand_chan()) for t in range(10) for b in range(6)]
    del pk[13]
    fn = tmp_path / "x.pcap"; L.write_pcap(fn, pk)
    times, ports, pls = L.read_pcap_udp(str(fn))
    p = np.frombuffer(b"".join(pls), np.uint8).reshape(-1, L.PAYLOAD_BYTES)
    assert L.guess_endian(p) == "big"
    r = L.channel_stream_report(times, p, "big")["fid1"]
    assert r["missing"] == 1 and r["received"] == 59 and r["out_of_order"] == 0
    ts, spec = L.assemble_spectra(p)
    assert len(ts) == 9 and spec.shape == (9, 4, 6144)

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
