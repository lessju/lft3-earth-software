# lft3-earth-software

Receive and spectrometer software for LFT3-Earth RFSoC 4x2 F-engine boards on an NVIDIA DGX Spark.

* `docs/architecture.md`: receiver (NVIDIA DAQIRI) → GPU corner turn → rawspec fine spectrometer (≈1.16 Hz),
  with sizing, the memory-bandwidth budget and output-volume constraints.
* `docs/packet_format.md`: channel and ADC packet formats as implemented, plus open questions for the firmware.
* `docs/bringup.md`: link → NIC counters → pcap check → DAQIRI → rawspec → live pipeline.
* `python/lft3/packets.py`: decoder for both packet types, pcap reader, stream checks, synthetic packet generator.
* `python/lft3/finespec.py`: reference two-stage spectrometer (NumPy/CuPy), the golden model for the GPU path.
* `tools/lft3_pcap_check.py`: summarise a tcpdump capture (endianness, loss, NCO, tone position).
* `tools/lft3_make_test_pcap.py`: synthetic capture with a tone, for tests or tcpreplay.
* `scripts/link_setup.sh`, `scripts/nic_rate.sh`: link bring-up and NIC-level rate/drop monitor.
* `config/daqiri/lft3_rx_ibverbs.yaml`: draft DAQIRI receive config (two flows: channel stream and ADC dump).

## Quick start
```bash
pip install numpy pytest            # cupy optional, for GPU runs of finespec
python -m pytest -q python/tests
python tools/lft3_make_test_pcap.py /tmp/test.pcap && python tools/lft3_pcap_check.py /tmp/test.pcap
```

## Status
| Component | State |
|---|---|
| Packet decoder, pcap tools, reference spectrometer | written, unit-tested on synthetic packets (8 tests) |
| Link/NIC scripts, DAQIRI config | drafts; not yet run on the Spark |
| DAQIRI → corner turn → rawspec glue | designed (docs/architecture.md), not yet written |
