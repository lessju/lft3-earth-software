# RFSoC monitoring and control (M&C)

Status (2026-11-12): first implementation in `src/lft3/board/` and `src/lft3/monitoring/`, driven from
`lft3-board`. Every command has run against the live board (§4.1). Unit tests use a fake board that
checks the bring-up matches the notebook step for step. Covers sections 7 ("Backend monitoring and
control") and 8 ("System management, monitoring and control") of `docs/full_architecture.pdf`.

## 1. What the architecture asks for

| Requirement (full_architecture.pdf) | Where it stands |
|---|---|
| Backend monitored and controlled through casperfpga | `lft3.board` wraps casperfpga; all register access goes through it |
| Operate many boards together (6 in the full system) | `lft3/board/station.py`: every operation on all boards in parallel, per-board results, barrier-released sync |
| Diagnostic data from the boards (spectra, ADC samples, triggered dumps) | `lft3/board/capture.py` + `lft3-board snapshot/capture` (§3.1) |
| A register map describing what each register does | `lft3/board/registers.py` (first version; firmware request #9 asks for the authoritative one) |
| Physical metrics: temperatures, voltages, currents, power | `lft3/board/sensors.py` reads the board's sysfs over SSH. **Needs an SSH key** on the board |
| Firmware metrics: error counts, packet metrics | `lft3/board/telemetry.py`: snapshots, counter rates, health checks |
| Diagnostic spectra, antenna/beam RMS | Firmware FFT/ADC snapshots (`lft3-board snapshot`); coarse quick-look spectra from the GPU runtime (docs/user_guide.md §5, §7) |
| Single source of truth for instrument configuration, in git | `config/boards/<name>.yaml`, loaded by `lft3.config` |
| Coordinator on a master node; lock against concurrent reconfiguration | Per-board lock file now. The coordinator is still to design (§6) |
| CLI first; metrics/logging/alerting (Prometheus/Grafana suggested) | `lft3-board` CLI; Prometheus `/metrics` endpoint and JSON lines |

## 2. Layers

```
config/boards/rfsoc0.yaml ──► lft3.config.BoardConfig        (what the board should be doing)
                                     │
            ┌────────────────────────┼─────────────────────────────┐
            ▼                        ▼                             ▼
   lft3.board.control       lft3.board.telemetry          lft3.board.sensors
   writes, under a lock     read-only snapshots,          rails/temperatures
   (program, clocks, MTS,   counter rates, health         from the board's sysfs
   NCO, packetizer, MAC,    checks vs the config          over SSH
   sync, trigger)                    │                             │
            │                        └──────────────┬──────────────┘
            ▼                                       ▼
        casperfpga (katcp, 192.168.2.100)   lft3.monitoring.metrics: Prometheus text / JSON lines
                                                    ▼
                                    lft3-board monitor --prometheus-port 9101
                                    (→ Prometheus → Grafana dashboards + alerts)
```

The control layer never reads health and the telemetry layer never writes. Monitoring can therefore
run continuously, from anywhere, while one operator controls the board.

## 3. Using it

```bash
lft3-board list                                  # configured boards
lft3-board status                                # one health check (two snapshots 1 s apart)
lft3-board status --arp --json                   # + destination MAC readback, machine-readable
lft3-board monitor --interval 5 --prometheus-port 9101 --jsonl /opt/lft3arth/data/monitor/rfsoc0.jsonl

lft3-board start                                 # full bring-up (reprograms!), then sync
lft3-board start --no-program                    # re-apply config to the running design
lft3-board set-dest-mac [--mac aa:bb:..]         # 100GbE destination (default from config); --show to read
lft3-board sync | trigger                        # sync pulse / manual DDR dump + re-arm
```

`-b NAME` (or a path to a YAML file) selects the board; the default is `rfsoc0`. Commands that change
the board take `/tmp/lft3-board-<name>.lock`. A second operator gets "board is being reconfigured by
pid … user … since …" instead of interleaving register writes.

`lft3-board start` performs the sequence from the bring-up notebook (`mts_ddr_8way.ipynb`, previously
`scripts/start_firmware.py`): program, set destination MAC, RFDC init + LMK/LMX, MTS, mixer event source,
NCO (fine mixers + header fields), DDR/requantiser/packet mask, PPS drift period, then a sync. Differences
from the notebook: it also writes `packetizer_fid` and `expected_clk_period` from the config. It does not
fire a DDR dump unless `--trigger` is given. It takes ~10 s including programming.

### 3.1 Station and captures

`lft3-board` works on the whole station (`config/station.yaml`) unless `-b` picks boards. `Station.each(fn)`
runs `fn(board)` on every board in its own thread and returns a `BoardResult` per board, so one unreachable
board is reported without hiding the rest. `start` initialises all boards in parallel under their locks, then
releases every board's sync write from a barrier (spread: ~ms over katcp).

Captures (`lft3/board/capture.py`, saved by `lft3.products.captures`, plotted by `lft3-plot capture`):
FFT snapshot (8 lanes x 1024 words of 24-bit I/Q → 8192 bins), ADC snapshot (2 BRAMs x 1024 words of 4 int16
I/Q pairs → 8192 samples), DDR dump (trigger, receive on UDP `adc_port`, separate boards by fid, order by
sequence number, zero-fill gaps). Tested on the live board 2026-11-12. The dump needs
`net.core.rmem_max` ≥ 256 MB (root, once); with the default 212 kB, 55% of a 64 MiB dump was dropped in the
socket (`RcvbufErrors` matched the missing count exactly; the NIC delivered everything on its own queue).

## 4. Health checks (current)

| Check | Rule | Status if broken |
|---|---|---|
| `tx_packet_rate` | 100GbE packets/s within 0.1% of `expected_packet_rate` × (bits in packet_mask)/6 | FAIL |
| `packetizer_fifo_full_cnt`, `packetizer_blocked_word_cnt` | rate 0 (board-side data loss / back-pressure) | FAIL |
| `pps` | 0.4–2.1 edges/s (whole edges counted in a ~1 s window) | WARN (INFO if `timing.pps: none`) |
| `sample_clock_vs_pps` | `raw_cycles_per_pps` within 1 ppm of 156.25e6 | WARN only if `timing.pps: reference`, else INFO |
| `expected_clk_period` | equals `timing.expected_clk_period` | WARN |
| `header_nco0..3`, `packetizer_fid`, `packetizer_packet_mask` | equal to the config | FAIL |
| `ch_lsb0..3` | equal to the config | WARN |
| `destination_mac` (with `--arp`) | equal to the config (broadcast = misconfigured) | FAIL |

Levels: OK < INFO < UNKNOWN < WARN < FAIL. `timing.pps` in the board config says how far the PPS can be
trusted (`none | temporary | reference`). rfsoc0's PPS is **temporary**, so clock-accuracy results are
informational. Exit status of `lft3-board status`: 0 if nothing FAILs, 2 otherwise (usable from scripts
and cron).

### 4.1 Write functions tested on the live board (2026-11-12)

| Command | Result |
|---|---|
| `lft3-board start` | reprogrammed and brought up in 9.6 s. Tiles 0 and 2 state 15, all config checks OK afterwards, stream back at 915 527/s |
| `lft3-board start --no-program` | re-applied the config to the running design, all checks OK |
| `lft3-board set-dest-mac --mac ff:ff:ff:ff:ff:ff`, then without `--mac` | NIC moved ~925 k packets/s from unicast to broadcast and back |
| `lft3-board sync` | clean (trig_cnt 0) |
| `lft3-board trigger` | trig_cnt 0→1, `packetizer_out_vld_ddr_cnt` = 2^26 words (4 GiB), and the host saw the wire rate step from 60.8 to ~71 Gb/s for ~3.5 s |
| write `expected_clk_period` | holds the value (plain software register). `pps_drift_err_cnt` still counts every PPS, because the measured period is ~3800 cycles from nominal (see below) |

### First run on the live board (2026-11-12)

Everything configured is as expected: 915 528 packets/s, no FIFO loss, header NCOs −2031, fid 0, mask 0x3f,
destination MAC unicast to the Spark. Two clock findings:

* **The sample clock measures −24.6 ppm relative to the PPS.** `raw_cycles_per_pps` = 156 246 153–180,
  steady. The PPS is **temporary and not fixed properly yet**, so this is not yet a statement about the
  clock: it is either the board's free-running reference (the LMK file in use is the internal-reference one)
  or the PPS source. It has to be re-measured with a proper PPS. If it holds, then: Every sky frequency scales with the sampling clock, so a
  2 GHz line is ~49 kHz (≈ 40 fine channels at 1.2 kHz) from where the FBH5 axis says it is. The board
  spurs still land on exact 125 MHz multiples because they come from the same clock. Fix: a disciplined
  10 MHz reference (the firmware repo has `rfsoc4x2_lmk_CLKin0_extref_10M_PL_122M88_LMXREF_245M76.txt` in
  `examples/1pps_ex`, for a different PL clock) or a measured correction in the writer.
* **`expected_clk_period` defaults to 208 333 332** (a 208.33 MHz clock). It is a plain software register.
  `lft3-board start` now writes 156250000 (`timing.expected_clk_period`). The drift counter will only stop
  counting once clock and PPS agree. `clk_cycles_per_pps` returns a different value on every read and is
  marked unreliable; `raw_cycles_per_pps` is the usable period.

Both are added to `docs/firmware_requests.md`.

## 5. Metrics

Prometheus text format on `http://<host>:<port>/metrics` (`lft3-board monitor --prometheus-port 9101`):

* `lft3_board_<register>{board=…}`: every readable register (the 100GbE ones shortened to `gbe_…`), with
  HELP text from the register map and TYPE counter/gauge;
* `lft3_board_<register>_rate`: per-second rate of each counter;
* `lft3_board_health{board=…,check=…}`: 0 OK, 1 UNKNOWN, 2 WARN, 3 FAIL;
* `lft3_board_rail_volts|rail_amps{rail=…}`, `lft3_board_temperature_celsius{sensor=…}` when sensors are on;
* `lft3_board_last_update_seconds`.

The same content as one JSON object per interval goes to `--jsonl FILE`.

**Storage.** The exporter keeps only the latest values: there is no database behind it yet, and only the
`--jsonl` file persists. Prometheus *is* the database. It scrapes `/metrics` every N seconds and stores the samples
in its own on-disk time-series database (default retention 15 days, configurable by time or size). For
years of history, add remote storage (VictoriaMetrics, Thanos or Mimir) behind it. Grafana queries Prometheus.

Suggested deployment (not yet set up): Prometheus on the master node scraping each board's `lft3-board
monitor` and the receiver's metrics; Grafana dashboards per board (stream rate, losses, clocks, rails,
temperatures); alert rules on `lft3_board_health >= 3`, rate drops and rail utilisation. The arch-doc
answer "use the casperfpga system" fits: casperfpga stays the only path to the hardware, and this layer
only publishes what it reads.

## 6. Open items and decisions

1. **Sensors:** install an SSH key for `casper@<board>` (then set `sensors.enabled: true`), or run a tiny
   agent on the board that serves the same sysfs values. Neither katcp `sensor-list` (only `mode`) nor
   any katcp request exposes them.
2. **Clock reference:** once the PPS is fixed, re-measure. If the offset is real: a disciplined 10 MHz +
   matching LMK file, or record the measured ppm in the products and correct the frequency axis. It affects
   every frequency we write.
3. **Metrics storage:** deploy Prometheus (its built-in time-series database) + Grafana (§5).
4. **Coordinator:** one process on the master node owning system state (boards, receivers, observation
   config), with the board lock moved from `/tmp` to the coordinator. The CLI becomes a client of it.
   Candidates: a small service with a REST/katcp interface. Decide once there is more than one board.
5. **Pipeline metrics:** the runtime counts packets/blocks/loss/reader lag in each ring and `lft3-pipeline run --metrics-port` exports them (docs/user_guide.md §5); exporting
   those to the same Prometheus is the next step for end-to-end monitoring.
6. **Firmware diagnostics:** `f_engineN_power_snapshot*`, `ewma_snapshot*` and the ADC snapshots
   (`listener.py` reads them) could feed low-rate diagnostic spectra and per-input RMS (arch doc §2).
7. **Remote power control:** `lft3-earth-main/host/scripts/power-rfsoc.py` drives a network power strip;
   wrap it as `lft3-board power on|off|status` once the strip's address is in the config.
