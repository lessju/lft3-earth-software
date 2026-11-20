"""Instrument configuration: YAML files under config/ are the single source of truth.

    cfg = load_board_config("rfsoc0")                 # config/boards/rfsoc0.yaml
    cfg = load_board_config("/path/to/board.yaml")
    obs = load_observation_config("default")          # config/observations/default.yaml
    st = load_station_config(); obs = st.observation_config()

The config directory is $LFT3_CONFIG_DIR, or the repository's config/ when running from a checkout.
"""
from __future__ import annotations

import math
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
    observation: str = "default"      # config/observations/<name>.yaml
    path: str | None = field(default=None, compare=False)

    def observation_config(self, name_or_path: str | None = None) -> ObservationConfig:
        return load_observation_config(name_or_path or self.observation)

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
        observation=str(_get(data, "observation", "default", required=False)),
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


# ------------------------------------------------------------------------------------------- observation

COARSE_CHANNEL_HZ = 152587.890625      # board channel width (1250 MSPS / 8192)
TOTAL_COARSE_CHANNELS = 6144
CHANNELS_PER_PACKET = 1024
MAX_FUSED_FFT = 1024                   # the fused spectrometer's limit (one block of 1024 spectra)


@dataclass
class SpectrometerSettings:
    fine_channels_per_coarse: int = 128
    integration_s: float = 1.0
    channel_start: int = 0
    channel_count: int = TOTAL_COARSE_CHANNELS
    record_fine: bool = True
    coarse_product: bool = True
    spectra_per_file: int = 3600

    @property
    def resolution_hz(self) -> float:
        return COARSE_CHANNEL_HZ / self.fine_channels_per_coarse

    @property
    def fine_channels(self) -> int:
        """Fine channels per beam."""
        return self.channel_count * self.fine_channels_per_coarse


@dataclass
class SearchSettings:
    live: bool = True
    window_spectra: int = 64
    window_step_spectra: int = 32
    max_drift_hz_s: float = 200.0
    snr_threshold: float = 20.0
    dedoppler_plan: str = "optimal"
    flatten_coarse_ripple: bool = True
    sk_flag: bool = False
    blank_edge_channels: int = 8
    gulp_channels: int = 2 ** 17
    max_missing_fraction: float = 0.01
    ring_spectra: int = 64
    hyperseti: dict = field(default_factory=dict)

    def hyperseti_config(self) -> dict:
        """The hyperseti pipeline config (GulpPipeline) for these settings, with the raw overrides applied."""
        cfg = {
            "preprocess": {"sk_flag": self.sk_flag, "normalize": True,
                           "blank_edges": {"n_chan": self.blank_edge_channels},
                           "blank_extrema": {"threshold": 10000}},
            "dedoppler": {"kernel": "dedoppler", "max_dd": self.max_drift_hz_s, "apply_smearing_corr": True,
                          "plan": self.dedoppler_plan},
            "hitsearch": {"threshold": self.snr_threshold, "merge_boxcar_trials": True},
            "pipeline": {},
        }
        if not self.blank_edge_channels:
            cfg["preprocess"].pop("blank_edges")
        for section, values in self.hyperseti.items():
            if isinstance(values, dict):
                cfg.setdefault(section, {}).update(values)
            else:
                cfg[section] = values
        return cfg


