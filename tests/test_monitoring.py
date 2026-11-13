import json
import urllib.request

from lft3.board.sensors import parse
from lft3.board.telemetry import Check, Snapshot
from lft3.monitoring.metrics import MetricsServer, to_json, to_prometheus

DUMP = """\
/sys/bus/i2c/devices/0-0040/iio:device1/in_current3_raw 4000
/sys/bus/i2c/devices/0-0040/iio:device1/in_current3_scale 2.5
/sys/bus/i2c/devices/0-0040/iio:device1/in_voltage1_raw 680
/sys/bus/i2c/devices/0-0040/iio:device1/in_voltage1_scale 1.25
/sys/bus/iio/devices/iio:device0/name ams
/sys/bus/iio/devices/iio:device0/in_temp0_ps_temp_raw 40000
/sys/bus/iio/devices/iio:device0/in_temp0_ps_temp_offset -36058
/sys/bus/iio/devices/iio:device0/in_temp0_ps_temp_scale 7.771514892
"""


def test_sensor_parse():
    s = parse(DUMP)
    assert len(s.rails) == 1
    r = s.rails[0]
    assert r.name == "VCC_0V85"
    assert abs(r.volts - 0.85) < 1e-9 and abs(r.amps - 10.0) < 1e-9
    assert abs(r.utilisation - 0.25) < 1e-9
    (name, t), = s.temperatures_c.items()
    assert name == "ams/in_temp0_ps_temp"
    assert abs(t - (40000 - 36058) * 7.771514892 / 1000) < 1e-9


def _sample():
    snap = Snapshot(123.0, {"pps_cnt": 5, "packetizer_onehundred_gbe_gmac_reg_tx_packet_rate": 915527,
                            "trig_cnt": None})
    checks = [Check("pps", "OK", "1/s"), Check("tx_packet_rate", "FAIL", "0/s")]
    return snap, {"pps_cnt": 1.0}, checks


def test_prometheus_text():
    snap, r, checks = _sample()
    text = to_prometheus("rfsoc0", snap, r, checks, parse(DUMP))
    assert 'lft3_board_pps_cnt{board="rfsoc0"} 5' in text
    assert 'lft3_board_gbe_tx_packet_rate{board="rfsoc0"} 915527' in text
    assert 'lft3_board_pps_cnt_rate{board="rfsoc0"} 1' in text
    assert 'lft3_board_health{board="rfsoc0",check="tx_packet_rate"} 4' in text
    assert 'lft3_board_rail_volts{board="rfsoc0",rail="VCC_0V85"} 0.8500' in text
    assert "trig_cnt" not in text                       # unreadable registers are omitted
    assert "# TYPE lft3_board_pps_cnt counter" in text


def test_json_line():
    snap, r, checks = _sample()
    doc = json.loads(to_json("rfsoc0", snap, r, checks))
    assert doc["board"] == "rfsoc0" and doc["health"]["tx_packet_rate"]["status"] == "FAIL"


def test_metrics_server_serves_latest():
    server = MetricsServer(0, bind="127.0.0.1")
    try:
        server.update("lft3_test 1\n")
        body = urllib.request.urlopen(f"http://127.0.0.1:{server.port}/metrics", timeout=5).read().decode()
        assert body == "lft3_test 1\n"
    finally:
        server.close()
