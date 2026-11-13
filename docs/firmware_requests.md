# Suggested firmware changes: spectrometer_8way_4x

From the host-software side (DGX Spark receiver), after first captures on 2026-11-12. Design:
`designs/spectrometer_8way/spectrometer_8way_4x.slx`, bitstream `spectrometer_8way_4x.fpg`.
Ordered by priority. Each item says what we saw, what we'd like, and why.

## 1. Destination IP and UDP ports as software registers (high)

**Now:** the destination is fixed at build time in the packetizer:
* `Concat1` = `Constant51` (10.17.16) ‖ `Constant43` (10) gives 10.17.16.10;
* `Constant58` = 60000 (channel stream) and `Constant59` = 40000 (ADC dump).

**Request:** replace these with software registers, e.g. `packetizer_dest_ip` (32 bit), `packetizer_chan_port` and
`packetizer_adc_port` (16 bit). Keep today's values as reset defaults.

**Why:** with several boards and several servers, every server would otherwise have to own 10.17.16.10 on the same
network, which is an IP conflict. Registers let one config file assign each board's destination.

## 2. Unique source MAC and IP per board (high)

**Now:** the board transmits from MAC 00:00:00:00:12:34 and IP 10.17.16.60, UDP source port 60000.

**Request:** make sure source MAC and IP can be set per board at start-up (if the gmac registers already allow it,
say how they get their current values), and that two boards never share a MAC.

**Why:** behind a switch, duplicate source MACs confuse the switch's address table. On the host, the network card
steers flows into queues by source IP/port, so identical sources can't be split per board.

## 3. Contiguous packet sequence number in the channel header (high)

**Now:** word 0 is `t (58) | bindex (3) | fid (3)`. `word0 >> 3` = 8t + b, but b only takes values 0–5, so the
sequence skips 2 of every 8 values.

**Request:** put `seq = (6·t + b) mod 2^32` in a fixed place in a spare header word, e.g. the top 32 bits of word 4.

**Why:** the ConnectX-7 network card can reorder packets in hardware (via the NVIDIA DAQIRI receiver) if the header
has a contiguous sequence field at a fixed bit offset. It also makes loss accounting trivial.

## 4. Absolute time reference for `t` (high)

**Now:** `t` (spectrum count) and `t_trig` (156.25 MHz cycles) count from the last software sync. PPS only feeds
monitoring counters (`pps_cnt`, `clk_cycles_per_pps`, `pps_drift_err_cnt`) and doesn't drive sync.

**Request:** an "arm sync on next PPS" mode, so that t = 0 lands on a known PPS edge. Software then knows the UTC
second it armed. Optionally, a register that records the sync time.

**Why:** every science product needs a start time (`tstart` in filterbank/FBH5, GUPPI `STT_*`). At present,
packet times can't be tied to UTC better than the software write latency, and boards can't be aligned with each other.

## 5. Exact NCO frequency in the headers (medium)

**Now:** software writes `int(round(nco_MHz))` into `packetizer_hdr_nco0..3`, so −2031.25 MHz is sent as −2031.

**Request:** carry the NCO in finer units, e.g. signed int32 in kHz (±2.1 THz range) or in Hz. This could be a
software convention change only, but please document the unit in the header spec.

**Why:** the sky frequency of each channel is NCO + channel offset. A 0.25 MHz error is ~1.6 coarse channels.

## 6. `pfb_lsb` header field out of step with the data (medium)

**Now (firmware README):** `ch_lsb_select` changes the data immediately, but the header `pfb_lsb` only updates on
the next sync, so headers are wrong until then.

**Request:** latch `ch_lsb_select` into the data path on the same sync that updates the header, or update both together.

**Why:** downstream we use `pfb_lsb` to put spectra on a common power scale. A mismatched header silently corrupts that.

## 7. 100GbE transmit counters look wrong (medium)

**Now:** `packetizer_onehundred_gbe_gmac_reg_tx_overflow_count` rises ~0.7×10⁹/s and `tx_almost_full_count`
~2.2×10⁹/s, while `tx_packet_rate` = 915 527/s (correct), `packetizer_fifo_full_cnt`, `fifo_ddr_full_cnt` and
`blocked_word_cnt` are 0, and `packetizer_gbe_ovflow_reg` = 1.

**Request:** check what these count (per cycle? wrong clock?) and whether `gbe_ovflow_reg` is sticky. We'd like
one counter we can trust to say "the board dropped data".

**Why:** we can only prove the link is loss-free if the board side reports it reliably.

## 8. Spurs at 125 MHz harmonics (medium; may be hardware)

**Seen:** with nothing connected to the inputs, all four inputs show tones +15–20 dB above the noise at
RF 1750, 1875, 2000 and 2250 MHz (14, 15, 16, 18 × 125 MHz; NCO −2031.25 MHz). Each is strongest on a different input.

**Request:** check for coupling from the 125 MHz PL clock or the LMK/LMX outputs. The 1875 MHz tone sits exactly at
fs/8 of the 1250 MSPS stream (channel k = 2048).

## 9. Input / polarisation labelling (low)

**Assumed:** inputs follow `ADC_MAP = [(0,0), (0,1), (2,0), (2,1)]` = beam 1 X, beam 1 Y, beam 2 X, beam 2 Y.

**Request:** confirm this, and say which front-panel connector is which. Ideally, add a small header field or
register describing the input → beam/pol mapping.

## 10. Documentation (low)

* PFB is 8 taps (firmware README); the design review and packet spec say 4. Please update the spec.
* A register map: name, width, units, when it takes effect (immediately vs on sync), reset value. The current
  README notes (sync-latched registers, `trig_rst`, multiplier format) are very useful and could seed it.
* Packet spec: the endianness and byte order inside 512-bit words (we confirmed them from `host/scripts/listener.py`)
  and the NCO unit/sign.

## 11. Sample clock reference and PPS registers (high)

**Seen (2026-11-12, `lft3-board status`):** `raw_cycles_per_pps` = 156 246 153–180, i.e. **−24.6 ppm** relative
to the PPS. The PPS in the lab is temporary, so this still has to be confirmed with a proper PPS. If it is the
clock, every sky frequency at 2 GHz is off by ~49 kHz. `expected_clk_period` defaults to 208 333 332 (a
208.33 MHz clock), so `pps_drift_err_cnt` increments on every PPS. It is writable, and the host now sets it
to 156250000 at start-up. `clk_cycles_per_pps` returns a different value on every read
(14.7 M, 171 M, 119 M), so it does not look like a latched period.

**Request:** (a) clock the board from a disciplined 10 MHz reference (GPS/maser) with a matching LMK file, and
document which clock files to use; (b) change the design default of
`expected_clk_period` to 156250000 and document the drift tolerance; (c) document what `clk_cycles_per_pps` is.

## Notes, not requests

* `fid` is 3 bits, so at most 8 boards. That's enough for the planned 6.
* The ADC dump currently shares the 100 G link (≈10 Gb/s on top of ≈60.5 Gb/s), which fits. A separate slower link,
  as suggested in the system-design notes, is not needed for the receiver.
* The destination MAC is now set at runtime from the host (ARP table entry 10, `lft3-board set-dest-mac`), so no
  change is needed there beyond item 1.
