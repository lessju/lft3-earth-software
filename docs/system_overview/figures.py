"""Diagrams and charts for the LFT3-Earth system overview (docs/system_overview/build.py).

    python figures.py OUT_DIR

Each figure is drawn with matplotlib patches and saved as SVG (text kept as text) in one visual language:
  board / FPGA  orange   network  grey   host CPU  blue   GPU  green   storage  purple   readers  teal
Numbers in the figures are the measured / configured values documented in docs/runtime.md.
"""
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch, Rectangle  # noqa: E402

C = {
    "board": ("#fde3c8", "#c8641e"), "net": ("#e6e6e6", "#6b6b6b"), "cpu": ("#d6e6f7", "#2f6db0"),
    "gpu": ("#d7f0d4", "#2e8b3a"), "store": ("#e8dcf4", "#6b3fa0"), "reader": ("#d2f1ef", "#1f8a83"),
    "mem": ("#fff6c9", "#a08800"), "warn": ("#fadbd8", "#b03a2e"), "plain": ("#ffffff", "#333333"),
}
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9, "axes.titlesize": 10,
                     "svg.fonttype": "none"})   # SVG keeps text as text (searchable, crisp); fonts mapped in build.py


def fig(w, h):
    f, ax = plt.subplots(figsize=(w, h))
    ax.set_xlim(0, w * 10)
    ax.set_ylim(0, h * 10)
    ax.axis("off")
    return f, ax


def box(ax, x, y, w, h, text, kind="plain", fs=8.5, bold=False, align="center", rad=0.6, lw=1.2, va="center"):
    face, edge = C[kind]
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle=f"round,pad=0.02,rounding_size={rad}", fc=face, ec=edge,
                                lw=lw))
    tx = x + w / 2 if align == "center" else x + 0.6
    ty = y + h / 2 if va == "center" else y + h - 0.5
    ax.text(tx, ty, text, ha=align, va=va if va == "center" else "top", fontsize=fs,
            fontweight="bold" if bold else "normal", linespacing=1.25)


def group(ax, x, y, w, h, title, kind, fs=9):
    face, edge = C[kind]
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=1.0", fc=face, ec=edge,
                                lw=1.6, alpha=0.45))
    ax.text(x + 0.8, y + h - 0.6, title, ha="left", va="top", fontsize=fs, fontweight="bold", color=edge)


def arrow(ax, x0, y0, x1, y1, text="", color="#333333", lw=1.4, fs=7.5, style="-|>", ls="-", off=(0, 0.6),
          rad=0.0, ha="center"):
    ax.add_patch(FancyArrowPatch((x0, y0), (x1, y1), arrowstyle=style, mutation_scale=11, color=color, lw=lw,
                                 linestyle=ls, connectionstyle=f"arc3,rad={rad}"))
    if text:
        ax.text((x0 + x1) / 2 + off[0], (y0 + y1) / 2 + off[1], text, ha=ha, va="bottom", fontsize=fs, color=color)


def save(f, out, name):
    """Vector SVG (embedded in the PDF as drawings by build.py; usable in other documents as is)."""
    path = out / f"{name}.svg"
    f.savefig(path, bbox_inches="tight", pad_inches=0.05)
    plt.close(f)
    # keep spaces in labels as drawn (SVG collapses whitespace unless told otherwise)
    svg = path.read_text()
    path.write_text(svg.replace("<svg ", '<svg xml:space="preserve" ', 1))


# ------------------------------------------------------------------------------------------------ figures

def system_overview(out):
    f, ax = fig(11, 6.4)
    group(ax, 1, 34, 31, 28, "RFSoC 4x2 board (×5 in full system)", "board")
    box(ax, 2.5, 50.5, 28, 7, "4 ADC inputs · 5 GSPS · NCO mix\n+ decimate → 1250 MSPS complex", "board", fs=7.6)
    box(ax, 2.5, 42.5, 28, 6.5, "PFB 8192 ch, 8 taps → keep 6144\nrequantise to 8-bit I + 8-bit Q", "board", fs=7.6)
    box(ax, 2.5, 35.5, 13, 5.5, "DDR ring\n4 GiB, 4-bit", "board", fs=7.6)
    box(ax, 17.5, 35.5, 13, 5.5, "packetizer\n60.8 Gb/s", "board", fs=7.6)
    arrow(ax, 15.5, 38.2, 17.5, 38.2)
    arrow(ax, 16.5, 50.5, 16.5, 49)
    arrow(ax, 9, 42.5, 9, 41)
    arrow(ax, 24, 42.5, 24, 41)
    group(ax, 36, 3, 73, 59, "NVIDIA DGX Spark (GB10)", "plain")
    box(ax, 38, 46, 15, 10, "ConnectX-7\n2 × QSFP\n(link0, link1)", "net", fs=7.8)
    ax.text(38, 44.3, "UDP 60000: 915 527 pkt/s\nUDP 40000: DDR dumps", fontsize=6.8, va="top")
    box(ax, 57, 46, 17, 10, "lft3-capture\n(one per link)\nDAQIRI · assembler\nscatter kernel", "cpu", fs=7.6)
    box(ax, 78, 43, 29, 14, "voltage ring per link\nGPU memory, last ~2 s\n[beam][channel][t][pol] int8\n"
                            "50 MB blocks of 6.71 ms", "gpu", fs=7.8)
    box(ax, 57, 26, 15, 10, "lft3-spectrometer\n128-pt FFT, Stokes I\n1 s integrations", "reader", fs=7.2)
    box(ax, 74, 26, 15, 10, "lft3-dump\ntrigger → window\nof raw voltages", "reader", fs=7.2)
    box(ax, 91, 26, 16, 10, "your reader\n(C++, Python/CuPy)\ne.g. a live search", "reader", fs=7.2)
    box(ax, 38, 6, 17, 11, "FBH5 spectra\nfine 1.19 kHz\ncoarse 152.6 kHz", "store", fs=7.4)
    box(ax, 57, 6, 15, 11, "GUPPI RAW\nraw-voltage\ndumps", "store", fs=7.4)
    box(ax, 74, 6, 15, 11, "hits CSV\nlft3-search\n(hyperseti)\nreads FBH5", "store", fs=7.2)
    box(ax, 91, 6, 16, 11, "lft3-view\nbrowser viewer\nreads FBH5", "reader", fs=7.4)
    arrow(ax, 30.5, 39, 38, 49, lw=2.2)
    ax.text(31.2, 44.5, "100\nGbE", fontsize=7, color="#333")
    arrow(ax, 53, 51, 57, 51, lw=2)
    arrow(ax, 74, 51, 78, 51, lw=2)
    arrow(ax, 84, 43, 66, 36)
    arrow(ax, 89, 43, 81.5, 36)
    arrow(ax, 97, 43, 99, 36)
    arrow(ax, 61, 26, 48, 17)
    arrow(ax, 81.5, 26, 66, 17)
    group(ax, 1, 3, 31, 25, "Control and monitoring", "cpu")
    box(ax, 2.5, 15.5, 28, 7, "lft3-board: program, clocks, NCO,\nsync, trigger, health checks", "cpu", fs=7.4)
    box(ax, 2.5, 4.5, 28, 9, "lft3-pipeline: run, status,\ntrigger, stop, metrics\nconfig/*.yaml: source of truth", "cpu",
        fs=7.4)
    arrow(ax, 27, 22.5, 27, 34, lw=1.4)
    ax.text(17, 30, "1 GbE katcp\n192.168.2.100", fontsize=6.8, va="center", ha="center")
    arrow(ax, 30.5, 9, 36, 9, ls=":", lw=1.1)
    ax.text(31, 10, "runs", fontsize=6.6)
    save(f, out, "f01_system")

