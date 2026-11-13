"""Metrics export for monitoring systems.

* Prometheus text exposition format (scraped over HTTP by Prometheus, graphed in Grafana), served by
  MetricsServer or written to a file for node_exporter's textfile collector.
* JSON lines, for logs and quick scripts.

Metric names: lft3_board_<register> (raw value), lft3_board_<register>_rate (per second, counters),
lft3_board_health{check=...} (0 OK, 1 INFO, 2 UNKNOWN, 3 WARN, 4 FAIL), lft3_board_rail_volts/amps{rail=...},
lft3_board_temperature_celsius{sensor=...}. Every series carries board="<name>".
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from lft3.board.registers import REGISTERS
from lft3.board.sensors import Sensors
from lft3.board.telemetry import Check, Snapshot

HEALTH_CODE = {"OK": 0, "INFO": 1, "UNKNOWN": 2, "WARN": 3, "FAIL": 4}


def _metric_name(register: str) -> str:
    return "lft3_board_" + register.replace("packetizer_onehundred_gbe_gmac_reg_", "gbe_")


def _labels(**kv: str) -> str:
    return "{" + ",".join(f'{k}="{str(v)}"' for k, v in kv.items()) + "}"


def to_prometheus(board: str, snap: Snapshot, rates: dict[str, float | None], checks: list[Check],
                  sensors: Sensors | None = None) -> str:
    lines: list[str] = []
    for name, value in sorted(snap.values.items()):
        if value is None:
            continue
        reg = REGISTERS.get(name)
        m = _metric_name(name)
        kind = "counter" if reg is not None and reg.kind == "counter" else "gauge"
        if reg is not None:
            lines.append(f"# HELP {m} {reg.description}")
        lines.append(f"# TYPE {m} {kind}")
        lines.append(f"{m}{_labels(board=board)} {value}")
    for name, rate in sorted(rates.items()):
        if rate is not None:
            m = _metric_name(name) + "_rate"
            lines.append(f"# TYPE {m} gauge")
            lines.append(f"{m}{_labels(board=board)} {rate:.6g}")
    lines.append("# HELP lft3_board_health 0 OK, 1 INFO, 2 UNKNOWN, 3 WARN, 4 FAIL")
    lines.append("# TYPE lft3_board_health gauge")
    for c in checks:
        lines.append(f"lft3_board_health{_labels(board=board, check=c.name)} {HEALTH_CODE[c.status]}")
    if sensors is not None:
        lines.append("# TYPE lft3_board_rail_volts gauge")
        for r in sensors.rails:
            lines.append(f"lft3_board_rail_volts{_labels(board=board, rail=r.name)} {r.volts:.4f}")
        lines.append("# TYPE lft3_board_rail_amps gauge")
        for r in sensors.rails:
            lines.append(f"lft3_board_rail_amps{_labels(board=board, rail=r.name)} {r.amps:.4f}")
        lines.append("# TYPE lft3_board_temperature_celsius gauge")
        for k, t in sorted(sensors.temperatures_c.items()):
            lines.append(f"lft3_board_temperature_celsius{_labels(board=board, sensor=k)} {t:.2f}")
    lines.append(f"lft3_board_last_update_seconds{_labels(board=board)} {snap.time:.3f}")
    return "\n".join(lines) + "\n"


def to_json(board: str, snap: Snapshot, rates: dict[str, float | None], checks: list[Check],
            sensors: Sensors | None = None) -> str:
    doc = {
        "board": board,
        "time": snap.time,
        "registers": snap.values,
        "rates": {k: v for k, v in rates.items() if v is not None},
        "destination_mac": snap.destination_mac,
        "health": {c.name: {"status": c.status, "detail": c.detail} for c in checks},
    }
    if sensors is not None:
        doc["rails"] = {r.name: {"volts": r.volts, "amps": r.amps, "utilisation": r.utilisation} for r in sensors.rails}
        doc["temperatures_c"] = sensors.temperatures_c
    return json.dumps(doc, sort_keys=True)


class MetricsServer:
    """Serves the latest Prometheus text on http://<bind>:<port>/metrics from a background thread."""

    def __init__(self, port: int, bind: str = "0.0.0.0"):
        self._text = "# no data yet\n"
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 (http.server API)
                if self.path.rstrip("/") not in ("", "/metrics"):
                    self.send_error(404)
                    return
                with outer._lock:
                    body = outer._text.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):  # quiet
                pass

        self._server = ThreadingHTTPServer((bind, port), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def update(self, text: str) -> None:
        with self._lock:
            self._text = text

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
