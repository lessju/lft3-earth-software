"""Diagrams and charts for the LFT3-Earth system overview (docs/system_overview/build.py).

    python figures.py OUT_DIR

Each figure is drawn with matplotlib patches and saved as SVG (text kept as text) in one visual language:
  board / FPGA  orange   network  grey   host CPU  blue   GPU  green   storage  purple   readers  teal
Numbers in the figures are the measured / configured values documented in docs/runtime.md.
"""
import os
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
    preview = os.environ.get("LFT3_FIG_PREVIEW")   # optional PNG previews for reviewing layouts
    if preview:
        f.savefig(Path(preview) / f"{name}.png", dpi=110, bbox_inches="tight", pad_inches=0.05)
    plt.close(f)
    # keep spaces in labels as drawn (SVG collapses whitespace unless told otherwise)
    svg = path.read_text()
    path.write_text(svg.replace("<svg ", '<svg xml:space="preserve" ', 1))


# ------------------------------------------------------------------------------------------------ figures

def system_overview(out):
    f, ax = fig(11, 6.2)
    group(ax, 1, 33, 29, 27, "RFSoC board", "board")
    box(ax, 3, 49, 25, 6, "ADCs + down-conversion", "board")
    box(ax, 3, 41.5, 25, 6, "polyphase filterbank", "board")
    box(ax, 3, 34.5, 11.5, 5.5, "DDR ring", "board")
    box(ax, 16.5, 34.5, 11.5, 5.5, "packetizer", "board")
    arrow(ax, 15.5, 49, 15.5, 47.5)
    arrow(ax, 22, 41.5, 22, 40)
    arrow(ax, 14.5, 37.2, 16.5, 37.2)
    group(ax, 1, 3, 29, 23, "Host control", "cpu")
    box(ax, 3, 14, 25, 6, "lft3-board", "cpu")
    box(ax, 3, 5, 25, 6, "lft3-pipeline", "cpu")
    arrow(ax, 15.5, 20, 15.5, 33, lw=1.2)
    ax.text(16.5, 28, "1 GbE control", fontsize=7.5, color="#555")
    group(ax, 35, 3, 74, 57, "DGX Spark", "plain")
    box(ax, 38, 44, 15, 9, "NIC", "net")
    box(ax, 58, 44, 18, 9, "lft3-capture", "cpu")
    box(ax, 81, 42, 25, 13, "voltage ring\n(GPU memory)", "gpu")
    arrow(ax, 28, 37.2, 38, 47, lw=2.4)
    ax.text(30.5, 44, "100 GbE", fontsize=7.5, color="#555", rotation=44)
    arrow(ax, 53, 48.5, 58, 48.5, lw=2)
    arrow(ax, 76, 48.5, 81, 48.5, lw=2)
    readers = [("spectrometer", 52), ("dump", 70.5), ("other readers", 89)]
    for name, x in readers:
        box(ax, x, 26, 16, 7, name, "reader")
        arrow(ax, 93.5, 42, x + 8, 33)
    box(ax, 40, 7, 15, 9, "FBH5\nspectra", "store")
    box(ax, 59, 7, 15, 9, "GUPPI RAW\ndumps", "store")
    box(ax, 78, 7, 13, 9, "search\nhits", "store")
    box(ax, 94, 7, 13, 9, "viewer", "reader")
    arrow(ax, 60, 26, 48, 16)
    arrow(ax, 78.5, 26, 67, 16)
    arrow(ax, 28, 8, 35, 8, ls=":", lw=1)
    save(f, out, "f01_system")


