# Bring-up checklist: RFSoC 4x2 → DGX Spark

1. **Link.**
   * Use a 100G QSFP28 DAC/AOC into one Spark QSFP port, then run `sudo scripts/link_setup.sh <netdev>` (add
     `10.17.16.10/24`, the destination IP in the firmware, only if kernel tools such as tcpdump should see the
     stream as addressed to this host).
   * `ibdev2netdev` shows which of the four logical interfaces comes up. On this Spark the RFSoC port appears as
     `enp1s0f0np0` (PCI 0000:01:00.0) and `enP2p1s0f0np0` (PCI 0002:01:00.0), the two PCIe halves of one QSFP port.
     The `*_phy` counters are per physical port, so both report the same traffic.
   * If the link stays down, force `speed 100000 autoneg off` and match FEC (`rs` or `off`) to the RFSoC CMAC. A FEC mismatch is the usual cause.
2. **Rate at the NIC, before any software.**
   * Run `scripts/nic_rate.sh <netdev>`.
   * Expect ≈60.9 Gb/s and ≈0.916 Mpps per board on the wire, with `rx_crc_errors_phy` flat.
   * `rx_out_of_buffer` will climb while no application consumes the packets. That is expected at this stage.
   * Check which `rx_vport_*_packets` counter moves. After programming, the firmware (spectrometer_8way_4x) sends
     to the **broadcast MAC** (`rx_vport_broadcast_packets` ≈ 0.92 M/s), so **both** PCIe halves (`enp1s0f0np0` and
     `enP2p1s0f0np0`) receive a full copy of the stream, doubling the PCIe and memory load. `lft3-board start` (or
     `lft3-board set-dest-mac`) sets the unicast MAC of one function from the board config; then
     `rx_vport_unicast_packets` moves instead.
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
5. **Real-time runtime on synthetic data.** `make pipeline-test`, then `lft3-pipeline run --source synthetic
   --duration 60` (`docs/user_guide.md`): capture, spectrometer and search keep up, and the spectra open in
   `lft3-plot`. The spectrometer is cross-checked against `src/lft3/dsp/finespec.py` by the unit tests.
6. **Live pipeline.** `lft3-pipeline run` on the board: 60.8 Gb/s, no missing packets, readers at lag 0. Repeat the
   tone test end to end: the tone appears at the expected fine channel and, with a drift, as a live-search hit.
7. **Viewer.** Look at coarse or zoomed fine spectra with `lft3-view` / `lft3-plot` (`docs/user_guide.md`).
