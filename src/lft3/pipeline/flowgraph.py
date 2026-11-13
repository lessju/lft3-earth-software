"""Build CyberEther flowgraphs for the real-time pipeline (pipeline/, the lft3.cep plugin) from the board
configuration and a few run options, instead of hand-editing YAML.

    opts = PipelineOptions(source="network", fft_length=128, integration_s=1.0, monitor=True)
    yaml_text = build_flowgraph(board_cfg, opts, output_dir="/opt/lft3arth/data/spectra/rfsoc0")

Sources:
  network    live stream from the board through DAQIRI (container runs privileged, as root)
  synthetic  generated packets (noise + a tone), no board needed
  file       replay a tcpdump capture of the channel stream (classic pcap), optionally looped
"""
from __future__ import annotations

from dataclasses import dataclass

import yaml

from lft3.config import BoardConfig
from lft3.packets import CH_PER_PKT, NCHAN, SPECTRUM_RATE

SOURCES = ("network", "synthetic", "file")


@dataclass
class PipelineOptions:
    source: str = "network"
    file: str | None = None               # source=file: pcap path
    loop: bool = False                    # source=file: replay forever
    real_time: bool = True                # synthetic/file: pace at the real spectrum rate
    fft_length: int = 128                 # fine FFT length (128 -> 1.19 kHz channels)
    integration_s: float = 1.0
    channel_start: int = 0                # coarse channels, multiples of 1024
    channel_count: int = NCHAN
    monitor: bool = False                 # add the quick-look display branch (lineplot + waterfall per beam)
    monitor_integration_s: float = 0.1
    spectra_per_file: int = 3600
    record: bool = True
    tone_channel: int = 4000              # synthetic source
    tone_input: int = 1
    tone_frequency: float = 0.1015625
    drop_fraction: float = 0.0

    def validate(self) -> None:
        if self.source not in SOURCES:
            raise ValueError(f"source must be one of {SOURCES}")
        if self.source == "file" and not self.file:
            raise ValueError("source=file needs a capture file")
        if self.fft_length < 2 or self.fft_length & (self.fft_length - 1) or self.fft_length > 2048:
            raise ValueError("fft_length must be a power of two, 2..2048 (fused spectrometer limit)")
        if self.channel_start % CH_PER_PKT or self.channel_count % CH_PER_PKT or self.channel_count <= 0 \
                or self.channel_start + self.channel_count > NCHAN:
            raise ValueError("channels must be whole packets: start/count multiples of 1024 within 0..6144")
        if self.integration_s <= 0 or self.monitor_integration_s <= 0:
            raise ValueError("integration times must be positive")

    @property
    def segments(self) -> int:
        return max(1, 1024 // self.fft_length)        # blocks of >= 1024 spectra (6.7 ms)

    @property
    def spectra_per_block(self) -> int:
        return self.fft_length * self.segments

    def blocks_for(self, seconds: float) -> int:
        return max(1, round(seconds * SPECTRUM_RATE / self.spectra_per_block))

    @property
    def tsamp(self) -> float:
        return self.blocks_for(self.integration_s) * self.spectra_per_block / SPECTRUM_RATE


def _node(name: str, module: str, config: dict | None = None, inputs: dict | None = None,
          device: str = "cuda", meta: tuple | None = None) -> dict:
    n = {"name": name, "module": module, "device": device, "runtime": "native", "provider": "generic"}
    if config:
        n["config"] = config
    if inputs:
        n["input"] = inputs
    if meta:
        x, y, w, h = meta
        n["meta"] = {"node": {"x": x, "y": y, "width": w, "height": h, "configCollapsed": True}}
    return n


def build_flowgraph(board: BoardConfig, opts: PipelineOptions, output_dir: str) -> str:
    opts.validate()
    beams = [i.beam for i in board.inputs]
    pols = [0 if i.pol == "X" else 1 for i in board.inputs]
    receiver = {
        "source": opts.source, "sourceFile": opts.file or "", "sourceLoop": opts.loop,
        "realTime": opts.real_time,
        "syntheticToneChannel": opts.tone_channel, "syntheticToneInput": opts.tone_input,
        "syntheticToneFrequency": opts.tone_frequency, "syntheticDropFraction": opts.drop_fraction,
        "interfaceAddress": board.receiver_interface, "sourceAddress": board.source_ip,
        "udpPort": board.channel_port, "boardId": board.board_id,
        "masterCore": board.receiver_master_core, "workerCore": board.receiver_worker_core,
        "processingCore": board.receiver_processing_core,
        "packetsPerBurst": 1024, "maxConcurrentBursts": 64,
        "channelStart": opts.channel_start, "channelCount": opts.channel_count,
        "fftLength": opts.fft_length, "segments": opts.segments,
        "inputBeam": str(beams), "inputPolarization": str(pols),
        "dataType": "CI8", "zeroFill": True, "maxOpenBlocks": 4, "maxBlockLag": 2,
        "outputPoolSize": 24,                 # 160 ms of slack while downstream blocks start (50 MB each)
    }
    graph = [
        _node("receiver", "lft3_receiver", receiver, meta=(20, 20, 280, 0)),
        _node("finespec", "lft3_finespec",
              {"mode": "fine", "beams": 2, "integrationBlocks": opts.blocks_for(opts.integration_s)},
              {"buffer": "${graph.receiver.output.output}"}, meta=(340, 20, 220, 0)),
        _node("writer", "lft3_fbh5_writer",
              {"outputDirectory": output_dir, "filePrefix": board.name, "recording": opts.record,
               "ncoMHz": board.nco_mhz, "syncTimeUnix": 0, "spectraPerFile": opts.spectra_per_file,
               "sourceName": board.name},
              {"input": "${graph.finespec.output.buffer}"}, meta=(600, 20, 260, 0)),
    ]
    if opts.monitor:
        group = 6 if opts.channel_count % 6 == 0 else 1          # ~1000 display points at full band
        graph.append(_node("quicklook", "lft3_finespec",
                           {"mode": "coarse", "beams": 2, "binsPerOutput": group,
                            "integrationBlocks": opts.blocks_for(opts.monitor_integration_s)},
                           {"buffer": "${graph.receiver.output.output}"}, meta=(340, 420, 220, 0)))
        band = (f"Band fraction (0 = {-board.nco_mhz - 468.75 + opts.channel_start * 0.152587890625:.1f} MHz, "
                f"1 = {-board.nco_mhz - 468.75 + (opts.channel_start + opts.channel_count) * 0.152587890625:.1f} MHz)")
        for b, y in ((0, 420), (1, 900)):
            graph += [
                _node(f"beam{b}_slice", "slice", {"slice": f"[{b}, :, 0, 0]", "contiguous": True},
                      {"buffer": "${graph.quicklook.output.buffer}"}, meta=(600, y, 160, 0)),
                _node(f"beam{b}_host", "duplicate", {"hostAccessible": True, "outputDevice": "cpu"},
                      {"buffer": f"${{graph.beam{b}_slice.output.buffer}}"}, meta=(800, y, 160, 0)),
                _node(f"beam{b}_db", "amplitude", None, {"signal": f"${{graph.beam{b}_host.output.buffer}}"},
                      device="cpu", meta=(1000, y, 140, 0)),
                _node(f"beam{b}_range", "range", {"min": 145, "max": 180},
                      {"signal": f"${{graph.beam{b}_db.output.signal}}"}, device="cpu", meta=(1180, y, 160, 0)),
                _node(f"beam{b}_lineplot", "lineplot",
                      {"averaging": 1, "maxHold": False, "fill": True, "xLabel": band, "yLabel": f"Beam {b} (2x dB)"},
                      {"signal": f"${{graph.beam{b}_range.output.signal}}"}, device="cpu", meta=(1380, y, 900, 200)),
                _node(f"beam{b}_waterfall", "waterfall",
                      {"height": 512, "averaging": 1, "xLabel": band,
                       "yLabel": f"Time ({opts.monitor_integration_s:g} s per line)"},
                      {"signal": f"${{graph.beam{b}_range.output.signal}}"}, device="cpu",
                      meta=(1380, y + 240, 900, 220)),
            ]
    doc = {
        "version": 2,
        "title": f"LFT3 {board.name} ({opts.source})",
        "summary": (f"{opts.source} source, {opts.channel_count} coarse channels from {opts.channel_start}, "
                    f"{opts.fft_length}-point fine FFT ({152587.890625 / opts.fft_length:.1f} Hz), "
                    f"{opts.tsamp:.4f} s integrations{', with monitor' if opts.monitor else ''}. "
                    "Generated by lft3-pipeline."),
        "graph": graph,
    }
    return yaml.safe_dump(doc, sort_keys=False, width=120)