def signal_chain(out):
    f, ax = fig(11, 2.4)
    steps = [("ADC\n5 GSPS", "board"), ("mix with\nNCO", "board"), ("decimate\n× 4", "board"), ("PFB\n8192 ch", "board"),
             ("keep\n6144 ch", "board"), ("requantise\n8 + 8 bit", "board"), ("packetize", "board"), ("100 GbE", "net")]
    x = 6
    ax.text(1, 17, "RF", fontsize=8.5, va="center")
    for i, (t, k) in enumerate(steps):
        box(ax, x, 12, 10.5, 10, t, k)
        arrow(ax, x - 2.4 if i else 3.5, 17, x, 17)
        x += 12.9
    box(ax, 57, 1, 22, 6.5, "DDR ring (4-bit, 0.86 s)", "board", fs=8)
    arrow(ax, 32, 12, 59, 7.5, ls="--", lw=1)
    arrow(ax, 79, 4.2, 88, 12, ls="--", lw=1)
    ax.text(82.5, 5, "on trigger", fontsize=7.5, color="#555")
    save(f, out, "f02_signal_chain")


def packet_layout(out):
    f, ax = fig(11, 4.6)
    W = 88
    words = [("t  (58 bits)", 58), ("b", 3), ("fid", 3)], [("nco0", 32), ("nco1", 32)], [("nco2", 32), ("nco3", 32)], \
        [("pfb_lsb0", 16), ("pfb_lsb1", 16), ("pfb_lsb2", 16), ("pfb_lsb3", 16)]
    ax.text(1, 43, "header", fontsize=9, fontweight="bold")
    for i, fields in enumerate(words):
        yy = 36 - i * 4.4
        ax.text(1, yy + 1.7, f"word {i}", fontsize=7.5, va="center", color="#555")
        x = 10
        for txt, bits in fields:
            w = W * bits / 64
            box(ax, x, yy, w, 3.5, txt, "cpu", fs=7.5, rad=0.2, lw=0.8)
            x += w
    ax.text(10, 40.5, "bit 63", fontsize=7, color="#555")
    ax.text(98, 40.5, "bit 0", fontsize=7, color="#555", ha="right")
    ax.text(1, 17, "payload", fontsize=9, fontweight="bold")
    labels = ["ch 0", "ch 1", "ch 2", "…", "ch 1023"]
    for i, lab in enumerate(labels):
        box(ax, 10 + i * 17.6, 11, 17, 4.2, lab, "gpu", fs=7.5, rad=0.2, lw=0.8)
    parts = ["I0", "Q0", "I1", "Q1", "I2", "Q2", "I3", "Q3"]
    for i, lab in enumerate(parts):
        box(ax, 10 + i * 6.5, 2, 6.2, 4, lab, "gpu" if i < 4 else "mem", fs=7.5, rad=0.2, lw=0.8)
    ax.plot([10, 10], [6, 11], color="#888", lw=0.8, ls=":")
    ax.plot([27, 62], [11, 6], color="#888", lw=0.8, ls=":")
    ax.text(23, 0.2, "beam 0 (X, Y)", fontsize=7.5, ha="center", va="top", color="#2e8b3a")
    ax.text(49, 0.2, "beam 1 (X, Y)", fontsize=7.5, ha="center", va="top", color="#a08800")
    save(f, out, "f03_packet")


def freq_map(out):
    f, ax = fig(11, 3.1)
    ax.text(1, 27.5, "PFB bins", fontsize=8, color="#555")
    box(ax, 12, 23, 30, 5, "0 … 3071", "board")
    box(ax, 43, 23, 24, 5, "3072 … 5119  (dropped)", "net", fs=7.8)
    box(ax, 68, 23, 30, 5, "5120 … 8191", "board")
    ax.text(1, 11.5, "channel k", fontsize=8, color="#555")
    box(ax, 12, 9, 43, 5, "k = 0 … 3071   (b = 0, 1, 2)", "gpu")
    box(ax, 55, 9, 43, 5, "k = 3072 … 6143   (b = 3, 4, 5)", "gpu")
    arrow(ax, 83, 23, 33, 14, color="#c8641e", lw=1.2)
    arrow(ax, 27, 23, 76, 14, color="#c8641e", lw=1.2)
    ax.plot([12, 98], [5.5, 5.5], color="#333", lw=0.9)
    for x, lab in ((12, "1562.5"), (55, "2031.25"), (98, "2499.8")):
        ax.plot([x, x], [5, 6], color="#333", lw=0.9)
        ax.text(x, 2.6, lab, fontsize=7.5, ha="center")
    ax.text(103, 4.4, "MHz", fontsize=7.5, va="center")
    save(f, out, "f04_freq_map")


