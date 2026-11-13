# Bring-up checklist: RFSoC 4x2 → DGX Spark

1. **Link.**
   * Use a 100G QSFP28 DAC/AOC into one Spark QSFP port, then run `sudo scripts/link_setup.sh <netdev> 10.0.0.1/24`.
   * `ibdev2netdev` shows which of the four logical interfaces comes up. On this Spark the RFSoC port appears as
     `enp1s0f0np0` (PCI 0000:01:00.0) and `enP2p1s0f0np0` (PCI 0002:01:00.0), the two PCIe halves of one QSFP port.
     The `*_phy` counters are per physical port, so both report the same traffic.
   * If the link stays down, force `speed 100000 autoneg off` and match FEC (`rs` or `off`) to the RFSoC CMAC. A FEC mismatch is the usual cause.
2. **Rate at the NIC, before any software.**
   * Run `scripts/nic_rate.sh <netdev>`.
   * Expect ≈60.9 Gb/s and ≈0.916 Mpps per board on the wire, with `rx_crc_errors_phy` flat.
   * `rx_out_of_buffer` will climb while no application consumes the packets. That is expected at this stage.
   * Check which `rx_vport_*_packets` counter moves. The current firmware (spectrometer_8way_4x) sends to the
     **broadcast MAC** (`rx_vport_broadcast_packets` ≈ 0.92 M/s), so **both** PCIe halves (`enp1s0f0np0` and
     `enP2p1s0f0np0`) receive a full copy of the stream. That doubles the PCIe and memory load. For DAQIRI, have the board
     send to the unicast MAC of one function (an ARP-cache entry for a destination IP on that interface).
   * With no consumer, the kernel takes roughly 0.41 Mpps on a single queue (all packets share one UDP 4-tuple, so RSS
     can't spread them) and drops the rest as `rx_out_of_buffer`. tcpdump will therefore see roughly 45% of packets,
     which is enough for format checks.
3. **Packet format.**
   * Capture with `sudo tcpdump -i <netdev> -s 0 -B 1048576 -c 20000 -w cap.pcap udp` (expect drops at this rate; a few thousand packets is enough),
     then run `lft3-pcap check cap.pcap`.
   * Check: word endianness, bindex 0..5, a stable fid, NCO/pfb_lsb values matching the firmware registers, and payload length 8256.
   * Inject a CW tone at a known offset from the NCO centre. The tool prints the peak channel k and its offset: a mirrored offset means I/Q is swapped (`--iq QI`).
   * Do the same for the ADC-dump port after a manual trigger.
4. **DAQIRI receive.**
   * Build DAQIRI (see `third_party/README.md`) and run its raw-ibverbs RX benchmark with `config/daqiri/lft3_rx_ibverbs.yaml`
     (PCI 0000:01:00.0, UDP 60000 channel / 40000 ADC are filled in).
   * Pass criterion: zero `rx_out_of_buffer` and zero application-level loss over ≥ 1 hour, with a dump triggered periodically.
5. **rawspec on recorded data.**
   * Build rawspec for GB10 and benchmark it on a synthetic GUPPI RAW file with the parameters in `docs/architecture.md`:
     it must process > 3.75 G complex samples/s.
   * Cross-check its output against `src/lft3/dsp/finespec.py` on the same data.
6. **Live pipeline.** DAQIRI → corner turn → rawspec, with the tone test repeated end-to-end at 1.16 Hz resolution.
7. **Viewer.** Feed coarse or zoomed fine spectra to CyberEther.