def signal_chain(out):
    f, ax = fig(11, 2.9)
    steps = [("RF in\n1.56–2.5\nGHz", "net"), ("ADC\n5 GSPS real\n4 inputs", "board"),
             ("down-\nconvert\nNCO −2031.25\nMHz", "board"), ("decimate\n×4 → 1250\nMSPS\ncomplex", "board"),
             ("PFB\n8192 pt\n8 taps\n152.6 kHz", "board"), ("keep\n6144 ch\n937.5 MHz", "board"),
             ("requantise\n8-bit I +\n8-bit Q\n(ch_lsb)", "board"), ("packetizer\n6 × 1024 ch\nper\nspectrum", "board"),
             ("100 GbE\n60.8 Gb/s", "net")]
    x = 1
    for i, (t, k) in enumerate(steps):
        box(ax, x, 12, 10.5, 13, t, k, fs=7.2)
        if i:
            arrow(ax, x - 1.6, 18.5, x, 18.5)
        x += 12.1
    box(ax, 37.3, 1, 30, 7.5, "DDR4 ring: 4-bit I/Q, 5 GB/s, 4 GiB = 0.86 s;\nread out through the packetizer on trigger",
        "board", fs=7.2)
    arrow(ax, 42, 12, 42, 8.5, ls="--")
    box(ax, 70, 1, 38, 7.5, "one spectrum per input every 6.5536 µs (152 587.89 /s);\nt counts spectra since the last sync",
        "plain", fs=7.2)
    save(f, out, "f02_signal_chain")

