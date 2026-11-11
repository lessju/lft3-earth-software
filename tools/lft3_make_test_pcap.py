#!/usr/bin/env python3
"""Write a synthetic LFT3 channel-packet pcap (noise + one tone) to exercise the tools,
or to replay with tcpreplay as a software stand-in for the RFSoC."""
import argparse, os, sys
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))
from lft3 import packets as L
ap = argparse.ArgumentParser(); ap.add_argument("out"); ap.add_argument("--ntime", type=int, default=256)
ap.add_argument("--fid", type=int, default=0); ap.add_argument("--tone-k", type=int, default=4000)
ap.add_argument("--tone-beam", type=int, default=1); ap.add_argument("--tone-frac", type=float, default=0.1,
                help="tone offset inside coarse channel, cycles/sample (-0.5..0.5)")
ap.add_argument("--endian", default="big", choices=["big", "little"]); ap.add_argument("--port", type=int, default=60000)
a = ap.parse_args(); rng = np.random.default_rng(0); pk = []
for t in range(a.ntime):
    for b in range(L.PKTS_PER_T):
        d = rng.normal(0, 8, (1024, 4)) + 1j * rng.normal(0, 8, (1024, 4))
        if b == a.tone_k // 1024:
            d[a.tone_k % 1024, a.tone_beam] += 60 * np.exp(2j * np.pi * a.tone_frac * t)
        d = np.clip(d.real, -127, 127) + 1j * np.clip(d.imag, -127, 127)
        pk.append(L.make_channel_packet(t, b, a.fid, d, a.endian))
L.write_pcap(a.out, pk, dport=a.port); print(f"wrote {len(pk)} packets to {a.out}")