@dataclass
class ObservationConfig:
    name: str
    description: str = ""
    spectrometer: SpectrometerSettings = field(default_factory=SpectrometerSettings)
    search: SearchSettings = field(default_factory=SearchSettings)
    dump_enabled: bool = True
    dump_max_window_s: float = 1.0
    ring_seconds: float = 2.0
    path: str | None = field(default=None, compare=False)

    @property
    def tsamp(self) -> float:
        """Spectrum length after rounding to whole 1024-spectrum blocks."""
        blocks = max(1, round(self.spectrometer.integration_s * COARSE_CHANNEL_HZ / 1024))
        return blocks * 1024 / COARSE_CHANNEL_HZ

    def summary(self) -> str:
        sp, se = self.spectrometer, self.search
        text = (f"{self.name}: {sp.fine_channels_per_coarse} fine channels per board channel "
                f"({sp.resolution_hz:.2f} Hz), {self.tsamp:.4f} s spectra, channels {sp.channel_start}+"
                f"{sp.channel_count}")
        if se.live:
            text += (f"; live search {se.window_spectra} spectra every {se.window_step_spectra}, "
                     f"|drift| <= {se.max_drift_hz_s:g} Hz/s (step {self.drift_resolution_hz_s:.3g}), "
                     f"S/N >= {se.snr_threshold:g}")
        return text

    @property
    def drift_resolution_hz_s(self) -> float:
        return self.spectrometer.resolution_hz / (self.search.window_spectra * self.tsamp)


def _fine_channels(d: dict) -> int:
    has_n, has_res = "fine_channels_per_coarse" in d, "resolution_hz" in d
    if has_n and has_res:
        raise ConfigError("spectrometer: give fine_channels_per_coarse or resolution_hz, not both")
    if has_res:
        res = float(d["resolution_hz"])
        if res <= 0:
            raise ConfigError("spectrometer.resolution_hz must be positive")
        return 2 ** max(1, round(math.log2(COARSE_CHANNEL_HZ / res)))
    return int(d.get("fine_channels_per_coarse", 128))


def parse_observation_config(data: dict, path: str | None = None) -> ObservationConfig:
    data = data or {}
    sp_d = data.get("spectrometer") or {}
    se_d = data.get("search") or {}
    dump_d = data.get("dump") or {}
    ring_d = data.get("ring") or {}
    known = {"spectrometer": {"fine_channels_per_coarse", "resolution_hz", "integration_s", "coarse_channels",
                              "record_fine", "coarse_product", "spectra_per_file"},
             "search": set(SearchSettings.__dataclass_fields__), "dump": {"enabled", "max_window_s"},
             "ring": {"seconds"}}
    for section, d in (("spectrometer", sp_d), ("search", se_d), ("dump", dump_d), ("ring", ring_d)):
        unknown = set(d) - known[section]
        if unknown:
            raise ConfigError(f"{section}: unknown keys {sorted(unknown)}")
    chans = sp_d.get("coarse_channels", [0, TOTAL_COARSE_CHANNELS])
    if not isinstance(chans, (list, tuple)) or len(chans) != 2:
        raise ConfigError("spectrometer.coarse_channels must be [start, count]")
    sp = SpectrometerSettings(
        fine_channels_per_coarse=_fine_channels(sp_d),
        integration_s=float(sp_d.get("integration_s", 1.0)),
        channel_start=int(chans[0]), channel_count=int(chans[1]),
        record_fine=bool(sp_d.get("record_fine", True)),
        coarse_product=bool(sp_d.get("coarse_product", True)),
        spectra_per_file=int(sp_d.get("spectra_per_file", 3600)))
    defaults = SearchSettings()
    se = SearchSettings(**{k: type(getattr(defaults, k))(se_d[k]) if k != "hyperseti" else dict(se_d[k] or {})
                           for k in se_d})
    obs = ObservationConfig(
        name=str(data.get("name", Path(path).stem if path else "observation")),
        description=str(data.get("description", "")),
        spectrometer=sp, search=se,
        dump_enabled=bool(dump_d.get("enabled", True)),
        dump_max_window_s=float(dump_d.get("max_window_s", 1.0)),
        ring_seconds=float(ring_d.get("seconds", 2.0)),
        path=path)
    validate_observation(obs)
    return obs


