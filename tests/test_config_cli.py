import pytest
import yaml

from lft3.cli import board as board_cli
from lft3.cli import pcap as pcap_cli
from lft3.config import ConfigError, config_dir, list_boards, load_board_config, parse_board_config


def test_repository_board_config_loads(cfg):
    assert cfg.name == "rfsoc0"
    assert cfg.adc_map == [(0, 0), (0, 1), (2, 0), (2, 1)]
    assert cfg.tile_mask == 5 and cfg.packet_mask == 0x3F
    assert cfg.header_nco == -2031
    assert "rfsoc0" in list_boards()


def test_invalid_config_is_rejected():
    data = yaml.safe_load(open(config_dir() / "boards" / "rfsoc0.yaml"))
    data["board_id"] = 9
    with pytest.raises(ConfigError):
        parse_board_config(data)
    data["board_id"] = 0
    data["adc"]["inputs"][1]["pol"] = "X"            # beam 0 X twice
    with pytest.raises(ConfigError):
        parse_board_config(data)
    with pytest.raises(ConfigError):
        load_board_config("no_such_board")


def test_pcap_cli_round_trip(tmp_path, capsys):
    out = tmp_path / "t.pcap"
    assert pcap_cli.main(["synth", str(out), "--ntime", "32", "--tone-k", "4000", "--tone-beam", "1"]) == 0
    assert pcap_cli.main(["check", str(out)]) == 0
    text = capsys.readouterr().out
    assert "looks like CHANNEL" in text
    assert "beam 1: " in text and "peak k=4000" in text


def test_board_cli_status_on_fake_board(monkeypatch, fake_fpga, capsys):
    from test_telemetry import healthy_registers
    cfg = load_board_config("rfsoc0")
    fake_fpga.registers.update(healthy_registers(cfg))
    monkeypatch.setattr("lft3.board.control._default_connect", lambda host: fake_fpga)
    assert board_cli.main(["-v", "status", "--interval", "0.01"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("rfsoc0 (192.168.2.100):")
    assert "tx_packet_rate" in out
    # read-only: nothing was written
    assert not [op for op, _, _ in fake_fpga.log if op == "write_int"]
