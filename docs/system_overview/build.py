"""Build docs/system_overview/lft3_system_overview.pdf: how the LFT3-Earth system works.

    python figures.py figures && python build.py            (needs reportlab, svglib, matplotlib)

The text is in this file; the diagrams are drawn by figures.py. Numbers are the measured values recorded in
docs/runtime.md and docs/sizing.md (2026-11-13/14); update them together. Keep file paths in the appendix only.
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
VERSION = "1.1"
DATE = dt.date(2026, 11, 16).strftime("%d %B %Y")

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
    "bg": ParagraphStyle("bg", fontName="Sans", fontSize=8.4, leading=11.8, textColor=INK,
                         backColor=colors.HexColor("#f4f1ea"), borderPadding=(6, 8, 6, 8), leftIndent=6, rightIndent=6,
                         spaceBefore=5, spaceAfter=10),
    "num": ParagraphStyle("num", fontName="Serif", fontSize=9.6, leading=13.4, textColor=INK, leftIndent=17,
                          bulletIndent=3, spaceAfter=2.5, alignment=TA_LEFT),
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
                               PageTemplate("body", [frame], onPageEnd=self._page)])
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


def H1U(t):
    """Unnumbered chapter (the summary), so that the numbered chapters and their cross-references stay fixed."""
    if story:
        story.append(PageBreak())
    story.append(Paragraph(t, S["h1"]))


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


def BG(title, t):
    """Background box: explains a concept the average reader may not know."""
    story.append(Paragraph(f'<font name="Sans-Bold" color="#8a5a12">{title[0].upper() + title[1:]}.</font> {t}', S["bg"]))


def NL(*items):
    """Numbered list."""
    for i, t in enumerate(items, 1):
        story.append(Paragraph(t, S["num"], bulletText=f"{i}."))
    story.append(Spacer(1, 4))


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
                           'products, viewer, search and sizing</font>',
                           ParagraphStyle("cs", fontName="Sans", fontSize=12.5, leading=17, textColor=colors.white)))
    story.append(Spacer(1, 2.6 * cm))
    story.append(figure_drawing("f01_system", A4[0] - 4.2 * cm))
    story.append(Spacer(1, 0.8 * cm))
    meta = [["Scope", "One RFSoC 4x2 board streaming to one NVIDIA DGX Spark, and the sizing of the full station: "
                      "6 boards with one 100 GbE link each"],
            ["State", "Implemented and running live at 60.8 Gb/s; 2-hour soak without loss of the system's own "
                      "making. Production hardware sized, not yet bought"],
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
    H1U("Summary")
    P("LFT3-Earth's digital backend turns the radio signals of six RFSoC 4x2 boards into spectra, raw-voltage "
      "recordings and search candidates. Each board digitises four inputs, two dual-polarisation beams, splits "
      "937.5 MHz of band into 6144 channels of 152.6 kHz, and streams them over one 100 GbE link at 60.8 Gb/s. The "
      "host software receives that stream, divides each channel further on a GPU, writes the spectra to disk, "
      "records raw voltages on demand, and monitors and controls the boards.")
    H3("Where it stands")
    B("One board streams to one NVIDIA DGX Spark, the development host. The real-time runtime runs live at line "
      "rate: a 2-hour soak test lost no data through the system's own doing.",
      "The receiver never waits for anything downstream. It writes voltages into a ring in GPU memory, and "
      "independent readers process them at their own pace: today the 1.2 kHz spectrometer and the raw-voltage dump. "
      "A slow or crashed reader cannot cause data loss.",
      "Spectra are written in the Breakthrough Listen formats and open with the community tools. A browser viewer "
      "shows hour-long files interactively, and a hyperseti drift search runs on finished files.",
      "The frequency axis and its sign are confirmed by the board's own clock spurs and by an injected test tone.")
    H3("What the full station needs")
    B("Every processing stage is limited by memory bandwidth, not arithmetic. This makes the cost of the full "
      "station predictable from the measurements on the Spark.",
      "At the interim 1.2 kHz resolution the station needs little: two current GPUs, or three DGX Sparks.",
      "At about 1 Hz, the resolution narrowband technosignature searches use, the <b>drift search dominates</b>: "
      "about 60 % of the cost, ahead of the fine channeliser. Its cost grows with the length of the search window "
      "and much more slowly with the drift range.",
      "<b>Recommendation:</b> three servers, each with two NVIDIA RTX PRO 6000 Blackwell GPUs and one dual-port "
      "100 GbE card cabled directly to two boards. Each GPU then runs at about 57 % at 1.16 Hz. The hardware costs "
      "roughly $150–200k, plus a head node and about 0.5 PB of storage. The same servers run the interim system at "
      "under 10 % load, so they can be bought before the final resolution is chosen.")
    H3("Open decisions and risks")
    B("<b>Science choices that set the size</b>: the band processed at Hz resolution, the search window, the length "
      "of the raw-voltage buffer for transients, and the trigger rate. Halving the band, or finding a four times "
      "faster search kernel, halves the number of GPUs. The buffer length sets the RAM, and the trigger rate sets "
      "the storage.",
      "<b>Not yet measured</b>: the wideband transient search, the fused Hz-resolution channeliser, and GPUDirect "
      "transfers from the network card on the chosen GPU. A week on one rented RTX PRO 6000 server, after porting "
      "the runtime from the Spark to an x86 server, settles them before purchase.",
      "<b>Still to verify on the board</b>: which inputs pair into a dual-polarisation beam, and an exact time "
      "reference through a firmware sync aligned to the PPS.")

    H1("About this document")
    P("This document explains how the LFT3-Earth digital backend works, end to end. It starts from the radio signal "
      "at the RFSoC board's inputs and ends with spectra on disk, raw-voltage dumps, search hits and the browser "
      "viewer. It then sizes the computing needed for the full station. It is written for engineers and astronomers "
      "who need to understand, operate, debug or extend the system. It covers what each part does, how data moves "
      "through memory, which algorithms are used, and why they were chosen. Where to find each part in the "
      "repository, and the documents with more detail, are listed in the appendix.")
    P("All numbers were measured on the system itself, a DGX Spark with the GB10 chip, between 12 and 14 November "
      "2026, unless stated otherwise. Program names are set in " + mono("monospace") + ". In the diagrams, colour "
      "shows where something runs: orange is the board and FPGA, grey the network, blue the host CPU, green the GPU "
      "and its memory, purple storage, and teal the reader processes. Diagrams are kept sparse; the text around them "
      "carries the detail. Shaded boxes explain concepts specific to radio instrumentation or GPU programming; skip "
      "them if they are familiar.")
    TABLE([["Quantity", "Value"],
           ["Station", "6 RFSoC 4x2 boards (5 UHF, 1 VHF), one 100 GbE link each"],
           ["Sampling", "5 GSPS real per ADC input → 1250 MSPS complex after down-conversion"],
           ["Coarse channels (FPGA)", "8192-point, 8-tap polyphase filterbank; 6144 channels of 152.587890625 kHz "
                                      "kept = 937.5 MHz per input"],
           ["Inputs and beams", "4 ADC inputs per board = 2 dual-polarisation beams"],
           ["Link rate", "915 527 packets/s of 8298 B = 60.8 Gb/s per board; 366 Gb/s for the station"],
           ["Fine channels (GPU)", "128-point FFT per coarse channel → 786 432 channels of 1.192 kHz per beam, "
                                   "integrated over 0.99992 s; Hz resolution planned"],
           ["Time base", "t counts spectra (6.5536 µs each) since the last board sync; blocks of 1024 spectra "
                         "(6.71 ms)"],
           ["Products", "fine and coarse FBH5 spectra, GUPPI RAW voltage dumps on trigger, hyperseti hits"],
           ["Capacity of one Spark", "2 boards with headroom; compute for up to 5; at Hz resolution about 1"],
           ["Station sizing", "at 1.16 Hz with the drift search: one RTX PRO 6000 GPU per board, 3 servers of "
                              "2 boards"]],
          [0.28, 0.72], "Key numbers.")

    # ------------------------------------------------------------------------------------------------ 2
    H1("The system at a glance")
    FIGURE("f01_system", "The whole system for one board. Thick arrows carry the 60.8 Gb/s data stream; the dotted "
                         "arrow is control. The full station has six boards, each with one 100 GbE link.")
    P("The system has three layers, kept deliberately independent:")
    B("<b>The board</b> does the hard real-time work at 5 GSPS: sampling, down-conversion, the coarse polyphase "
      "filterbank and packetising. The host controls and monitors it over the management network with "
      + mono("lft3-board") + ".",
      "<b>The real-time runtime</b> on the host receives the stream at line rate. Its one rule: <i>the receiver never "
      "waits for anything downstream</i>. It writes every 6.71 ms block of voltages into a ring in GPU memory. Any "
      "number of reader processes read the ring at their own pace: the spectrometer, the dump process, and in future "
      "the searches.",
      "<b>Products and tools</b> work on files: FBH5 spectra, GUPPI RAW dumps, search hits, the browser viewer "
      + mono("lft3-view") + ", plots, and the " + mono("lft3-pipeline") + " orchestrator that starts, watches and "
      "stops everything.")
    P("Configuration lives in git and is the single source of truth. A station file lists the boards and the data "
      "directory. One file per board describes its bitstream, clocks, NCO, input mapping, network addresses, the "
      "host-side link with its CPU cores, and timing.")

    # ------------------------------------------------------------------------------------------------ 3
    H1("The RFSoC board")
    H2("Signal chain")
    FIGURE("f02_signal_chain", "Signal chain inside the FPGA, for each of the four inputs. The dashed path is the DDR "
                               "ring buffer, read out only on a trigger.", width=A4[0] - 4.2 * cm)
    P("Each RFSoC 4x2 board has four ADC inputs sampled at 5 GSPS. The digital down-converter mixes each input with "
      "an NCO at −2031.25 MHz and decimates by 4, giving 1250 MSPS complex samples centred on 2031.25 MHz. An "
      "8192-point, 8-tap polyphase filterbank (PFB) splits that 1.25 GHz into 152.587890625 kHz channels. 6144 of "
      "them, 937.5 MHz of the band, are kept. Each channel sample is requantised to 8-bit I and 8-bit Q. A bit "
      "selection setting sets the level: about 11 counts RMS on noise with the current setting, and a strong tone at "
      "−60 dBm clips.")
    BG("down-conversion, NCO and decimation", "The ADC samples a real-valued signal 5 billion times a second, which "
       "covers 0–2.5 GHz. The band of interest is shifted down to zero frequency by multiplying the samples with a "
       "complex sinusoid from a <i>numerically controlled oscillator</i> (NCO) at −2031.25 MHz. After this mixing, a "
       "signal at 2031.25 MHz sits at 0 Hz and the result is complex: an in-phase (I) and a quadrature (Q) component per "
       "sample, which together keep track of positive and negative frequency offsets. A low-pass filter then keeps the "
       "central 1.25 GHz, and since that needs only 1.25 GS/s, three of every four samples are dropped. This is "
       "<i>decimation</i>.")
    BG("polyphase filterbank (PFB)", "A filterbank splits a wideband signal into many narrow frequency channels, each "
       "with its own lower-rate time series. A plain FFT does this too, but its channels have a sinc-shaped response: "
       "they overlap strongly and a strong signal leaks into many neighbours. A PFB first multiplies the input by a long, "
       "carefully designed filter spread over several FFT lengths, here 8 taps of 8192 samples, and sums those "
       "segments, then takes one FFT. The channels come out nearly flat-topped with steep edges and little leakage. "
       "Each channel's samples are still complex, one every 6.5536 µs, which is 8192 divided by 1.25 GHz.")
    P("One spectrum, all 6144 channels of all four inputs, is produced every 6.5536 µs. The firmware numbers them with "
      "a 58-bit counter <b>t</b> that starts at 0 on every <i>sync</i>. The packetizer sends each spectrum as six UDP "
      "packets of 1024 channels each. In parallel, a DDR4 ring buffer keeps the last 0.86 s of raw 4-bit samples. A "
      "trigger reads it out through the packetizer over the same 100 GbE link, at about 10 Gb/s for 3.5 s.")

    H2("Bringing a board up")
    FIGURE("f15_bringup", "The bring-up sequence of lft3-board start, about 10 s per board.")
    P(mono("lft3-board start") + " follows the firmware author's bring-up notebook step by step:")
    B("program the bitstream and set the destination MAC address. It lives in the 100 GbE core's address table, "
      "defaults to broadcast and is lost on every reprogram;",
      "initialise the RF data converters and the LMK/LMX clock chips, then multi-tile synchronisation (MTS);",
      "select SYSREF as the mixer event source, then program the NCO. The packet header carries the NCO as a signed "
      "whole number of MHz, so the exact value, −2031.25 MHz, comes from the configuration, not from the packets;",
      "set the requantisers, the DDR size and read delay, the packet mask, the board id and the expected PPS period;",
      "issue a <b>sync</b>: t restarts at 0 and the header fields take effect. Some settings, such as the packet mask "
      "and the board id, only apply after a sync.")
    BG("MTS, SYSREF and sync", "An RFSoC has several ADC 'tiles', each with its own clock dividers. <i>Multi-tile "
       "synchronisation</i> (MTS) aligns them so that all inputs sample at the same instants, which matters when inputs "
       "are later combined into polarisations and beams. It relies on <i>SYSREF</i>, a slow reference pulse distributed "
       "with the sample clock; NCO settings are also applied on a SYSREF edge so all mixers change phase together. The "
       "LFT3 firmware's own <i>sync</i> is different: it restarts the spectrum counter t and latches pending settings.")
    P("In a station, every board is initialised in parallel, each under its own lock. The sync writes are then "
      "released together, so all boards share a time base to within milliseconds. A sync aligned to the PPS in "
      "firmware has been requested and would make it exact.")

    H2("Monitoring and control")
    P("Control and monitoring are separate layers. The control layer only writes, under a per-board lock. The "
      "telemetry layer only reads, so monitoring can run continuously while someone operates the board. Both use "
      "casperfpga over katcp on the 1 GbE management network. Board sensors such as rail voltages and temperatures "
      "are read from the board's operating system over SSH.")
    TABLE([["Health check", "Rule", "If broken"],
           ["transmitted packet rate", "within 0.1 % of 915 527 /s × enabled packets / 6", "FAIL"],
           ["packetizer FIFO full, blocked words", "rate 0: no board-side loss or back-pressure", "FAIL"],
           ["header NCO, board id, packet mask", "equal to the configuration", "FAIL"],
           ["destination MAC", "equal to the configuration; broadcast means misconfigured", "FAIL"],
           ["PPS, sample clock against PPS", "edges present; clock within 1 ppm, informational while the PPS is "
                                             "temporary", "WARN / INFO"],
           ["requantiser setting, expected clock period", "equal to the configuration", "WARN"]],
          [0.3, 0.5, 0.2], "Board health checks of lft3-board status. Any FAIL gives exit code 2, so the check can "
                           "run from cron.")
    P(mono("lft3-board trigger") + " fires a DDR dump and re-arms the trigger. " + mono("lft3-board snapshot") +
      " reads the firmware's FFT and ADC snapshot memories for diagnostics. " + mono("lft3-board monitor") + " exports "
      "everything as Prometheus metrics and JSON lines.")

    # ------------------------------------------------------------------------------------------------ 4
    H1("Packets on the wire")
    H2("The channel packet")
    FIGURE("f03_packet", "Channel packet: a 64-byte header, of which four of the eight 64-bit words are used, and a "
                         "payload of 1024 channels, each holding one complex sample of each of the four inputs.")
    P("Every channel packet is a UDP datagram of 8256 bytes to port 60000: a 64-byte header and an 8192-byte "
      "payload. The header is eight big-endian 64-bit words. Word 0 holds the spectrum counter t (58 bits), the "
      "packet index b within the spectrum (0–5) and the board id (3 bits). Words 1 and 2 hold the four NCO values as "
      "signed whole MHz, and word 3 the four PFB bit selections. The payload holds 1024 consecutive channels, "
      "k = 1024·b + i. Each channel is 8 bytes: input 0 I, input 0 Q, input 1 I, … input 3 Q, as signed bytes. "
      "Inputs 0 and 1 are assumed to form beam 0 (X and Y) and inputs 2 and 3 beam 1; an injected tone on each input "
      "will confirm this.")
    BG("big-endian", "A 64-bit word is eight bytes; big-endian, or network order, sends the most significant byte "
       "first. The host CPUs store numbers the other way round, so the capture code byte-swaps the four header words it "
       "reads. The payload is a sequence of single bytes and needs no swapping.")
    TABLE([["", "Per packet", "Per spectrum (6 packets)", "Per second, one board"],
           ["Packets", "1", "6", "915 527"],
           ["UDP payload", "8256 B", "49.5 kB", "7.56 GB (60.5 Gb/s)"],
           ["Samples", "1024 ch × 4 inputs", "6144 ch × 4 inputs", "3.75 × 10<super>9</super> complex samples"],
           ["On the wire, with Ethernet, IP and UDP", "8298 B", "49.8 kB", "60.95 Gb/s"]],
          [0.28, 0.2, 0.24, 0.28], "Channel stream rates. The link needs an MTU of 9000.")

    H2("Channels and sky frequency")
    FIGURE("f04_freq_map", "How the PFB's bins are reordered into packet channels k, and the sky frequency of k for an "
                           "NCO at −2031.25 MHz.")
    P("The PFB produces 8192 bins. The packetizer sends bins 5120–8191, the negative frequency offsets, as packets "
      "b = 0–2, and bins 0–3071, the positive offsets, as packets b = 3–5. So the channel index k increases with "
      "frequency across the whole band:")
    P("<para alignment='center'><font name='Serif-Italic'>RF(k) = −NCO + (k − 3072) × 152.587890625 kHz</font></para>")
    P("With NCO = −2031.25 MHz the band runs from 1562.50 MHz (k = 0) to 2499.85 MHz (k = 6143). Two checks confirm "
      "the sign and the axis: the board's own clock spurs land on exact multiples of 125 MHz, and an 1800 MHz test "
      "tone appears at 1800.04 MHz with no mirror image. The 40 kHz offset matches the board's sample clock running "
      "about 22 ppm slow against the signal generator.")

    H2("DDR dump packets")
    P("Triggered DDR dumps reuse the same packet size on UDP port 40000. Each payload holds 2048 time samples of the "
      "four inputs at 4 bits I and 4 bits Q. A 4 GiB dump is 2<super>19</super> packets. The host receives them on a "
      "UDP socket for now; the capture process could also take them on a second receive queue.")

    # ------------------------------------------------------------------------------------------------ 5
    H1("The development host: NVIDIA DGX Spark")
    FIGURE("f05_spark", "The DGX Spark as the runtime uses it. Red: the three cores reserved for one board's capture.")
    P("The development system runs on a DGX Spark, built around the GB10 chip: 20 Arm cores, 10 Cortex-X925 "
      "performance cores and 10 Cortex-A725 efficiency cores, and a Blackwell GPU with 48 streaming multiprocessors. "
      "They share <b>128 GB of LPDDR5x unified memory</b> with a peak bandwidth of 273 GB/s. That single fact shapes "
      "the whole design:")
    B("There is no PCIe copy between CPU and GPU memory. The GPU reads the network card's receive buffers in place, "
      "so the Spark needs no GPUDirect RDMA, and does not support it.",
      "Every byte moved by the NIC, the CPU or the GPU competes for the same 273 GB/s. Memory bandwidth, not "
      "arithmetic, is the binding constraint (§10).",
      "How memory is allocated still matters. GPU kernels reach 237 GB/s on ordinary GPU allocations, but only "
      "172 GB/s on host memory registered for GPU access.")
    BG("DMA and pinned memory", "A network card writes received packets into memory itself, without the CPU copying "
       "them: <i>direct memory access</i> (DMA). To do so it needs memory that the operating system will never move or "
       "swap out while the card holds its address: <i>pinned</i> or page-locked memory. On the Spark that pinned memory "
       "is the same physical DRAM the GPU reads, so a packet the NIC has written can be read by a GPU kernel directly.")
    P("The ConnectX-7 network card has two QSFP ports, so one Spark can take two boards directly. Each port appears "
      "as two network devices, one per half of its PCIe link; a 100 GbE cable from a board lands on one half. "
      "Packets are received with NVIDIA's DAQIRI library using raw ibverbs: a hardware flow rule steers the board's "
      "UDP stream to one receive queue and bypasses the kernel network stack, which only manages about 0.4 million "
      "packets per second.")
    BG("kernel bypass and busy-polling", "Normally every packet goes through the operating system's network stack: an "
       "interrupt, protocol processing and a copy into a socket buffer, costing microseconds per packet. At 915 527 "
       "packets per second that is too slow. With <i>ibverbs</i>, the RDMA interface of NVIDIA network cards, a "
       "program owns a receive queue directly: it gives the card buffers and finds the filled ones by reading a "
       "completion queue in memory. DAQIRI's worker thread checks that queue in a tight loop, <i>busy-polling</i>, "
       "which costs a whole core but never waits for an interrupt.")
    P("Each board gets three performance cores: DAQIRI's master thread, its receive worker busy-polling at 100 %, "
      "and the capture thread. Readers use the remaining cores.")

    # ------------------------------------------------------------------------------------------------ 6
    H1("The real-time runtime")
    H2("Architecture")
    P("The runtime is a set of independent processes around one shared structure per board: the <b>ring</b>. One "
      "writer, " + mono("lft3-capture") + ", fills it at line rate. Readers attach to it, read it at their own pace, "
      "and can come and go at any time. The writer never waits for a reader. A reader that falls more than the ring's "
      "length behind loses blocks and counts them as overruns, so the capture stays lossless whatever the readers "
      "do. Each process runs in its own container, so a crashing or slow reader cannot affect the capture.")
    FIGURE("f14_processes", "Processes and containers for one board. They share only shared memory and the GPU ring.")
    P(mono("lft3-pipeline") + " starts the capture first, waits until its ring exists, then starts the readers. On "
      "stop, by Ctrl-C, a time limit or " + mono("lft3-pipeline stop") + ", it reverses the order: readers first, so "
      "they close their files while the ring still exists, then the capture. Each run writes its logs, status time "
      "series and NIC counters to a run directory.")
    N("<b>Why not a dataflow framework?</b> The first implementation was a CyberEther plugin. It ran at line rate, but "
      "CyberEther runs every block in lock-step on one scheduler thread, so any slow consumer stalls the receiver, "
      "which then drops packets. Holoscan 5.0 and hashpipe were also evaluated. Holoscan 5.0 is a fresh rewrite with "
      "Beta APIs; hashpipe hands each buffer to one consumer through semaphores, which is back-pressure. The ring "
      "design keeps the property that matters: the readers can never hold up the receiver.")

    H2("A packet's journey through memory")
    FIGURE("f06_data_movement", "Where one board's bytes go. Every arrow is traffic on the shared memory bus; red "
                                "marks the ring slot currently being written.")
    P("Data enters through the NIC's DMA engine. DAQIRI splits each frame: the first 106 bytes, the Ethernet, IP and "
      "UDP headers plus the 64-byte LFT3 header, land in a <i>header segment</i> buffer that the CPU reads. The "
      "8192-byte payload lands in a <i>payload segment</i> buffer. Both are pinned host memory, 64 Ki buffers of each, "
      "0.5 GB in all. The capture thread reads only the headers, about 97 MB/s, and never touches the payloads.")
    BG("header-data split", "The NIC can scatter one incoming frame into two buffers: the first N bytes into one, the "
       "rest into another. Here that separates what the CPU needs, the headers at 1.3 % of the bytes, from what only "
       "the GPU needs, the samples, so the CPU's caches are never filled with sample data.")
    P("For each packet the CPU decides where the payload belongs: which block from t, and which time row and "
      "1024-channel group from b. It appends a small <i>scatter job</i> record to a pinned job list: payload pointer, "
      "block pointer, t and channel offset. Once per burst of up to 1024 packets, a GPU <b>scatter kernel</b> reads all "
      "the payloads of the burst straight from DAQIRI's buffers and writes them into the right places of the ring "
      "block. That is the only time the payload is copied: 7.5 GB/s read and 7.5 GB/s written per board.")
    P("Readers then read each block once from GPU memory. The spectrometer reads 7.5 GB/s and writes 6.3 MB per "
      "second of spectra. The dump process copies a window to pinned host memory only when triggered. Altogether one "
      "board costs about 30 GB/s of memory traffic before any search runs.")

    H2("Lifetime of a packet and of a block")
    FIGURE("f07_packet_lifetime", "Lifetime of one packet, left, and of the block it ends up in, right of the dashed "
                                  "line, across the parts of the system that handle them.")
    P("A packet's payload lives in DAQIRI's receive buffer for only 1–2 ms:")
    B("the NIC writes it; the DAQIRI worker hands it out as part of a <i>burst</i> once 1024 packets have arrived, "
      "about 1.1 ms at line rate, or after a 1 ms timeout;",
      "the capture thread parses the burst's headers, places every packet with the assembler, sorts the jobs and "
      "launches the scatter kernel, in about 83 µs;",
      "a CUDA event signals that the kernel has finished, and only then is the burst returned to DAQIRI for reuse. "
      "The CPU never waits for the GPU: it polls events between bursts.")
    P("The block lives much longer. It is <i>open</i> while its 6144 packets arrive over 6.71 ms, <i>sealed</i> once "
      "complete or stale, with missing packets zero-filled by the same kernel, and <i>published</i> to readers once "
      "its last kernel has finished. It then stays readable in the ring for about 2 s, until the writer reuses its "
      "slot. The spectrometer normally reads it within a millisecond of publication.")

    H2("Assembling blocks")
    FIGURE("f08_assembler", "Packets of five consecutive blocks, 16 of each block's 1024 time steps drawn; filled = "
                            "received. The assembler keeps the four newest blocks open and seals them in time order.")
    P("The assembler is plain C++, independent of DAQIRI and CUDA, and unit-tested. A packet (t, b) belongs to block "
      "t / 1024, slot (t mod 1024) × 6 + b. Its rules:")
    B("up to 4 blocks may be open at once, to tolerate slightly out-of-order arrival;",
      "the oldest open block is sealed when complete, or when the newest block seen is more than 2 blocks ahead. "
      "Missing slots are then listed for zero-fill, and counted;",
      "blocks for which no packet arrived at all are counted as <i>skipped</i>;",
      "duplicates and packets for already-sealed blocks are rejected and counted;",
      "if t jumps back by more than 1024 blocks, the board was re-synced: open blocks are flushed and the time base "
      "starts over;",
      "capture starts only at a block boundary, so the first block is complete.")
    P("Each new block gets the <b>next ring slot in allocation order</b>, not a slot derived from its block number. "
      "After a re-sync, block numbers restart and would otherwise collide with blocks still being sealed.")

    H2("The scatter kernel")
    BG("GPU execution: threads, warps, blocks, shared memory", "A GPU kernel runs thousands of threads. They execute "
       "in groups of 32 called <i>warps</i>, which issue each instruction together. A warp's memory accesses are fast "
       "when its 32 threads touch neighbouring addresses, because the hardware then <i>coalesces</i> them into a few "
       "wide transactions of 32–128 bytes. Threads are organised in <i>blocks</i> of up to 1024 threads that run on "
       "one streaming multiprocessor and can share a small, very fast on-chip memory, <i>shared memory</i>. Threads of "
       "a block wait for each other at a <i>barrier</i>; threads of one warp can also exchange register values "
       "directly with <i>shuffle</i> instructions, without shared memory or barriers.")
    FIGURE("f09_scatter", "The scatter kernel. One channel, highlighted, is a column of the packets as they arrive, "
                          "and must become a row of the ring. A tile of 32 packets × 32 channels is loaded row-wise "
                          "and stored column-wise through shared memory.")
    P("The scatter turns packet order, where one packet is one time sample of 1024 channels, into ring order, where "
      "one row is 1024 time samples of one channel. Done naively, each thread would write 4 bytes to a different "
      "row, which wastes most of each memory transaction. The kernel instead loads a tile of 32 packets × 32 "
      "channels into shared memory, each warp reading 256 contiguous bytes of one packet, and writes it out "
      "transposed. Each warp then writes 32 consecutive time samples of one channel row, a contiguous 128-byte run. "
      "For the tile's packets to be consecutive in time, the CPU first sorts each burst's jobs by channel group with "
      "a counting sort, at negligible cost. Missing packets go through the same kernel with no source and are "
      "written as zeros.")
    P("The tiled kernel measured 1.4× faster than the naive one. It takes about 83 µs per burst of 1024 packets and "
      "runs at about 200 GB/s: it is limited by memory bandwidth, as it should be. The shared-memory tile is padded "
      "by one column, 32 × 33 entries, so that column-wise accesses do not all hit the same memory bank.")

    H2("The ring and its protocol")
    FIGURE("f10_ring", "The ring of one board. The writer, red, fills slots in turn; head marks the newest published "
                       "block; each reader, teal, follows at its own position.")
    P("Each board's ring has two parts:")
    B("<b>Control plane</b>: a 560 kB block of shared memory with a fixed binary layout. It holds the layout, "
      "observation id, configuration hash and software version; the publication head and a futex word; capture "
      "statistics; up to 16 reader entries with name, cursor, lag, overruns and heartbeat; the table from sequence "
      "number to slot; and per-slot metadata: block number, t0, missing packets, NCO and the estimated UNIX time "
      "of t = 0. The C++ layout is fixed by compile-time checks and mirrored byte for byte in Python, which reads it "
      "for status and metrics.",
      "<b>Data plane</b>: the voltage blocks, by default 299 slots of 50.3 MB, 15 GB or 2.0 s. They live in one CUDA "
      "VMM allocation with 2 MiB pages, exported as a file descriptor and handed to readers over a unix socket. "
      "Each reader maps it <i>read-only</i> at full GPU bandwidth, so a buggy reader cannot corrupt the data.")
    P("Each block is laid out as [beam][channel][1024 t][pol], one complex int8 per element. This is GUPPI RAW order "
      "per beam: a fine FFT reads contiguous samples, and a dump writes a block as it is. An earlier layout kept one "
      "row per channel across the whole ring, so that any FFT length would read contiguously. It was measured and "
      "rejected: the scatter's writes to 12 288 far-apart rows thrashed the GPU's address translation, and throughput "
      "fell to 6 GB/s, below the required 7.5 GB/s.")
    BG("TLB", "Programs use virtual addresses; the hardware translates them to physical memory one page at a time, "
       "4 KiB or 2 MiB, and caches recent translations in a <i>translation lookaside buffer</i> (TLB). A kernel that "
       "touches thousands of pages at once, as the rejected layout did, misses the TLB constantly, and every miss costs "
       "a walk through the page tables. Large pages and compact access patterns avoid it.")
    BG("CUDA VMM and sharing GPU memory", "CUDA's virtual memory management API separates allocating physical GPU "
       "memory from mapping it at an address. An allocation made this way can be exported as an ordinary Linux file "
       "descriptor and sent to another process over a unix socket; that process imports it and maps the same memory "
       "into its own address space, here read-only. Measured on the Spark, this keeps the full 237 GB/s in the other "
       "process, whereas host memory registered with CUDA reads at 172 GB/s.")
    H3("Publication and reading: a seqlock")
    BG("seqlock and futex", "A <i>seqlock</i> lets one writer update data that many readers read, without readers ever "
       "blocking the writer. Each data item carries a sequence number; a reader notes it, reads, then checks it is "
       "unchanged. If it changed, the writer was there meanwhile and the reader discards what it read. A <i>futex</i> "
       "is a Linux primitive that lets threads, even in different processes, sleep on a word in shared memory until "
       "another thread changes it and wakes them, so idle readers use no CPU.")
    P("Every published block gets a sequence number n = 0, 1, 2, …; the control block maps n to the slot that holds "
      "it. The writer never waits for readers:")
    NL("Before any GPU write into a slot, mark the slot invalid by setting its sequence number to a reserved value.",
       "Run the scatter and zero-fill kernels into it and record a CUDA event after them.",
       "When the event has completed, write the slot's metadata, set its sequence number to n, record slot(n), "
       "advance the head to n + 1, and wake all waiting readers.")
    P("A reader, in any process, follows the head:")
    NL("Wait until the head passes n, sleeping on the futex. If n is older than head − (S − 8), it has fallen too far "
       "behind: jump ahead and count the skipped blocks as overruns.",
       "Look up slot(n), check its sequence number is n, and copy the metadata.",
       "Run its GPU work on the block and wait for it to finish.",
       "Check the sequence number again. If it changed, the writer reused the slot during the read: discard the "
       "result and count an overrun.",
       "Record n + 1 as its cursor, with its lag and a heartbeat; status tools read these.")
    P("For synthetic and file sources only, readers can be <i>lossless</i>. The writer then waits until every "
      "lossless reader with a live heartbeat has moved at least 8 slots beyond a slot before reusing it. Tests and "
      "benchmarks use this to process every block. The 8-slot margin came out of a parity test, which caught a bug "
      "when the margin was 0.")

    H2("The fine spectrometer")
    BG("why a second FFT", "Each 152.6 kHz board channel is itself a time series of complex samples. Fourier "
       "transforming N consecutive samples of one channel splits it into N finer channels of 152.6 kHz / N; with N = 128 "
       "that is 1.19 kHz. Squaring the magnitudes gives power, and adding many such spectra, <i>integration</i>, "
       "reduces the noise fluctuations so faint steady signals stand out. Narrowband technosignature searches need "
       "resolutions of about 1 Hz, N ≈ 131 072, a future mode sized in §11.")
    FIGURE("f11_finespec", "Fine spectrometer for one coarse channel of one beam and one block: eight 128-point "
                           "segments are transformed, detected and summed; 149 blocks make a one-second spectrum.")
    P("For every block, " + mono("lft3-spectrometer") + " runs one fused kernel over all pairs of coarse channel and "
      "beam. Each row of 1024 dual-polarisation samples is cut into eight segments of 128. Each segment is Fourier "
      "transformed for both polarisations, |X|² + |Y|² (Stokes I) is formed, and the eight results are summed in "
      "registers. The sum is added to an accumulator in GPU memory, fftshifted so that frequency increases across "
      "the 128 fine bins. The voltages are read exactly once, and nothing larger than the accumulator is written. "
      "That fusion is what makes it fast: separate FFT, detect and integrate passes, as in CyberEther's own blocks, "
      "move about 6× more memory and ran at only 0.59× real time.")
    BG("Stokes I and fftshift", "A beam has two orthogonal polarisations, X and Y. The total intensity, Stokes I, is "
       "|X|² + |Y|², independent of how the signal is polarised. An FFT returns bin 0, zero offset, first, then the "
       "positive offsets, then the negative ones; <i>fftshift</i> rotates the bins so that the most negative offset comes "
       "first and frequency increases monotonically across each coarse channel.")
    H3("The warp FFT")
    FIGURE("f12_warp_fft", "Warp FFT for N = 128: each of the 32 lanes holds 4 samples; a small transform in "
                           "registers is followed by a 32-point FFT across the lanes. 8 lanes and 3 of 5 stages drawn.")
    P("The first kernel did the FFT in shared memory with 64 threads per channel and beam. Profiling showed it limited "
      "by its 56 barriers per block of data, not by arithmetic. A twiddle table was tried and was not faster. The "
      "current kernel gives each FFT to one warp of 32 threads and never synchronises. It uses the classic "
      "Cooley–Tukey idea that an N-point FFT with N = R × 32 can be done as R-point transforms followed by 32-point "
      "transforms, with a 'twiddle' phase factor in between. For N = 128, R = 4:")
    NL("Lane j, for j = 0 … 31, loads samples j, j+32, j+64 and j+96 of both polarisations. Each load by the warp "
       "covers 128 contiguous bytes, so memory access is coalesced.",
       "Each lane computes a 4-point DFT of its own four samples, entirely in registers.",
       "It multiplies output q by the twiddle factor e<super>−2πi·jq/128</super>.",
       "For each q, the 32 lanes then form a 32-point FFT among themselves: five radix-2 stages, in each of which a lane "
       "exchanges its value with lane j xor h, for h = 16, 8, 4, 2, 1, using a shuffle instruction, adds or "
       "subtracts, and multiplies by a twiddle.",
       "Lane l now holds the frequency bins q + 4·bitrev(l). It forms |X|² + |Y|², sums the 8 segments of the block in "
       "registers, and adds the 4 fftshifted bins to the accumulator.")
    P("The same scheme covers 64 and 256 points; other lengths use the shared-memory kernel. The warp kernel takes "
      "0.30 ms per 6.71 ms block for both beams, against 0.50 ms before, is close to memory-bound, and matches a "
      "reference DFT to better than 10<super>−4</super>.")
    H3("Time grid, missing data and the coarse product")
    B("<b>Integration windows are aligned to absolute block numbers</b>: window w covers blocks 149w … 149w + 148, "
      "152 576 spectra or 0.99992 s. All readers and all boards therefore share one time grid. A reader that starts "
      "mid-window waits for the next boundary. Each spectrum records the t of its first sample.",
      "<b>Missing data</b> is recorded per spectrum: zero-filled packets, plus 6144 for every block the capture "
      "skipped or the reader lost. Spectra are written regardless, and the count is what a later mask will use. A gap "
      "of whole windows, or a board re-sync, starts new files.",
      "<b>The coarse quick-look product costs nothing extra</b>. An FFT preserves total energy up to a factor N "
      "(Parseval's theorem), so the power in a board channel equals the sum of its 128 fine bins divided by 128. The "
      "writer thread computes it on the CPU from each fine spectrum. It agrees with a direct computation from the "
      "voltages to 2.6 × 10<super>−6</super>. A separate coarse reader is only needed for a faster cadence, such as "
      "0.1 s.",
      "<b>Frequency axis</b>: the first channel frequency and channel width come from the exact NCO in the "
      "configuration; the header's whole-MHz value is only a fallback. <b>Time</b>: the UNIX time of t = 0 is "
      "estimated from the first packet's arrival, to about a millisecond, until the firmware provides a sync aligned "
      "to the PPS.")

    H2("Triggered raw-voltage dumps")
    FIGURE("f13_dump", "A dump copies a window around the trigger time out of the ring: the part before the trigger, "
                       "teal, is already there; the part after, red, is copied as it arrives.")
    P(mono("lft3-pipeline trigger") + " sends a command to each board's " + mono("lft3-dump") + " over a unix socket: "
      "a UNIX time plus seconds before and after, by default 0.5 + 0.5. The dump process converts the time to block "
      "numbers using the capture's time base. It copies every block of the window that is still in the ring, and the "
      "later ones as they are published, into a pinned host buffer, checking each with the seqlock. It then writes the "
      "window to NVMe as GUPPI RAW, one file per beam. Each GUPPI block is one beam of a ring block, behind a header "
      "of 80-character cards. The window has to be copied first because NVMe cannot absorb 7.5 GB/s, and the ring "
      "only holds about 2 s. Live, a 1 s window of 7.5 GB was collected in 0.5–0.7 s and written in 2.8 s.")
    BG("GUPPI RAW", "The raw-voltage file format of the Green Bank and Breakthrough Listen backends, read by "
       "rawspec, blimpy and hyperseti. A file is a sequence of blocks; each block has a plain-text header of 80-character "
       "'KEY = value' cards, as in FITS, ending with END, followed by the samples ordered by channel, then time, then "
       "polarisation, as signed 8-bit real and imaginary parts.")

    H2("Products")
    TABLE([["Product", "Content"],
           ["Fine spectra", "FBH5, one file per beam: [time, 1, 786 432] float32, 1.192 kHz × 0.99992 s; per-spectrum "
                            "timestamps and missing-packet counts; SIGPROC header values; LFT3 metadata: NCO, "
                            "observation id, stream, configuration hash, software version. Compressed in chunks of "
                            "65 536 channels. About 16 GB per hour per board."],
           ["Coarse spectra", "same layout, 6144 channels of 152.6 kHz"],
           ["Latest snapshot", "the newest coarse spectrum per beam with its axes, for dashboards"],
           ["Raw voltages", "GUPPI RAW, 6144 channels × 2 polarisations × 8-bit complex, 6.5536 µs samples, "
                            "with a JSON description"],
           ["Search hits", "frequency, drift rate, S/N, channel, RFI label, observation id, stream"],
           ["Run records", "one log per process, the run plan, a status time series and NIC counters"]],
          [0.18, 0.82], "Products. Their locations on disk are listed in the appendix.")
    BG("FBH5", "Filterbank data in HDF5, the Breakthrough Listen format for spectra: a 3-D dataset [time, "
       "polarisation, frequency] with the SIGPROC header values as attributes: first channel frequency, channel "
       "spacing, time per spectrum and start time. HDF5 stores the dataset in independently compressed <i>chunks</i>, "
       "so a reader can decode just the part it needs.")
    P("FBH5 files open with blimpy, turboSETI and hyperseti as well as the project's own tools. GUPPI RAW opens with "
      "rawspec and blimpy. Files rotate every 3600 spectra, and the timestamps continue exactly across rotations, so "
      "tools can chain them into one run.")
    H2("What the spectra look like")
    P("The figure below shows real output: the first hour of the 2-hour soak test of 13 November 2026, beam 0, with "
      "no test signal applied. Power is in dB relative to the median channel, which is the receiver's own noise. "
      "These are the same views " + mono("lft3-view") + " offers interactively (§7).")
    FIGURE("c5_spectra", "Fine spectra from the soak test. (a) Full band, 5-minute average with the coarse-channel "
                         "response divided out; each plotted point covers 256 fine channels. (b) The spur at 2125 MHz "
                         "at full resolution, without that correction. (c) Wi-Fi channel 1 over one hour, 10-second "
                         "rows.", width=15.5 * cm)
    B("<b>Board spurs</b> (a). The narrow lines at 1750, 1875, 2000, 2125 and 2250 MHz, +10 to +36 dB, are exact "
      "multiples of 125 MHz: the FPGA's internal clock leaking into the ADCs. Because they land exactly where expected, "
      "they confirm the frequency axis and its sign. They belong in the static RFI mask.",
      "<b>Mean and peak</b> (a). One plotted point covers 256 fine channels, so a one-channel spur is diluted in the "
      "mean, dark, but kept in the peak, light. The viewer can show either reduction.",
      "<b>Coarse-channel ripple</b> (b). Without the correction, the noise floor dips by about 3 dB every 152.6 kHz: "
      "the edges of the board's PFB channels. The fine channels show the coarse channels' shape, and the plotting "
      "tools can divide it out. The spur occupies one 1.19 kHz channel. The small regular features near each coarse "
      "channel's centre repeat in every channel and are not yet explained.",
      "<b>RFI next to the board</b> (c). The Spark's own Wi-Fi, channel 1 at 2401–2423 MHz, switches on and off; the "
      "dark vertical line at 2412 MHz is the unused centre subcarrier of the Wi-Fi signal.")

    # ------------------------------------------------------------------------------------------------ 7
    H1("The viewer")
    FIGURE("f16_viewer", "lft3-view: files are read and reduced on the host; the browser receives only the "
                         "screen-sized tile it draws.")
    P("A one-hour full-band file holds 3600 spectra × 786 432 channels, about 11 GB before compression. "
      + mono("lft3-view") + " is a C++ HTTP server on the host with a single-page browser client. The browser never "
      "receives raw data, only the tile it is about to draw:")
    B("the <b>run catalog</b> chains rotated files of one beam into one timeline using their timestamps; a re-sync "
      "starts a new run;",
      "the HDF5 library is used only for metadata and the chunk index. The spectra themselves are read in parallel on "
      "all cores, and only the 65 536-channel chunks a view covers are decompressed;",
      "the reduction to screen pixels, maximum or mean with optional coarse-channel flattening, is vectorised and runs "
      "in the same pass;",
      "tiles are sent as 16-bit dB planes, compressed, 2.2–2.7× smaller than float32.")
    P("Measured on cold files: a whole-file overview of a one-hour file takes 1.7 s, reading 6.8 GB/s from NVMe; "
      "zooms take under 70 ms. h5py manages about 0.5 GB/s on the same files. The page offers pan and zoom, "
      "per-channel normalisation, spectrum and time-series strips, a drift-rate measurement tool, and marks for "
      "trigger times and known RFI. In 'follow live' mode it also shows files that are still being written.")

    # ------------------------------------------------------------------------------------------------ 8
    H1("Searching for narrowband signals")
    P(mono("lft3-search") + " runs hyperseti, Breakthrough Listen's GPU drift-rate search, on finished fine FBH5 "
      "files. It runs in its own Python environment with CuPy and a tested hyperseti version. Files that are still "
      "being written are never opened: a file counts as finished once the next file of the same beam exists or it "
      "has been idle for two minutes. The defaults are adapted to 1.19 kHz channels:")
    BG("drift rate and dedoppler search", "A transmitter on a rotating planet, received by a rotating Earth, is "
       "Doppler shifted by an amount that changes slowly with time, so a narrowband signal appears as a slanted line "
       "in a time–frequency plot. Its slope, in Hz/s, is the <i>drift rate</i>. A <i>dedoppler</i> search sums the "
       "power along straight lines of many trial slopes and reports lines whose summed power stands out from the "
       "noise by more than a threshold signal-to-noise ratio (S/N). Terrestrial interference usually has zero drift "
       "or an erratic one.")
    BG("spectral kurtosis", "A statistic that compares the variance of a channel's power with what pure Gaussian noise "
       "would give for the number of samples summed. Impulsive or switching interference makes it deviate, so it is a "
       "cheap way to flag RFI. It only works if the number of summed samples is known exactly.")
    B("no spectral-kurtosis flagging: hyperseti would otherwise flag all of the data, because its assumed "
      "accumulation count does not include the sum of two polarisations;",
      "drift rates up to 200 Hz/s: at 10 Hz/s a signal would not cross even one 1.19 kHz channel within a file, and "
      "there would be no drift trials;",
      "S/N threshold 20. Hits within two fine channels of a 125 MHz board spur, or in the Spark's Wi-Fi channel 1, "
      "are labelled as RFI but kept.")
    P("On live data the strongest hits are exactly the board's spur comb at 1750, 1875, 2125, 2250 and 2375 MHz, "
      "correctly labelled. A 70-second file of one beam takes about 2.5 s. A live search would read spectra from a "
      "ring, with the same mechanism the Python ring module already demonstrates with CuPy. How much a search costs "
      "at Hz resolution is measured in §11.")
    N("<b>Important.</b> A search running on the Spark during a live capture once made the NIC drop 277 packets. The "
      "search loaded the shared memory bus enough that the NIC's own buffer overflowed; the runtime itself was "
      "unaffected. On the Spark, heavy GPU or memory jobs on the capture host must be throttled or run elsewhere "
      "(§10). Servers with discrete GPUs do not share this bus (§11).")

    # ------------------------------------------------------------------------------------------------ 9
    H1("Operating the system")
    CODE("lft3-board start                         # program, initialise and sync every board (~10 s)\n"
         "lft3-board status                        # health checks\n"
         "lft3-pipeline run                        # every board: capture + spectrometer + dump, until Ctrl-C\n"
         "lft3-pipeline run --source synthetic     # without a board: noise + a tone\n"
         "lft3-pipeline status                     # every ring on this host: rate, loss, readers\n"
         "lft3-pipeline trigger --label test       # raw-voltage dump of every board\n"
         "lft3-pipeline stop                       # readers first (files closed), then capture\n"
         "lft3-search --watch                      # hyperseti on finished files\n"
         "lft3-view                                # browser viewer on port 8687")
    P("A healthy run prints, every 10 s per board: 60.8 Gb/s, no increase in partial or skipped blocks or missing "
      "packets, and readers at lag 0 with no overruns. Every run gets a directory with one log per process, the exact "
      "container commands, a time series of all counters, and the NIC's drop and pause counters. Those NIC counters "
      "separate loss on the host from loss before the host: if they stay flat, the packets never arrived. The same "
      "counters can be served to Prometheus.")
    P("Containers: the capture container gets network access with only the capabilities DAQIRI needs, raw network "
      "access, locked memory and the RDMA devices, and is not privileged. Readers run as the invoking user with no "
      "network at all, and talk to the capture only through shared memory and the GPU. All binaries are built in one "
      "container image with CUDA 13. No step needs root on the host.")

    # ------------------------------------------------------------------------------------------------ 10
    H1("Performance and capacity")
    H2("Cost of one board")
    FIGURE("c1_kernels", "GPU time per 6.71 ms block of one board's two beams, measured with Nsight Systems on an "
                         "otherwise idle GPU.", width=13.5 * cm)
    P("On its own, one board uses well under a tenth of the GPU: the scatter takes 0.50 ms and the fine spectrometer "
      "0.30 ms per 6.71 ms block. On the CPU it uses one core busy-polling for DAQIRI, about 4 % of the capture "
      "thread's core, and about 18 % of a core for the spectrometer. Memory is the scarcer resource. By default each "
      "board holds about 23 GB: the 15 GB ring, the 7.5 GB pinned dump window and 0.5 GB of DAQIRI buffers.")
    FIGURE("c4_memory", "Estimated memory traffic per board and for several boards, against the usable bandwidth of "
                        "the Spark's shared memory.", width=13.5 * cm)
    BG("why memory bandwidth, not GPU load, is the limit", "On the Spark the NIC, CPU and GPU share one 273 GB/s memory. "
       "Every packet byte is written by the NIC, read and written again by the scatter, read by each reader, and "
       "possibly copied by a dump: about 30 GB/s per board in total. A kernel that waits for memory still shows as "
       "busy in GPU utilisation, so utilisation rises faster than the useful work as boards are added. The NIC is the "
       "most fragile participant: if its DMA writes cannot get to memory fast enough, its small on-chip buffer "
       "overflows and packets are dropped, whatever the GPU is doing.")
    H2("How many boards fit on one Spark")
    FIGURE("c2_capacity", "Capacity test: N simultaneous real-time synthetic streams, each a full board with a "
                          "recording spectrometer, plus N × 7.6 GB/s of memory writes standing in for the NICs' DMA.",
           width=13.5 * cm)
    P("The capacity benchmark runs N board streams at once. It counts as a pass when every capture keeps real time, "
      "no reader overruns, and reader lag stays under a quarter of the ring. Five streams pass, with readers needing "
      "at most 2.5 ms of each 6.71 ms block. Six do not: the spectrometers fall behind. Even then, every capture kept "
      "real time and lost nothing, which is the isolation working as designed. GPU utilisation grows faster than "
      "linearly because all streams compete for the same memory bus, which slows every kernel.")
    P("Two limits lie outside this test. <b>The network</b>: the Spark's two QSFP ports take one board each, so two "
      "boards per Spark is what is known to work; four would need a switch and has not been tested. <b>The memory bus "
      "seen by the NIC</b>: the synthetic test has no NIC, but live, a search on the same machine made the NIC drop "
      "packets. Capacity planning has to leave memory-bus headroom, not just GPU headroom.")
    TABLE([["Configuration", "Boards per Spark", "Sparks for 6 boards", "Comment"],
           ["Two boards per Spark", "2", "3", "one board per QSFP port, known to work; about 46 % GPU, room for "
                                              "throttled searches"],
           ["Four boards per Spark", "4", "2", "fits the compute at 87 % GPU but needs a switch, untested, and leaves "
                                               "little for searches"],
           ["Hz resolution, 1.16 Hz", "about 1", "about 6", "the FFT alone runs at 3.1× real time per beam on the GB10; "
                                                            "the drift search adds more (§11)"]],
          [0.26, 0.15, 0.17, 0.42], "Capacity of DGX Sparks for the full station of six boards, twelve dual-polarisation "
                                    "beams.")

    H2("Comparison with rawspec")
    FIGURE("c3_rawspec", "rawspec, Breakthrough Listen's standard fine channeliser, against our spectrometer, per "
                         "block of one beam, on a log scale.", width=13.5 * cm)
    P("Breakthrough Listen and the related SETI backends also form fine channels with a plain FFT after the FPGA's "
      "polyphase filterbank, as we do. rawspec is their standard tool: cuFFT with load and store callbacks, built for "
      "very long FFTs. Built for this GPU and run on a GUPPI RAW dump from our system, it produces the same spectra "
      "as ours after its 1/127 sample scaling, with a median relative difference of 7.6 × 10<super>−8</super>. At "
      "128 points our fused kernel is about 60× faster. At 2<super>17</super> points rawspec cannot keep up with even "
      "one beam on the GB10. A plain cuFFT at that length runs at 3.1× real time per beam before detection, which "
      "sets the cost of the Hz-resolution mode.")

    # ------------------------------------------------------------------------------------------------ 11
    H1("Sizing the full station")
    P("The Sparks are development machines. This chapter scales the measurements above to the full station of six "
      "boards, 366 Gb/s, on servers with discrete GPUs, and finds what drives the cost: the fine resolution, and above "
      "all the narrowband drift search. NVIDIA's top datacentre GPUs and servers are left out; the candidates are the "
      "RTX PRO Blackwell cards, with the L40S and L4 for comparison.")
    H2("A common unit of cost")
    P("Every stage measured so far is limited by memory bandwidth, not arithmetic: the scatter at about 200 GB/s, the "
      "warp FFT at 167 GB/s, cuFFT at long lengths, and the hyperseti dedoppler kernel. A 2<super>17</super>-point "
      "FFT of a whole board needs about 320 GFLOP/s, a fraction of a percent of a modern GPU. So costs are expressed "
      "in <b>GB10-equivalents</b>: the fraction of one Spark GPU, with its 273 GB/s, that a stage keeps busy. They "
      "are moved to other GPUs by the ratio of memory bandwidths. The GB10 measurements already include the "
      "contention of a shared memory bus, so the scaling is slightly conservative for discrete cards.")
    TABLE([["GPU", "Memory", "Bandwidth", "GB10-eq.", "Price, approximate"],
           ["DGX Spark (GB10)", "128 GB shared", "273 GB/s", "1.0", "about $4k per Spark"],
           ["L4", "24 GB", "300 GB/s", "1.1", "about $2.5k"],
           ["L40S", "48 GB", "864 GB/s", "3.2", "$7–9k"],
           ["RTX PRO 4500 Blackwell", "32 GB", "896 GB/s", "3.3", "about $3k"],
           ["RTX PRO 5000 Blackwell", "48 or 72 GB", "1344 GB/s", "4.9", "$5–8k"],
           ["RTX PRO 6000 Blackwell Server", "96 GB", "about 1600 GB/s", "5.9", "$8–14k"]],
          [0.3, 0.16, 0.18, 0.12, 0.24], "Candidate GPUs, compared by memory bandwidth. Prices are November 2026 "
                                         "street prices and move quickly.")
    P("Consumer GeForce cards are excluded. Their driver licence restricts datacentre use, there are no passively "
      "cooled server versions, and they do not support GPUDirect RDMA, which lets the network card write packets "
      "straight into GPU memory. DAQIRI uses that path on discrete GPUs.")

    H2("Cost per board")
    TABLE([["Stage", "GB10-eq. per board", "Basis"],
           ["Capture, scatter, 1.2 kHz spectra and quick-look", "0.15", "measured"],
           ["Fine channeliser at 2<super>17</super> points, 1.16 Hz, detection fused into the FFT", "0.75",
            "measured cuFFT 0.63 plus an estimate for detection"],
           ["Same, with separate convert, FFT, detect and accumulate passes", "2.3", "measured"],
           ["Drift search with hyperseti: 32 spectra of 10 s, ±4 Hz/s, full band", "2.1", "measured"],
           ["Wideband search: detection and dedispersion of the coarse data", "0.3 (0.1–1)", "estimate"],
           ["RFI flagging on the fine data", "0.05", "estimate"],
           ["<b>Interim system at 1.2 kHz</b>", "<b>about 0.5</b>", ""],
           ["<b>Full band at 1.16 Hz with the drift search</b>", "<b>about 3.3</b>", ""]],
          [0.58, 0.18, 0.24], "Cost per board, two dual-polarisation beams. The VHF board counts at half, because "
                              "two of its four inputs are padding.")
    P("Two measurements were added for this study. A sweep of cuFFT over all lengths from 2<super>7</super> to "
      "2<super>20</super> points shows a step: up to 2<super>14</super> points one pass through memory costs 0.27 "
      "GB10-equivalents per board; from 2<super>15</super> two passes cost 0.63–0.65. And hyperseti's brute-force "
      "dedoppler kernel was timed on synthetic data with 1.16 Hz channels and 10 s spectra, with an injected "
      "drifting tone that it found every time:")
    TABLE([["Spectra per search", "Window", "Drift range", "Drift trials", "Speed", "GB10-eq. per board"],
           ["16", "160 s", "±4 Hz/s", "191", "6.2 M channels/s", "1.6"],
           ["32", "320 s", "±4 Hz/s", "383", "2.5 M channels/s", "2.1"],
           ["64", "640 s", "±4 Hz/s", "767", "0.7 M channels/s", "3.6"],
           ["32", "320 s", "±1 / ±10 Hz/s", "255 / 447", "3.5 / 2.1 M channels/s", "1.5 / 2.4"]],
          [0.16, 0.1, 0.16, 0.15, 0.23, 0.2], "hyperseti on the GB10. A board at 1.16 Hz has 1.61 × 10<super>9</super> "
                                              "Stokes-I channels.")
    P("hyperseti tests every drift up to the window length and then doubles its step. Its cost therefore depends only "
      "weakly on the drift range and grows roughly with the number of spectra per search. At Hz resolution the "
      "search costs about three times as much as the channeliser. Its kernel reaches about 3 × 10<super>10</super> "
      "cell sums per second, well below what the GPU can do, so a better kernel may be several times faster; that "
      "is not yet measured.")

    H2("Cost against fine resolution")
    FIGURE("c6_sizing_cost", "Cost per board against fine channel width, by stage, for a 32-spectrum search window "
                             "of 10 s spectra at ±4 Hz/s. Dashed: what one and two RTX PRO 6000 carry at 70 % load. "
                             "The dot marks 1.16 Hz.", width=14 * cm)
    FIGURE("c7_sizing_gpus", "RTX PRO 6000 GPUs for the six boards at 70 % planned load, against fine channel width, "
                             "for three search windows, and with only a quarter of the band at fine resolution.",
           width=14 * cm)
    B("<b>Down to about 20 Hz, compute is cheap.</b> Capture, the FFT and the wideband estimate dominate, and two "
      "GPUs carry the station. GPU memory, not processing, sets that count: the default 2 s ring is 15 GB per board.",
      "<b>Below about 5 Hz the drift search takes over</b>, and it grows faster than the number of channels: halving "
      "the channel width doubles the channels and also widens the drift range in channels. At 1.16 Hz the search is "
      "about 60 % of the cost.",
      "<b>The search window matters more than the drift range.</b> At 1.16 Hz, 64 spectra per window instead of 32 "
      "takes the station from 6 to 11 GPUs; ±1 instead of ±10 Hz/s changes the cost by less than 2×.",
      "<b>0.58 Hz and finer</b> needs 11–17 GPUs or more, and GPU memory per board passes 50 GB because the ring must "
      "hold two FFT lengths.",
      "<b>The cheapest lever is the band.</b> Hz resolution on a quarter of the band costs 1.2 GB10-equivalents per "
      "board, less than the full band at 4.7 Hz, and the station then needs 2 GPUs.",
      "<b>A faster search kernel pays most.</b> Four times faster takes the 1.16 Hz station from 6 GPUs to 3; twice "
      "as fast is not enough to fit two boards on one GPU.")

    H2("Memory, network and storage")
    B("<b>GPU memory</b> at 1.16 Hz is about 31 GB per board: the ring, the Stokes-I accumulator and output buffer, "
      "and FFT and search workspace. A 96 GB card holds one board with plenty of room.",
      "<b>Host memory</b> is driven by two science choices. The search window costs about 155 GB per board at 16-bit "
      "precision. A raw-beam buffer for transients costs 7.5 GB per second of buffer per board. A dispersed pulse at "
      "a dispersion measure of 1000 sweeps 0.3–1.24 GHz in 43 s, so a long buffer means a terabyte of RAM per "
      "server. It cannot go on NVMe: writing 7.5 GB/s continuously wears out flash in weeks.",
      "<b>PCIe</b> carries 7.6 GB/s per board from network card to GPU, about 15 % of a Gen5 x16 slot.",
      "<b>Storage</b> is driven by the trigger rate, not by the spectra. The 1.2 kHz spectra are about 2.3 TB per day "
      "for the station. Full-band Hz spectra would be 56 TB per day per board and cannot be kept; only hits and short "
      "excerpts are. A triggered dump of all six boards is about 71 GB: 1.7 TB per day at one per hour, 10 TB per day "
      "at one per ten minutes.")

    H2("Recommended configuration")
    TABLE([["Item", "Per server", "Station"],
           ["Servers", "2U, one AMD EPYC processor with 24–32 cores", "3, each serving 2 boards"],
           ["GPU", "2 × RTX PRO 6000 Blackwell Server, one per board", "6, each about 57 % loaded at 1.16 Hz"],
           ["Network", "one ConnectX card with 2 × 100 GbE, cabled directly to two boards", "6 direct links, no switch"],
           ["Memory", "512 GB for a 10 s raw buffer; 768 GB–1 TB for 30 s", ""],
           ["Disk", "system mirror and 4 × 7.68 TB NVMe for staging", "plus about 0.5 PB central storage"],
           ["Approximate cost", "$40–60k", "$150–200k, plus a head node and storage"]],
          [0.18, 0.5, 0.32], "Recommended production configuration: full band at 1.16 Hz with the drift search, with "
                             "about 40 % GPU headroom.")
    P("The same hardware runs the interim 1.2 kHz system at under 10 % GPU load, so it can be bought before the fine "
      "resolution is settled. A fourth identical server would serve as a spare for any two boards, a development "
      "machine, and capacity for reprocessing. A cheaper RTX PRO 5000 per board also fits at 1.16 Hz, but with no "
      "headroom.")
    H3("One large server behind a switch")
    P("All six boards can also feed one 8-GPU server through a switch. Bandwidth is not the obstacle: 366 Gb/s, or "
      "426 Gb/s when every board dumps, fits on three 200 GbE links with two boards each, and each GPU receives only "
      "7.6 GB/s. The constraints lie elsewhere:")
    B("about 160 PCIe lanes are needed, more than one processor provides, so the server needs two processors or PCIe "
      "switches. Each network card must sit under the same PCIe switch as the GPUs it feeds, never across the link "
      "between the two processors;",
      "1.4–2.2 TB of RAM and 5–6 kW in one 4U chassis;",
      "one server or switch fault stops all six boards.")
    P("A switch also brings requirements from the firmware. A board sends to the broadcast address until its "
      "destination is set, and again after every reprogram, so each board's port must be isolated in its own VLAN or "
      "it floods 61 Gb/s to every port. The switch must not forget the servers' addresses, which receive but hardly "
      "send. Each board needs its own source address. The boards appear to ignore pause frames, so no server link may "
      "run above about 70 %. The cost is about the same as the three-server design. A switch in front of two or three "
      "servers, which can move boards between servers, is the better use of one.")
    H3("Before buying")
    NL("Port the runtime to an x86 server with a discrete GPU: build for that architecture, receive with GPUDirect "
       "into GPU memory, and add explicit copies wherever the CPU now reads GPU data directly through unified memory.",
       "Rent or borrow one RTX PRO 6000 server for a week. Run the capacity and search benchmarks, check the scaling "
       "by memory bandwidth, and confirm GPUDirect RDMA from the network card.",
       "Benchmark the wideband search, the least certain cost.",
       "Write and measure the fused Hz-resolution channeliser.",
       "Settle the science choices that move the numbers: the band at Hz resolution, the search window, the raw "
       "buffer length and the trigger rate.")

    # ------------------------------------------------------------------------------------------------ 12
    H1("Design decisions")
    TABLE([["Decision", "Evidence"],
           ["Own ring runtime instead of CyberEther", "CyberEther's lock-step scheduler lets a slow consumer stall the "
                                                     "receiver; its DSP blocks ran at 0.59× real time"],
           ["Not Holoscan 5.0 for now", "decoupling works in a test, but 5.0 is a fresh rewrite with Beta APIs, and "
                                        "DAQIRI still targets 4.x"],
           ["Not hashpipe; keep its conventions", "its semaphore handshake is back-pressure, one consumer per buffer; "
                                                  "GUPPI RAW and observation keys kept"],
           ["Ring of 50 MB blocks", "time-contiguous rows: scatter 6 GB/s, limited by the TLB; blocks: 83 GB/s at any "
                                    "ring size, long-FFT gather 232 GB/s"],
           ["CUDA VMM memory shared by file descriptor", "registered shared memory reads 172 GB/s against 237 GB/s; the "
                                                        "VMM import keeps full bandwidth in other processes"],
           ["Writer never waits; seqlock and overrun counts", "6-stream overload test: captures stayed lossless while "
                                                             "readers overran"],
           ["Backpressure margin of 8 slots, tests only", "parity run found lossless readers skipping blocks with "
                                                          "margin 0"],
           ["Quick-look derived from fine spectra", "a separate reader cost 0.23 ms and 7.5 GB/s per block; Parseval "
                                                    "gives it for free, agreeing to 2.6 × 10<super>−6</super>"],
           ["Warp FFT kernel", "shared-memory kernel barrier-bound at 0.50 ms; warp kernel 0.30 ms, verified against "
                               "a DFT"],
           ["Integration windows on absolute block numbers", "one time grid for all readers and boards"],
           ["Capture container not privileged", "three capabilities verified live at line rate"],
           ["Plain FFT, no fine PFB", "same choice as rawspec and seticore; affordable later if leakage matters"],
           ["Production: one RTX PRO 6000 per board, 3 servers", "all stages bandwidth-bound; 3.3 GB10-equivalents per "
                                                                 "board at 1.16 Hz against 4.1 per card at 70 %"]],
          [0.38, 0.62], "The main design decisions.")
    P("These choices were validated end to end: identical output to the CyberEther implementation, to float "
      "rounding, on deterministic synthetic data; unit tests for every component in C++, CUDA and Python; and a "
      "2-hour live soak with a DDR dump and a raw-voltage dump every five minutes. The soak had no loss of the "
      "system's own making; the earlier rare gaps at DDR dumps did not recur.")

    # ------------------------------------------------------------------------------------------------ 13
    H1("Limitations and next steps")
    B("<b>Second board on one Spark</b>: the configuration supports it; the board's network addresses must be "
      "confirmed once it is cabled, then a two-board soak run.",
      "<b>Input to beam and polarisation mapping</b>: only one connector is mapped so far; the pairing assumed by the "
      "layout needs an injected tone on each input.",
      "<b>Time reference</b>: t = 0 is estimated from packet arrival, to about a millisecond. A sync aligned to the "
      "PPS in firmware would make timestamps exact. The board clock also reads about −24 ppm against the temporary "
      "PPS.",
      "<b>Hz resolution</b>: needs a fused channeliser that gathers 64–128 blocks per channel, and the science "
      "choices of §11.",
      "<b>Production hardware</b>: port the runtime to x86 with GPUDirect, then test on one RTX PRO 6000 server "
      "before buying (§11).",
      "<b>Live search</b>: today on finished files. Next: a reader on a spectra ring, a faster dedoppler kernel, and "
      "heimdall for wideband transients fed from the coarse data.",
      "<b>Pause frames</b>: test whether the board honours them; if it did, NIC buffer overflows would become short "
      "delays instead of loss.",
      "<b>Flags</b>: missing data is counted per spectrum; a channel and time mask is the next step, seeded from the "
      "known-RFI list.")

    # ------------------------------------------------------------------------------------------------ appendix
    H1("Appendix: repository map, file locations and glossary")
    TABLE([["Path", "What"],
           ["config/station.yaml", "the boards of the station and the data directory"],
           ["config/boards/&lt;board&gt;.yaml", "one board: bitstream, clocks, NCO, inputs, network, receiver link and "
                                               "cores, timing"],
           ["config/known_rfi.json", "known RFI, used by the viewer and the search labels"],
           ["src/lft3/board/", "board control, telemetry, captures, sensors (lft3-board)"],
           ["src/lft3/pipeline/, src/lft3/cli/", "run planner and lft3-pipeline"],
           ["src/lft3/ring.py", "Python view of the rings: status, metrics, triggers, CuPy access"],
           ["src/lft3/search/", "lft3-search (hyperseti)"],
           ["src/lft3/products/", "readers and plots for FBH5 and captures (lft3-plot)"],
           ["pipeline/src/capture/", "lft3-capture: assembler, scatter kernel, DAQIRI setup, pcap and synthetic "
                                     "sources"],
           ["pipeline/src/common/", "packet format, block layout, ring protocol"],
           ["pipeline/src/spectrometer/", "fused FFT kernels, FBH5 writer, lft3-spectrometer"],
           ["pipeline/src/dump/", "GUPPI RAW writer, lft3-dump"],
           ["pipeline/bench/", "ring, capacity, rawspec and hyperseti benchmarks"],
           ["tools/viewer/", "lft3-view"],
           ["docs/user_guide.md", "operating the system"],
           ["docs/runtime.md", "runtime implementation, design decisions and measurements"],
           ["docs/sizing.md, docs/sizing_figures/", "station sizing, the sizing model and an interactive calculator"],
           ["docs/system_overview/", "this document: text in build.py, diagrams in figures.py"]],
          [0.36, 0.64], "Where things are in the repository.")
    TABLE([["Item", "Location"],
           ["Fine spectra", "&lt;data&gt;/spectra/&lt;board&gt;/&lt;board&gt;_&lt;UTC&gt;_beam&lt;N&gt;.h5"],
           ["Coarse spectra", "&lt;data&gt;/spectra/&lt;board&gt;/quicklook/"],
           ["Latest snapshot", "&lt;run&gt;/latest/&lt;board&gt;_latest.json and .f32"],
           ["Raw voltages", "&lt;data&gt;/dumps/&lt;board&gt;/…_beam&lt;N&gt;.0000.raw, with a JSON sidecar"],
           ["Search hits", "&lt;data&gt;/hits/&lt;board&gt;/&lt;file&gt;.csv"],
           ["Run records", "&lt;data&gt;/runs/&lt;UTC&gt;_&lt;board&gt;_&lt;source&gt;/: logs, plan.txt, "
                           "status.jsonl, nic-&lt;link&gt;.csv, triggers.log"],
           ["Ring control block", "/dev/shm/lft3-&lt;board&gt;-&lt;link&gt;"],
           ["Ring data socket", "/dev/shm/lft3-&lt;board&gt;-&lt;link&gt;.sock"],
           ["FBH5 per-spectrum data", "/lft3_timestamp, /lft3_missing_packets; lft3_* root attributes"]],
          [0.3, 0.7], "File locations. &lt;data&gt; is the data directory of the station file.")
    TABLE([["Term", "Meaning"],
           ["block", "1024 consecutive spectra of one board (6.71 ms); the unit of capture, publication and reading"],
           ["bindex (b)", "which 1024-channel slice of a spectrum a packet carries (0–5)"],
           ["burst", "a batch of up to 1024 received packets handed over by DAQIRI"],
           ["coarse channel", "one of the board's 6144 PFB channels, 152.6 kHz wide"],
           ["DAQIRI", "NVIDIA's high-rate packet I/O library, raw ibverbs on ConnectX cards"],
           ["FBH5", "filterbank data in HDF5, the Breakthrough Listen spectra format"],
           ["fine channel", "one FFT bin within a coarse channel; 1.192 kHz with a 128-point FFT"],
           ["GB10-equivalent", "the fraction of one DGX Spark GPU a stage keeps busy; the sizing unit of §11"],
           ["GUPPI RAW", "raw-voltage file format: 80-character header cards and blocks of [channel][time][pol] "
                         "samples"],
           ["link, stream", "one board's 100 GbE connection, with its own capture process and ring"],
           ["overrun", "a block a reader lost because the writer reused its slot first"],
           ["ring, slot", "the per-board circular buffer of blocks in GPU memory; one block-sized position in it"],
           ["seqlock", "versioned read: check a sequence number before and after reading to detect concurrent "
                       "overwrites"],
           ["sync", "board command that restarts t at 0 and applies pending header settings"],
           ["t", "spectrum counter since the last sync, one per 6.5536 µs"],
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