def spark_hw(out):
    f, ax = fig(11, 5.2)
    group(ax, 1, 25, 52, 24, "Arm CPU (20 cores)", "cpu")
    for i, c in enumerate([5, 6, 7, 8, 9, 15, 16, 17, 18, 19]):
        box(ax, 3 + (i % 5) * 9.8, 37.5 - (i // 5) * 5.3, 9, 4.4, f"X925 · {c}", "warn" if c in (5, 6, 7) else "cpu",
            fs=7.5, rad=0.3)
    for i, c in enumerate([0, 1, 2, 3, 4, 10, 11, 12, 13, 14]):
        box(ax, 3 + i * 4.9, 26.5, 4.5, 4, f"{c}", "cpu", fs=7, rad=0.2)
    ax.add_patch(Rectangle((33, 45.6), 1.6, 1.4, fc=C["warn"][0], ec=C["warn"][1]))
    ax.text(35.2, 46.3, "link0 capture cores", fontsize=7, va="center", color="#b03a2e")
    group(ax, 57, 25, 52, 24, "Blackwell GPU (48 SMs)", "gpu")
    box(ax, 60, 29, 46, 13, "no copy to or from host memory:\nreads and writes the same DRAM", "gpu", fs=8)
    box(ax, 1, 13, 108, 8, "128 GB LPDDR5x unified memory · 273 GB/s, shared by CPU, GPU and NIC", "mem", fs=9,
        bold=True)
    box(ax, 1, 1, 46, 8, "ConnectX-7 · 2 × QSFP (100 GbE links)", "net", fs=8)
    box(ax, 51, 1, 28, 8, "NVMe 3.7 TB", "store", fs=8)
    box(ax, 83, 1, 26, 8, "1 GbE to boards", "net", fs=8)
    for x in (24, 65, 96):
        arrow(ax, x, 9, x, 13, style="<|-|>", lw=1.2)
    for x in (27, 83):
        arrow(ax, x, 21, x, 25, style="<|-|>", lw=1.2)
    save(f, out, "f05_spark")


def data_movement(out):
    f, ax = fig(11, 5.8)
    box(ax, 1, 40, 11, 9, "NIC", "net", bold=True)
    group(ax, 16, 28, 32, 28, "pinned host memory", "mem", fs=8.5)
    box(ax, 18, 44, 28, 6, "headers (106 B each)", "cpu")
    box(ax, 18, 36.5, 28, 6, "payloads (8192 B each)", "gpu")
    box(ax, 18, 29.5, 28, 5, "job list", "cpu")
    arrow(ax, 12, 47, 18, 47)
    arrow(ax, 12, 42, 18, 39.5)
    box(ax, 54, 43, 18, 9, "capture thread\n(CPU)", "cpu")
    arrow(ax, 46, 47, 54, 47.5)
    arrow(ax, 57, 43, 46, 33)
    group(ax, 78, 28, 31, 28, "GPU", "gpu", fs=8.5)
    box(ax, 81, 35, 25, 10, "scatter\nkernel", "gpu")
    arrow(ax, 46, 39.5, 81, 41, "7.5 GB/s", fs=7.5, off=(0, 0.4))
    arrow(ax, 46, 32, 81, 37.5)
    group(ax, 16, 1, 93, 23, "the ring (GPU memory)", "gpu", fs=8.5)
    for i in range(10):
        k = "warn" if i == 6 else "gpu"
        box(ax, 18 + i * 8.8, 10.5, 8, 7, "", k, rad=0.3)
    arrow(ax, 93.5, 35, 74.5, 17.5, "7.5 GB/s", fs=7.5, off=(4.5, -1))
    box(ax, 18, 2.5, 25, 5.5, "spectrometer", "reader")
    box(ax, 82, 2.5, 25, 5.5, "dump", "reader")
    arrow(ax, 30, 10.5, 30, 8)
    arrow(ax, 94, 10.5, 94, 8)
    save(f, out, "f06_data_movement")


def packet_lifetime(out):
    f, ax = fig(11, 5.4)
    lanes = [("NIC", "net"), ("DAQIRI worker", "cpu"), ("capture thread", "cpu"), ("GPU", "gpu"), ("ring", "gpu"),
             ("readers", "reader")]
    ys = [47, 39, 31, 23, 15, 7]
    for (name, k), y in zip(lanes, ys):
        ax.add_patch(Rectangle((16, y - 3.2), 93, 6.4, fc=C[k][0], ec="none", alpha=0.55))
        ax.text(1, y, name, fontsize=8, va="center", fontweight="bold", color=C[k][1])
    # label placement per event (dx, dy, ha, va), chosen to keep clear of the incoming/outgoing arrows
    up, right, left, down = (0, 1.3, "center", "bottom"), (1.0, 0, "left", "center"), (-1.0, 0, "right", "center"), \
        (0, -1.3, "center", "top")
    ev = [(19, 0, "arrives", up), (28, 0, "DMA", up), (33, 1, "burst ready", right), (45, 2, "parse + place", down),
          (57, 2, "launch", up), (62, 3, "scatter", left), (72, 2, "burst freed", up), (72, 4, "row written", right)]
    pts = {}
    for x, lane, t, (dx, dy, ha, va) in ev:
        y = ys[lane] - 1.2
        pts[(x, lane)] = (x, y)
        ax.plot([x], [y], "o", color="#333", ms=4)
        ax.text(x + dx, y + dy, t, fontsize=7.3, ha=ha, va=va)
    chain = [((19, 0), (28, 0)), ((28, 0), (33, 1)), ((33, 1), (45, 2)), ((45, 2), (57, 2)), ((57, 2), (62, 3)),
             ((62, 3), (72, 2)), ((62, 3), (72, 4))]
    for a, b in chain:
        (x0, y0), (x1, y1) = pts[a], pts[b]
        arrow(ax, x0 + 0.6, y0, x1 - 0.6, y1, lw=0.9)
    ax.plot([84, 84], [3, 51], ls="--", color="#888", lw=0.8)
    ax.text(84, 52, "block complete", fontsize=7.3, ha="center")
    for x, lane, t, (dx, dy, ha, va) in ((90, 2, "seal + publish", up), (97, 4, "readable ~2 s", right),
                                         (90, 5, "read", left)):
        y = ys[lane] - 1.2
        ax.plot([x], [y], "o", color="#333", ms=4)
        ax.text(x + dx, y + dy, t, fontsize=7.3, ha=ha, va=va)
    arrow(ax, 90.6, ys[2] - 1.2, 96.5, ys[4] - 1.2, lw=0.9)
    arrow(ax, 96.5, ys[4] - 1.2, 90.6, ys[5] - 1.2, lw=0.9)
    ax.annotate("", xy=(72, 1.2), xytext=(19, 1.2), arrowprops=dict(arrowstyle="<->", color="#555", lw=0.8))
    ax.text(45.5, -0.6, "≈ 1–2 ms per packet", fontsize=7.3, ha="center", color="#555")
    save(f, out, "f07_packet_lifetime")


def assembler(out):
    import numpy as np
    f, ax = fig(11, 3.6)
    rng = np.random.default_rng(3)
    blocks = [("published", 1.0, "#9fbfa3"), ("complete", 1.0, None), ("open", 0.93, None), ("open", 0.55, None),
              ("newest", 0.2, None)]
    for j, (name, frac, fixed) in enumerate(blocks):
        x0 = 9 + j * 20.5
        ax.text(x0 + 9, 33.5, name, fontsize=8, ha="center")
        for b in range(6):
            for t in range(16):
                filled = True if frac >= 1 else rng.random() < frac
                col = fixed or ("#2e8b3a" if filled else "#ffffff")
                ax.add_patch(Rectangle((x0 + t * 1.15, 5 + b * 4.3), 1.05, 3.9, fc=col, ec="#aaa", lw=0.3))
    ax.text(1, 18, "b", fontsize=8, va="center")
    ax.annotate("", xy=(25, 1.6), xytext=(9, 1.6), arrowprops=dict(arrowstyle="->", color="#555", lw=0.8))
    ax.text(26, 1.6, "t", fontsize=8, va="center")
    ax.add_patch(FancyBboxPatch((28.6, 3.8), 80.3, 27.4, boxstyle="round,pad=0.3", fc="none", ec="#2f6db0", lw=1,
                                ls="--"))
    ax.text(68.5, 0.6, "open blocks (at most 4)", fontsize=7.5, color="#2f6db0", ha="center")
    save(f, out, "f08_assembler")


def scatter(out):
    f, ax = fig(11, 3.8)
    hi_in, hi_tile, hi_out = "#2e8b3a", "#a08800", "#1f8a83"
    for r in range(8):
        for c in range(8):
            ax.add_patch(Rectangle((4 + c * 2.6, 4 + r * 3.4), 2.4, 3.1, fc=hi_in if c == 2 else "#d7f0d4",
                                   ec="#2e8b3a", lw=0.5))
    ax.text(14.5, 33, "packets", fontsize=8.5, ha="center")
    ax.text(14.5, 1, "→ channel", fontsize=7.3, ha="center", color="#555")
    ax.text(1.8, 17.6, "← packet (t)", fontsize=7.3, rotation=90, va="center", color="#555")
    arrow(ax, 27, 17.6, 36, 17.6, "load", fs=7.5)
    for r in range(8):
        for c in range(8):
            ax.add_patch(Rectangle((38 + c * 2.6, 4 + r * 3.4), 2.4, 3.1, fc=hi_tile if c == 2 else "#fff6c9",
                                   ec="#a08800", lw=0.5))
    ax.text(48.5, 33, "shared-memory tile", fontsize=8.5, ha="center")
    arrow(ax, 61, 17.6, 70, 17.6, "store", fs=7.5)
    for r in range(8):
        ax.add_patch(Rectangle((72, 4 + r * 3.4), 26, 3.1, fc=hi_out if r == 5 else "#d2f1ef", ec="#1f8a83", lw=0.5))
    ax.text(85, 33, "ring rows", fontsize=8.5, ha="center")
    ax.text(85, 1, "→ t", fontsize=7.3, ha="center", color="#555")
    ax.text(100, 22.6, "one channel", fontsize=7.3, va="center", color=hi_out)
    save(f, out, "f09_scatter")


def ring(out):
    f, ax = fig(11, 4.2)
    group(ax, 1, 3, 40, 37, "control block (shared memory)", "cpu", fs=8.5)
    for i, t in enumerate(["layout · ids", "head · futex", "capture statistics", "reader cursors", "seq → slot",
                           "slot metadata"]):
        box(ax, 3, 31 - i * 5, 36, 4, t, "cpu", fs=7.8, rad=0.2, lw=0.8)
    group(ax, 45, 3, 64, 37, "voltage blocks (GPU memory)", "gpu", fs=8.5)
    kinds = ["gpu"] * 3 + ["reader"] + ["gpu"] * 2 + ["warn"] + ["gpu"] * 3
    for i, k in enumerate(kinds):
        box(ax, 48 + i * 6, 12, 5.3, 14, "", k, rad=0.3)
    arrow(ax, 87.5, 34, 87.5, 26.5)
    ax.text(87.5, 34.8, "writer", fontsize=7.8, ha="center", color="#b03a2e")
    arrow(ax, 69.5, 34, 69.5, 26.5)
    ax.text(69.5, 34.8, "reader", fontsize=7.8, ha="center", color="#1f8a83")
    ax.text(81.3, 8.5, "head", fontsize=7.8, ha="center")
    arrow(ax, 81.3, 10, 81.3, 12, lw=1)
    ax.annotate("", xy=(108, 6), xytext=(48, 6), arrowprops=dict(arrowstyle="->", color="#555", lw=0.8))
    ax.text(78, 3.8, "time (wraps around)", fontsize=7.3, ha="center", color="#555")
    save(f, out, "f10_ring")


def finespec(out):
    f, ax = fig(11, 3.8)
    for i in range(8):
        box(ax, 3 + i * 7.6, 28, 7, 5, f"{i}", "gpu", fs=8, rad=0.2, lw=0.8)
    ax.text(3, 34.5, "one row: 1024 samples = 8 segments of 128", fontsize=7.8, color="#555")
    for i in range(8):
        x = 6.5 + i * 7.6
        arrow(ax, x, 28, x, 24.5, lw=0.8)
        box(ax, 3 + i * 7.6, 19.5, 7, 5, "FFT", "reader", fs=7.5, rad=0.2, lw=0.8)
        arrow(ax, x, 19.5, 33, 15, lw=0.6)
    box(ax, 22, 9, 22, 6, "Σ |X|² + |Y|²", "reader")
    arrow(ax, 44, 12, 52, 12)
    box(ax, 52, 9, 22, 6, "accumulator\n(149 blocks)", "gpu", fs=8)
    arrow(ax, 74, 12, 82, 12)
    box(ax, 82, 9, 25, 6, "1 s spectrum\n786 432 ch", "store", fs=8)
    arrow(ax, 94.5, 9, 94.5, 4.5)
    box(ax, 82, 0, 25, 4.5, "coarse: Σ over 128 bins", "store", fs=7.3, rad=0.2)
    save(f, out, "f11_finespec")


def warp_fft(out):
    f, ax = fig(11, 3.6)
    for j in range(8):
        lane = j if j < 7 else 31
        x = 2 + j * 4.6
        ax.text(x + 2, 31.5, f"{lane}" if j != 6 else "…", fontsize=7.3, ha="center", color="#555")
        if j == 6:
            continue
        for k in range(4):
            box(ax, x, 5 + (3 - k) * 6.2, 4, 5.4, "", "gpu", rad=0.15, lw=0.6)
    ax.text(18, 34.5, "lanes (4 samples each)", fontsize=8, ha="center")
    arrow(ax, 39, 17.5, 46, 17.5)
    box(ax, 46, 11, 18, 13, "4-point DFT\n+ twiddle\n(registers)", "reader", fs=8)
    arrow(ax, 64, 17.5, 69, 17.5)
    bx, dx = 71, 9.5
    for s_ in range(4):
        for l in range(8):
            ax.plot([bx + s_ * dx], [29 - l * 3.4], "o", ms=2.8, color="#2e8b3a")
    for s_, h in enumerate([4, 2, 1]):
        for l in range(8):
            for p_ in (l, l ^ h):
                ax.plot([bx + s_ * dx, bx + (s_ + 1) * dx], [29 - l * 3.4, 29 - p_ * 3.4], color="#2e8b3a", lw=0.6,
                        alpha=0.75)
    ax.text(bx + 1.5 * dx, 34.5, "32-point FFT across lanes (shuffles)", fontsize=8, ha="center")
    save(f, out, "f12_warp_fft")


def dump(out):
    f, ax = fig(11, 3.0)
    n = 24
    for i in range(n):
        k = "gpu"
        if 9 <= i <= 12:
            k = "reader"
        elif 13 <= i <= 16:
            k = "warn"
        box(ax, 2 + i * 4.4, 15, 4, 7, "", k, rad=0.2, lw=0.6)
    tx = 2 + 13 * 4.4 - 0.2
    ax.plot([tx, tx], [12.5, 25], color="#b03a2e", lw=1.6)
    ax.text(tx, 26, "trigger", ha="center", fontsize=8, color="#b03a2e")
    ax.annotate("", xy=(2 + 9 * 4.4, 12.5), xytext=(tx, 12.5), arrowprops=dict(arrowstyle="<->", color="#1f8a83"))
    ax.text((2 + 9 * 4.4 + tx) / 2, 9.8, "pre", ha="center", fontsize=7.5, color="#1f8a83")
    ax.annotate("", xy=(2 + 17 * 4.4 - 0.4, 12.5), xytext=(tx, 12.5), arrowprops=dict(arrowstyle="<->", color="#b03a2e"))
    ax.text((2 + 17 * 4.4 + tx) / 2, 9.8, "post", ha="center", fontsize=7.5, color="#b03a2e")
    ax.text(2, 24, "ring", fontsize=8)
    box(ax, 30, 0.5, 22, 6, "pinned host buffer", "reader", fs=8)
    box(ax, 62, 0.5, 22, 6, "GUPPI RAW (NVMe)", "store", fs=8)
    arrow(ax, 52, 3.5, 62, 3.5)
    arrow(ax, tx, 9, 44, 6.5, lw=1)
    save(f, out, "f13_dump")


def processes(out):
    f, ax = fig(11, 4.0)
    box(ax, 1, 28, 20, 8, "lft3-pipeline", "cpu", bold=True)
    group(ax, 26, 13, 83, 26, "containers (one set per link)", "plain", fs=8.5)
    box(ax, 29, 24, 22, 9, "capture", "cpu")
    box(ax, 57, 24, 22, 9, "spectrometer", "reader")
    box(ax, 85, 24, 22, 9, "dump", "reader")
    box(ax, 29, 15, 78, 6, "/dev/shm  ·  GPU ring", "mem", fs=8.5)
    for x in (40, 68, 96):
        arrow(ax, x, 24, x, 21, style="<|-|>", lw=1.1)
    arrow(ax, 21, 32, 29, 30)
    box(ax, 29, 1, 36, 7, "run directory: logs, status, NIC counters", "store", fs=7.8)
    box(ax, 71, 1, 36, 7, "lft3-pipeline status · trigger · stop", "cpu", fs=7.8)
    arrow(ax, 89, 8, 89, 15, style="<|-|>", lw=1.1)
    arrow(ax, 11, 28, 29, 5, lw=1, ls=":")
    save(f, out, "f14_processes")


def bringup(out):
    f, ax = fig(11, 1.6)
    steps = ["program", "destination\nMAC", "clocks", "MTS", "SYSREF\nevents", "NCO", "requantiser\n+ packetizer",
             "PPS", "sync"]
    x = 1
    for i, s in enumerate(steps):
        box(ax, x, 3, 10.6, 10, s, "board", fs=8)
        if i:
            arrow(ax, x - 1.4, 8, x, 8)
        x += 12.1
    save(f, out, "f15_bringup")


def viewer(out):
    f, ax = fig(11, 3.4)
    box(ax, 1, 12, 15, 10, "browser", "plain", bold=True)
    group(ax, 21, 1, 66, 31, "lft3-view (C++, on the Spark)", "cpu", fs=8.5)
    box(ax, 24, 17, 18, 8, "run catalog", "cpu")
    box(ax, 45, 17, 18, 8, "chunk index", "cpu")
    box(ax, 66, 17, 18, 8, "parallel read\n+ decode", "cpu", fs=8)
    box(ax, 24, 4, 27, 8, "encode tile (16-bit dB)", "cpu", fs=8)
    box(ax, 57, 4, 27, 8, "reduce to pixels", "cpu", fs=8)
    box(ax, 92, 12, 17, 10, "FBH5\nfiles", "store")
    arrow(ax, 16, 19, 24, 21)
    arrow(ax, 42, 21, 45, 21)
    arrow(ax, 63, 21, 66, 21)
    arrow(ax, 92, 17, 84, 20)
    arrow(ax, 75, 17, 75, 12)
    arrow(ax, 57, 8, 51, 8)
    arrow(ax, 24, 8, 16, 14.5)
    ax.text(17, 23.5, "request", fontsize=7.3, color="#555")
    ax.text(15, 8, "tile", fontsize=7.3, color="#555", ha="right")
    save(f, out, "f16_viewer")


# ------------------------------------------------------------------------------------------------ charts

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