def packet_layout(out):
    f, ax = fig(11, 5.6)
    ax.text(1, 54, "Channel packet (UDP payload 8256 B; Ethernet frame 8298 B, MTU 9000)", fontsize=10,
            fontweight="bold")
    # header words
    y = 40
    words = [("word 0", [("t: spectrum counter (58 bits)", 58), ("b (3)", 3), ("fid (3)", 3)]),
             ("word 1", [("nco0 (int32, whole MHz)", 32), ("nco1", 32)]),
             ("word 2", [("nco2", 32), ("nco3", 32)]),
             ("word 3", [("pfb_lsb0 (16)", 16), ("pfb_lsb1", 16), ("pfb_lsb2", 16), ("pfb_lsb3", 16)])]
    W = 88
    for i, (name, fields) in enumerate(words):
        yy = y - i * 4.6
        ax.text(1, yy + 1.8, name, fontsize=7.5, va="center")
        x = 9
        for txt, bits in fields:
            w = W * bits / 64
            box(ax, x, yy, w, 3.6, txt, "cpu", fs=7, rad=0.2, lw=0.8)
            x += w
    ax.text(9, 44.6, "header: 8 × 64-bit words, big-endian (words 4–7 unused); MSB on the left", fontsize=7.5)
    ax.text(98.5, 30, "64 B", fontsize=7.5, va="center")
    # payload
    yp = 12
    ax.text(1, yp + 9.5, "payload: 1024 channels × 8 B = 8192 B", fontsize=8, fontweight="bold")
    for i in range(6):
        box(ax, 9 + i * 12, yp + 2, 11.4, 5, f"ch {i}" if i < 4 else ("…" if i == 4 else "ch 1023"), "gpu", fs=7.5,
            rad=0.2, lw=0.8)
    ax.text(83, yp + 4.5, "channel k = 1024·b + i", fontsize=7.5, va="center")
    # zoom of one channel
    zx = 9
    labels = ["I in0", "Q in0", "I in1", "Q in1", "I in2", "Q in2", "I in3", "Q in3"]
    for i, lab in enumerate(labels):
        box(ax, zx + i * 9.2, 1, 8.8, 4, lab, "gpu" if i // 2 % 2 == 0 else "mem", fs=7, rad=0.2, lw=0.8)
    arrow(ax, 12, yp + 2, 20, 5.2, style="-", ls=":", color="#777")
    ax.text(85, 3, "1 byte each, int8\ninput 0 first, I before Q\ninputs 0,1 = beam 0 X,Y\ninputs 2,3 = beam 1 X,Y",
            fontsize=7.2, va="center")
    save(f, out, "f03_packet")


def freq_map(out):
    f, ax = fig(11, 3.2)
    ax.text(1, 29.5, "FPGA PFB bins (8192)", fontsize=8)
    box(ax, 1, 22, 26, 5, "0 … 3071", "board", fs=8, rad=0.2)
    box(ax, 28, 22, 22, 5, "3072 … 5119 (discarded)", "net", fs=8, rad=0.2)
    box(ax, 51, 22, 26, 5, "5120 … 8191", "board", fs=8, rad=0.2)
    ax.text(1, 16, "channel index k in the packets (6144)", fontsize=8)
    box(ax, 1, 8.5, 38, 5, "k = 0 … 3071  (bindex b = 0, 1, 2)\n= bins 5120 … 8191 (negative offsets)", "gpu",
        fs=7.5, rad=0.2)
    box(ax, 40, 8.5, 38, 5, "k = 3072 … 6143  (b = 3, 4, 5)\n= bins 0 … 3071 (positive offsets)", "gpu", fs=7.5,
        rad=0.2)
    arrow(ax, 64, 22, 20, 13.5, color="#c8641e")
    arrow(ax, 14, 22, 59, 13.5, color="#c8641e")
    box(ax, 80, 6, 29, 22, "RF(k) = −NCO + (k − 3072)\n         × 152.587890625 kHz\n\nNCO = −2031.25 MHz:\n"
                           "k = 0   → 1562.50 MHz\nk = 3072 → 2031.25 MHz\nk = 6143 → 2499.85 MHz\n\n"
                           "ascending k = ascending RF\n(confirmed by 125 MHz spurs\nand a 1800 MHz test tone)",
        "plain", fs=7.4, align="left", va="top")
    save(f, out, "f04_freq_map")


def spark_hw(out):
    f, ax = fig(11, 6)
    group(ax, 1, 2, 108, 56, "DGX Spark (GB10 superchip)", "plain")
    group(ax, 3, 30, 50, 25, "Arm CPU: 20 cores", "cpu")
    ax.text(5, 51.2, "red = link0 capture: 5 DAQIRI master · 6 RX worker (100 %) · 7 processing", fontsize=6.6,
            color="#b03a2e")
    for i, c in enumerate([5, 6, 7, 8, 9, 15, 16, 17, 18, 19]):
        k = "warn" if c in (5, 6, 7) else "cpu"
        box(ax, 5 + (i % 5) * 9.5, 44.5 - (i // 5) * 5.6, 8.5, 4.8, f"X925 {c}", k, fs=7, rad=0.3)
    ax.text(5, 37.0, "A725 cores (readers, system)", fontsize=6.8)
    for i, c in enumerate([0, 1, 2, 3, 4, 10, 11, 12, 13, 14]):
        box(ax, 5 + (i % 10) * 4.7, 31.5, 4.4, 4.5, f"{c}", "cpu", fs=6.5, rad=0.2)
    group(ax, 57, 30, 50, 25, "Blackwell GPU (sm_121, 48 SMs)", "gpu")
    box(ax, 59, 33, 46, 17, "scatter · fine FFT · coarse power · dump copies\nsearch (hyperseti, CuPy)\n\n"
                            "integrated GPU: no PCIe copy; it reads and writes\nthe same LPDDR5x as the CPU and the NIC",
        "gpu", fs=7.8)
    box(ax, 3, 15, 104, 11, "128 GB LPDDR5x unified memory · 273 GB/s shared by CPU, GPU and NIC DMA\n"
                            "(measured GPU: 237 GB/s read, 174 GB/s write; registered host memory 172 / 111 GB/s)",
        "mem", fs=8.5, bold=True)
    box(ax, 3, 4, 50, 8.5, "ConnectX-7: 2 QSFP ports, each = 2 PCIe Gen5 x4 halves\n"
                           "link0 → enp1s0f0np0 (0000:01:00.0); link1 → port 2", "net", fs=7.8)
    box(ax, 57, 4, 24, 8.5, "NVMe 3.7 TB\n~2.7 GB/s writes (dumps)", "store", fs=7.8)
    box(ax, 84, 4, 23, 8.5, "1 GbE enP7s7\n192.168.2.101 → board\ncontrol (katcp)", "net", fs=7.4)
    for x in (28, 69, 95):
        arrow(ax, x, 12.5, x, 15, style="<|-|>")
    arrow(ax, 28, 26, 28, 30, style="<|-|>")
    arrow(ax, 82, 26, 82, 30, style="<|-|>")
    save(f, out, "f05_spark")


def data_movement(out):
    f, ax = fig(11, 7.4)
    ax.text(1, 72, "Where one stream's bytes go (per second, full band: 915 527 packets, 7.5 GB of samples)",
            fontsize=9.5, fontweight="bold")
    box(ax, 1, 52, 12, 10, "ConnectX-7\nNIC", "net", bold=True)
    group(ax, 17, 39, 33, 29, "host pinned memory (DAQIRI, 0.5 GB)", "mem", fs=7.8)
    box(ax, 19, 57, 29, 6, "header segments: 106 B × 64 Ki\n(Ethernet/IP/UDP + LFT3 header)", "cpu", fs=7)
    box(ax, 19, 49.5, 29, 6, "payload segments: 8192 B × 64 Ki", "gpu", fs=7)
    box(ax, 19, 41, 29, 6.5, "ScatterJob lists: payload ptr, block ptr,\nt, channel offset (sorted by group)", "cpu",
        fs=6.8)
    arrow(ax, 13, 59, 19, 60, fs=7)
    arrow(ax, 13, 55, 19, 52.5, fs=7)
    ax.text(13.3, 63.5, "DMA,\nheader/data\nsplit", fontsize=6.6, va="bottom")
    box(ax, 55, 57, 23, 10, "capture thread (CPU)\nparse header →\nAssembler → block, slot", "cpu", fs=7.2)
    arrow(ax, 48, 60, 55, 62, fs=6.6)
    ax.text(48.5, 64, "97 MB/s", fontsize=6.6)
    arrow(ax, 61, 57, 48, 45.5, fs=6.6)
    ax.text(56, 50, "jobs", fontsize=6.6)
    group(ax, 82, 39, 27, 29, "GPU", "gpu", fs=8)
    box(ax, 84, 46, 23, 12, "scatter kernel\n32 jobs × 32 channels\ntiles, 128-B runs\n≈ 83 µs per burst", "gpu",
        fs=7)
    arrow(ax, 48, 52.5, 84, 52.5)
    ax.text(66, 53.2, "reads payload 7.5 GB/s", fontsize=6.6, ha="center")
    arrow(ax, 48, 43, 84, 48.5)
    group(ax, 17, 3, 92, 32, "GPU memory: CUDA VMM allocation = the ring (shared by fd with other processes)", "gpu",
          fs=7.8)
    for i in range(9):
        k = "warn" if i == 6 else "gpu"
        box(ax, 19 + i * 9.8, 17, 9.2, 8.5, f"slot\n{i if i < 7 else ('…' if i == 7 else 'S−1')}", k, fs=7,
            rad=0.3)
    ax.text(19, 27.3, "S = 299 slots × 50.3 MB = 15 GB = 2.0 s; red = slot being written (invalid to readers)",
            fontsize=7)
    arrow(ax, 95.5, 46, 82.5, 25.7, fs=6.6)
    ax.text(91, 36.5, "writes 7.5 GB/s", fontsize=6.6)
    box(ax, 19, 4.5, 27, 9, "spectrometer kernel\nreads 7.5 GB/s →\naccumulator (2 × 786 432)", "reader", fs=6.9)
    box(ax, 49, 4.5, 26, 9, "accumulator → host pinned\n6.3 MB per spectrum (1/s)\n→ writer thread → NVMe",
        "reader", fs=6.9)
    box(ax, 78, 4.5, 29, 9, "dump: ring → pinned host\n7.5 GB per 1 s window (~0.7 s)\n→ GUPPI RAW on NVMe", "reader",
        fs=6.9)
    arrow(ax, 32, 17, 32, 13.5)
    arrow(ax, 46, 9, 49, 9)
    arrow(ax, 92, 17, 92, 13.5)
    ax.text(1, 46, "memory traffic\nper stream\n≈ 30 GB/s:\n\nNIC write 7.5\nscatter read 7.5\nscatter write 7.5\n"
                   "FFT read 7.5\n\n+ dumps,\nsearches", fontsize=6.9, va="top", color="#a08800")
    save(f, out, "f06_data_movement")

def packet_lifetime(out):
    f, ax = fig(11, 6.6)
    lanes = [("NIC", "net"), ("DAQIRI worker\n(core 6)", "cpu"), ("capture thread\n(core 7)", "cpu"),
             ("GPU", "gpu"), ("ring slot", "gpu"), ("readers", "reader")]
    ys = [56, 47, 38, 29, 20, 11]
    for (name, k), y in zip(lanes, ys):
        ax.add_patch(Rectangle((14, y - 3.8), 95, 7.6, fc=C[k][0], ec="none", alpha=0.5))
        ax.text(1, y, name, fontsize=8, va="center", fontweight="bold", color=C[k][1])
    ax.text(14, 64, "time →  (one packet, then the block it belongs to; typical values at 60.8 Gb/s)",
            fontsize=8.5, fontweight="bold")
    ev = [(16, 0, "frame arrives;\nflow rule → RX queue"), (29, 0, "DMA into header\nand payload buffers"),
          (33, 1, "burst complete:\n1024 pkts ≈ 1.1 ms\n(or 1 ms timeout)"), (46, 2, "get_rx_burst: parse\nheaders, "
          "assembler\n→ block + slot"), (60, 2, "sort jobs,\nlaunch scatter"), (64, 3, "scatter kernel\n≈ 83 µs"),
          (74, 2, "kernel done:\nburst returned\nto DAQIRI"), (74, 4, "payload now in\nrow t of the block")]
    pts = {}
    for x, lane, t in ev:
        y = ys[lane] - 2.6
        pts[(x, lane)] = (x, y)
        ax.plot([x], [y], "o", color="#333", ms=4)
        ax.text(x + 0.6, y + 0.9, t, fontsize=6.4, va="bottom")
    chain = [((16, 0), (29, 0)), ((29, 0), (33, 1)), ((33, 1), (46, 2)), ((46, 2), (60, 2)), ((60, 2), (64, 3)),
             ((64, 3), (74, 2)), ((64, 3), (74, 4))]
    for a, b in chain:
        (x0, y0), (x1, y1) = pts[a], pts[b]
        arrow(ax, x0 + 0.5, y0, x1 - 0.5, y1, lw=0.9)
    ax.plot([88, 88], [7, 61], ls="--", color="#888", lw=0.8)
    ax.text(88.6, 60.5, "block complete\n(6144 packets = 6.71 ms)", fontsize=6.4, va="top")
    ax.text(88.6, 38, "seal: zero-fill gaps\n→ CUDA event\n→ publish seq n", fontsize=6.4, va="center")
    ax.text(88.6, 20, "readable ~2 s, then the\nslot is reused (seq n+S)", fontsize=6.4, va="center")
    ax.text(88.6, 11, "spectrometer reads it\nwithin ~1 ms (lag 0);\ndump copies on trigger", fontsize=6.4,
            va="center")
    ax.text(14, 2.5, "A payload stays in DAQIRI's pinned buffer only until its burst's scatter kernel has run "
                     "(~1–2 ms); its data then lives in the ring for ~2 s.", fontsize=7.3)
    save(f, out, "f07_packet_lifetime")

def assembler(out):
    f, ax = fig(11, 5.2)
    ax.text(1, 49, "Assembler: packets → blocks of 1024 spectra (6 packets per spectrum, 6144 slots per block)",
            fontsize=9, fontweight="bold")
    import numpy as np
    rng = np.random.default_rng(3)
    blocks = [("block n−1\nsealed, published", 1.0), ("block n\ncomplete → sealed", 1.0),
              ("block n+1\nopen (gaps)", 0.93), ("block n+2\nopen", 0.55), ("block n+3\nopen (newest)", 0.2)]
    for j, (name, frac) in enumerate(blocks):
        x0 = 4 + j * 21
        ax.text(x0, 42.5, name, fontsize=7.5, va="bottom")
        for b in range(6):
            for t in range(16):
                filled = rng.random() < frac if frac < 1 else True
                col = "#2e8b3a" if filled else "#ffffff"
                if j == 0:
                    col = "#9fbfa3"
                ax.add_patch(Rectangle((x0 + t * 1.15, 14 + b * 4.4), 1.05, 4.0, fc=col, ec="#999", lw=0.3))
        ax.text(x0, 11.5, "t →  (1024 spectra; 16 drawn)", fontsize=6.3)
    ax.text(1.2, 27, "bindex\n b = 0…5", fontsize=6.8, rotation=90, va="center")
    box(ax, 4, 0.5, 104, 9.5, "At most 4 open blocks. The oldest is sealed when complete, or when the newest block is more "
                              "than 2 ahead (stale):\nits missing slots are zero-filled and counted. Blocks with no "
                              "packet at all are 'skipped'. Duplicates and packets for sealed\nblocks are rejected. t "
                              "jumping back by more than 1024 blocks = stream restart (re-sync).", "plain", fs=7.2)
    save(f, out, "f08_assembler")


def scatter(out):
    f, ax = fig(11, 4.6)
    ax.text(1, 44, "Scatter kernel: one CUDA block = 32 packets (jobs) × 32 channels, transposed through shared memory",
            fontsize=9, fontweight="bold")
    for r in range(8):
        for c in range(8):
            ax.add_patch(Rectangle((3 + c * 2.6, 8 + r * 3.6), 2.4, 3.3, fc="#d7f0d4", ec="#2e8b3a", lw=0.5))
    ax.text(3, 38, "packet payloads (pinned host)\nrow = one packet, 8 B per channel\nwarp reads 256 contiguous B",
            fontsize=7.2, va="bottom")
    arrow(ax, 25, 22, 35, 22, "load", fs=7.5)
    for r in range(8):
        for c in range(8):
            ax.add_patch(Rectangle((37 + c * 2.6, 8 + r * 3.6), 2.4, 3.3, fc="#fff6c9", ec="#a08800", lw=0.5))
    ax.text(37, 38, "shared memory tile\n32 × 33 × 8 B\n(+1 column: no bank conflicts)", fontsize=7.2, va="bottom")
    arrow(ax, 59, 22, 69, 22, "store\ntransposed", fs=7.5)
    for r in range(8):
        ax.add_patch(Rectangle((71, 8 + r * 3.6), 20.8, 3.3, fc="#d2f1ef", ec="#1f8a83", lw=0.5))
    ax.text(71, 38, "ring block rows [beam][channel][t][pol]\nwarp writes 32 consecutive t\nof one channel row = 128-B run",
            fontsize=7.2, va="bottom")
    box(ax, 94, 8, 15, 28, "Jobs are sorted\nby channel group\non the CPU, so a\ntile's 32 jobs are\nconsecutive in t.\n\n"
                           "Missing packets:\nsame kernel,\nnull source →\nzeros.\n\n≈ 83 µs per burst\n≈ 200 GB/s", "plain",
        fs=7)
    save(f, out, "f09_scatter")


def ring(out):
    f, ax = fig(11, 6.4)
    group(ax, 1, 34, 50, 28, "control plane: /dev/shm/lft3-<stream> (560 KB, fixed layout)", "cpu", fs=8)
    rows = [("header: layout, obs id, config hash, software, state", 54), ("head = next seq · futex word · writer heartbeat", 50),
            ("capture statistics (packets, blocks, loss, Gb/s)", 46), ("16 reader entries: name, cursor, lag, overruns, heartbeat", 42),
            ("seqToSlot[4096]: which slot holds sequence n", 38.5), ("slot metadata[S]: seq, block, t0, missing, NCO, UNIX t0", 35)]
    for t, y in rows:
        box(ax, 3, y, 46, 3.4, t, "cpu", fs=7, rad=0.2, lw=0.8)
    group(ax, 55, 34, 54, 28, "data plane: CUDA VMM allocation (fd via .sock)", "gpu", fs=8)
    for i in range(10):
        k = "warn" if i == 3 else ("gpu" if i != 2 else "reader")
        lab = ["n−2", "n−1", "n", "write", "", "", "", "", "n−S+1", ""][i]
        box(ax, 57 + i * 5.1, 44, 4.7, 10, lab, k, fs=6.5, rad=0.3, lw=0.8)
    ax.text(57, 40.5, "slots fill in allocation order; each = 50.3 MB block [2][6144][1024][2] int8", fontsize=7)
    ax.text(57, 37, "green: published · teal: a reader's cursor · red: acquired by the writer (seq = invalid)",
            fontsize=7)
    # protocol
    ax.text(1, 29, "Writer (capture), never waits for readers:", fontsize=8.5, fontweight="bold", color="#2f6db0")
    ax.text(1, 26.5, "1  acquireSlot(): slot.seq ← invalid (release), before any GPU write\n"
                     "2  scatter + zero-fill kernels; record CUDA event\n"
                     "3  event done → slot metadata; slot.seq ← n; seqToSlot[n] ← slot; head ← n+1 (release); futex wake",
            fontsize=7.6, va="top", family="DejaVu Sans Mono")
    ax.text(1, 15, "Reader (any process), lock-free:", fontsize=8.5, fontweight="bold", color="#1f8a83")
    ax.text(1, 12.5, "1  wait(n): futex until head > n          (if n < head − (S − 8): skip ahead, count overruns)\n"
                     "2  begin(n): slot = seqToSlot[n]; check slot.seq == n; copy metadata\n"
                     "3  GPU work on the block; synchronise\n"
                     "4  stillValid(n): slot.seq == n again? no → overrun, discard the result\n"
                     "5  consumed(n+1): cursor, lag, heartbeat (lossless readers hold the writer back, synthetic/file only)",
            fontsize=7.6, va="top", family="DejaVu Sans Mono")
    save(f, out, "f10_ring")


def finespec(out):
    f, ax = fig(11, 5.6)
    ax.text(1, 53, "Fine spectrometer (per block, per beam and coarse channel): one 1024-sample row → 8 FFTs of 128",
            fontsize=9, fontweight="bold")
    for i in range(8):
        box(ax, 3 + i * 9.4, 41, 8.8, 6, f"seg {i}\n128 × (X,Y)", "gpu", fs=6.8, rad=0.2, lw=0.8)
    ax.text(3, 48.5, "row = 1024 dual-pol samples = 6.71 ms", fontsize=7.2)
    for i in range(8):
        arrow(ax, 7.4 + i * 9.4, 41, 7.4 + i * 9.4, 37.5, lw=0.8)
        box(ax, 3 + i * 9.4, 32, 8.8, 5.2, "FFT X, Y\n|X|²+|Y|²", "reader", fs=6.5, rad=0.2, lw=0.8)
        arrow(ax, 7.4 + i * 9.4, 32, 40, 26.5, lw=0.6)
    box(ax, 28, 20.5, 24, 6, "Σ over 8 segments\n(in registers)", "reader", fs=7.2)
    arrow(ax, 40, 20.5, 40, 16.5)
    box(ax, 22, 9.5, 36, 7, "accumulator += (fftshift: bin b → (b+64) mod 128)\nfor 149 blocks = 1192 FFTs = 0.99992 s",
        "gpu", fs=7.2)
    arrow(ax, 40, 9.5, 40, 5.5)
    box(ax, 6, 0.5, 68, 5, "spectrum: 6144 coarse × 128 fine = 786 432 channels of 1.192 kHz per beam", "store",
        fs=7.4)
    box(ax, 80, 26, 29, 23, "Integration windows are aligned\nto absolute block numbers:\nwindow w = blocks [149w, 149w+148]"
                            "\n\nall readers and links share\none time grid; a reader that\nstarts mid-window waits for the\n"
                            "next boundary", "plain", fs=7.2)
    box(ax, 80, 1, 29, 21, "Coarse quick-look (no extra pass):\nP_coarse(k) = Σ_b P_fine(k, b) / 128\n"
                           "(Parseval: Σ|X_b|² = N Σ|x_n|²)\n\nmissing per spectrum = zero-filled\npackets + skipped/lost "
                           "blocks\n× 6144", "plain", fs=7.2)
    save(f, out, "f11_finespec")


def warp_fft(out):
    f, ax = fig(11, 5.4)
    ax.text(1, 51, "Warp FFT (N = 128 = 4 × 32): one warp per (channel, beam), no shared memory, no barriers",
            fontsize=9, fontweight="bold")
    for j in range(8):
        lane = j if j < 7 else 31
        x = 3 + j * 6.2
        ax.text(x + 2.6, 46.5, f"lane {lane}" if j != 6 else "…", fontsize=6.6, ha="center")
        if j == 6:
            continue
        for k in range(4):
            box(ax, x, 34.5 + (3 - k) * 2.8, 5.4, 2.5, f"x[{lane}+{32 * k}]", "gpu", fs=5.6, rad=0.15, lw=0.6)
    steps = ["1  each lane loads 4 samples n = j + 32k",
             "   (4-byte X,Y loads: 128 B per warp per k)",
             "2  4-point DFT over k in registers:",
             "   y_q[j] = Σ_k x[j+32k] · W4^(kq)",
             "3  twiddle: z_q[j] = y_q[j] · W128^(jq)",
             "4  for each q, a 32-point FFT across the lanes:",
             "   5 radix-2 stages with __shfl_xor (h = 16 … 1)",
             "5  lane l holds X[q + 4·bitrev5(l)]; |X|² + |Y|²",
             "   summed over the 8 segments in registers",
             "6  write 4 fftshifted bins per lane"]
    for i, t in enumerate(steps):
        indent = t.startswith("   ")
        ax.text(3, 31.5 - i * 2.35, t, fontsize=7.2, family="DejaVu Sans Mono" if indent else None)
    bx, dx = 64, 11
    for s_ in range(4):
        for l in range(8):
            ax.plot([bx + s_ * dx], [44 - l * 3.6], "o", ms=3.2, color="#2e8b3a")
            ax.text(bx - 2.2, 44 - l * 3.6, f"{l}", fontsize=6, va="center") if s_ == 0 else None
    for s_, h in enumerate([4, 2, 1]):
        for l in range(8):
            for p_ in (l, l ^ h):
                ax.plot([bx + s_ * dx, bx + (s_ + 1) * dx], [44 - l * 3.6, 44 - p_ * 3.6], color="#2e8b3a", lw=0.6,
                        alpha=0.75)
        ax.text(bx + s_ * dx + dx / 2, 47, f"xor {h}", fontsize=6.6, ha="center")
    ax.text(bx - 3, 15, "butterfly pattern across lanes (8 of 32 lanes and\n3 of 5 stages shown): each lane trades "
                        "values with\nlane l xor h, then adds or subtracts and multiplies\nby a twiddle; no shared "
                        "memory, no __syncthreads", fontsize=6.9, va="top")
    box(ax, 3, 0.5, 104, 6, "Before: 64-thread blocks, 7 synchronised stages × 8 segments = 56 __syncthreads per block "
                            "of data → 0.50 ms per 6.7 ms block.\nWarp kernel: 0.30 ms (1.66×), ~167 GB/s, close to "
                            "memory-bound; matches a reference DFT to < 1e-4.", "plain", fs=7.1)
    save(f, out, "f12_warp_fft")

def dump(out):
    f, ax = fig(11, 3.6)
    for i in range(22):
        k = "gpu"
        if 9 <= i <= 15:
            k = "warn" if i > 12 else "reader"
        box(ax, 3 + i * 4.6, 20, 4.2, 7, "", k, rad=0.2, lw=0.6)
    ax.text(3, 29, "ring (oldest → newest block)", fontsize=7.5)
    ax.plot([3 + 12.5 * 4.6] * 2, [17, 30], color="#b03a2e", lw=1.6)
    ax.text(3 + 12.5 * 4.6, 31, "trigger time t", ha="center", fontsize=7.5, color="#b03a2e")
    ax.text(3 + 9 * 4.6, 16, "← pre (0.5 s): already in the ring", fontsize=7, color="#1f8a83")
    ax.text(3 + 13 * 4.6, 13, "post (0.5 s): copied as they are published →", fontsize=7, color="#b03a2e")
    box(ax, 3, 1, 44, 8, "GPU copies each block into a pinned host buffer\n(7.5 GB/s of window in ~0.7 s; checked with "
                         "the seqlock)", "reader", fs=7.2)
    box(ax, 50, 1, 58, 8, "then written at NVMe speed (~2.8 s per 1 s window): one GUPPI RAW file\nper beam; "
                          "each block = 80-character header cards + one beam of a ring block\n"
                          "([channel][time][pol][re, im] int8)", "store", fs=7.0)
    save(f, out, "f13_dump")


def processes(out):
    f, ax = fig(11, 5.8)
    box(ax, 1, 46, 26, 10, "lft3-pipeline run\n(Python, host)\nplans + launches containers,\nstatus, metrics, NIC log",
        "cpu", fs=7.4)
    group(ax, 31, 3, 78, 55, "containers (image lft3-runtime), all --ipc=host, one set per link", "plain")
    box(ax, 33, 41, 22, 12, "lft3-<board>-<link>-capture\n--net host, NET_RAW,\nIPC_LOCK, /dev/infiniband\n"
                            "cpuset = link cores", "cpu", fs=7)
    box(ax, 59, 41, 22, 12, "…-spectrometer\nas you (uid + groups)\n--net none\ncpuset = reader_cpus", "reader",
        fs=7)
    box(ax, 85, 41, 22, 12, "…-dump\nas you, --net none\ntrigger socket\n/dev/shm/...-dump.sock", "reader", fs=7)
    box(ax, 33, 20, 74, 12, "/dev/shm (host IPC namespace):  lft3-<stream> (control block)  ·  lft3-<stream>.sock (ring "
                            "fd)\n·  lft3-<stream>-dump.sock (triggers)\n+ GPU: the ring's VMM allocation, imported by "
                            "each reader", "mem", fs=7.4)
    for x in (44, 70, 96):
        arrow(ax, x, 41, x, 32, style="<|-|>")
    box(ax, 33, 5, 35, 11, "logs: <run_dir>/<stream>-<role>.log\nstatus.jsonl, nic-<link>.csv, plan.txt", "store",
        fs=7.2)
    box(ax, 72, 5, 35, 11, "lft3-pipeline status / trigger / stop\nlft3.ring (Python): reads /dev/shm\nPrometheus "
                           "(--metrics-port)", "cpu", fs=7.2)
    arrow(ax, 27, 51, 33, 47)
    ax.plot([14, 14], [40, 46], ls=":", color="#333", lw=1)
    box(ax, 1, 20, 26, 20, "start order: capture →\nwait for its ring →\nreaders\n\nstop order: readers\n(close files) → "
                           "capture\n\nCtrl-C, --duration or\nlft3-pipeline stop", "plain", fs=7.2)
    save(f, out, "f14_processes")


def bringup(out):
    f, ax = fig(11, 2.5)
    steps = ["program\n.fpg", "destination\nMAC (ARP 10)", "RFDC init\nLMK / LMX", "MTS", "mixer event\n= SYSREF",
             "NCO\n(fine mixers\n+ header)", "requantiser,\nDDR, packet\nmask, fid", "PPS period", "sync\n(t = 0)"]
    x = 1
    for i, s in enumerate(steps):
        box(ax, x, 8, 10.8, 12, s, "board", fs=7.2)
        if i:
            arrow(ax, x - 1.3, 14, x, 14)
        x += 12.1
    ax.text(1, 3, "lft3-board start: ~10 s; every board in parallel under its lock, syncs released from a barrier. "
                  "packet_mask, fid, pfb_lsb only take effect after a sync.", fontsize=7.5)
    save(f, out, "f15_bringup")


def viewer(out):
    f, ax = fig(11, 4.4)
    box(ax, 1, 14, 18, 18, "browser\n(canvas waterfall,\nstrips, drift tool,\nmarks)", "plain", fs=7.6)
    group(ax, 23, 2, 64, 40, "lft3-view (C++, on the Spark, token-protected HTTP :8687)", "cpu", fs=8)
    box(ax, 25, 25, 18, 11, "run catalog\nchains rotated files\nby lft3_timestamp", "cpu", fs=7)
    box(ax, 46, 25, 18, 11, "HDF5 metadata +\nchunk index only\n(H5Dchunk_iter)", "cpu", fs=7)
    box(ax, 67, 25, 18, 11, "parallel pread +\ninflate/unshuffle\nper 64 Ki-ch chunk", "cpu", fs=7)
    box(ax, 25, 5, 28, 13, "encode u16db:\n16-bit dB planes,\nhi bytes RLE, gzip\n(2.2–2.7× smaller)", "cpu", fs=7)
    box(ax, 57, 5, 28, 13, "reduce to the screen tile:\nmax / mean per pixel,\noptional flatten\n(vectorised, all cores)",
        "cpu", fs=7)
    box(ax, 91, 14, 18, 18, "FBH5 files\nspectra/<board>/\n+ quicklook/\ntriggers.log\nknown_rfi.json", "store",
        fs=7.2)
    arrow(ax, 19, 27, 25, 30.5)
    ax.text(19.5, 32, "GET /api/tile", fontsize=6.8)
    arrow(ax, 43, 30.5, 46, 30.5)
    arrow(ax, 64, 30.5, 67, 30.5)
    arrow(ax, 91, 25, 85, 28)
    arrow(ax, 76, 25, 76, 18)
    arrow(ax, 57, 11.5, 53, 11.5)
    arrow(ax, 25, 9, 19, 17)
    ax.text(10.5, 9.5, "tile ~2 MB", fontsize=6.8)
    save(f, out, "f16_viewer")

def chart_kernels(out):
    f, ax = plt.subplots(figsize=(7.2, 2.6))
    names = ["scatter (capture)", "fine FFT, shared-mem kernel", "fine FFT, warp kernel", "coarse power (old reader)"]
    vals = [0.50, 0.50, 0.30, 0.23]
    cols = ["#2f6db0", "#9a9a9a", "#2e8b3a", "#9a9a9a"]
    ax.barh(names, vals, color=cols)
    ax.axvline(6.71, color="#b03a2e", ls="--")
    for i, v in enumerate(vals):
        ax.text(v + 0.05, i, f"{v:.2f} ms ({100 * v / 6.71:.1f} %)", va="center", fontsize=8)
    ax.set_xlim(0, 1.0)
    ax.set_xlabel("GPU time per 6.71 ms block of one stream (2 beams), alone on the GPU")
    ax.invert_yaxis()
    ax.spines[["top", "right"]].set_visible(False)
    f.tight_layout()
    save(f, out, "c1_kernels")


def chart_capacity(out):
    f, ax = plt.subplots(figsize=(7.2, 3.0))
    n = [1, 2, 3, 4, 5, 6]
    gpu = [17, 46, 71, 87, 96, 96]
    ok = [True, True, True, True, True, False]
    bars = ax.bar(n, gpu, color=["#2e8b3a" if o else "#b03a2e" for o in ok])
    for b, g, o in zip(bars, gpu, ok):
        ax.text(b.get_x() + b.get_width() / 2, g + 1.5, f"{g} %\n{'pass' if o else 'FAIL'}", ha="center", fontsize=8)
    ax.set_xticks(n, [f"{i}\n{60.8 * i:.0f} Gb/s" for i in n])
    ax.set_ylim(0, 118)
    ax.set_ylabel("GPU utilisation (%)")
    ax.set_xlabel("simultaneous real-time streams (2 beams each), with NIC-DMA memory load")
    ax.spines[["top", "right"]].set_visible(False)
    f.tight_layout()
    save(f, out, "c2_capacity")


def chart_rawspec(out):
    f, ax = plt.subplots(figsize=(7.2, 2.8))
    labels = ["ours, 128-pt\n(warp kernel)", "rawspec, 128-pt\n(best batching)", "rawspec, 2^17-pt", "cuFFT 2^17 floor\n(FFT only)"]
    vals = [0.15, 9.2 + 0.63, 13.2 + 5.2, 6.71 / 3.1]
    cols = ["#2e8b3a", "#9a9a9a", "#9a9a9a", "#2f6db0"]
    ax.bar(labels, vals, color=cols)
    ax.axhline(6.71, color="#b03a2e", ls="--")
    ax.text(-0.4, 7.3, "real time: 6.71 ms per block", color="#b03a2e", fontsize=8, ha="left")
    for i, v in enumerate(vals):
        ax.text(i, v * 1.15, f"{v:.2f} ms", ha="center", fontsize=8)
    ax.set_yscale("log")
    ax.set_ylim(0.05, 60)
    ax.set_ylabel("ms per 1024-spectrum block, one beam")
    ax.spines[["top", "right"]].set_visible(False)
    f.tight_layout()
    save(f, out, "c3_rawspec")


def chart_memory(out):
    f, ax = plt.subplots(figsize=(7.2, 2.9))
    parts = [("NIC DMA write", 7.6, "#6b6b6b"), ("scatter read", 7.5, "#7aa3d6"), ("scatter write", 7.5, "#2f6db0"),
             ("fine FFT read", 7.5, "#2e8b3a")]
    n = [1, 2, 3, 4, 5, 6]
    bottom = [0.0] * len(n)
    for name, v, c in parts:
        ax.bar(n, [v * k for k in n], bottom=bottom, color=c, label=f"{name} ({v} GB/s per stream)", width=0.6)
        bottom = [b + v * k for b, k in zip(bottom, n)]
    for k, b in zip(n, bottom):
        ax.text(k, b + 4, f"{b:.0f}", ha="center", fontsize=8)
    ax.axhline(230, color="#b03a2e", ls="--")
    ax.text(6.35, 236, "~230 GB/s usable (273 GB/s peak, shared with everything else)", color="#b03a2e", fontsize=8,
            ha="right")
    ax.set_xticks(n, [f"{k} stream{'s' if k > 1 else ''}" for k in n])
    ax.set_ylabel("GB/s of memory traffic")
    ax.set_ylim(0, 270)
    ax.legend(fontsize=7, loc="upper left", frameon=False, bbox_to_anchor=(0.0, 0.84))
    ax.spines[["top", "right"]].set_visible(False)
    f.tight_layout()
    save(f, out, "c4_memory")


ALL = [system_overview, signal_chain, packet_layout, freq_map, spark_hw, data_movement, packet_lifetime, assembler,
       scatter, ring, finespec, warp_fft, dump, processes, bringup, viewer, chart_kernels, chart_capacity,
       chart_rawspec, chart_memory]

if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "figures")
    out.mkdir(parents=True, exist_ok=True)
    for fn in ALL:
        fn(out)
    print(f"{len(ALL)} figures in {out}")
