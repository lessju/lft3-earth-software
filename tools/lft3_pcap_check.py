#!/usr/bin/env python3
"""Summarise an RFSoC capture: tcpdump -i <if> -s 0 -c 20000 -w cap.pcap ; then
   python tools/lft3_pcap_check.py cap.pcap [--endian big|little] [--iq IQ|QI]"""
import argparse, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))
from lft3.packets import main
ap = argparse.ArgumentParser(); ap.add_argument("pcap"); ap.add_argument("--endian", choices=["big", "little"])
ap.add_argument("--iq", default="IQ", choices=["IQ", "QI"]); a = ap.parse_args()
main(a.pcap, a.endian, a.iq)
