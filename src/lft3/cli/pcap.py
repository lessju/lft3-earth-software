"""lft3-pcap: inspect tcpdump captures of the RFSoC streams, or write synthetic ones.

    lft3-pcap check cap.pcap [--endian big|little] [--iq IQ|QI]
    lft3-pcap synth out.pcap [--ntime 256] [--tone-k 4000] [--tone-beam 1] [--tone-frac 0.1]
"""
from __future__ import annotations

import argparse

import numpy as np

from lft3 import packets as L


def check(path: str, endian: str | None = None, iq_order: str = "IQ") -> None:
    times, ports, pls = L.read_pcap_udp(path)
    counts = {int(k): int(v) for k, v in zip(*np.unique(ports, return_counts=True))}
    print(f"{len(pls)} UDP packets; dst ports: {counts}")
    print("payload lengths:", sorted({len(x) for x in pls}))
    for port in np.unique(ports):
        sel = [x for x, pp in zip(pls, ports) if pp == port and len(x) == L.PAYLOAD_BYTES]
        if not sel:
            continue
        p = np.frombuffer(b"".join(sel), np.uint8).reshape(-1, L.PAYLOAD_BYTES)
        e = endian or L.guess_endian(p)
        h = L.parse_channel_headers(p, e)
        looks_channel = np.mean(h["b"] < L.PKTS_PER_T) > 0.99
        print(f"\n== port {port}: {len(p)} packets, word endian={e}, "
              f"looks like {'CHANNEL' if looks_channel else 'ADC'} packets")
        if looks_channel:
            for k, v in L.channel_stream_report(times[ports == port], p, e).items():
                print(f"  {k}: {v}")
            ts, spec = L.assemble_spectra(p, e, iq_order)
            if len(ts):
                pw = (np.abs(spec) ** 2).mean(0)
                for beam in range(L.NBEAM):
                    kmax = int(pw[beam].argmax())
                    print(f"  beam {beam}: mean power {pw[beam].mean():.2f}, peak k={kmax} "
                          f"(PFB {L.PFB_CHAN[kmax]}, offset {L.BASEBAND_OFFSET_HZ[kmax] / 1e6:+.4f} MHz)")
        else:
            a = L.parse_adc_headers(p, e)
            missing = int(a["s"].max() - a["s"].min() + 1 - len(np.unique(a["s"])))
            print(f"  s: {a['s'][0]}..{a['s'][-1]}  missing={missing}"
                  f"  fid={sorted(set(a['fid'].tolist()))} src={sorted(set(a['src'].tolist()))}"
                  f"  t_trig={int(a['t_trig'][0])} ddr_size={a['ddr_size'][0]} adc_lsb={a['adc_lsb'][0].tolist()}")
            x = L.unpack_adc_payload(p, e, iq_order)
            print(f"  ADC rms per input: {np.sqrt((np.abs(x) ** 2).mean((0, 1))).round(2).tolist()}")


def synth(out: str, ntime: int = 256, fid: int = 0, tone_k: int = 4000, tone_beam: int = 1,
          tone_frac: float = 0.1, endian: str = "big", port: int = L.CHANNEL_PORT) -> int:
    """Noise + one tone in channel `tone_k`, beam `tone_beam`, `tone_frac` cycles/sample. Returns packet count."""
    rng = np.random.default_rng(0)
    pk = []
    for t in range(ntime):
        for b in range(L.PKTS_PER_T):
            d = rng.normal(0, 8, (L.CH_PER_PKT, L.NBEAM)) + 1j * rng.normal(0, 8, (L.CH_PER_PKT, L.NBEAM))
            if b == tone_k // L.CH_PER_PKT:
                d[tone_k % L.CH_PER_PKT, tone_beam] += 60 * np.exp(2j * np.pi * tone_frac * t)
            d = np.clip(d.real, -127, 127) + 1j * np.clip(d.imag, -127, 127)
            pk.append(L.make_channel_packet(t, b, fid, d, endian))
    L.write_pcap(out, pk, dport=port)
    return len(pk)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="lft3-pcap", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="summarise a capture")
    c.add_argument("pcap")
    c.add_argument("--endian", choices=["big", "little"])
    c.add_argument("--iq", default="IQ", choices=["IQ", "QI"])
    s = sub.add_parser("synth", help="write a synthetic channel-stream capture (noise + tone)")
    s.add_argument("out")
    s.add_argument("--ntime", type=int, default=256)
    s.add_argument("--fid", type=int, default=0)
    s.add_argument("--tone-k", type=int, default=4000)
    s.add_argument("--tone-beam", type=int, default=1)
    s.add_argument("--tone-frac", type=float, default=0.1, help="cycles/sample inside the channel (-0.5..0.5)")
    s.add_argument("--endian", default="big", choices=["big", "little"])
    s.add_argument("--port", type=int, default=L.CHANNEL_PORT)
    a = ap.parse_args(argv)
    if a.cmd == "check":
        check(a.pcap, a.endian, a.iq)
    else:
        n = synth(a.out, a.ntime, a.fid, a.tone_k, a.tone_beam, a.tone_frac, a.endian, a.port)
        print(f"wrote {n} packets to {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
