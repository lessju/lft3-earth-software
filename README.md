# lft3-earth-software

Host software for the LFT3-Earth digital backend: RFSoC 4x2 F-engine boards stream 152.6 kHz channels
over 100GbE to an NVIDIA DGX Spark, which receives them, channelises them further, and writes spectra.
This repository also monitors and controls the boards.

## Layout

```
src/lft3/                Python package (Python >= 3.8: it shares the casperfpga environment)
  packets.py               RFSoC packet formats, pcap reader/writer, synthetic packets
  dsp/                     reference ("golden model") signal processing (finespec.py)
  board/                   RFSoC monitoring & control: station (all boards), control, telemetry, captures
  pipeline/                run planner for the real-time runtime (lft3-pipeline)
  ring.py                  Python view of the runtime's voltage rings (status, metrics, triggers, CuPy access)
  products/                readers + plots for spectra (FBH5) and board captures (HDF5)
  monitoring/              metrics export (Prometheus text, JSON lines)
  config.py                instrument configuration loader (station + boards)
  cli/                     console scripts: lft3-board, lft3-pipeline, lft3-plot, lft3-pcap
tests/                   pytest (fake casperfpga board; tests/hardware/ needs the live board)
pipeline/                C++/CUDA real-time runtime: lft3-capture → GPU voltage ring → lft3-spectrometer, lft3-dump
config/                  instrument configuration, the single source of truth
  station.yaml             which boards make up the station, data directory
  boards/                  one YAML file per RFSoC board
  daqiri/                  DAQIRI receive configurations / benchmarks
scripts/                 host setup shell scripts (100G link, NIC counters)
tools/viewer/            lft3-view: fast C++ browser viewer for FBH5 spectra (pan/zoom waterfalls)
docs/                    design and operations documents (start with user_guide.md, operations.md)
third_party/             external dependencies (not vendored) and how to build them
```

Planned additions follow the subsystems of `docs/full_architecture.pdf`: `lft3.observation` (observation
management) and `lft3.triggering` as Python subpackages, and further ring readers in `pipeline/` or Python
(search paths: hyperseti, heimdall).

## Getting started

```bash
make install            # pip install -e '.[dev]' into /opt/lft3arth/lft3arth_venv (Python 3.8 + casperfpga)
make test               # Python unit tests, no hardware
make lint               # ruff
make pipeline           # build the runtime in the lft3-runtime container
make pipeline-test      # ... and run the C++/CUDA tests on the GPU
make test-hardware      # read-only checks against the live board
```

Operating the system (`docs/operations.md`):

```bash
lft3-board start                   # program + initialise every board of the station, sync them together
lft3-board status                  # health of every board
lft3-board snapshot fft --plot     # PFB spectra / `snapshot adc` / `capture` (triggered DDR dump)
lft3-pipeline run [--source network|synthetic|file]   # → fine + coarse FBH5 spectra, per link
lft3-pipeline status / trigger / stop               # watch, raw-voltage dump, stop
lft3-plot waterfall $(lft3-plot latest) --fmin 2400 --fmax 2425 --flatten
```

## Documents

| Document | Contents |
|---|---|
| `docs/user_guide.md` | **Start here:** running the real-time system: setup, config, runs, products, troubleshooting, capacity |
| `docs/operations.md` | Day-to-day commands for boards, captures, pipeline runs and plots |
| `docs/runtime.md` | How the real-time runtime is implemented, the design decisions behind it, performance and capacity |
| `docs/monitoring_control.md` | RFSoC M&C: CLI, health checks, metrics, open decisions |
| `docs/packet_format.md` | Channel and ADC packet formats as implemented |
| `docs/architecture.md` | Original DGX Spark sizing and the rawspec plan for Hz resolution |
| `docs/bringup.md` | Link → NIC → capture → receiver bring-up checklist |
| `docs/firmware_requests.md` | Requested firmware changes (for the firmware developer) |
| `docs/full_architecture.pdf` | Long-term system design notes |

## Status

| Component | State |
|---|---|
| Packet decoder, pcap tools, reference spectrometer | unit-tested |
| Board M&C (`lft3-board`) | station-wide; all commands tested on the live board (start, status, snapshots, DDR dump, MAC, sync) |
| Pipeline runner, plots (`lft3-pipeline`, `lft3-plot`) | network / synthetic / file sources tested live; status, triggers, metrics |
| Real-time runtime (`pipeline/`) | live at 60.8 Gb/s, no loss; fine + coarse FBH5, GUPPI RAW dumps; 4–5 streams per Spark measured |
| Hz-resolution (rawspec-style) path, searches, coordinator | planned |
