import numpy as np
import pytest

from lft3 import packets as L

rng = np.random.default_rng(42)

def rand_chan():
    return rng.integers(-127, 128, (1024, 4)) + 1j * rng.integers(-127, 128, (1024, 4))

@pytest.mark.parametrize("endian", ["big", "little"])
def test_channel_roundtrip(endian):
    d = rand_chan()
    pkt = L.make_channel_packet(123456789, 5, 3, d, endian, nco=(11, -2031, 33, -1), pfb_lsb=(1, 2, 3, 4))
    assert len(pkt) == L.PAYLOAD_BYTES == 8256
    p = np.frombuffer(pkt, np.uint8)[None]
    h = L.parse_channel_headers(p, endian)
    assert int(h["t"][0]) == 123456789 and h["b"][0] == 5 and h["fid"][0] == 3
    assert h["nco"][0].tolist() == [11, -2031, 33, -1] and h["pfb_lsb"][0].tolist() == [1, 2, 3, 4]
    np.testing.assert_array_equal(L.unpack_channel_payload(p, endian)[0], d.astype(np.complex64))

@pytest.mark.parametrize("endian", ["big", "little"])
def test_adc_roundtrip(endian):
    x = rng.integers(-8, 8, (2048, 4)) + 1j * rng.integers(-8, 8, (2048, 4))
    pkt = L.make_adc_packet(77, (1 << 40) + 5, 4, 17, x, endian, nco=(1, -2031, 3, 4), adc_lsb=(9, 8, 7, 6),
                            ddr_size=2**30, ddr_read_delay=12)
    assert len(pkt) == 8256
    p = np.frombuffer(pkt, np.uint8)[None]
    h = L.parse_adc_headers(p, endian)
    assert h["s"][0] == 77 and int(h["t_trig"][0]) == (1 << 40) + 5 and h["fid"][0] == 4 and h["src"][0] == 17
    assert h["nco"][0].tolist() == [1, -2031, 3, 4]
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
