"""
LFT3-Earth RFSoC 4x2 packet decoder (packetizer spec v2).

Decodes the two UDP packet types described in data_rates_packetizer_v2.xlsx:
  * Channel packets: 64-B header + 1024 channels x 4 beams x (8b I, 8b Q)  = 8256 B
  * ADC packets:     64-B header + 2048 samples x 4 ADCs x (4b I, 4b Q)    = 8256 B

Unspecified in the sheet, so configurable here (verify on a real capture):
  WORD_ENDIAN   'big' (network order) or 'little' 64-/32-bit words
  IQ_ORDER      'IQ' = I in the high byte/nibble of each complex sample
  beam/ADC order inside a word: index 4p+0 in the most-significant bits
The defaults (big, IQ, 4p+0 first) match the firmware's own parser (lft3-earth-main host/scripts/listener.py).
Header NCO fields are signed int32 in whole MHz (e.g. -2031 for an NCO of -2031.25 MHz).
UDP dst ports: CHANNEL_PORT (60000) and ADC_PORT (40000).
Command line: `lft3-pcap check capture.pcap` (summary) and `lft3-pcap synth out.pcap` (test capture).
"""
from __future__ import annotations

import struct

import numpy as np

# ---- constants from the spec sheet -----------------------------------------
FS_DECIM_HZ   = 1250e6            # 5 GSPS real -> IQ, 4x decimation
NFFT          = 8192
CHAN_BW_HZ    = FS_DECIM_HZ / NFFT  # 152.587890625 kHz
NCHAN         = 6144              # transmitted channels per beam
CH_PER_PKT    = 1024
PKTS_PER_T    = NCHAN // CH_PER_PKT   # 6, indexed by bindex b
NBEAM         = 4
HDR_BYTES     = 64
PAYLOAD_BYTES = HDR_BYTES + CH_PER_PKT * NBEAM * 2   # 8256
ADC_SAMP_PER_PKT = 2048
NADC          = 4
SPECTRUM_RATE = FS_DECIM_HZ / NFFT   # spectra per second (152587.89)
FPGA_CLK_HZ   = 156.25e6          # t_trig counts these cycles
CHANNEL_PORT  = 60000             # UDP dst port, channel stream
ADC_PORT      = 40000             # UDP dst port, ADC/DDR dump

