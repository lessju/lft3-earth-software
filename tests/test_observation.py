"""Observation config (config/observations/*.yaml) and how a run plan and the live search use it."""
import struct

import pytest
import yaml

from lft3 import ring
from lft3.config import (
    ConfigError,
    load_observation_config,
    load_station_config,
    observation_to_dict,
    parse_observation_config,
)
from lft3.pipeline.runtime import RunOptions, plan_run, search_cpus
from lft3.search.core import axes_from_nco, rfi_label, window_ends_at


def test_repository_observation_loads_and_round_trips(tmp_path):
    st = load_station_config()
    obs = st.observation_config()
    assert obs.name == st.observation == "default"
    assert obs.spectrometer.fine_channels_per_coarse == 128
    assert obs.spectrometer.resolution_hz == pytest.approx(1192.09, abs=0.01)
    assert obs.tsamp == pytest.approx(149 * 1024 / 152587.890625)
    assert obs.drift_resolution_hz_s == pytest.approx(1192.09 / (64 * obs.tsamp), rel=1e-4)
    cfg = obs.search.hyperseti_config()
    assert cfg["dedoppler"]["max_dd"] == 200.0 and cfg["hitsearch"]["threshold"] == 20.0
    assert cfg["dedoppler"]["plan"] == "optimal"
    assert cfg["preprocess"]["sk_flag"] is False and cfg["preprocess"]["blank_edges"] == {"n_chan": 8}
    p = tmp_path / "o.yaml"
    p.write_text(yaml.safe_dump(observation_to_dict(obs)))
    back = load_observation_config(str(p))
    assert back.spectrometer == obs.spectrometer and back.search == obs.search


def test_resolution_hz_picks_the_nearest_power_of_two():
    o = parse_observation_config({"spectrometer": {"resolution_hz": 600}})
    assert o.spectrometer.fine_channels_per_coarse == 256
    with pytest.raises(ConfigError, match="not both"):
        parse_observation_config({"spectrometer": {"resolution_hz": 600, "fine_channels_per_coarse": 256}})


@pytest.mark.parametrize("bad, match", [
    ({"spectrometer": {"fine_channels_per_coarse": 100}}, "power of two"),
    ({"spectrometer": {"fine_channels_per_coarse": 131072}}, "Hz-resolution path"),
    ({"spectrometer": {"coarse_channels": [512, 1024]}}, "whole packets"),
    ({"search": {"max_drift_hz_s": 10, "window_spectra": 16, "window_step_spectra": 8}}, "never crosses"),
    ({"search": {"window_step_spectra": 128}}, "window_step_spectra"),
    ({"search": {"dedoppler_plan": "full"}}, "dedoppler_plan"),
    ({"search": {"ring_spectra": 8}}, "ring_spectra"),
    ({"search": {"window": 16}}, "unknown keys"),
    ({"ring": {"seconds": 1.0}}, "ring.seconds"),
])
def test_invalid_observations_are_rejected(bad, match):
    with pytest.raises(ConfigError, match=match):
        parse_observation_config(bad)


def test_hyperseti_overrides_merge():
    raw = {"dedoppler": {"plan": "stepped"}, "preprocess": {"poly_fit": 3}}
    o = parse_observation_config({"search": {"hyperseti": raw}})
    cfg = o.search.hyperseti_config()
    assert cfg["dedoppler"]["plan"] == "stepped" and cfg["dedoppler"]["max_dd"] == 200.0
    assert cfg["preprocess"]["poly_fit"] == 3 and cfg["preprocess"]["normalize"] is True


def _arg(argv, key):
    return argv[argv.index(key) + 1]


def test_run_options_from_observation_and_search_process(cfg, tmp_path):
    obs = load_observation_config("default")
    opts = RunOptions.from_observation(obs, source="synthetic", lossless=True, random_noise=True, tone_drift_hz_s=50.0,
                                       fft_length=64)
    assert opts.fft_length == 64 and opts.search_live and opts.spectra_ring == 64
    plan = plan_run(cfg, opts, "/data", str(tmp_path / "run"), "20261120T000000")
    roles = [p.role for p in plan.processes]
    assert roles == ["capture", "spectrometer", "dump", "search"]
    cap, spec, _dump, search = plan.processes
    assert _arg(cap.argv, "--noise") == "random" and _arg(cap.argv, "--tone-drift") == "50"
    assert _arg(spec.argv, "--spectra-ring") == "64" and _arg(spec.argv, "--fft-length") == "64"
    assert search.host and "--lossless" in search.argv
    assert _arg(search.argv, "--observation") == str(tmp_path / "run" / "observation.yaml")
    assert _arg(search.argv, "--stream") == f"{cfg.name}-link0"
    run = search.argv_to_run()
    assert run[0] != "docker" and "lft3.search.live" in run
    off = RunOptions.from_observation(obs, search_live=False)
    assert off.spectra_ring == 0 and "search" not in [p.role for p in plan_run(cfg, off, "/d", str(tmp_path)).processes]
    with pytest.raises(ValueError, match="random_noise"):
        RunOptions.from_observation(obs, source="synthetic", tone_drift_hz_s=5.0).validate()


