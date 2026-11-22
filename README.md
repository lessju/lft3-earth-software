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
  ring.py                  Python view of the runtime's rings (status, metrics, triggers, CuPy access, readers)
  products/                readers + plots for spectra (FBH5) and board captures (HDF5)
  monitoring/              metrics export (Prometheus text, JSON lines), NIC counters
  search/                  drift search: live on the spectra ring, and lft3-search on files (search venv)
  config.py                configuration loader (station, boards, observations)
  cli/                     console scripts: lft3-board, lft3-pipeline, lft3-plot, lft3-pcap
tests/                   pytest (fake casperfpga board; tests/hardware/ needs the live board)
pipeline/                C++/CUDA real-time runtime: lft3-capture → GPU voltage ring → lft3-spectrometer, lft3-dump
config/                  instrument configuration, the single source of truth
  station.yaml             which boards make up the station, data directory, observation in use
  observations/            how data is processed: resolution, integration, products, search, triggers
  boards/                  one YAML file per RFSoC board
  daqiri/                  DAQIRI receive configurations / benchmarks
scripts/                 host setup shell scripts (100G link, NIC counters)
tools/viewer/            lft3-view: fast C++ browser viewer for FBH5 spectra (pan/zoom waterfalls)
docs/                    design and operations documents (start with user_guide.md, operations.md)
third_party/             external dependencies (not vendored) and how to build them
```

Planned additions follow the subsystems of `docs/full_architecture.pdf`: observation management and triggering
as Python subpackages, and further ring readers (heimdall for wideband transients).

## Getting started

```bash
make install            # pip install -e '.[dev]' into /opt/lft3arth/lft3arth_venv (Python 3.8 + casperfpga)
make viewer             # build lft3-view; install and viewer link it and lft3-search into the venv's bin/
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
lft3-pipeline run [--source network|synthetic|file]   # → fine + coarse FBH5 spectra and live search hits, per link
lft3-pipeline status / trigger / stop               # watch, raw-voltage dump, stop
lft3-search FILE.h5                                 # hyperseti on a finished file (scripts/setup_search_env.sh)
lft3-plot waterfall $(lft3-plot latest) --fmin 2400 --fmax 2425 --flatten
```

## Documents

| Document | Contents |
|---|---|
| `docs/user_guide.md` | **Start here:** running the real-time system: setup, config, runs, products, troubleshooting, capacity |
| `docs/operations.md` | Day-to-day commands for boards, captures, pipeline runs and plots |
| `docs/system_overview/lft3_system_overview.pdf` | **How the whole system works**, with diagrams (PDF; source in the same folder) |
| `docs/runtime.md` | How the real-time runtime is implemented, the design decisions behind it, performance and capacity |
| `docs/monitoring_control.md` | RFSoC M&C: CLI, health checks, metrics, open decisions |
| `docs/packet_format.md` | Channel and ADC packet formats as implemented |
| `docs/sizing.md` | Backend sizing for the full station: GPUs, servers, RAM, storage (from the Spark measurements) |
| `docs/architecture.md` | The original DGX Spark plan: data flow, Hz-resolution sizing and output volume |
| `docs/bringup.md` | Link → NIC → capture → receiver bring-up checklist |
| `docs/firmware_requests.md` | Requested firmware changes (for the firmware developer) |
| `docs/full_architecture.pdf` | Long-term system design notes |

## Status

| Component | State |
|---|---|
| Packet decoder, pcap tools, reference spectrometer | unit-tested |
| Board M&C (`lft3-board`) | station-wide; all commands tested on the live board (start, status, snapshots, DDR dump, MAC, sync) |
| Pipeline runner, plots (`lft3-pipeline`, `lft3-plot`) | network / synthetic / file sources tested live; status, triggers, metrics |
| Real-time runtime (`pipeline/`) | live at 60.8 Gb/s without loss (2 h and 1 h soaks); fine + coarse FBH5, GUPPI RAW dumps; 5 streams per Spark measured; resolution/integration benchmark |
| Search (`lft3-search`) | live: hyperseti on the spectra in GPU memory during the run, zero-drift hits dropped (identical windows and hits to the file search); triggers to the dump built, off until the rules are agreed |
| Observation config (`config/observations/`) | resolution, integration, products, search, triggers: one source of truth for runs and searches |
| Hz-resolution channeliser, hit clustering, coordinator | planned (rawspec is too slow on the GB10 at 2^17: docs/runtime.md §2) |
