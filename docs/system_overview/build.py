"""Build docs/system_overview/lft3_system_overview.pdf: how the LFT3-Earth system works.

    python figures.py figures && python build.py            (needs reportlab, svglib, matplotlib)

The text is in this file; the diagrams are drawn by figures.py. Numbers are the measured values recorded in
docs/runtime.md (2026-11-13); update both together.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm, mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (BaseDocTemplate, Frame, Image, KeepTogether, NextPageTemplate, PageBreak,
                                PageTemplate, Paragraph, Spacer, Table, TableStyle)
from reportlab.platypus.tableofcontents import TableOfContents
from svglib import fonts as svgfonts
from svglib.svglib import svg2rlg

HERE = Path(__file__).resolve().parent
FIG = HERE / "figures"
OUT = HERE / "lft3_system_overview.pdf"
VERSION = "1.0"
DATE = dt.date(2026, 11, 13).strftime("%d %B %Y")

# ---------------------------------------------------------------------------------------------- fonts, styles
FONTS = {"Sans": "DejaVuSans.ttf", "Sans-Bold": "DejaVuSans-Bold.ttf", "Sans-Oblique": "DejaVuSans-Oblique.ttf",
         "Mono": "DejaVuSansMono.ttf", "Serif": "DejaVuSerif.ttf", "Serif-Bold": "DejaVuSerif-Bold.ttf",
         "Serif-Italic": "DejaVuSerif-Italic.ttf"}
FONT_DIRS = [Path("/usr/share/fonts/truetype/dejavu")]
try:
    import matplotlib
    FONT_DIRS.append(Path(matplotlib.get_data_path()) / "fonts" / "ttf")
except ImportError:
    pass
for name, file in FONTS.items():
    for d in FONT_DIRS:
        if (d / file).exists():
            pdfmetrics.registerFont(TTFont(name, str(d / file)))
            break
# Map the font names in the SVG figures (matplotlib writes 'DejaVu Sans' / 'DejaVu Sans Mono', bold as weight 700)
# to the embedded TTFs. svglib needs the file path to accept a mapping.
def _font_path(file):
    for d in FONT_DIRS:
        if (d / file).exists():
            return str(d / file)
    return None


for fam, weight, style, rl in (("DejaVu Sans", "normal", "normal", "Sans"), ("DejaVu Sans", "bold", "normal", "Sans-Bold"),
                               ("DejaVu Sans", "700", "normal", "Sans-Bold"),
                               ("DejaVu Sans", "normal", "italic", "Sans-Oblique"),
                               ("DejaVu Sans Mono", "normal", "normal", "Mono")):
    svgfonts.register_font(fam, _font_path(FONTS[rl]), weight=weight, style=style, rlgFontName=rl)
pdfmetrics.registerFontFamily("Serif", normal="Serif", bold="Serif-Bold", italic="Serif-Italic", boldItalic="Serif-Bold")
pdfmetrics.registerFontFamily("Sans", normal="Sans", bold="Sans-Bold", italic="Sans-Oblique", boldItalic="Sans-Bold")

INK = colors.HexColor("#1d2733")
ACCENT = colors.HexColor("#2f6db0")
MUTED = colors.HexColor("#5b6670")
RULE = colors.HexColor("#c9d3dd")
SHADE = colors.HexColor("#f2f5f8")

S = {
    "body": ParagraphStyle("body", fontName="Serif", fontSize=9.6, leading=13.6, textColor=INK, alignment=TA_JUSTIFY,
                           spaceAfter=6),
    "h1": ParagraphStyle("h1", fontName="Sans-Bold", fontSize=17, leading=21, textColor=ACCENT, spaceBefore=4,
                         spaceAfter=10, keepWithNext=1),
    "h2": ParagraphStyle("h2", fontName="Sans-Bold", fontSize=12, leading=15, textColor=INK, spaceBefore=10,
                         spaceAfter=5, keepWithNext=1),
    "h3": ParagraphStyle("h3", fontName="Sans-Bold", fontSize=10, leading=13, textColor=INK, spaceBefore=7,
                         spaceAfter=3, keepWithNext=1),
    "title": ParagraphStyle("title", fontName="Sans-Bold", fontSize=17, leading=21, textColor=ACCENT, spaceAfter=10),
    "bullet": ParagraphStyle("bullet", fontName="Serif", fontSize=9.6, leading=13.4, textColor=INK, leftIndent=13,
                             bulletIndent=3, spaceAfter=2.5, alignment=TA_LEFT),
    "caption": ParagraphStyle("caption", fontName="Sans", fontSize=8, leading=10.5, textColor=MUTED,
                              alignment=TA_LEFT, spaceBefore=3, spaceAfter=10),
    "code": ParagraphStyle("code", fontName="Mono", fontSize=7.8, leading=10.2, textColor=INK, backColor=SHADE,
                           borderPadding=(5, 6, 5, 6), leftIndent=4, rightIndent=4, spaceBefore=4, spaceAfter=9),
    "cell": ParagraphStyle("cell", fontName="Sans", fontSize=8, leading=10.3, textColor=INK),
    "cellb": ParagraphStyle("cellb", fontName="Sans-Bold", fontSize=8, leading=10.3, textColor=colors.white),
    "note": ParagraphStyle("note", fontName="Sans", fontSize=8.6, leading=12, textColor=INK, backColor=colors.HexColor("#eef4fb"),
                           borderColor=ACCENT, borderWidth=0, borderPadding=(6, 8, 6, 8), leftIndent=6, rightIndent=6,
                           spaceBefore=6, spaceAfter=10),
    "toc1": ParagraphStyle("toc1", fontName="Sans-Bold", fontSize=10, leading=15, leftIndent=0),
    "toc2": ParagraphStyle("toc2", fontName="Sans", fontSize=9, leading=12.5, leftIndent=16, textColor=MUTED),
}


def mono(t: str) -> str:
    return f'<font name="Mono" size="8.6">{t}</font>'


class Doc(BaseDocTemplate):
    def __init__(self, path):
        super().__init__(str(path), pagesize=A4, leftMargin=2.1 * cm, rightMargin=2.1 * cm, topMargin=2.2 * cm,
                         bottomMargin=2.0 * cm, title="LFT3-Earth digital backend: how the system works",
                         author="LFT3-Earth software", subject="System overview", creator="docs/system_overview/build.py")
        frame = Frame(self.leftMargin, self.bottomMargin, self.width, self.height, id="f")
        self.addPageTemplates([PageTemplate("cover", [frame], onPage=self._cover),
                               PageTemplate("body", [frame], onPage=self._page)])
        self.section = ""

    def _cover(self, c, d):
        w, h = A4
        c.saveState()
        c.setFillColor(ACCENT)
        c.rect(0, h - 7.2 * cm, w, 7.2 * cm, fill=1, stroke=0)
        c.restoreState()

    def beforeDocument(self):
        self.section = ""

    def _page(self, c, d):
        w, h = A4
        c.saveState()
        c.setStrokeColor(RULE)
        c.setLineWidth(0.6)
        c.line(self.leftMargin, h - 1.45 * cm, w - self.rightMargin, h - 1.45 * cm)
        c.setFont("Sans", 7.6)
        c.setFillColor(MUTED)
        c.drawString(self.leftMargin, h - 1.25 * cm, "LFT3-Earth digital backend · how the system works")
        c.drawRightString(w - self.rightMargin, h - 1.25 * cm, self.section or "Contents")
        c.line(self.leftMargin, 1.45 * cm, w - self.rightMargin, 1.45 * cm)
        c.drawString(self.leftMargin, 1.0 * cm, f"Version {VERSION} · {DATE}")
        c.drawRightString(w - self.rightMargin, 1.0 * cm, f"{d.page}")
        c.restoreState()

    def afterFlowable(self, fl):
        if isinstance(fl, Paragraph) and fl.style.name in ("h1", "h2"):
            text = fl.getPlainText()
            level = 0 if fl.style.name == "h1" else 1
            key = f"h{id(fl)}"
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(text, key, level=level, closed=level > 0)
            self.notify("TOCEntry", (level, text, self.page, key))
            if level == 0:
                self.section = text


# ---------------------------------------------------------------------------------------------- content helpers
story: list = []
_sec = [0, 0]
_fig = [0]
_tab = [0]


def H1(t):
    _sec[0] += 1
    _sec[1] = 0
    if story:
        story.append(PageBreak())
    story.append(Paragraph(f"{_sec[0]}  {t}", S["h1"]))


def H2(t):
    _sec[1] += 1
    story.append(Paragraph(f"{_sec[0]}.{_sec[1]}  {t}", S["h2"]))


def H3(t):
    story.append(Paragraph(t, S["h3"]))


def P(t):
    story.append(Paragraph(t, S["body"]))


def B(*items):
    for t in items:
        story.append(Paragraph(t, S["bullet"], bulletText="•"))
    story.append(Spacer(1, 4))


def N(t):
    story.append(Paragraph(t, S["note"]))


def CODE(t):
    t = t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br/>").replace(" ", "&nbsp;")
    story.append(Paragraph(t, S["code"]))


def _take_headings():
    """Headings directly before a figure/table travel with it (keepWithNext does not hold across KeepTogether)."""
    heads = []
    while story and isinstance(story[-1], Paragraph) and story[-1].style.name in ("h1", "h2", "h3"):
        heads.insert(0, story.pop())
    return heads


def figure_drawing(name, width):
    """An SVG figure as a vector ReportLab drawing scaled to `width` points."""
    d = svg2rlg(str(FIG / f"{name}.svg"))
    k = width / d.width
    d.width, d.height = d.width * k, d.height * k
    d.scale(k, k)
    d.hAlign = "CENTER"
    return d


def FIGURE(name, caption, width=None):
    _fig[0] += 1
    img = figure_drawing(name, width or (A4[0] - 4.2 * cm))
    story.append(KeepTogether(_take_headings() + [img, Paragraph(f"<b>Figure {_fig[0]}.</b> {caption}", S["caption"])]))


def TABLE(rows, widths, caption=None, header=True):
    _tab[0] += 1
    data = []
    for i, r in enumerate(rows):
        st = S["cellb"] if (header and i == 0) else S["cell"]
        data.append([Paragraph(str(c), st) for c in r])
    total = A4[0] - 4.2 * cm
    t = Table(data, colWidths=[total * w for w in widths], repeatRows=1 if header else 0, hAlign="LEFT")
    style = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE),
             ("TOPPADDING", (0, 0), (-1, -1), 3.2), ("BOTTOMPADDING", (0, 0), (-1, -1), 3.2),
             ("LEFTPADDING", (0, 0), (-1, -1), 4.5), ("RIGHTPADDING", (0, 0), (-1, -1), 4.5)]
    if header:
        style += [("BACKGROUND", (0, 0), (-1, 0), ACCENT)]
        style += [("BACKGROUND", (0, i), (-1, i), SHADE) for i in range(2, len(rows), 2)]
    t.setStyle(TableStyle(style))
    items = [t]
    if caption:
        items.append(Paragraph(f"<b>Table {_tab[0]}.</b> {caption}", S["caption"]))
    else:
        items.append(Spacer(1, 8))
    heads = _take_headings()
    story.extend(heads[:0])
    story.append(KeepTogether(heads + items) if len(rows) < 18 else KeepTogether(heads + [t]))
    if caption and len(rows) >= 18:
        story.append(items[-1])


# ---------------------------------------------------------------------------------------------- cover + contents
def cover():
    story.append(Spacer(1, 0.6 * cm))
    story.append(Paragraph('<font color="white">LFT3-Earth digital backend</font>',
                           ParagraphStyle("ct", fontName="Sans-Bold", fontSize=26, leading=31, textColor=colors.white)))
    story.append(Paragraph('<font color="white">How the system works: board, packets, host, real-time runtime, '
                           'products, viewer and search</font>',
                           ParagraphStyle("cs", fontName="Sans", fontSize=12.5, leading=17, textColor=colors.white)))
    story.append(Spacer(1, 2.6 * cm))
    story.append(figure_drawing("f01_system", A4[0] - 4.2 * cm))
    story.append(Spacer(1, 0.8 * cm))
    meta = [["Scope", "One RFSoC 4x2 board (rfsoc0) streaming to one NVIDIA DGX Spark; the design scales to "
                      "5 boards × 2 links"],
            ["State", "Implemented and running live: 60.8 Gb/s per link, 2-hour soak without loss of the system's own "
                      "making"],
            ["Source", "lft3-earth-software repository; this document is generated by docs/system_overview/build.py"],
            ["Version", f"{VERSION} · {DATE}"]]
    t = Table([[Paragraph(a, S["cellb"]), Paragraph(b, S["cell"])] for a, b in meta],
              colWidths=[2.6 * cm, A4[0] - 4.2 * cm - 2.6 * cm], hAlign="LEFT")
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (0, -1), ACCENT), ("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE),
                           ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 4),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    story.append(t)
    story.append(NextPageTemplate("body"))
    story.append(PageBreak())
    story.append(Paragraph("Contents", S["title"]))
    toc = TableOfContents()
    toc.levelStyles = [S["toc1"], S["toc2"]]
    story.append(toc)


# ---------------------------------------------------------------------------------------------- the text
def content():
    H1("About this document")
    P("This document explains how the LFT3-Earth digital backend works, end to end. It starts from the radio signal "
      "at the RFSoC board's inputs and ends with spectra on disk, raw-voltage dumps, search hits and the browser "
      "viewer. It is written for engineers and astronomers who need to understand, operate, debug or extend the "
      "system. It covers what each part does, how data moves through memory, which algorithms are used, and why "
      "they were chosen. For day-to-day commands see <i>docs/user_guide.md</i>. For the implementation detail behind "
      "each design decision see <i>docs/runtime.md</i>.")
    P("All numbers in this document were measured on the system itself (DGX Spark, GB10) on 12–13 November 2026, "
      "unless stated otherwise. File and command names are set in " + mono("monospace") + ". In the diagrams, colour "
      "shows where something runs: orange is the board and FPGA, grey the network, blue the host CPU, green the GPU "
      "and its memory, purple storage, and teal the reader processes.")
    TABLE([["Quantity", "Value"],
           ["Sampling", "5 GSPS real per ADC input → 1250 MSPS complex after down-conversion"],
           ["Coarse channels (FPGA)", "8192-point, 8-tap polyphase filterbank; 6144 channels of 152.587890625 kHz "
                                      "kept = 937.5 MHz per input"],
           ["Inputs / beams", "4 ADC inputs per board = 2 dual-polarisation beams per link"],
           ["Link rate", "915 527 packets/s of 8298 B = 60.8 Gb/s on the wire per link (one per board today, "
                         "two planned)"],
           ["Fine channels (GPU)", "128-point FFT per coarse channel → 786 432 channels of 1.192 kHz per beam, "
                                   "integrated over 0.99992 s"],
           ["Time base", "t counts spectra (6.5536 µs each) since the last board sync; blocks of 1024 spectra "
                         "(6.71 ms)"],
           ["Products", "fine and coarse FBH5 spectra, GUPPI RAW voltage dumps on trigger, hyperseti hits"],
           ["Capacity (one Spark)", "2 links (1 board) with headroom; compute supports up to 5 links; Hz-resolution "
                                    "≈ 1 link"]],
          [0.28, 0.72], "Key numbers.")

    # ------------------------------------------------------------------------------------------------ 2
    H1("The system at a glance")
    FIGURE("f01_system", "The whole system. Each RFSoC board digitises four inputs, channelises them into 152.6 kHz "
                         "channels and streams them over 100 GbE to the DGX Spark. There, one capture process per link "
                         "keeps the last ~2 s of voltages in a GPU ring, and independent readers turn them into "
                         "spectra, raw-voltage dumps and, later, live searches. Control and monitoring use a "
                         "separate 1 GbE network.")
    P("The system has three layers, kept deliberately independent:")
    B("<b>The board</b> (FPGA firmware, built elsewhere) does the hard real-time work at 5 GSPS: sampling, "
      "down-conversion, the coarse polyphase filterbank and packetising. It is controlled and monitored from the host "
      "over katcp, by " + mono("lft3-board") + ".",
      "<b>The real-time runtime</b> on the Spark (" + mono("pipeline/") + ", C++/CUDA) receives the stream at line "
      "rate. Its one rule: <i>the receiver never waits for anything downstream</i>. It writes every 6.71 ms block of "
      "voltages into a ring in GPU memory. Any number of reader processes read the ring at their own pace: the "
      "spectrometer, the dump process, and in future the searches.",
      "<b>Products and tools</b> work on files: FBH5 spectra, GUPPI RAW dumps, search hits, the browser viewer "
      + mono("lft3-view") + ", plots, and the " + mono("lft3-pipeline") + " orchestrator that starts, watches and "
      "stops everything.")
    P("Configuration lives in git and is the single source of truth: " + mono("config/station.yaml") + " lists the "
      "boards and the data directory. " + mono("config/boards/&lt;board&gt;.yaml") + " describes each board: "
      "bitstream, clocks, NCO, input mapping, network addresses, the host-side links and their CPU cores, and timing.")

    # ------------------------------------------------------------------------------------------------ 3
    H1("The RFSoC board")
    H2("Signal chain")
    FIGURE("f02_signal_chain", "Signal chain inside the FPGA for each of the four inputs.", width=A4[0] - 4.2 * cm)
    P("Each RFSoC 4x2 board has four ADC inputs sampled at 5 GSPS. The digital down-converter mixes each input with "
      "an NCO at −2031.25 MHz and decimates by 4, giving 1250 MSPS complex samples centred on 2031.25 MHz. An "
      "8192-point, 8-tap polyphase filterbank (PFB) splits that 1.25 GHz into 152.587890625 kHz channels. 6144 of "
      "them, 937.5 MHz of the band, are kept. Each channel sample is requantised to 8-bit I and 8-bit Q. The bit "
      "selection " + mono("ch_lsb") + " sets the level: about 11 counts RMS on noise with the current setting, and a "
      "strong tone at −60 dBm clips.")
    P("One spectrum, all 6144 channels of all four inputs, is produced every 6.5536 µs. The firmware numbers them with "
      "a 58-bit counter <b>t</b> that starts at 0 on every <i>sync</i>. The packetizer sends each spectrum as six UDP "
      "packets of 1024 channels each. In parallel, a DDR4 ring buffer keeps the last 0.86 s of raw 4-bit samples, and "
      "a trigger reads it out over the same 100 GbE link (UDP port 40000, ~10 Gb/s for ~3.5 s).")

    H2("Bringing a board up")
    FIGURE("f15_bringup", "The bring-up sequence performed by lft3-board start (≈ 10 s per board).")
    P(mono("lft3-board start") + " follows the firmware author's bring-up notebook step by step:")
    B("program the bitstream and set the destination MAC (entry 10 of the 100 GbE core's ARP table, which defaults "
      "to broadcast and is lost on every reprogram);",
      "initialise the RF data converters and the LMK/LMX clock chips, then multi-tile synchronisation (MTS);",
      "select SYSREF as the mixer event source, then program the NCO. The packet header carries the NCO as a signed "
      "whole number of MHz, so the exact value (−2031.25 MHz) comes from the configuration, not from the packets;",
      "set the requantisers, DDR size and read delay, packet mask and board id (" + mono("fid") + "), and the expected "
      "PPS period;",
      "issue a <b>sync</b>: t restarts at 0 and the header fields take effect. Some settings (packet mask, fid, "
      "pfb_lsb) only apply after a sync.")
    P("In a station, every board is initialised in parallel, each under its own lock. The sync writes are then "
      "released together from a barrier, so all boards share a time base to within milliseconds. A PPS-aligned sync "
      "in firmware (requested) would make it exact.")

    H2("Monitoring and control")
    P("Control and monitoring are separate layers. " + mono("lft3.board.control") + " only writes, under a per-board "
      "lock. " + mono("lft3.board.telemetry") + " only reads, so monitoring can run continuously while someone "
      "operates the board. Both go through casperfpga (katcp) on the 1 GbE management network. Board sensors (rails, "
      "temperatures) are read over SSH from the board's sysfs.")
    TABLE([["Health check", "Rule", "If broken"],
           ["tx_packet_rate", "within 0.1 % of 915 527 /s × (packets enabled)/6", "FAIL"],
           ["packetizer FIFO full / blocked words", "rate 0 (no board-side loss or back-pressure)", "FAIL"],
           ["header NCO, fid, packet mask", "equal to the configuration", "FAIL"],
           ["destination MAC", "equal to the configuration (broadcast = misconfigured)", "FAIL"],
           ["PPS, sample clock vs PPS", "edges present; clock within 1 ppm (informational while the PPS is "
                                        "temporary)", "WARN / INFO"],
           ["ch_lsb, expected clock period", "equal to the configuration", "WARN"]],
          [0.3, 0.5, 0.2], "Board health checks (lft3-board status; exit code 2 on any FAIL, usable from cron).")
    P(mono("lft3-board trigger") + " fires a DDR dump and re-arms the trigger. " + mono("lft3-board snapshot") +
      " reads the firmware's FFT and ADC snapshot memories for diagnostics. " + mono("lft3-board monitor") + " exports "
      "everything as Prometheus metrics and JSON lines.")

    # ------------------------------------------------------------------------------------------------ 4
    H1("Packets on the wire")
    H2("The channel packet")
    FIGURE("f03_packet", "Channel packet layout: a 64-byte big-endian header and 1024 channels × 4 inputs × (I, Q).")
    P("Every channel packet is a UDP datagram of 8256 bytes to port 60000: a 64-byte header and an 8192-byte "
      "payload. The header is eight big-endian 64-bit words. Word 0 holds the spectrum counter t (58 bits), the "
      "packet index b within the spectrum (bindex, 0–5) and the board id fid (3 bits). Words 1–2 hold the four "
      "NCO values (signed int32, whole MHz), and word 3 the four PFB bit-selections. The payload holds 1024 "
      "consecutive channels, k = 1024·b + i. Each channel is 8 bytes: input 0 I, input 0 Q, input 1 I, … "
      "input 3 Q, as signed bytes. Inputs 0/1 form beam 0 (X, Y) and inputs 2/3 beam 1 (assumed from the ADC map, "
      "to be verified with an injected tone).")
    TABLE([["", "Per packet", "Per spectrum (6 packets)", "Per second, one link"],
           ["Packets", "1", "6", "915 527"],
           ["UDP payload", "8256 B", "49.5 kB", "7.56 GB (60.5 Gb/s)"],
           ["Samples", "1024 ch × 4 inputs", "6144 ch × 4 inputs", "3.75 × 10<super>9</super> complex samples"],
           ["On the wire (with Ethernet/IP/UDP)", "8298 B", "49.8 kB", "60.95 Gb/s"]],
          [0.28, 0.2, 0.24, 0.28], "Channel stream rates (MTU 9000 needed).")

    H2("Channels and sky frequency")
    FIGURE("f04_freq_map", "How PFB bins map to packet channels and sky frequency.")
    P("The PFB produces 8192 bins. The packetizer sends bins 5120–8191 (negative frequency offsets) as packets b = "
      "0–2 and bins 0–3071 (positive offsets) as packets b = 3–5. So the channel index k increases with frequency "
      "across the whole band:")
    P("<para alignment='center'><font name='Serif-Italic'>RF(k) = −NCO + (k − 3072) × 152.587890625 kHz</font></para>")
    P("With NCO = −2031.25 MHz the band runs from 1562.50 MHz (k = 0) to 2499.85 MHz (k = 6143). Two checks confirm "
      "the sign and the axis: the board's own clock spurs land on exact multiples of 125 MHz (1750, 1875, 2000, 2125, "
      "2250, 2375 MHz), and an 1800 MHz test tone appears at 1800 MHz with no mirror image.")

    H2("DDR dump packets")
    P("Triggered DDR dumps reuse the same packet size on UDP port 40000. Each payload holds 2048 time samples of the "
      "four inputs at 4 bits I + 4 bits Q. A 4 GiB dump is 2<super>19</super> packets. The host receives them on a "
      "UDP socket for now; the capture process could also take them on a second DAQIRI queue.")

    # ------------------------------------------------------------------------------------------------ 5
    H1("The host: NVIDIA DGX Spark")
    FIGURE("f05_spark", "The DGX Spark as the runtime uses it: CPU cores, GPU, unified memory and I/O.")
    P("The DGX Spark is built around the GB10 superchip: 20 Arm cores (10 Cortex-X925 performance cores and 10 "
      "Cortex-A725 efficiency cores) and a Blackwell GPU (compute capability 12.1, 48 SMs). They share <b>128 GB of "
      "LPDDR5x unified memory</b> with a peak bandwidth of 273 GB/s. That single fact shapes the whole design:")
    B("There is no PCIe copy between CPU and GPU memory. The GPU can read the network card's receive buffers in "
      "place, so the Spark needs no GPUDirect RDMA (and doesn't have it).",
      "Every byte moved by the NIC, the CPU or the GPU competes for the same 273 GB/s. Memory bandwidth, not "
      "arithmetic, is the binding constraint (§9).",
      "How memory is allocated still matters. GPU kernels reach 237 GB/s on " + mono("cudaMalloc") + " or CUDA VMM "
      "memory, but only 172 GB/s on host memory registered with " + mono("cudaHostRegister") + ".")
    P("The ConnectX-7 network card has two QSFP ports. Each appears as two network devices, one per PCIe Gen5 x4 "
      "half. A 100 GbE cable from a board lands on one half, " + mono("enp1s0f0np0") + " (PCI 0000:01:00.0) for link0. "
      "Packets are received with NVIDIA's DAQIRI library using raw ibverbs: a hardware flow rule steers the board's "
      "UDP stream to one receive queue, bypassing the kernel network stack, which only manages ~0.4 Mpps.")
    P("CPU cores are assigned per link. For link0: core 5 runs DAQIRI's master thread, core 6 its receive worker "
      "(busy-polling at 100 %), and core 7 the capture thread. The readers run on other cores (8, 9 and the A725 "
      "cores). A second link would use 15–17.")

    # ------------------------------------------------------------------------------------------------ 6
    H1("The real-time runtime")
    H2("Architecture")
    P("The runtime is a set of independent processes around one shared structure per link: the <b>ring</b>. One "
      "writer, " + mono("lft3-capture") + ", fills it at line rate. Readers attach to it, read it at their own pace, "
      "and can come and go at any time. The writer never waits for a reader. A reader that falls more than the ring's "
      "length behind loses blocks and counts them as overruns, so the capture stays lossless whatever the readers "
      "do. Each process runs in its own container, so a crashing or slow reader cannot affect the capture.")
    FIGURE("f14_processes", "Processes and containers for one link. lft3-pipeline starts the capture, waits for its "
                            "ring to appear, then starts the readers; on stop it reverses the order.")
    N("<b>Why not a dataflow framework?</b> The first implementation was a CyberEther plugin. It ran at line rate, but "
      "CyberEther runs every block in lock-step on one scheduler thread, so any slow consumer stalls the receiver, "
      "which then drops packets. Holoscan 5.0 (too new: a rewrite with Beta APIs) and hashpipe (semaphore handshakes "
      "with one consumer per buffer) were evaluated. The ring design keeps the property that matters: the readers can "
      "never hold up the receiver. The decision record is in docs/runtime.md §2.")

    H2("A packet's journey through memory")
    FIGURE("f06_data_movement", "Where one stream's bytes go. Every arrow is real memory traffic on the shared "
                                "LPDDR5x bus.")
    P("Data enters through the NIC's DMA engine. DAQIRI splits each frame: the first 106 bytes (Ethernet, IP and UDP "
      "headers plus the 64-byte LFT3 header) land in a <i>header segment</i> buffer that the CPU reads, and the "
      "8192-byte payload lands in a <i>payload segment</i> buffer. Both are pinned host memory (64 Ki buffers of each, "
      "0.5 GB). The capture thread reads only the headers, about 97 MB/s. It never touches the payloads.")
    P("For each packet the CPU decides where the payload belongs: which block (from t), which time row and which "
      "1024-channel group (from b). It appends a small <i>ScatterJob</i> record (payload pointer, block pointer, t, "
      "channel offset) to a pinned job list. Once per burst (up to 1024 packets) a GPU <b>scatter kernel</b> reads all "
      "the payloads of the burst straight from DAQIRI's buffers and writes them into the right places of the ring "
      "block. That is the only time the payload is copied: 7.5 GB/s read and 7.5 GB/s written per link.")
    P("Readers then read each block once from GPU memory. The spectrometer reads 7.5 GB/s and writes 6.3 MB per "
      "second of spectra. The dump process copies a window to pinned host memory only when triggered. Altogether one "
      "link costs about 30 GB/s of memory traffic before any search runs.")

    H2("Lifetime of a packet and of a block")
    FIGURE("f07_packet_lifetime", "Lifetime of one packet (top lanes) and of the block it ends up in (bottom lanes).")
    P("A packet's payload lives in DAQIRI's receive buffer for only 1–2 ms:")
    B("the NIC writes it; the DAQIRI worker hands it out as part of a <i>burst</i> once 1024 packets have arrived "
      "(about 1.1 ms at line rate) or after a 1 ms timeout;",
      "the capture thread parses the burst's headers, places every packet with the assembler, sorts the jobs and "
      "launches the scatter kernel (about 83 µs);",
      "a CUDA event signals that the kernel has finished, and only then is the burst returned to DAQIRI for reuse. "
      "The CPU never waits for the GPU: it polls events between bursts.")
    P("The block lives much longer. It is <i>open</i> while its 6144 packets arrive (6.71 ms), <i>sealed</i> once "
      "complete or stale (missing packets are zero-filled by the same kernel), and <i>published</i> to readers once "
      "its last kernel has finished. It then stays readable in the ring for ~2 s, until the writer reuses its slot. "
      "The spectrometer normally reads it within a millisecond of publication.")

    H2("Assembling blocks")
    FIGURE("f08_assembler", "The assembler keeps up to four blocks open and seals them strictly in time order.")
    P("The assembler (" + mono("pipeline/src/capture/assembler.cc") + ") is plain C++, independent of DAQIRI and CUDA, "
      "and unit-tested. A packet (t, b) belongs to block t / 1024, slot (t mod 1024) × 6 + b. Its rules:")
    B("up to 4 blocks may be open at once, to tolerate slightly out-of-order arrival;",
      "the oldest open block is sealed when complete, or when the newest block seen is more than 2 blocks ahead "
      "(stale). Missing slots are then listed for zero-fill, and counted;",
      "blocks for which no packet arrived at all are counted as <i>skipped</i>;",
      "duplicates and packets for already-sealed blocks are rejected (counted as duplicate / late);",
      "if t jumps back by more than 1024 blocks, the board was re-synced: open blocks are flushed and the time base "
      "starts over;",
      "capture starts only at a block boundary (t multiple of 1024, first packet), so the first block is complete.")
    P("Each new block gets the <b>next ring slot in allocation order</b>, not a slot derived from its block number. "
      "After a re-sync, block numbers restart and would otherwise collide with blocks still being sealed.")

    H2("The scatter kernel")
    FIGURE("f09_scatter", "The scatter kernel transposes 32 packets × 32 channels through shared memory, so that both "
                          "the reads and the writes are coalesced.")
    P("The scatter turns packet order (one packet = one time sample of 1024 channels) into ring order (one row = 1024 "
      "time samples of one channel). Done naively, each thread would write 4 bytes to a different row, which wastes "
      "most of each memory transaction. The kernel instead loads a tile of 32 packets × 32 channels into shared memory "
      "(each warp reads 256 contiguous bytes of one packet) and writes it out transposed. Each warp then writes 32 "
      "consecutive time samples of one channel row, a contiguous 128-byte run. For the tile's packets to be "
      "consecutive in time, the CPU first sorts each burst's jobs by channel group (a counting sort, negligible "
      "cost). Missing packets go through the same kernel with a null source and are written as zeros. The tiled "
      "kernel measured 1.4× faster than the naive one and runs at about 200 GB/s, i.e. memory-bound.")

    H2("The ring and its protocol")
    FIGURE("f10_ring", "The ring: a small control block in POSIX shared memory, and the voltage blocks in a CUDA VMM "
                       "allocation shared with readers through a file descriptor.")
    P("Each link has one ring with two parts:")
    B("<b>Control plane</b>: " + mono("/dev/shm/lft3-&lt;stream&gt;") + ", 560 kB with a fixed binary layout. It "
      "holds the layout, observation id, configuration hash and software version; the publication head and a "
      "futex word; capture statistics; up to 16 reader entries (name, cursor, lag, overruns, heartbeat); the "
      "seq→slot table; and per-slot metadata (block number, t0, missing packets, NCO, estimated UNIX time of t = 0). "
      "The C++ layout is fixed by static assertions and mirrored byte for byte in Python (" + mono("lft3.ring") +
      "), which reads it for status and metrics.",
      "<b>Data plane</b>: the voltage blocks, S slots × 50.3 MB (default 299 slots = 15 GB = 2.0 s). They live in "
      "one CUDA VMM allocation (" + mono("cuMemCreate") + ", 2 MiB pages), exported as a POSIX file descriptor and "
      "handed to readers over the unix socket " + mono("/dev/shm/lft3-&lt;stream&gt;.sock") + ". Each reader maps "
      "it <i>read-only</i> at full device bandwidth, so a buggy reader cannot corrupt the data.")
    P("Each block is laid out as [beam][channel][1024 t][pol], one complex int8 per element. This is GUPPI RAW order "
      "per beam: a fine FFT reads contiguous samples, and a dump writes a block as it is. An earlier layout kept one "
      "row per channel across the whole ring, so that any FFT length would read contiguously. It was measured and "
      "rejected: the scatter's writes to 12 288 far-apart rows thrashed the GPU's address translation, and throughput "
      "fell to 6 GB/s, below the required 7.5 GB/s.")
    H3("Publication and reading (a seqlock)")
    P("The writer marks a slot invalid <i>before</i> any GPU write into it. It publishes the block only after the "
      "kernels have completed: metadata, then the slot's sequence number, then the seq→slot entry, then the head, "
      "then a futex wake-up for waiting readers. A reader waits for the head to pass the block it wants, checks the "
      "slot's sequence number, does its GPU work, and checks the sequence number again afterwards. If it changed, the "
      "writer reused the slot during the read: the result is discarded and counted as an overrun. Readers that fall "
      "more than (S − 8) blocks behind jump ahead and count what they skipped.")
    P("For synthetic and file sources only, readers can be <i>lossless</i>. The writer then waits until every "
      "lossless reader with a live heartbeat has moved at least 8 slots beyond a slot before reusing it. Tests and "
      "benchmarks use this to process every block. The 8-slot margin came out of a parity test, which caught a bug "
      "when the margin was 0.")

    H2("The fine spectrometer")
    FIGURE("f11_finespec", "Fine spectrometer: per block, beam and coarse channel, eight 128-point FFTs are detected "
                           "and summed; 149 blocks make one 1-second spectrum.")
    P("For every block, " + mono("lft3-spectrometer") + " runs one fused kernel over all (coarse channel, beam) pairs. "
      "Each row of 1024 dual-polarisation samples is cut into eight segments of 128. Each segment is Fourier "
      "transformed for both polarisations, |X|² + |Y|² (Stokes I) is formed, and the eight results are summed in "
      "registers. The sum is added to an accumulator in GPU memory, fftshifted so that frequency increases across "
      "the 128 fine bins. The voltages are read exactly once, and nothing larger than the accumulator is written. "
      "That fusion is what makes it fast: separate FFT, detect and integrate passes (as in CyberEther's own blocks) "
      "move about 6× more memory and ran at only 0.59× real time.")
    H3("The warp FFT")
    FIGURE("f12_warp_fft", "One warp computes a 128-point FFT entirely in registers: a 4-point DFT per lane, a "
                           "twiddle, and a 32-point FFT across lanes with shuffles.")
    P("The first kernel did the FFT in shared memory with 64 threads per (channel, beam). Profiling showed it limited "
      "by its 56 barriers per block of data, not by arithmetic. A twiddle table was tried and was not faster. The "
      "current kernel gives each FFT to one warp of 32 threads and never synchronises. For N = 128 = 4 × 32, lane j "
      "loads the four samples j, j+32, j+64 and j+96 (each warp load is 128 contiguous bytes). Each lane does a "
      "4-point DFT in registers and multiplies by the twiddle W<sub>128</sub><super>jq</super>. Then, for each of "
      "the four outputs, the lanes perform a 32-point FFT among themselves: five radix-2 stages in which each lane "
      "exchanges values with lane j xor h using " + mono("__shfl_xor_sync") + ". Lane l ends up holding bins q + "
      "4·bitrev(l), and detection and summation happen in registers. The same scheme covers 64 and 256 points. The "
      "kernel takes 0.30 ms per 6.71 ms block for both beams (previously 0.50 ms), is close to memory-bound, and "
      "matches a reference DFT to better than 10<super>−4</super>.")
    H3("Time grid, missing data and the coarse product")
    B("<b>Integration windows are aligned to absolute block numbers</b>: window w covers blocks 149w … 149w + 148 "
      "(152 576 spectra = 0.99992 s). All readers and all links therefore share one time grid. A reader that starts "
      "mid-window waits for the next boundary. " + mono("/lft3_timestamp") + " records each spectrum's first t.",
      "<b>Missing data</b> is recorded per spectrum in " + mono("/lft3_missing_packets") + ": zero-filled packets, "
      "plus 6144 for every block the capture skipped or the reader lost. Spectra are written regardless, and the count "
      "is what a later mask will use. A gap of whole windows, or a board re-sync, starts new files.",
      "<b>The coarse quick-look product costs nothing extra</b>: by Parseval's theorem, the power in a board channel "
      "equals the sum of its 128 fine bins divided by 128. The writer thread computes it on the CPU from each fine "
      "spectrum. It agrees with a direct computation from the voltages to 2.6 × 10<super>−6</super>. A separate "
      "coarse-mode reader is only needed for a faster cadence (e.g. 0.1 s).",
      "<b>Frequency axis</b>: fch1 and foff come from the exact NCO in the configuration; the header's whole-MHz "
      "value is only a fallback. <b>Time</b>: the UNIX time of t = 0 is estimated from the first packet's arrival "
      "(~ms accuracy) until the firmware provides a PPS-aligned sync.")

    H2("Triggered raw-voltage dumps")
    FIGURE("f13_dump", "A dump copies the window around the trigger time out of the ring: the part before the "
                       "trigger is already there, the rest is copied as it arrives.")
    P(mono("lft3-pipeline trigger") + " sends a command to each link's " + mono("lft3-dump") + " over a unix socket: "
      "a UNIX time plus seconds before and after (default 0.5 + 0.5). The dump process converts the time to block "
      "numbers using the capture's time base. It copies every block of the window that is still in the ring, and the "
      "later ones as they are published, into a pinned host buffer, checking each with the seqlock. It then writes the "
      "window to NVMe as GUPPI RAW, one file per beam. Each GUPPI block is one beam of a ring block, behind a header "
      "of 80-character cards (OBSNCHAN, NPOL = 4, NBITS = 8, TBIN, OBSFREQ, OBSBW, PKTIDX = t0, …). The window has to "
      "be copied first because NVMe cannot absorb 7.5 GB/s, and the ring only holds ~2 s. Live, a 1 s window "
      "(7.5 GB) was collected in 0.5–0.7 s and written in 2.8 s.")

    H2("Products")
    TABLE([["Product", "Where", "Content"],
           ["Fine spectra", "&lt;data&gt;/spectra/&lt;board&gt;/&lt;board&gt;_&lt;UTC&gt;_beam&lt;N&gt;.h5",
            "FBH5: /data [time, 1, 786 432] float32, 1.192 kHz × 0.99992 s; /lft3_timestamp, /lft3_missing_packets; "
            "SIGPROC attributes (fch1, foff, tsamp, tstart, nfpc); lft3_* metadata (NCO, obs id, stream, config hash, "
            "software). Chunks of 65 536 channels, shuffle + deflate. ~16 GB/hour per link."],
           ["Coarse spectra", "…/spectra/&lt;board&gt;/quicklook/", "same layout, 6144 channels of 152.6 kHz (nfpc = 1)"],
           ["Latest snapshot", "&lt;run&gt;/latest/&lt;board&gt;_latest.{json,f32}",
            "the newest coarse spectrum, float32 [beam][6144], with axes in JSON (for dashboards)"],
           ["Raw voltages", "&lt;data&gt;/dumps/&lt;board&gt;/…_beam&lt;N&gt;.0000.raw",
            "GUPPI RAW, 6144 channels × 2 pols × 8-bit complex, 6.5536 µs samples; sidecar JSON"],
           ["Search hits", "&lt;data&gt;/hits/&lt;board&gt;/&lt;file&gt;.csv",
            "frequency, drift rate, S/N, channel, RFI label, obs id, stream"],
           ["Run records", "&lt;data&gt;/runs/&lt;UTC&gt;_&lt;board&gt;_&lt;source&gt;/",
            "one log per process, plan.txt, status.jsonl, nic-&lt;link&gt;.csv"]],
          [0.16, 0.34, 0.50], "Products. Beam numbers N include the link's beam offset (link0: 0, 1; link1: 2, 3).")
    P("FBH5 files open with blimpy, turboSETI and hyperseti as well as the project's own tools. GUPPI RAW opens with "
      "rawspec and blimpy. Files rotate every 3600 spectra, and " + mono("/lft3_timestamp") + " continues exactly "
      "across rotations, so tools can chain them into one run.")

    # ------------------------------------------------------------------------------------------------ 7
    H1("The viewer")
    FIGURE("f16_viewer", "lft3-view: all data work happens in C++ on the Spark; the browser receives only a "
                         "screen-sized tile.")
    P("A one-hour full-band file holds 3600 spectra × 786 432 channels, about 11 GB before compression. "
      + mono("lft3-view") + " is a C++ HTTP server on the Spark with a single-page browser client. The browser never "
      "receives raw data, only the tile it is about to draw:")
    B("the <b>run catalog</b> chains rotated files of one beam into one timeline using " + mono("lft3_timestamp") +
      "; a re-sync starts a new run;",
      "the HDF5 library is used only for metadata and the chunk index. The spectra themselves are read with "
      "parallel " + mono("pread") + " on all cores, and only the 65 536-channel chunks a view covers are inflated and "
      "unshuffled;",
      "the reduction to screen pixels (max or mean, optional coarse-channel flattening) is vectorised and runs in the "
      "same pass;",
      "tiles are sent as 16-bit dB planes, gzip-compressed, 2.2–2.7× smaller than float32.")
    P("Measured on cold files: a whole-file overview of a one-hour file takes 1.7 s (6.8 GB/s from NVMe); zooms take "
      "under 70 ms. h5py manages about 0.5 GB/s on the same files. The page offers pan and zoom, per-channel "
      "normalisation, spectrum and time-series strips, a drift-rate measurement tool, and marks for trigger times "
      "(from " + mono("triggers.log") + ") and known RFI (from " + mono("config/known_rfi.json") + "). In 'follow "
      "live' mode it also shows files that are still being written.")

    # ------------------------------------------------------------------------------------------------ 8
    H1("Searching for narrowband signals")
    P(mono("lft3-search") + " runs hyperseti, Breakthrough Listen's GPU drift-rate search, on finished fine FBH5 files. "
      "It runs in a separate Python environment (" + mono("/opt/lft3arth/search_venv") + ": Python 3.12, CuPy for "
      "CUDA 13, hyperseti pinned to a tested commit). Files that are still being written are never opened: a file "
      "counts as finished once the next file of the same beam exists or it has been idle for two minutes. The "
      "defaults are adapted to 1.19 kHz channels:")
    B("no spectral-kurtosis flagging (hyperseti would otherwise flag 100 % of the data, because its assumed "
      "accumulation count does not include the sum of two polarisations);",
      "drift rates up to 200 Hz/s: at 10 Hz/s a signal would not cross even one 1.19 kHz channel within a file, and "
      "there would be no drift trials;",
      "S/N threshold 20. Hits within two fine channels of a 125 MHz board spur, or in the Spark's Wi-Fi channel 1, "
      "are labelled as RFI but kept.")
    P("On live data the strongest hits are exactly the board's spur comb (1750, 1875, 2125, 2250 and 2375 MHz), "
      "correctly labelled. A 70-second file of one beam takes about 2.5 s. A live search would read spectra from a "
      "ring (§6), using the same mechanism the Python " + mono("lft3.ring") + " module already demonstrates with CuPy.")
    N("<b>Important.</b> A search running on the Spark during a live capture once made the NIC drop 277 packets. The "
      "search loaded the shared memory bus enough that the NIC's own buffer overflowed (" + mono("rx_discards_phy") +
      "); the runtime itself was unaffected. Heavy GPU or memory jobs on the capture host must be throttled or run "
      "elsewhere (§9).")

    # ------------------------------------------------------------------------------------------------ 9
    H1("Operating the system")
    CODE("lft3-board start                         # program, initialise and sync every board (~10 s)\n"
         "lft3-board status                        # health checks\n"
         "lft3-pipeline run                        # all links: capture + spectrometer + dump, until Ctrl-C\n"
         "lft3-pipeline run --source synthetic     # without a board: noise + a tone\n"
         "lft3-pipeline status                     # every ring on this host: rate, loss, readers\n"
         "lft3-pipeline trigger --label test       # raw-voltage dump of every link\n"
         "lft3-pipeline stop                       # readers first (files closed), then capture\n"
         "/opt/lft3arth/search_venv/bin/lft3-search --watch     # hyperseti on finished files\n"
         "tools/viewer/build/lft3-view             # browser viewer on port 8687")
    P("A healthy run prints, every 10 s per link: 60.8 Gb/s, no increase in partial or skipped blocks or missing "
      "packets, and readers at lag 0 with no overruns. Every run gets a directory with one log per process, the exact "
      "container commands, a JSON time series of all counters, and the NIC's drop and pause counters. Those NIC "
      "counters separate loss on the host (" + mono("rx_out_of_buffer") + ", " + mono("rx_discards_phy") + ") from "
      "loss before the host (counters flat: the packets never arrived). With " + mono("--metrics-port") + " the same "
      "counters are served to Prometheus.")
    P("Containers: the capture container gets network access with only the capabilities DAQIRI needs (" +
      mono("NET_RAW") + ", " + mono("IPC_LOCK") + " and the RDMA devices; no privileged container). Readers run as the "
      "invoking user with no network at all, and talk to the capture only through " + mono("/dev/shm") + " and the "
      "GPU. All binaries are built in the " + mono("lft3-runtime") + " image (CUDA 13, rdma-core, meson). No step "
      "needs root on the host.")

    # ------------------------------------------------------------------------------------------------ 10
    H1("Performance and capacity")
    H2("Cost of one link")
    FIGURE("c1_kernels", "GPU time per 6.71 ms block of one link (two beams), measured with Nsight Systems on an "
                         "otherwise idle GPU.", width=13.5 * cm)
    P("On its own, one link uses well under a tenth of the GPU: the scatter takes 0.50 ms and the fine spectrometer "
      "0.30 ms per 6.71 ms block. On the CPU it uses one core busy-polling (DAQIRI's worker), about 4 % of the capture "
      "thread's core, and about 18 % of a core for the spectrometer. Memory is the scarcer resource. By default each "
      "link holds about 23 GB: the 15 GB ring, the 7.5 GB pinned dump window and 0.5 GB of DAQIRI buffers.")
    FIGURE("c4_memory", "Estimated memory traffic per link and for several links, against the usable bandwidth of the "
                        "shared LPDDR5x.", width=13.5 * cm)
    H2("How many links fit on one Spark")
    FIGURE("c2_capacity", "Capacity test: N simultaneous real-time synthetic streams, each with a recording "
                          "spectrometer, plus N × 7.6 GB/s of memory writes standing in for the NICs' DMA.",
           width=13.5 * cm)
    P("The capacity benchmark (" + mono("pipeline/bench/capacity") + ") runs N links at once. It counts as a pass "
      "when every capture keeps real time, no reader overruns, and reader lag stays under a quarter of the ring. Five "
      "links pass (readers need at most 2.5 ms of each 6.71 ms block). Six do not: the spectrometers fall behind. "
      "Even then, every capture kept real time and lost nothing, which is the isolation working as designed. GPU "
      "utilisation grows faster than linearly because all streams compete for the same memory bus, which slows every "
      "kernel.")
    P("Two limits lie outside this test. <b>The network</b>: each of the Spark's two QSFP ports takes one 100 GbE "
      "cable, so two board links per Spark is what is known to work; four would need a switch and has not been tested. "
      "<b>The memory bus seen by the NIC</b>: the synthetic test has no NIC, but live, a search on the same machine "
      "made the NIC drop packets. Capacity planning has to leave memory-bus headroom, not just GPU headroom.")
    TABLE([["Configuration", "Links per Spark", "Sparks for 5 boards", "Comment"],
           ["One board per Spark (recommended)", "2", "5", "known-good network; ~46 % GPU, room for throttled "
                                                           "searches"],
           ["Two boards per Spark", "4", "3", "fits the compute (87 % GPU) but needs 4 links via a switch "
                                              "(untested), and leaves little for searches"],
           ["Hz resolution (Nf = 2<super>17</super>, ~1.2 Hz)", "≈ 1", "≈ 10", "the FFT alone is 3.1× real time per "
                                                                      "beam on GB10; needs subbands or more GPUs"]],
          [0.3, 0.15, 0.17, 0.38], "Capacity answer for the full system of 5 boards × 2 links (10 links, 20 beams).")

    H2("Comparison with rawspec")
    FIGURE("c3_rawspec", "rawspec, Breakthrough Listen's standard fine channeliser, against our spectrometer, per "
                         "block of one beam (log scale).", width=13.5 * cm)
    P("Breakthrough Listen and the related SETI backends also form fine channels with a plain FFT after the FPGA's "
      "polyphase filterbank, as we do. rawspec is their standard tool: cuFFT with load and store callbacks, built for "
      "very long FFTs. Built for this GPU and run on a GUPPI RAW dump from our system, it produces the same spectra "
      "as ours after its 1/127 sample scaling (median relative difference 7.6 × 10<super>−8</super>). At 128 points "
      "our fused kernel is about 60× faster. At 2<super>17</super> points rawspec cannot keep up with even one beam "
      "on GB10. A plain cuFFT at that length runs at 3.1× real time per beam before detection, which sets the cost "
      "of a future Hz-resolution mode.")

    # ------------------------------------------------------------------------------------------------ 11
    H1("Design decisions")
    TABLE([["Decision", "Evidence"],
           ["Own ring runtime instead of CyberEther", "CyberEther's lock-step scheduler lets a slow consumer stall the "
                                                     "receiver; its DSP blocks ran at 0.59× real time"],
           ["Not Holoscan 5.0 (for now)", "decoupling works in a test, but 5.0 is a fresh rewrite with Beta tensor/queue "
                                          "APIs and DAQIRI still on 4.x"],
           ["Not hashpipe; keep its conventions", "semaphore handshake = back-pressure, one consumer per buffer; GUPPI RAW "
                                                  "and observation keys kept"],
           ["Ring of 50 MB blocks", "time-contiguous rows: scatter 6 GB/s (TLB); blocks: 83 GB/s at any size, long-FFT "
                                    "gather 232 GB/s"],
           ["CUDA VMM memory shared by fd", "registered shm reads 172 GB/s vs 237 GB/s; VMM import gives full "
                                            "bandwidth in other processes"],
           ["Writer never waits; seqlock + overrun counts", "6-stream overload test: captures stayed lossless while "
                                                           "readers overran"],
           ["Backpressure margin of 8 slots (tests only)", "parity run found lossless readers skipping blocks with "
                                                           "margin 0"],
           ["Quick-look derived from fine spectra", "separate reader cost 0.23 ms + 7.5 GB/s per block; Parseval gives "
                                                    "it for free (agreement 2.6 × 10<super>−6</super>)"],
           ["Warp FFT kernel", "shared-memory kernel barrier-bound (0.50 ms); warp kernel 0.30 ms, verified against "
                               "a DFT"],
           ["Integration windows on absolute block numbers", "one time grid for all readers and links"],
           ["Capture without --privileged", "NET_RAW + IPC_LOCK + RDMA devices verified live at line rate"],
           ["Plain FFT, no fine PFB", "same choice as rawspec/seticore; affordable later if leakage matters"]],
          [0.38, 0.62], "The main design decisions (details and measurements: docs/runtime.md §2).")
    P("These choices were validated end to end: identical output to the CyberEther implementation (to float "
      "rounding) on deterministic synthetic data, unit tests for every component (C++/CUDA and Python), and a 2-hour "
      "live soak with a DDR dump and a raw-voltage dump every five minutes. The soak had no loss of the system's own "
      "making; the earlier rare gaps at DDR dumps did not recur.")

    # ------------------------------------------------------------------------------------------------ 12
    H1("Limitations and next steps")
    B("<b>Second link</b>: configured but not yet cabled. Its PCI function, UDP port and source address must be "
      "confirmed, then a two-link soak run.",
      "<b>Input → beam/polarisation mapping</b>: only one connector is mapped so far; the pairing assumed by the "
      "layout needs an injected tone on each input.",
      "<b>Time reference</b>: t = 0 is estimated from packet arrival (~ms). A PPS-aligned sync in firmware would make "
      "timestamps exact. The board clock also reads about −24 ppm against the temporary PPS.",
      "<b>Hz resolution</b>: needs a fused cuFFT path that gathers 64–128 blocks per channel, and a decision on band "
      "and output volume (full band at ~1 Hz is terabytes per hour).",
      "<b>Live search</b>: today on finished files; next a throttled reader on a spectra ring, and heimdall for "
      "wideband transients fed from coarse detection.",
      "<b>Pause frames</b>: test whether the board honours 802.3x pause; if so, NIC buffer overflows would become "
      "short delays instead of loss.",
      "<b>Flags</b>: missing data is counted per spectrum; a channel/time mask (FBH5 /mask) is the next step, seeded "
      "from config/known_rfi.json.")

    # ------------------------------------------------------------------------------------------------ appendix
    H1("Appendix: repository map and glossary")
    TABLE([["Path", "What"],
           ["config/", "station.yaml, boards/&lt;board&gt;.yaml (incl. receiver.links), known_rfi.json"],
           ["src/lft3/board/", "board control, telemetry, captures, sensors (lft3-board)"],
           ["src/lft3/pipeline/runtime.py, cli/pipeline.py", "run planner and lft3-pipeline"],
           ["src/lft3/ring.py", "Python view of the rings (status, metrics, triggers, CuPy access)"],
           ["src/lft3/search/", "lft3-search (hyperseti)"],
           ["src/lft3/products/", "readers and plots for FBH5 and captures (lft3-plot)"],
           ["pipeline/src/capture/", "lft3-capture: assembler, scatter kernel, DAQIRI config, pcap and synthetic sources"],
           ["pipeline/src/common/", "packet format, block layout, ring protocol"],
           ["pipeline/src/spectrometer/", "fused FFT kernels, FBH5 writer, lft3-spectrometer"],
           ["pipeline/src/dump/", "GUPPI RAW writer, lft3-dump"],
           ["pipeline/bench/", "ring, capacity and rawspec benchmarks"],
           ["tools/viewer/", "lft3-view"],
           ["docs/", "user_guide.md (operations), runtime.md (implementation, decisions), this document"]],
          [0.36, 0.64], "Where things are.")
    TABLE([["Term", "Meaning"],
           ["block", "1024 consecutive spectra of one link (6.71 ms); the unit of capture, publication and reading"],
           ["bindex (b)", "which 1024-channel slice of a spectrum a packet carries (0–5)"],
           ["burst", "a batch of up to 1024 received packets handed over by DAQIRI"],
           ["coarse channel", "one of the board's 6144 PFB channels, 152.6 kHz wide"],
           ["DAQIRI", "NVIDIA's high-rate packet I/O library (raw ibverbs on ConnectX)"],
           ["FBH5", "filterbank data in HDF5, the Breakthrough Listen spectra format"],
           ["fine channel", "one FFT bin within a coarse channel; 1.192 kHz with a 128-point FFT"],
           ["GUPPI RAW", "raw-voltage file format: 80-character header cards + blocks of [channel][time][pol] samples"],
           ["link / stream", "one 100 GbE connection from a board, with its own capture process and ring "
                             "(&lt;board&gt;-&lt;link&gt;)"],
           ["overrun", "a block a reader lost because the writer reused its slot first"],
           ["ring / slot", "the per-link circular buffer of blocks in GPU memory / one block-sized position in it"],
           ["seqlock", "versioned read: check a sequence number before and after reading to detect concurrent "
                       "overwrites"],
           ["sync", "board command that restarts t at 0 and applies pending header settings"],
           ["t", "spectrum counter since the last sync (one per 6.5536 µs)"],
           ["VMM", "CUDA's virtual memory management API; used to share GPU memory between processes by file "
                   "descriptor"]],
          [0.2, 0.8], "Glossary.")


def main():
    cover()
    content()
    doc = Doc(OUT)
    doc.multiBuild(story)
    print(f"wrote {OUT} ({OUT.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