def test_search_cpus_avoid_the_big_cores():
    assert search_cpus("8,9,0-4") == "0,1,2,3,4"
    assert search_cpus("18,19,10-14") == "10,11,12,13,14"
    assert search_cpus("") == ""
    assert search_cpus("8,9") == "8,9"          # nothing else available: keep them


def test_window_grid():
    # windows of 16 every 8: window k = integrations [8k, 8k + 16)
    assert window_ends_at(15, 16, 8) == 0
    assert window_ends_at(23, 16, 8) == 1
    assert window_ends_at(16, 16, 8) is None and window_ends_at(7, 16, 8) is None
    ends = [k for k in range(100) if window_ends_at(k, 16, 8) is not None]
    assert ends == list(range(15, 100, 8))


def test_axes_from_nco_match_the_spectrometer():
    fch1, foff = axes_from_nco(-2031.25, 0, 128)
    assert foff == pytest.approx(0.0011920928955078125)
    assert fch1 == pytest.approx(1562.4237060546875)          # reference FBH5 (CyberEther synthetic run)
    assert rfi_label(1875.0, foff).startswith("rfi:board-spur")


def _spectra_control(tmp_path, stream="rfsoc0-link0-spectra"):
    b = bytearray(ring.CONTROL_BYTES)
    struct.pack_into("<QII Q", b, 0, ring.MAGIC, ring.VERSION, ring.CONTROL_BYTES, 99)
    b[24:24 + len(stream)] = stream.encode()
    struct.pack_into("<7Q", b, 264, 0, 6144, 152576, 2, 64, 2 * 786432 * 4, 402653184)
    struct.pack_into("<II", b, 328, 0, 1)
    struct.pack_into("<Q", b, 368, 10)
    struct.pack_into("<IIddd", b, 528, ring.KIND_SPECTRA, 128, 1562.42, 0.001192, 0.99992)
    (tmp_path / f"lft3-{stream}").write_bytes(bytes(b))
    return stream


def test_spectra_ring_status_and_python_reader(tmp_path, monkeypatch):
    monkeypatch.setattr(ring, "SHM_DIR", tmp_path)
    stream = _spectra_control(tmp_path)
    assert ring.list_streams("spectra") == [stream] and ring.list_streams("voltages") == []
    with ring.Ring(stream) as r:
        st = r.status()
        assert (st.kind, st.fine_channels_per_coarse, st.channels_out) == ("spectra", 128, 786432)
        assert st.fch1_mhz == 1562.42 and st.tsamp_s == 0.99992
        assert "lft3_spectra_ring_head" in ring.to_prometheus([st])
    with ring.RingReader(stream, "search", lossless=True) as rd:
        assert rd.entry == 0 and rd.cursor == 10
        rd.consumed(11, busy_ns=5_000_000, outputs=3)
        rd.add(1, 2)
        with ring.Ring(stream) as r:
            [e] = r.status().readers
            assert (e.name, e.lossless, e.cursor, e.outputs, e.overruns) == ("search", True, 11, 3, 2)
            assert e.busy_ms == pytest.approx(5.0)
        with ring.RingReader(stream, "second") as rd2:   # a second reader takes the next entry
            assert rd2.entry == 1
    with ring.Ring(stream) as r:
        assert r.status().readers == []                  # entries released on close


def test_version_1_rings_still_read_as_voltages(tmp_path, monkeypatch):
    monkeypatch.setattr(ring, "SHM_DIR", tmp_path)
    b = bytearray(ring.CONTROL_BYTES)
    struct.pack_into("<QII", b, 0, ring.MAGIC, 1, ring.CONTROL_BYTES)
    (tmp_path / "lft3-old").write_bytes(bytes(b))
    with ring.Ring("old") as r:
        assert r.status().kind == "voltages"


def test_merge_hits_keeps_the_strongest_of_each_cluster():
    from lft3.search.core import merge_hits
    #        drift idx, channel, boxcar, snr
    hits = [(5, 100, 1, 30.0), (6, 101, 1, 50.0), (5, 103, 1, 20.0), (9, 101, 1, 40.0), (6, 200, 1, 25.0)]
    d, c, b, s = zip(*hits)
    keep = merge_hits(d, c, b, s)
    # 50 @ (6,101) absorbs (5,100) and (5,103); (9,101) is 3 trials away; (6,200) far away
    assert keep == [1, 3, 4]
    assert merge_hits([], [], [], []) == []
