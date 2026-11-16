# LFT3-Earth packet format (as implemented in `src/lft3/packets.py`)

Sources: `data_rates_packetizer_v2.xlsx` (sheets *Data Rates*, *Channel Packets*, *ADC Packets*,
*Ringbuffer*, *Final Specs*), plus the firmware author's working parser
`lft3-earth-main/host/scripts/listener.py`, the firmware README, and register reads from a running
`spectrometer_8way_4x` board. Items marked **CONFIRMED (listener.py)** were assumptions in the first draft and are
now fixed by the firmware parser; the decoder keeps them configurable so a capture can still overrule them.

## Network

| Item | Value |
|---|---|
| Channel stream UDP dst port | **60000** |
| ADC (DDR dump) UDP dst port | **40000** |
| Board 100GbE source IP | 10.17.16.60 (`packetizer_onehundred_gbe` local IP register) |
| Board control (katcp/casperfpga) | 192.168.2.100 on the 1 GbE management network |
| UDP payload offset in the frame | 42 B (Ethernet 14 + IPv4 20 + UDP 8, no VLAN) |

## Channel packets (continuous F-engine stream, UHF board)

| Quantity | Value |
|---|---|
| Sampling | 5000 MSPS real → IQ, 4× decimation → 1250 MSPS complex |
| PFB | **8-tap** (firmware README; the design review says 4), 8192 points, 152.587890625 kHz channels |
| Channels sent | 6144 per beam (k = 0..6143), 937.5 MHz around NCO centre |
| Beams per board | 4 (beam j = 4p + {0..3}, p = board fid) |
| Sample format | 8-bit I / 8-bit Q, two's complement |
| Packets per timestamp | 6 (bindex b = 0..5, 1024 channels each) |
| UDP payload | 64 B header + 1024 ch × 4 beams × 2 B = **8256 B** |
| Ethernet frame | 8298 B (needs MTU ≥ 8284; use 9000) |
| Spectrum rate | 152 587.89 /s → **915 527 packets/s** per board |
| Rate | 60.0 Gb/s data, 60.47 Gb/s UDP payload, 60.95 Gb/s on the wire |

Header: eight 64-bit words, **big-endian / network order** (CONFIRMED (listener.py); the decoder can still auto-detect).

| Word | Bits (MSB → LSB) |
|---|---|
| 0 | t timestamp (58) · b bindex (3) · p fid (3) |
| 1 | nco0 (32) · nco1 (32) |
| 2 | nco2 (32) · nco3 (32) |

The nco fields are **signed int32, in whole MHz**: software writes `int(round(nco_MHz))`, so an NCO of
−2031.25 MHz shows up as −2031 (0xFFFFF811). Use them as labels only. The exact frequency has to come from the
observing configuration.
| 3 | pfb_lsb0 (16) · pfb_lsb1 (16) · pfb_lsb2 (16) · pfb_lsb3 (16) |
| 4–7 | optional metadata (unused) |

Payload: 1024 64-bit words, word i = channel k = 1024·b + i. On the wire the firmware emits 128 × 512-bit
words of 8 channels each, which in byte order gives the same 1024 × 8-byte sequence. Inside each 8-byte group,
beam 4p+0 comes first (most significant 16 bits), and each 16-bit sample is I in the high byte, Q in the low byte
(both CONFIRMED (listener.py)). Packets b = 0..2 carry FFT bins 5120–8191, and b = 3..5 carry bins 0–3071.

Channel ↔ frequency: k = 0 is PFB channel 5120 and k = 6143 is PFB channel 3071 (wrapping), i.e.
signed FFT bin = k − 3072, so the offset from the NCO centre is (k − 3072) × 152.587890625 kHz:
k ascending = frequency ascending, from −468.75 MHz to +468.60 MHz. The RF sign/flip relative to the
(negative) NCO setting has to be confirmed with a test tone.

## ADC packets (triggered ring-buffer dump)

| Quantity | Value |
|---|---|
| Capture | 4 ADC inputs, 1250 MSPS complex, 4-bit I / 4-bit Q → 5 GB/s into 4 GiB DDR4 (859 ms) |
| DDR | 512-bit (64 B) words, 16 sample-times × 4 ADCs each; `ddr_size` = last word address (max 2^26 − 1 → 4 GiB → 2^19 packets of 128 words) |
| Dump rate | 10 Gb/s after a trigger (≈151 k packets/s, ≈3.4 s to empty 4 GiB) |
| UDP payload | 64 B header + 2048 samples × 4 B = **8256 B** (same size as channel packets) |

Header: sixteen 32-bit words.

| Word | Content |
|---|---|
| 0 | s sequence number (restarts at 0 on each trigger) |
| 1–2 | t_trig (64-bit; 156.25 MHz FPGA clock cycles since previous sync) |
| 3 | fid (3) · src (5) · empty (24); src bits MSB→LSB = beam 0, beam 1, beam 2, beam 3, manual |
| 4–7 | nco0..nco3 (signed int32, whole MHz) |
| 8 | adc_lsb0..3 (8 bits each) |
| 9 | ddr_size |
| 10 | ddr_read_delay |
| 11–15 | optional metadata |

Payload: 2048 32-bit words, word i = sample n = 2048·s + i, ADC 4p+0 in the most significant byte, and each byte is
I in the high nibble, Q in the low nibble (both CONFIRMED (listener.py); this matches rawspec's complex4 convention).
The packet holding the trigger instant is s = (ddr_size + 1 − ddr_read_delay) // 128.

## Open questions for the firmware / packetizer spec

1. ~~Endianness, beam/ADC order, I/Q order.~~ Settled by listener.py (big-endian, index 0 first, I high).
   The RF sign of the spectrum (whether k ascending = RF ascending for a negative NCO) still needs a test tone.
2. ~~Telling the two packet types apart.~~ Settled: dst port 60000 (channel) vs 40000 (ADC).
3. ~~A contiguous packet sequence number.~~ Not needed: the capture assembler orders packets and counts loss from
   (t, b) in word 0.
4. ADC header: the 64-bit t_trig starts at byte offset 4 (not 8-byte aligned). This is harmless for software, but confirm it is intended.
5. **VHF board numbers are inconsistent:**
   * *Data Rates* uses 1096 channels/packet, 6576 channels, 8874-B frames and 64.2 Gb/s for the VHF board.
     *Channel Packets* defines k = 1024·b + i with 0 ≤ k ≤ 6143.
   * *Final Specs* counts the VHF board as 20 Gb/s ("5 × 60 + 20 = 320 Gb/s"). That matches neither 4 padded beams (64.2 Gb/s)
     nor 2 real beams (32.1 Gb/s).
   * The VHF band is 200 MHz wide, but the configuration ships ~1 GHz per beam, and 2 of the 4 beams are zeros.
6. pfb_lsb / adc_lsb (bit-slice positions) are needed to put spectra on a common power scale across
   requantisation changes. The spectrometers currently output raw power and carry these fields as metadata.
