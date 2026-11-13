"""Read-only checks against the live board. Run with `make test-hardware` (pytest -m hardware)."""
import pytest

from lft3.board.control import Rfsoc4x2
from lft3.board.telemetry import health, read_snapshot

pytestmark = pytest.mark.hardware


def test_live_board_streams_and_matches_config(cfg):
    import time
    board = Rfsoc4x2(cfg)
    a = read_snapshot(board.fpga)
    time.sleep(1.0)
    b = read_snapshot(board.fpga)
    checks = {c.name: c for c in health(cfg, b, a)}
    assert checks["tx_packet_rate"].status == "OK", checks["tx_packet_rate"].detail
    assert checks["packetizer_fifo_full_cnt"].status == "OK"
