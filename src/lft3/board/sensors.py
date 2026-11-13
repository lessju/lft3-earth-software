"""Board physical sensors (rail voltages/currents, temperatures).

They are only visible in the RFSoC's own Linux sysfs (I2C power monitors at 0x40-0x48, IIO temperature
channels), not over katcp. This module reads them with one SSH command (key-based, BatchMode) and parses
the output. Rail addresses and current limits come from the RFSoC 4x2 schematics via
lft3-earth-main/host/scripts/power_util.py.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass

# rail -> (I2C address, current limit in A)
RAILS = {
    "VCC_0V85": (0x40, 40.0),
    "VCC_3V3": (0x41, 6.0),
    "VCC_1V8": (0x42, 6.0),
    "VDAC_AVCC_0V925": (0x43, 3.0),
    "VADC_AVCC_0V925": (0x44, 3.0),
    "VADC_AVCC_AUX_1V8": (0x45, 2.0),
    "VDAC_AVCC_AUX_1V8": (0x46, 2.0),
    "VDAC_AVTT_2V5": (0x47, 2.0),
    "SYZYGY_VIO_BUS": (0x48, 6.0),
}

# Prints "<path> <value>" for every file we need; tolerant of missing devices.
REMOTE_COMMAND = (
    "for f in /sys/bus/i2c/devices/0-00*/iio:device*/in_current3_raw "
    "/sys/bus/i2c/devices/0-00*/iio:device*/in_current3_scale "
    "/sys/bus/i2c/devices/0-00*/iio:device*/in_voltage1_raw "
    "/sys/bus/i2c/devices/0-00*/iio:device*/in_voltage1_scale "
    "/sys/bus/iio/devices/iio:device*/in_temp*_raw /sys/bus/iio/devices/iio:device*/in_temp*_offset "
    "/sys/bus/iio/devices/iio:device*/in_temp*_scale /sys/bus/iio/devices/iio:device*/name; "
    "do [ -r \"$f\" ] && echo \"$f $(cat \"$f\")\"; done"
)


@dataclass
class Rail:
    name: str
    volts: float
    amps: float
    limit_amps: float

    @property
    def utilisation(self) -> float:
        return self.amps / self.limit_amps


@dataclass
class Sensors:
    rails: list[Rail]
    temperatures_c: dict[str, float]


def parse(dump: str) -> Sensors:
    files: dict[str, str] = {}
    for line in dump.splitlines():
        path, _, value = line.strip().partition(" ")
        if path:
            files[path] = value.strip()

    def num(path: str) -> float | None:
        try:
            return float(files[path])
        except (KeyError, ValueError):
            return None

    rails: list[Rail] = []
    for name, (addr, limit) in RAILS.items():
        prefix = f"/sys/bus/i2c/devices/0-00{addr:x}/"
        dev = next((p.rsplit("/", 1)[0] for p in files if p.startswith(prefix) and p.endswith("in_current3_raw")), None)
        if dev is None:
            continue
        i_raw, i_scale = num(dev + "/in_current3_raw"), num(dev + "/in_current3_scale")
        v_raw, v_scale = num(dev + "/in_voltage1_raw"), num(dev + "/in_voltage1_scale")
        if None in (i_raw, i_scale, v_raw, v_scale):
            continue
        rails.append(Rail(name, v_raw * v_scale / 1000.0, i_raw * i_scale / 1000.0, limit))

    temps: dict[str, float] = {}
    for path in files:
        if "/sys/bus/iio/devices/" not in path or not path.endswith("_raw") or "/in_temp" not in path:
            continue
        dev, fname = path.rsplit("/", 1)
        prefix = fname[:-4]                                   # in_temp0_ps_temp
        raw = num(path)
        offset = num(f"{dev}/{prefix}_offset")
        scale = num(f"{dev}/{prefix}_scale")
        if offset is None:
            offset = num(f"{dev}/in_temp_offset") or 0.0
        if scale is None:
            scale = num(f"{dev}/in_temp_scale")
        if raw is None or scale is None:
            continue
        dev_name = files.get(f"{dev}/name", dev.rsplit("/", 1)[-1])
        temps[f"{dev_name}/{prefix}"] = (raw + offset) * scale / 1000.0
    return Sensors(rails, temps)


def read_sensors(host: str, user: str = "casper", timeout: float = 10.0) -> Sensors:
    out = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", f"{user}@{host}", REMOTE_COMMAND],
        capture_output=True, text=True, timeout=timeout, check=True,
    ).stdout
    return parse(out)
