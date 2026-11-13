"""Shared fixtures: a fake casperfpga board that records every operation."""
from __future__ import annotations

from typing import Any

import pytest

from lft3.board.registers import GBE
from lft3.config import load_board_config


class FakeRfdc:
    ADC_TILE = 0
    EVNT_SRC_SYSREF = 1

    def __init__(self, log: list[tuple]):
        self.log = log
        self.mts_ok = True

    def __getattr__(self, name: str):
        def call(*args, **kwargs):
            self.log.append(("rfdc." + name, args, tuple(sorted(kwargs.items()))))
            if name == "run_mts":
                return self.mts_ok
            if name == "status":
                return {"ok": True}
            return None
        return call


class FakeGbe:
    def __init__(self, log: list[tuple]):
        self.log = log
        self.arp: dict[int, int] = {}

    def set_single_arp_entry(self, ip: str, mac: int) -> None:
        self.log.append(("gbe.set_single_arp_entry", (ip, mac), ()))
        o = [int(x) for x in ip.split(".")]
        self.arp[(o[2] * 256 + o[3]) % 512] = mac

    def get_arp_details(self, N: int = 256):
        return [self.arp.get(i, 0xFFFFFFFFFFFF) for i in range(N)]


class FakeFpga:
    def __init__(self, registers: dict[str, int] = None):
        self.log: list[tuple] = []
        self.registers: dict[str, int] = dict(registers or {})
        self.adcs = {"rfdc": FakeRfdc(self.log)}
        self.gbes = {GBE: FakeGbe(self.log)}

    def write_int(self, name: str, value: int) -> None:
        self.log.append(("write_int", (name, value), ()))
        self.registers[name] = value

    def read_uint(self, name: str) -> int:
        if name not in self.registers:
            raise KeyError(name)
        return self.registers[name]

    def upload_to_ram_and_program(self, path: str) -> None:
        self.log.append(("program", (path,), ()))

    def get_system_information(self, path: str) -> None:
        self.log.append(("get_system_information", (path,), ()))


@pytest.fixture
def cfg():
    return load_board_config("rfsoc0")


@pytest.fixture
def fake_fpga():
    return FakeFpga()


@pytest.fixture
def fake_connect(fake_fpga) -> Any:
    return lambda host: fake_fpga
