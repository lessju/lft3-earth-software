"""lft3.ring parses the C++ control block layout (pipeline/src/common/ring.hh; offsets static_asserted in ring.cc)."""
import struct

import pytest

from lft3 import ring


def _control(tmp_path, stream="rfsoc0-link0"):
    b = bytearray(ring.CONTROL_BYTES)
    struct.pack_into("<QII Q", b, 0, ring.MAGIC, ring.VERSION, ring.CONTROL_BYTES, 1234)
    b[24:24 + len(stream)] = stream.encode()
    b[72:76] = b"obs1"
    struct.pack_into("<7Q", b, 264, 0, 6144, 1024, 2, 298, 50331648, 15000000000)
    struct.pack_into("<4B4B", b, 320, 0, 0, 1, 1, 0, 1, 0, 1)
    struct.pack_into("<II dd d", b, 328, 0, 1, -2031.25, 152587.890625, 1.7e9)
    struct.pack_into("<Q", b, 368, 42)                                  # head
    stats = list(range(1, 17))
    stats[14] = 60812                                                   # rate_milli_gbps
    struct.pack_into("<16Q", b, 400, *stats)
    o = ring.OFF_READERS + 128                                          # reader entry 1
    struct.pack_into("<QQQI", b, o, 99, 40, 0, 1)
    b[o + 32:o + 43] = b"spectrometer"[:11]
    struct.pack_into("<5Q", b, o + 64, 40, 3, 2, 1500000, 7)
    seq, slot = 41, 7                                                   # block 41 lives in slot 7
    struct.pack_into("<Q", b, ring.OFF_SEQ_TO_SLOT + seq * 8, slot)
    struct.pack_into("<5Q4i4HIIdd", b, ring.OFF_SLOTS + slot * 128, seq, 1000, 1024000, 6, 0, -2031, -2031, -2031,
                     -2031, 0, 0, 0, 0, 0, 1, 1.7e9, 1.7e9 + 5)
    (tmp_path / f"lft3-{stream}").write_bytes(bytes(b))
    return stream


def test_ring_status_and_slot_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(ring, "SHM_DIR", tmp_path)
    stream = _control(tmp_path)
    (tmp_path / f"lft3-{stream}.sock").write_text("")                 # sockets are not streams
    assert ring.list_streams() == [stream]
    with ring.Ring(stream) as r:
        st = r.status()
        assert (st.stream, st.state, st.obs_id) == (stream, "running", "obs1")
        assert (st.channel_count, st.spectra_per_block, st.slots, st.head) == (6144, 1024, 298, 42)
        assert st.nco_mhz == -2031.25 and st.unix_time_at_t0 == 1.7e9
        assert st.rate_gbps == pytest.approx(60.812) and st.stats["packets_received"] == 1
        assert st.stats["blocks_partial"] == 9 and st.stats["slots_unavailable"] == 16
        assert st.ring_seconds == pytest.approx(298 * 1024 / 152587.890625)
        [rd] = st.readers
        assert (rd.name, rd.lossless, rd.cursor, rd.overruns, rd.lag_blocks, rd.outputs) == \
            ("spectromete", True, 40, 3, 2, 7)
        assert rd.busy_ms == pytest.approx(1.5)
        m = r.slot_meta(41)
        assert (m.block_index, m.t0, m.missing_packets, m.flags) == (1000, 1024000, 6, 1)
        assert m.nco == (-2031,) * 4
        assert r.slot_meta(40) is None
        text = ring.to_prometheus([st])
        assert f'lft3_capture_rate_gbps{{stream="{stream}"}} 60.812' in text
        assert f'lft3_reader_overruns_total{{stream="{stream}",reader="spectromete"}} 3' in text


def test_ring_rejects_other_files(tmp_path, monkeypatch):
    monkeypatch.setattr(ring, "SHM_DIR", tmp_path)
    (tmp_path / "lft3-bad").write_bytes(b"\0" * ring.CONTROL_BYTES)
    with pytest.raises(ValueError):
        ring.Ring("bad")
