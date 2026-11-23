from __future__ import annotations

from lft3.monitoring.nic import DROP_COUNTERS, NicLogger, parse_ethtool


def test_parse_ethtool():
    text = "NIC statistics:\n     rx_out_of_buffer: 12\n     rx_discards_phy: 3\n     weird: x\n"
    assert parse_ethtool(text) == {"rx_out_of_buffer": 12, "rx_discards_phy": 3}


def test_nic_logger_deltas_and_alert(tmp_path):
    values = iter([{"rx_out_of_buffer": 5, "rx_packets_phy": 100},
                   {"rx_out_of_buffer": 5, "rx_packets_phy": 200},
                   {"rx_out_of_buffer": 9, "rx_packets_phy": 300}])
    alerts = []
    log = NicLogger("eth0", tmp_path / "nic.csv", alert=alerts.append, read=lambda _: next(values))
    prev = log.read("eth0")
    (tmp_path / "nic.csv").write_text("hdr\n")
    prev = log.sample(prev)
    assert alerts == []
    log.sample(prev)
    assert len(alerts) == 1 and "rx_out_of_buffer +4" in alerts[0]
    rows = (tmp_path / "nic.csv").read_text().splitlines()[1:]
    i = 1 + log.keys.index("rx_packets_phy")
    assert [r.split(",")[i] for r in rows] == ["100", "100"]
    assert rows[1].split(",")[1 + DROP_COUNTERS.index("rx_out_of_buffer")] == "4"


def test_mtu_problems_flags_small_mtu(tmp_path):
    from lft3.monitoring.nic import mtu_problems

    pci, net = tmp_path / "pci", tmp_path / "net"
    for dev, func, mtu in (("enp1s0f0np0", "0000:01:00.0", "1500"), ("enp1s0f1np1", "0000:01:00.1", "9000")):
        (pci / func / "net" / dev).mkdir(parents=True)
        (net / dev).mkdir(parents=True)
        (net / dev / "mtu").write_text(mtu + "\n")
    msgs = mtu_problems(["0000:01:00.0", "0000:01:00.1", "0000:99:00.0"], pci, net)
    assert len(msgs) == 1
    assert "enp1s0f0np0" in msgs[0] and "MTU 1500" in msgs[0] and "link_setup.sh enp1s0f0np0" in msgs[0]
