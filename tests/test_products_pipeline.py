import h5py
import numpy as np
import pytest

from lft3.board.capture import Capture
from lft3.cli import plot as plot_cli
from lft3.pipeline.runtime import RunOptions, plan_run
from lft3.products import captures, fbh5, plot


def write_fbh5(path, nt=6, nchan=1024, tone=300):
    """Minimal file with the layout lft3_fbh5_writer produces (filterbankc99)."""
    data = np.ones((nt, 1, nchan), np.float32)
    data[:, 0, tone] = 100.0
    with h5py.File(path, "w") as h:
        h.attrs["CLASS"] = np.bytes_("FILTERBANK")
        h.attrs["lft3_nco_mhz"] = -2031.25
        d = h.create_dataset("data", data=data)
        for k, v in {"fch1": 2000.0, "foff": 0.001, "tsamp": 1.0, "tstart": 61356.5, "nchans": nchan, "nifs": 1,
                     "nfpc": 128, "source_name": np.bytes_("rfsoc0")}.items():
            d.attrs[k] = v
        h.create_dataset("lft3_missing_packets", data=np.array([5, 0, 0, 0, 0, 0], np.uint64))
    return path


def test_fbh5_selection_and_averaging(tmp_path):
    p = write_fbh5(tmp_path / "x.h5")
    s = fbh5.read_spectra(str(p), fmin=2000.1, fmax=2000.5, tmin=2, tmax=4)
    assert s.data.shape == (2, 401)
    assert s.freqs_mhz[0] == pytest.approx(2000.1) and s.freqs_mhz[-1] == pytest.approx(2000.5)
    assert s.data[:, 200].max() == 100.0                         # channel 300 = 2000.3 MHz
    assert s.times[0] == pytest.approx((61356.5 - 40587) * 86400 + 2)
    avg = fbh5.read_spectra(str(p), max_channels=128)
    assert avg.channel_average == 8 and avg.data.shape == (6, 128)
    assert fbh5.info(str(p))["duration_s"] == 6.0


def test_flatten_removes_coarse_channel_ripple(tmp_path):
    nfpc, ncoarse = 16, 64
    ripple = 1.0 + 0.5 * np.cos(2 * np.pi * (np.arange(nfpc) - nfpc / 2) / nfpc)
    data = np.tile(ripple, ncoarse)[None, None, :].repeat(3, 0).astype(np.float32)
    p = tmp_path / "r.h5"
    with h5py.File(p, "w") as h:
        d = h.create_dataset("data", data=data)
        for k, v in {"fch1": 1000.0, "foff": 0.01, "tsamp": 1.0, "tstart": 61356.5, "nfpc": nfpc}.items():
            d.attrs[k] = v
    flat = fbh5.read_spectra(str(p), flatten=True).data
    assert flat.std() / flat.mean() < 1e-6


def test_capture_round_trip_and_plots(tmp_path):
    rng = np.random.default_rng(0)
    adc = (rng.normal(0, 3, (4, 8192)) + 1j * rng.normal(0, 3, (4, 8192))).astype(np.complex64)
    cap = Capture("adc_dump", "rfsoc0", 1794482749.0, adc,
                  {"nco_mhz": -2031.25, "inputs": ["beam0X", "beam0Y", "beam1X", "beam1Y"],
                   "packets_expected": 4, "packets_received": 3, "received_mask": np.array([1, 1, 0, 1], bool)})
    path = captures.save(cap, tmp_path / "c.h5")
    back = captures.load(path)
    assert back.kind == "adc_dump" and back.board == "rfsoc0"
    np.testing.assert_array_equal(back.data, adc)
    assert back.attrs["inputs"] == ["beam0X", "beam0Y", "beam1X", "beam1Y"]
    assert back.attrs["received_mask"].tolist() == [True, True, False, True]
    assert plot.capture(back, tmp_path / "c.png").stat().st_size > 10000

    fft = Capture("fft_snapshot", "rfsoc0", 1794482749.0, rng.random((4, 8192)) + 1, {"nco_mhz": -2031.25})
    assert plot.capture(fft, tmp_path / "f.png").exists()


def test_plot_cli_on_spectra(tmp_path, capsys):
    p = str(write_fbh5(tmp_path / "s.h5"))
    assert plot_cli.main(["info", p]) == 0
    assert "FBH5 spectra (6, 1, 1024)" in capsys.readouterr().out
    assert plot_cli.main(["spectrum", p]) == 0
    assert plot_cli.main(["waterfall", p, "--fmin", "2000.2", "--fmax", "2000.4"]) == 0
    assert (tmp_path / "s_spectrum.png").exists() and (tmp_path / "s_waterfall.png").exists()


