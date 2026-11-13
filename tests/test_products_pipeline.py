import h5py
import numpy as np
import pytest
import yaml

from lft3.board.capture import Capture
from lft3.cli import plot as plot_cli
from lft3.pipeline.flowgraph import PipelineOptions, build_flowgraph
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


@pytest.mark.parametrize("source", ["network", "synthetic", "file"])
def test_flowgraph_generation(cfg, source):
    opts = PipelineOptions(source=source, file="/data/cap.pcap" if source == "file" else None,
                           fft_length=256, integration_s=0.5, channel_start=1024, channel_count=2048, monitor=True)
    doc = yaml.safe_load(build_flowgraph(cfg, opts, "/out"))
    nodes = {n["name"]: n for n in doc["graph"]}
    rx = nodes["receiver"]["config"]
    assert rx["source"] == source and rx["fftLength"] == 256 and rx["segments"] == 4
    assert rx["channelStart"] == 1024 and rx["channelCount"] == 2048
    assert rx["boardId"] == cfg.board_id and rx["interfaceAddress"] == cfg.receiver_interface
    assert rx["inputBeam"] == "[0, 0, 1, 1]" and rx["inputPolarization"] == "[0, 1, 0, 1]"
    blocks = nodes["finespec"]["config"]["integrationBlocks"]
    assert blocks == round(0.5 * 152587.890625 / 1024)
    assert nodes["writer"]["config"]["ncoMHz"] == -2031.25
    assert nodes["quicklook"]["config"]["mode"] == "coarse"
    assert "beam1_waterfall" in nodes
    if source == "file":
        assert rx["sourceFile"] == "/data/cap.pcap"


def test_flowgraph_rejects_bad_options(cfg):
    with pytest.raises(ValueError):
        build_flowgraph(cfg, PipelineOptions(fft_length=100), "/out")
    with pytest.raises(ValueError):
        build_flowgraph(cfg, PipelineOptions(channel_start=512), "/out")
    with pytest.raises(ValueError):
        build_flowgraph(cfg, PipelineOptions(source="file"), "/out")
