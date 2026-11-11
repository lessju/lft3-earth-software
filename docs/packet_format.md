# LFT3-Earth packet format (as implemented in `python/lft3/packets.py`)

Source: `data_rates_packetizer_v2.xlsx` (sheets *Data Rates*, *Channel Packets*, *ADC Packets*,
*Ringbuffer*, *Final Specs*). Items marked **ASSUMED** are not stated in the sheet and are
configurable in the decoder; confirm each one on a real capture (see `docs/bringup.md`).

## Channel packets (continuous F-engine stream, UHF board)

| Quantity | Value |
|---|---|
| Sampling | 5000 MSPS real → IQ, 4× decimation → 1250 MSPS complex |
| PFB | 4-tap, 8192 points, 152.587890625 kHz channels |
| Channels sent | 6144 per beam (k = 0..6143), 937.5 MHz around NCO centre |
| Beams per board | 4 (beam j = 4p + {0..3}, p = board fid) |
| Sample format | 8-bit I / 8-bit Q, two's complement |
| Packets per timestamp | 6 (bindex b = 0..5, 1024 channels each) |
| UDP payload | 64 B header + 1024 ch × 4 beams × 2 B = **8256 B** |
| Ethernet frame | 8298 B (needs MTU ≥ 8284; use 9000) |
| Spectrum rate | 152 587.89 /s → **915 527 packets/s** per board |
| Rate | 60.0 Gb/s data, 60.47 Gb/s UDP payload, 60.95 Gb/s on the wire |

Header: eight 64-bit words (**ASSUMED big-endian / network order**; the decoder auto-detects).

| Word | Bits (MSB → LSB) |
|---|---|
| 0 | t timestamp (58) · b bindex (3) · p fid (3) |
| 1 | nco0 (32) · nco1 (32) |
| 2 | nco2 (32) · nco3 (32) |
| 3 | pfb_lsb0 (16) · pfb_lsb1 (16) · pfb_lsb2 (16) · pfb_lsb3 (16) |
| 4–7 | optional metadata (unused) |

Payload: 1024 64-bit words, word i = channel k = 1024·b + i. Inside each word, beam 4p+0 is in
the most significant 16 bits (**ASSUMED**), and each 16-bit sample is I in the high byte, Q in the low byte (**ASSUMED**).

Channel ↔ frequency: k = 0 is PFB channel 5120 and k = 6143 is PFB channel 3071 (wrapping), i.e.
signed FFT bin = k − 3072, so the offset from the NCO centre is (k − 3072) × 152.587890625 kHz:
k ascending = frequency ascending, from −468.75 MHz to +468.60 MHz. The RF sign/flip relative to the
(negative) NCO setting has to be confirmed with a test tone.

## ADC packets (triggered ring-buffer dump)

| Quantity | Value |
|---|---|
| Capture | 4 ADC inputs, 1250 MSPS complex, 4-bit I / 4-bit Q → 5 GB/s into 4 GiB DDR4 (859 ms) |
| Dump rate | 10 Gb/s after a trigger (≈151 k packets/s, ≈3.4 s to empty 4 GiB) |
| UDP payload | 64 B header + 2048 samples × 4 B = **8256 B** (same size as channel packets) |

Header: sixteen 32-bit words.

| Word | Content |
|---|---|
| 0 | s sequence number (restarts at 0 on each trigger) |
| 1–2 | t_trig (64-bit; clock cycles since previous sync) |
| 3 | fid (3) · src (5) · empty (24) |
| 4–7 | nco0..nco3 |
| 8 | adc_lsb0..3 (8 bits each) |
| 9 | ddr_size |
| 10 | ddr_read_delay |
| 11–15 | optional metadata |

Payload: 2048 32-bit words, word i = sample n = 2048·s + i, ADC 4p+0 in the most significant byte
(**ASSUMED**), and each byte is I in the high nibble, Q in the low nibble (**ASSUMED**; this matches rawspec's complex4 convention).

## Open questions for the firmware / packetizer spec

1. **Endianness** of the 64-bit (channel) and 32-bit (ADC) words, the beam/ADC order inside a word, and the I/Q order.
   `tools/lft3_pcap_check.py` auto-detects the word order; a test tone at a known positive offset settles I/Q
   (swapped I/Q mirrors the tone about the NCO centre).
2. **Telling the two packet types apart.** Both are 8256-byte UDP payloads. Give them distinct UDP destination ports:
   this is also what NIC flow steering keys on.
3. **A contiguous packet sequence number** for the channel stream. word0 >> 3 = 8t + b skips 2 of every 8 values.
   A 32-bit `seq = (6t + b) mod 2^32` in an optional header word would let the ConnectX-7 hardware reorder
   (DAQIRI) place packets in time order and make loss accounting trivial. Suggested: top half of word 4.
4. ADC header: the 64-bit t_trig starts at byte offset 4 (not 8-byte aligned). This is harmless for software, but confirm it is intended.
5. **VHF board numbers are inconsistent:**
   * *Data Rates* uses 1096 channels/packet, 6576 channels, 8874-B frames and 64.2 Gb/s for the VHF board.
     *Channel Packets* defines k = 1024·b + i with 0 ≤ k ≤ 6143.
   * *Final Specs* counts the VHF board as 20 Gb/s ("5 × 60 + 20 = 320 Gb/s"). That matches neither 4 padded beams (64.2 Gb/s)
     nor 2 real beams (32.1 Gb/s).
   * The VHF band is 200 MHz wide, but the configuration ships ~1 GHz per beam, and 2 of the 4 beams are zeros.
6. pfb_lsb / adc_lsb (bit-slice positions) are needed to put spectra on a common power scale across
   requantisation changes. The spectrometers currently output raw power and carry these fields as metadata.