def _arg(argv, key):
    return argv[argv.index(key) + 1]


@pytest.mark.parametrize("source", ["network", "synthetic", "file"])
def test_run_plan(cfg, source, tmp_path):
    opts = RunOptions(source=source, file="/data/cap.pcap" if source == "file" else None, fft_length=256,
                      integration_s=0.5, channel_start=1024, channel_count=2048, lossless=source != "network",
                      quicklook_s=0.1)
    plan = plan_run(cfg, opts, "/data", str(tmp_path / "run"), "20261113T000000")
    roles = [p.role for p in plan.processes]
    assert roles == ["capture", "spectrometer", "quicklook", "dump"]
    cap, spec, ql, dump = plan.processes
    stream = f"{cfg.name}-link0"
    assert all(p.stream == stream and _arg(p.argv, "--stream") == stream for p in plan.processes)
    assert _arg(cap.argv, "--source") == source and _arg(cap.argv, "--channels") == "1024:2048"
    assert _arg(cap.argv, "--board-id") == str(cfg.board_id)
    assert _arg(cap.argv, "--input-beam") == "0,0,1,1" and _arg(cap.argv, "--input-pol") == "0,1,0,1"
    assert cap.privileged == (source == "network")
    assert cap.cpuset == "5,6,7"
    if source == "network":
        assert _arg(cap.argv, "--interface") == cfg.receiver_interface
        assert "--backpressure" not in cap.argv
    else:
        assert _arg(cap.argv, "--wait-readers") == "2" and "--lossless" in spec.argv and "--lossless" in ql.argv
    if source == "file":
        assert _arg(cap.argv, "--file") == "/data/cap.pcap" and "/data" in cap.mounts
    assert _arg(spec.argv, "--fft-length") == "256" and _arg(spec.argv, "--mode") == "fine"
    assert _arg(spec.argv, "--nco-mhz") == "-2031.25" and _arg(spec.argv, "--output-dir") == f"/data/spectra/{cfg.name}"
    assert _arg(spec.argv, "--coarse-dir") == f"/data/spectra/{cfg.name}/quicklook"
    assert _arg(spec.argv, "--latest").endswith("latest")
    assert _arg(ql.argv, "--mode") == "coarse" and _arg(ql.argv, "--integration") == "0.1"
    assert _arg(ql.argv, "--latest").endswith("latest_fast") and "--no-record" in ql.argv
    assert _arg(dump.argv, "--output-dir") == f"/data/dumps/{cfg.name}"
    assert plan.obs_id == f"{cfg.name}-20261113T000000" and _arg(cap.argv, "--obs-id") == plan.obs_id
    docker = spec.docker_argv(uid=1000, gid=1000)
    assert docker[:3] == ["docker", "run", "--rm"] and "--ipc=host" in docker and "--privileged" not in docker
    if source == "network":
        d = cap.docker_argv()
        assert "--privileged" not in d and "NET_RAW" in d and "IPC_LOCK" in d and "/dev/infiniband" in d


def test_run_plan_two_links(cfg, tmp_path):
    from lft3.config import LinkConfig
    cfg.links.append(LinkConfig("link1", "0000:01:00.1", 15, 16, 17, beam_offset=2, reader_cpus="18,19"))
    plan = plan_run(cfg, RunOptions(source="synthetic", dump=False), "/data", str(tmp_path))
    assert plan.streams() == [f"{cfg.name}-link0", f"{cfg.name}-link1"]
    l1 = [p for p in plan.processes if p.stream.endswith("link1")]
    assert [p.role for p in l1] == ["capture", "spectrometer"]
    assert _arg(l1[1].argv, "--beam-offset") == "2" and l1[0].cpuset == "15,16,17" and l1[1].cpuset == "18,19"
    only = plan_run(cfg, RunOptions(source="synthetic", links=["link1"]), "/data", str(tmp_path))
    assert only.streams() == [f"{cfg.name}-link1"]


def test_run_plan_rejects_bad_options(cfg, tmp_path):
    for bad in (RunOptions(fft_length=100), RunOptions(channel_start=512), RunOptions(source="file"),
                RunOptions(lossless=True), RunOptions(ring_seconds=1.0, dump_max_s=1.0)):
        with pytest.raises(ValueError):
            plan_run(cfg, bad, "/data", str(tmp_path))
