"""A station: every RFSoC board of the instrument, operated together.

Each board operation runs on all boards in parallel (one thread per board; casperfpga/katcp calls block on
the network, so threads give real concurrency). Results come back per board, so one failing board does not
hide the others:

    st = Station(load_station_config())
    st.start()                         # initialise all boards in parallel, then sync them together
    for name, r in st.status().items(): ...

Multi-board time alignment: the firmware syncs on a software register write, so `sync()` releases all
boards' writes from a barrier (spread typically ~ms over katcp). Sample-accurate alignment needs the
PPS-armed sync requested in docs/firmware_requests.md #4.
"""
from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

from lft3.config import StationConfig

from .control import BoardError, Rfsoc4x2
from .telemetry import Check, Snapshot, health, read_snapshot

log = logging.getLogger(__name__)


@dataclass
class BoardResult:
    board: str
    ok: bool
    value: Any = None
    error: str | None = None
    seconds: float = 0.0


class StationError(BoardError):
    def __init__(self, results: dict[str, BoardResult]):
        self.results = results
        failed = {n: r.error for n, r in results.items() if not r.ok}
        super().__init__("failed on " + "; ".join(f"{n}: {e}" for n, e in failed.items()))


class Station:
    def __init__(self, config: StationConfig, connect: Callable[[str], Any] | None = None, **board_kwargs):
        self.cfg = config
        self.boards: dict[str, Rfsoc4x2] = {
            b.name: Rfsoc4x2(b, connect=connect, **board_kwargs) for b in config.boards
        }

    def __len__(self) -> int:
        return len(self.boards)

    # -- parallel execution ------------------------------------------------------------------------
    def each(self, fn: Callable[[Rfsoc4x2], Any], names: Iterable[str] | None = None,
             raise_on_error: bool = False) -> dict[str, BoardResult]:
        """Run fn(board) on every board in parallel; returns {name: BoardResult} in station order."""
        selected = [self.boards[n] for n in (names or self.boards)]

        def run(board: Rfsoc4x2) -> BoardResult:
            t0 = time.monotonic()
            try:
                return BoardResult(board.cfg.name, True, fn(board), seconds=time.monotonic() - t0)
            except Exception as exc:  # report per board, keep going
                log.error("%s: %s", board.cfg.name, exc)
                return BoardResult(board.cfg.name, False, error=f"{type(exc).__name__}: {exc}",
                                   seconds=time.monotonic() - t0)

        with ThreadPoolExecutor(max_workers=max(1, len(selected))) as pool:
            results = list(pool.map(run, selected))
        out = {r.board: r for r in results}
        if raise_on_error and not all(r.ok for r in results):
            raise StationError(out)
        return out

    @contextlib.contextmanager
    def lock(self):
        """Lock every board (in a fixed order, so two stations cannot deadlock)."""
        with contextlib.ExitStack() as stack:
            for name in sorted(self.boards):
                stack.enter_context(self.boards[name].lock())
            yield

    # -- control ------------------------------------------------------------------------------------
    def sync(self) -> dict[str, BoardResult]:
        """Sync all boards as close together as software allows; value = write time of each board."""
        barrier = threading.Barrier(len(self.boards))

        def synced(board: Rfsoc4x2) -> float:
            _ = board.fpga  # connect before the barrier
            barrier.wait(timeout=30)
            t = time.time()
            board.sync()
            return t

        results = self.each(synced, raise_on_error=True)
        times = [r.value for r in results.values()]
        log.info("station sync: %d boards, spread %.1f ms", len(times), 1e3 * (max(times) - min(times)))
        return results

    def start(self, program: bool = True, trigger: bool = False) -> dict[str, BoardResult]:
        with self.lock():
            results = self.each(lambda b: b.initialise(program), raise_on_error=True)
            self.sync()
            if trigger:
                self.each(lambda b: b.trigger_dump(), raise_on_error=True)
        return results

    def set_destination_macs(self) -> dict[str, BoardResult]:
        with self.lock():
            return self.each(lambda b: (b.describe_design(), b.set_destination_mac())[1], raise_on_error=True)

    def trigger(self) -> dict[str, BoardResult]:
        with self.lock():
            return self.each(lambda b: b.trigger_dump(), raise_on_error=True)

    # -- monitoring -----------------------------------------------------------------------------------
    def snapshots(self, arp: bool = False) -> dict[str, BoardResult]:
        def snap(board: Rfsoc4x2) -> Snapshot:
            s = read_snapshot(board.fpga)
            if arp:
                board.describe_design()
                s.destination_mac = board.read_destination_mac()
            return s
        return self.each(snap)

    def status(self, interval: float = 1.0, arp: bool = False) -> dict[str, tuple[Snapshot, list[Check]] | None]:
        """Two snapshots `interval` apart on every board → (latest snapshot, health checks), None if unreachable."""
        first = self.snapshots(arp)
        time.sleep(interval)
        second = self.snapshots(False)
        out: dict[str, tuple[Snapshot, list[Check]] | None] = {}
        for name, board in self.boards.items():
            a, b = first[name], second[name]
            if not (a.ok and b.ok):
                out[name] = None
                continue
            b.value.destination_mac = a.value.destination_mac
            out[name] = (b.value, health(board.cfg, b.value, a.value))
        return out
