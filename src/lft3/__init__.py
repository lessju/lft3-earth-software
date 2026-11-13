"""LFT3-Earth host software.

Subpackages (mirroring docs/full_architecture.pdf):
  lft3.packets      RFSoC packet formats, pcap I/O, synthetic packets          (data transport & capture)
  lft3.dsp          reference ("golden model") signal processing                (downstream processing)
  lft3.board        RFSoC 4x2 monitoring & control over casperfpga             (backend M&C)
  lft3.monitoring   metrics export (Prometheus text format, JSON lines)        (system monitoring)
  lft3.config       instrument configuration: the single source of truth       (configuration management)
  lft3.cli          command-line entry points (lft3-board, lft3-pcap)

The real-time GPU runtime is the C++/CUDA code in pipeline/ (see docs/runtime.md, docs/user_guide.md).
"""
__version__ = "0.2.0"