def validate_observation(o: ObservationConfig) -> None:
    sp, se = o.spectrometer, o.search
    n = sp.fine_channels_per_coarse
    if n < 2 or n & (n - 1):
        raise ConfigError("spectrometer: fine channels per board channel must be a power of two >= 2")
    if n > MAX_FUSED_FFT:
        raise ConfigError(f"spectrometer: {n} fine channels per board channel ({sp.resolution_hz:.3g} Hz) needs the "
                          f"Hz-resolution path, which is not built yet (the fused spectrometer stops at "
                          f"{MAX_FUSED_FFT})")
    if sp.channel_start % CHANNELS_PER_PACKET or sp.channel_count % CHANNELS_PER_PACKET or sp.channel_count <= 0 \
            or sp.channel_start < 0 or sp.channel_start + sp.channel_count > TOTAL_COARSE_CHANNELS:
        raise ConfigError("spectrometer.coarse_channels: whole packets (multiples of 1024) within 0..6144")
    if sp.integration_s <= 0 or sp.spectra_per_file < 0:
        raise ConfigError("spectrometer: integration_s must be positive, spectra_per_file >= 0")
    if se.window_spectra < 4:
        raise ConfigError("search.window_spectra must be >= 4")
    if not 1 <= se.window_step_spectra <= se.window_spectra:
        raise ConfigError("search.window_step_spectra must be in 1..window_spectra")
    if se.max_drift_hz_s <= 0 or se.snr_threshold <= 0:
        raise ConfigError("search: max_drift_hz_s and snr_threshold must be positive")
    if se.max_drift_hz_s * se.window_spectra * o.tsamp < sp.resolution_hz:
        raise ConfigError(f"search: |drift| <= {se.max_drift_hz_s:g} Hz/s never crosses a {sp.resolution_hz:.4g} Hz "
                          f"channel in {se.window_spectra} spectra (hyperseti: 'No steps!'); raise max_drift_hz_s or "
                          "window_spectra")
    if se.dedoppler_plan not in ("optimal", "stepped"):
        raise ConfigError("search.dedoppler_plan must be optimal or stepped")
    if se.gulp_channels < 1024 or se.gulp_channels & (se.gulp_channels - 1):
        raise ConfigError("search.gulp_channels must be a power of two >= 1024")
    # The live reader keeps window_spectra in its own buffer; the ring only needs to cover its lag. The writer
    # keeps 8 slots of margin (kReadMargin) and needs >= 16 slots.
    if not 16 <= se.ring_spectra <= 4096:
        raise ConfigError("search.ring_spectra must be in 16..4096")
    if not 0 <= se.max_missing_fraction <= 1:
        raise ConfigError("search.max_missing_fraction must be in 0..1")
    if o.ring_seconds < 0.2 or (o.dump_enabled and o.dump_max_window_s + 0.2 > o.ring_seconds):
        raise ConfigError("ring.seconds must be >= 0.2 and longer than dump.max_window_s + 0.2 s")


def load_observation_config(name_or_path: str = "default") -> ObservationConfig:
    p = Path(name_or_path)
    if not p.suffix:
        p = config_dir() / "observations" / f"{name_or_path}.yaml"
    if not p.exists():
        raise ConfigError(f"observation config not found: {p}")
    with open(p) as fh:
        data = yaml.safe_load(fh)
    return parse_observation_config(data, str(p))


def list_observations() -> list[str]:
    return sorted(p.stem for p in (config_dir() / "observations").glob("*.yaml"))


def observation_to_dict(o: ObservationConfig) -> dict:
    """The YAML form of an observation config (load_observation_config reads it back unchanged)."""
    sp, se = o.spectrometer, o.search
    return {
        "name": o.name, "description": o.description,
        "spectrometer": {"fine_channels_per_coarse": sp.fine_channels_per_coarse, "integration_s": sp.integration_s,
                         "coarse_channels": [sp.channel_start, sp.channel_count], "record_fine": sp.record_fine,
                         "coarse_product": sp.coarse_product, "spectra_per_file": sp.spectra_per_file},
        "search": {k: getattr(se, k) for k in SearchSettings.__dataclass_fields__},
        "dump": {"enabled": o.dump_enabled, "max_window_s": o.dump_max_window_s},
        "ring": {"seconds": o.ring_seconds},
    }
