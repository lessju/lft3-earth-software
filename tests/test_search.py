"""lft3-search helpers (no hyperseti / GPU needed)."""
import os
import time

from lft3.search.hyperseti_search import finished_files, rfi_label


def test_rfi_labels_board_spurs_and_wifi():
    foff = 0.001192
    assert rfi_label(1875.0, foff) == "rfi:board-spur-1875MHz"
    assert rfi_label(2250.000238, foff) == "rfi:board-spur-2250MHz"
    assert rfi_label(2412.0, foff) == "rfi:wifi-ch1"
    assert rfi_label(1800.04, foff) == ""
    assert rfi_label(1875.01, foff) == ""          # 10 kHz away: more than 2 fine channels


def test_finished_files_skips_the_file_being_written(tmp_path):
    names = ["b_20261113T100000_beam0.h5", "b_20261113T110000_beam0.h5", "b_20261113T110000_beam1.h5"]
    for n in names:
        (tmp_path / n).write_bytes(b"")
    old = time.time() - 3600
    os.utime(tmp_path / names[2], (old, old))     # beam1: idle for an hour (run ended)
    done = [p.name for p in finished_files(tmp_path, idle_s=120)]
    # beam0 10:00 rotated (newer file exists); beam0 11:00 is still being written; beam1 is idle
    assert done == [names[0], names[2]]
