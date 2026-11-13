"""Instrument configuration: YAML files under config/ are the single source of truth.

    cfg = load_board_config("rfsoc0")                 # config/boards/rfsoc0.yaml
    cfg = load_board_config("/path/to/board.yaml")

The config directory is $LFT3_CONFIG_DIR, or the repository's config/ when running from a checkout.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


class ConfigError(ValueError):
    pass


def config_dir() -> Path:
    env = os.environ.get("LFT3_CONFIG_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "config"


@dataclass
class InputConfig:
    beam: int
    pol: str


@dataclass
class LinkConfig:
    """One 100 GbE link from the board to this host: one lft3-capture process and one ring."""
    name: str                          # "link0", "link1": the stream is "<board>-<link>"
    interface: str                     # ConnectX PCI function, e.g. 0000:01:00.0
    master_core: int = 5
    worker_core: int = 6
    processing_core: int = 7
    udp_port: int = 60000
    source_ip: str = ""                # flow match on the board's IP ("" = any)
    beam_offset: int = 0               # global beam number of this link's beam 0 (file names, ibeam)
    reader_cpus: str = ""              # cpuset for this link's reader containers ("" = any)


@dataclass
class BoardConfig:
    name: str
    host: str
    board_id: int
    bitstream: str
    lmk_file: str
    lmx_file: str
    adc_map: list[tuple[int, int]]
    tile_mask: int
    nco_mhz: float
    inputs: list[InputConfig]
    adc_lsb: int
    ch_lsb: int
    packet_mask: int
    ddr_size: int
    ddr_read_delay: int
    source_ip: str
    destination_ip: str
    destination_mac: str
    channel_port: int
    adc_port: int
    expected_packet_rate: int
    receiver_interface: str = "0000:01:00.0"
    receiver_master_core: int = 5
    receiver_worker_core: int = 6
    receiver_processing_core: int = 7
    pps: str = "none"                 # none | temporary | reference
    expected_clk_period: int = 156250000
    ssh_user: str = "casper"
    sensors_enabled: bool = False
    links: list[LinkConfig] = field(default_factory=list)
    path: str | None = field(default=None, compare=False)

    def stream(self, link: LinkConfig) -> str:
        """Ring / stream name of a link: <board>-<link>."""
        return f"{self.name}-{link.name}"

    def link(self, name: str) -> LinkConfig:
        for lk in self.links:
            if lk.name == name:
                return lk
        raise ConfigError(f"board {self.name} has no link '{name}' (have: {', '.join(x.name for x in self.links)})")

    @property
    def header_nco(self) -> int:
        """NCO as the firmware header carries it: signed whole MHz."""
        return int(round(self.nco_mhz))


def _get(d: dict, dotted: str, default=None, required: bool = True):
    cur = d
    for key in dotted.split("."):
        if not isinstance(cur, dict) or key not in cur:
            if required:
                raise ConfigError(f"missing '{dotted}'")
            return default
        cur = cur[key]
    return cur


def parse_board_config(data: dict, path: str | None = None) -> BoardConfig:
    adc_map = [tuple(int(v) for v in pair) for pair in _get(data, "adc.map")]
    inputs = [InputConfig(int(i["beam"]), str(i["pol"]).upper()) for i in _get(data, "adc.inputs")]
    cfg = BoardConfig(
        name=str(_get(data, "name")),
        host=str(_get(data, "host")),
        board_id=int(_get(data, "board_id")),
        bitstream=str(_get(data, "bitstream")),
        lmk_file=str(_get(data, "clocks.lmk")),
        lmx_file=str(_get(data, "clocks.lmx")),
        adc_map=adc_map,
        tile_mask=int(_get(data, "adc.tile_mask")),
        nco_mhz=float(_get(data, "adc.nco_mhz")),
        inputs=inputs,
        adc_lsb=int(_get(data, "requantisation.adc_lsb")),
        ch_lsb=int(_get(data, "requantisation.ch_lsb")),
        packet_mask=int(_get(data, "packetizer.packet_mask")),
        ddr_size=int(_get(data, "packetizer.ddr_size")),
        ddr_read_delay=int(_get(data, "packetizer.ddr_read_delay")),
        source_ip=str(_get(data, "network.source_ip")),
        destination_ip=str(_get(data, "network.destination_ip")),
        destination_mac=str(_get(data, "network.destination_mac")),
        channel_port=int(_get(data, "network.channel_port")),
        adc_port=int(_get(data, "network.adc_port")),
        expected_packet_rate=int(_get(data, "network.expected_packet_rate")),
        receiver_interface=str(_get(data, "receiver.interface", "0000:01:00.0", required=False)),
        receiver_master_core=int(_get(data, "receiver.master_core", 5, required=False)),
        receiver_worker_core=int(_get(data, "receiver.worker_core", 6, required=False)),
        receiver_processing_core=int(_get(data, "receiver.processing_core", 7, required=False)),
        pps=str(_get(data, "timing.pps", "none", required=False)).lower(),
        expected_clk_period=int(_get(data, "timing.expected_clk_period", 156250000, required=False)),
        ssh_user=str(_get(data, "sensors.ssh_user", "casper", required=False)),
        sensors_enabled=bool(_get(data, "sensors.enabled", False, required=False)),
        path=path,
    )
    cfg.links = _parse_links(data, cfg)
    _validate(cfg)
    return cfg


def _parse_links(data: dict, c: BoardConfig) -> list[LinkConfig]:
    """receiver.links: [...] (one entry per 100 GbE link); the older single-link keys receiver.interface /
    master_core / worker_core / processing_core describe one link called link0."""
    raw = _get(data, "receiver.links", None, required=False)
    if raw is None:
        raw = [{"name": "link0", "interface": c.receiver_interface, "master_core": c.receiver_master_core,
                "worker_core": c.receiver_worker_core, "processing_core": c.receiver_processing_core}]
    links = []
    for i, d in enumerate(raw):
        if "interface" not in d:
            raise ConfigError(f"receiver.links[{i}] needs an interface")
        links.append(LinkConfig(
            name=str(d.get("name", f"link{i}")), interface=str(d["interface"]),
            master_core=int(d.get("master_core", 5)), worker_core=int(d.get("worker_core", 6)),
            processing_core=int(d.get("processing_core", 7)), udp_port=int(d.get("udp_port", c.channel_port)),
            source_ip=str(d.get("source_ip", c.source_ip)), beam_offset=int(d.get("beam_offset", 2 * i)),
            reader_cpus=str(d.get("reader_cpus", ""))))
    if links:
        first = links[0]
        c.receiver_interface, c.receiver_master_core = first.interface, first.master_core
        c.receiver_worker_core, c.receiver_processing_core = first.worker_core, first.processing_core
    return links


def _validate(c: BoardConfig) -> None:
    names = [lk.name for lk in c.links]
    if not names or len(set(names)) != len(names) or any(not n or "/" in n for n in names):
        raise ConfigError("receiver.links needs at least one link, with unique plain names")
    cores = [x for lk in c.links for x in (lk.master_core, lk.worker_core, lk.processing_core)]
    if len(set(cores)) != len(cores):
        raise ConfigError("receiver.links: every link needs its own master/worker/processing cores")
    if not 0 <= c.board_id <= 7:
        raise ConfigError("board_id must be 0..7 (3-bit fid)")
    if len(c.adc_map) != 4 or len(c.inputs) != 4:
        raise ConfigError("adc.map and adc.inputs need 4 entries (one per ADC input)")
    pairs = {(i.beam, i.pol) for i in c.inputs}
    if len(pairs) != 4 or any(i.pol not in ("X", "Y") or i.beam not in (0, 1) for i in c.inputs):
        raise ConfigError("adc.inputs must assign each of beam 0/1 x pol X/Y exactly once")
    if not 0 <= c.packet_mask <= 0x3F:
        raise ConfigError("packetizer.packet_mask must be 0..0x3f")
    if not 0 < c.ddr_size < 2 ** 26:
        raise ConfigError("packetizer.ddr_size must be < 2^26")
    if c.pps not in ("none", "temporary", "reference"):
        raise ConfigError("timing.pps must be none, temporary or reference")
    if len(c.destination_mac.split(":")) != 6:
        raise ConfigError("network.destination_mac must look like aa:bb:cc:dd:ee:ff")


def load_board_config(name_or_path: str) -> BoardConfig:
    p = Path(name_or_path)
    if not p.suffix:
        p = config_dir() / "boards" / f"{name_or_path}.yaml"
    if not p.exists():
        raise ConfigError(f"board config not found: {p}")
    with open(p) as fh:
        data = yaml.safe_load(fh)
    return parse_board_config(data, str(p))


def list_boards() -> list[str]:
    return sorted(p.stem for p in (config_dir() / "boards").glob("*.yaml"))


@dataclass
class StationConfig:
    name: str
    boards: list[BoardConfig]
    data_dir: str
    path: str | None = field(default=None, compare=False)

    def board(self, name: str) -> BoardConfig:
        for b in self.boards:
            if b.name == name:
                return b
        raise ConfigError(f"board '{name}' is not in station '{self.name}'")


def load_station_config(name_or_path: str = "station", boards: list[str] | None = None) -> StationConfig:
    """Load config/station.yaml (or a path). `boards` restricts the station to those board names/paths;
    boards not listed in the station file are loaded on their own (handy for a spare board)."""
    p = Path(name_or_path)
    if not p.suffix:
        p = config_dir() / f"{name_or_path}.yaml"
    if not p.exists():
        raise ConfigError(f"station config not found: {p}")
    with open(p) as fh:
        data = yaml.safe_load(fh)
    names = list(boards) if boards else [str(b) for b in _get(data, "boards")]
    station = StationConfig(
        name=str(_get(data, "name")),
        boards=[load_board_config(n) for n in names],
        data_dir=str(_get(data, "data_dir", "/opt/lft3arth/data", required=False)),
        path=str(p),
    )
    _validate_station(station)
    return station


def _validate_station(st: StationConfig) -> None:
    if not st.boards:
        raise ConfigError("station has no boards")
    for attr, what in (("name", "name"), ("host", "control address"), ("board_id", "board_id (fid)")):
        values = [getattr(b, attr) for b in st.boards]
        if len(set(values)) != len(values):
            raise ConfigError(f"boards share a {what}: {values}")