# channel k -> PFB channel (k=0 is PFB 5120, k=6143 is PFB 3071, wraps)
K = np.arange(NCHAN)
PFB_CHAN = (K + 5120) % NFFT
SIGNED_BIN = np.where(PFB_CHAN < NFFT // 2, PFB_CHAN, PFB_CHAN - NFFT)   # -3072..3071
BASEBAND_OFFSET_HZ = SIGNED_BIN * CHAN_BW_HZ    # offset from NCO centre (k=3072 is DC)

def _u64(buf: np.ndarray, endian: str) -> np.ndarray:
    return buf.view(">u8" if endian == "big" else "<u8").astype(np.uint64)

def _u32(buf: np.ndarray, endian: str) -> np.ndarray:
    return buf.view(">u4" if endian == "big" else "<u4").astype(np.uint64)

# ---- channel packets ---------------------------------------------------------
def parse_channel_headers(p: np.ndarray, endian: str = "big") -> dict:
    """p: (N, 8256) uint8 UDP payloads. Returns dict of header field arrays."""
    w = _u64(np.ascontiguousarray(p[:, :HDR_BYTES]), endian).reshape(-1, 8)
    w0, w1, w2, w3 = w[:, 0], w[:, 1], w[:, 2], w[:, 3]
    return dict(
        t=(w0 >> np.uint64(6)), b=((w0 >> np.uint64(3)) & np.uint64(7)).astype(int),
        fid=(w0 & np.uint64(7)).astype(int),
        nco=np.stack([w1 >> np.uint64(32), w1 & np.uint64(0xFFFFFFFF),
                      w2 >> np.uint64(32), w2 & np.uint64(0xFFFFFFFF)], 1).astype(np.uint32).view(np.int32),
        pfb_lsb=np.stack([(w3 >> np.uint64(s)) & np.uint64(0xFFFF) for s in (48, 32, 16, 0)], 1).astype(np.uint16),
        meta=w[:, 4:8],
    )

def unpack_channel_payload(p: np.ndarray, endian: str = "big", iq_order: str = "IQ") -> np.ndarray:
    """Returns complex64 (N, 1024 channels, 4 beams) for packets p (N, 8256)."""
    d = np.ascontiguousarray(p[:, HDR_BYTES:]).reshape(-1, CH_PER_PKT, 8)
    if endian == "little":            # bytes reversed inside each 64-bit word
        d = d[..., ::-1]
    d = d.view(np.int8).reshape(-1, CH_PER_PKT, NBEAM, 2).astype(np.float32)
    re, im = (d[..., 0], d[..., 1]) if iq_order == "IQ" else (d[..., 1], d[..., 0])
    return (re + 1j * im).astype(np.complex64)

# ---- ADC packets -------------------------------------------------------------
def parse_adc_headers(p: np.ndarray, endian: str = "big") -> dict:
    w = _u32(np.ascontiguousarray(p[:, :HDR_BYTES]), endian).reshape(-1, 16)
    t_trig = (w[:, 1] << np.uint64(32)) | w[:, 2] if endian == "big" else (w[:, 2] << np.uint64(32)) | w[:, 1]
    return dict(
        s=w[:, 0].astype(np.uint32), t_trig=t_trig,
        fid=(w[:, 3] >> np.uint64(29)).astype(int), src=((w[:, 3] >> np.uint64(24)) & np.uint64(0x1F)).astype(int),
        nco=w[:, 4:8].astype(np.uint32).view(np.int32),
        adc_lsb=np.stack([(w[:, 8] >> np.uint64(s)) & np.uint64(0xFF) for s in (24, 16, 8, 0)], 1).astype(np.uint8),
        ddr_size=w[:, 9].astype(np.uint32), ddr_read_delay=w[:, 10].astype(np.uint32), meta=w[:, 11:16],
    )

def unpack_adc_payload(p: np.ndarray, endian: str = "big", iq_order: str = "IQ") -> np.ndarray:
    """Returns complex64 (N, 2048 samples, 4 ADCs), 4-bit two's complement I/Q."""
    d = np.ascontiguousarray(p[:, HDR_BYTES:]).reshape(-1, ADC_SAMP_PER_PKT, 4)
    if endian == "little":
        d = d[..., ::-1]
    hi = (d >> 4).astype(np.int8); lo = (d & 0x0F).astype(np.int8)
    hi = np.where(hi > 7, hi - 16, hi).astype(np.float32); lo = np.where(lo > 7, lo - 16, lo).astype(np.float32)
    re, im = (hi, lo) if iq_order == "IQ" else (lo, hi)
    return (re + 1j * im).astype(np.complex64)

# ---- pcap reading ------------------------------------------------------------
def read_pcap_udp(path: str):
    """Classic libpcap (tcpdump -w). Returns (times[s], dst_ports, payload list)."""
    with open(path, "rb") as f:
        gh = f.read(24)
        magic = struct.unpack("<I", gh[:4])[0]
        if magic in (0xA1B2C3D4, 0xA1B23C4D): e = "<"
        elif magic in (0xD4C3B2A1, 0x4D3CB2A1): e = ">"
        else: raise ValueError("not a classic pcap file (pcapng? use: editcap -F pcap in.pcapng out.pcap)")
        nano = magic in (0xA1B23C4D, 0x4D3CB2A1)
        linktype = struct.unpack(e + "I", gh[20:24])[0]
        if linktype != 1: raise ValueError(f"linktype {linktype} not Ethernet")
        times, ports, pls = [], [], []
        while True:
            rh = f.read(16)
            if len(rh) < 16: break
            ts, tf, incl, orig = struct.unpack(e + "IIII", rh)
            pkt = f.read(incl)
            off = 12; et = struct.unpack(">H", pkt[off:off + 2])[0]; off += 2
            while et == 0x8100:                       # VLAN tag(s)
                et = struct.unpack(">H", pkt[off + 2:off + 4])[0]; off += 4
            if et != 0x0800 or pkt[off + 9] != 17: continue   # IPv4/UDP only
            ihl = (pkt[off] & 0x0F) * 4; u = off + ihl
            dport, ulen = struct.unpack(">HH", pkt[u + 2:u + 6])
            if orig != incl: raise ValueError("truncated capture; re-run tcpdump with -s 0")
            times.append(ts + tf * (1e-9 if nano else 1e-6)); ports.append(dport)
            pls.append(pkt[u + 8:u + ulen])
    return np.array(times), np.array(ports), pls

def guess_endian(p: np.ndarray) -> str:
    """Pick the word order for which channel-header fields look sane (b<6, t monotonic)."""
    score = {}
    for e in ("big", "little"):
        h = parse_channel_headers(p, e)
        seq = h["t"].astype(np.int64) * PKTS_PER_T + h["b"]
        score[e] = np.mean(h["b"] < PKTS_PER_T) + np.mean(np.diff(seq) > 0) if len(seq) > 1 else np.mean(h["b"] < 6)
    return max(score, key=score.get)

def channel_stream_report(times, p: np.ndarray, endian: str) -> dict:
    h = parse_channel_headers(p, endian)
    rep = {"packets": len(p), "fids": sorted(set(h["fid"].tolist())), "bindex_values": sorted(set(h["b"].tolist()))}
    for fid in rep["fids"]:
        m = h["fid"] == fid
        seq = h["t"][m].astype(np.int64) * PKTS_PER_T + h["b"][m]
        ds = np.diff(seq)
        rep[f"fid{fid}"] = dict(
            t_first=int(h["t"][m][0]), t_last=int(h["t"][m][-1]),
            expected=int(seq.max() - seq.min() + 1), received=int(m.sum()),
            missing=int(seq.max() - seq.min() + 1 - len(np.unique(seq))),
            out_of_order=int((ds < 0).sum()), duplicates=int(len(seq) - len(np.unique(seq))),
            nco=h["nco"][m][0].tolist(), pfb_lsb=h["pfb_lsb"][m][0].tolist())
    if len(times) > 1 and times[-1] > times[0]:
        rep["capture_pps"] = (len(times) - 1) / (times[-1] - times[0])
        rep["capture_gbps_payload"] = rep["capture_pps"] * PAYLOAD_BYTES * 8 / 1e9
    return rep

def assemble_spectra(p: np.ndarray, endian="big", iq_order="IQ"):
    """Complete timestamps only -> (t array, complex64 (T, 4 beams, 6144 channels k))."""
    h = parse_channel_headers(p, endian); data = unpack_channel_payload(p, endian, iq_order)
    out = {}
    for i, (t, b) in enumerate(zip(h["t"].tolist(), h["b"].tolist())):
        if b < PKTS_PER_T:
            out.setdefault(t, {})[b] = data[i]
    ts = sorted(t for t, d in out.items() if len(d) == PKTS_PER_T)
    spec = np.empty((len(ts), NBEAM, NCHAN), np.complex64)
    for j, t in enumerate(ts):
        spec[j] = np.concatenate([out[t][b] for b in range(PKTS_PER_T)], 0).T
    return np.array(ts, np.uint64), spec

# ---- synthetic packets (for tests / a software sender) -------------------------
def make_channel_packet(t, b, fid, data_c, endian="big", nco=(0, 0, 0, 0), pfb_lsb=(0, 0, 0, 0)) -> bytes:
    """data_c: complex (1024, 4) with |re|,|im| <= 127."""
    w0 = (int(t) << 6) | ((b & 7) << 3) | (fid & 7)
    nco = [int(v) & 0xFFFFFFFF for v in nco]          # signed whole MHz -> two's complement int32
    w1 = (nco[0] << 32) | nco[1]; w2 = (nco[2] << 32) | nco[3]
    w3 = (pfb_lsb[0] << 48) | (pfb_lsb[1] << 32) | (pfb_lsb[2] << 16) | pfb_lsb[3]
    fmt = (">" if endian == "big" else "<") + "8Q"
    hdr = struct.pack(fmt, w0, w1, w2, w3, 0, 0, 0, 0)
    iq = np.stack([np.real(data_c), np.imag(data_c)], -1).round().astype(np.int8)  # (1024,4,2)
    body = iq.reshape(CH_PER_PKT, 8).view(np.uint8)
    if endian == "little": body = body[:, ::-1]
    return hdr + body.tobytes()

def make_adc_packet(s, t_trig, fid, src, samples_c, endian="big", nco=(0, 0, 0, 0), adc_lsb=(0, 0, 0, 0),
                    ddr_size=0, ddr_read_delay=0) -> bytes:
    """samples_c: complex (2048, 4) with integer re/im in -8..7."""
    w3 = ((fid & 7) << 29) | ((src & 0x1F) << 24)
    nco = [int(v) & 0xFFFFFFFF for v in nco]
    w8 = (adc_lsb[0] << 24) | (adc_lsb[1] << 16) | (adc_lsb[2] << 8) | adc_lsb[3]
    if endian == "big":
        words = [s, t_trig >> 32, t_trig & 0xFFFFFFFF]
    else:
        words = [s, t_trig & 0xFFFFFFFF, t_trig >> 32]
    words += [w3, *nco, w8, ddr_size, ddr_read_delay, 0, 0, 0, 0, 0]
    hdr = struct.pack((">" if endian == "big" else "<") + "16I", *words)
    re = np.real(samples_c).round().astype(np.int16) & 0xF
    im = np.imag(samples_c).round().astype(np.int16) & 0xF
    body = ((re << 4) | im).astype(np.uint8)                     # (2048, 4) bytes, ADC 4p+0 first
    if endian == "little": body = body[:, ::-1]
    return hdr + body.tobytes()

def write_pcap(path, payloads, dport=60000, rate_pps=PKTS_PER_T * SPECTRUM_RATE):
    with open(path, "wb") as f:
        f.write(struct.pack("<IHHiIII", 0xA1B23C4D, 2, 4, 0, 0, 65535, 1))
        for i, pl in enumerate(payloads):
            udp = struct.pack(">HHHH", 50000, dport, 8 + len(pl), 0) + pl
            ip = struct.pack(">BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 0, 0x4000, 64, 17, 0,
                             bytes([10, 0, 0, 2]), bytes([10, 0, 0, 1])) + udp
            eth = b"\x02\x00\x00\x00\x00\x01" + b"\x02\x00\x00\x00\x00\x02" + b"\x08\x00" + ip
            tns = int(i * 1e9 / rate_pps)
            f.write(struct.pack("<IIII", tns // 10**9, tns % 10**9, len(eth), len(eth)) + eth)
