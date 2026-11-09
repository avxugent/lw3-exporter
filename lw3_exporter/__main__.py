"""
Entry point for the LW3 Prometheus Exporter.

Usage::

    python -m lw3_exporter [-c config.yaml] [--port 9879] [--host 0.0.0.0]

Or via the installed console script::

    lw3-exporter [-c config.yaml]

Endpoints
---------

``GET /metrics``
    Application self-metrics (process info, Python runtime, request counters).
    Intended for Prometheus to scrape the exporter process itself.

``GET /probe``
    Probe one or more LW3 devices and return their metrics.

    **Config-file mode** — scrapes all devices listed in the configuration file:

        GET /probe

    **URL-parameter mode** — probes a single device specified via query
    parameters; no config-file entry is required for that device:

        GET /probe?host=192.168.1.10&name=matrix-101

    Available query parameters:

        host        (required) Hostname or IP address of the device.
        name        Label used in Prometheus metrics. Defaults to the value of host.
        port        LW3 WebSocket (HTTPS) port. Default: 443.
        timeout     Request timeout in seconds. Default: 10.0.
        username    LW3 username. Fallback only — OpenBao is tried first
                    (see bao.py); this and the config file's per-device
                    ``username`` are what's used if that lookup misses.
        password    LW3 password. Same fallback ordering as ``username``.
        verify_tls  "true"/"false" — verify the device's TLS certificate.
                    Default: false (devices typically carry a self-signed
                    cert on their management interface).

``GET /healthz``
    Liveness check; always returns ``200 OK``. Never requires auth.

Target restrictions
-------------------

``allowed_hosts`` (list of regexes, ``fullmatch``ed against the literal ``host``
string) and ``allowed_ports`` (default ``[443]``) restrict URL-parameter mode.
Rejected targets get ``403`` before any OpenBao lookup or connection. Empty
``allowed_hosts`` = unrestricted, with a startup warning. Config-file devices
are never checked.

Authentication
--------------

If ``auth.username`` and ``auth.password`` are set (config file, or the
``LW3_AUTH_USERNAME`` / ``LW3_AUTH_PASSWORD`` env vars, which take precedence),
every endpoint except ``/healthz`` requires HTTP Basic auth. Unset = open.
The exporter speaks plain HTTP; run it behind a TLS-terminating proxy.
"""

from __future__ import annotations

import argparse
import base64
import hmac
import logging
import os
import re
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import yaml
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
    REGISTRY,
)

from . import bao
from .collector import LW3Collector

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Application-level metrics (self-monitoring)
# ---------------------------------------------------------------------------

_PROBE_REQUESTS_TOTAL = Counter(
    "lw3_exporter_probe_requests_total",
    "Total number of /probe requests received.",
    ["result"],  # labels: "success" | "error"
)

_PROBE_DURATION_SECONDS = Histogram(
    "lw3_exporter_probe_duration_seconds",
    "End-to-end duration of a /probe request in seconds.",
)

# ---------------------------------------------------------------------------
# Configuration loading
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG: dict[str, Any] = {
    "listen_host": "0.0.0.0",
    "listen_port": 9879,
    "log_level": "INFO",
    # HTTP Basic auth for the exporter's own endpoints. Disabled unless both
    # username and password are set (config file or LW3_AUTH_* env vars).
    "auth": {"username": None, "password": None},
    # URL-parameter mode target restrictions. Empty allowed_hosts = unrestricted
    # (a warning is logged). When set, `host` must fullmatch one of the regexes
    # and `port` must be in allowed_ports. Config-file devices are never checked.
    "allowed_hosts": [],
    "allowed_ports": [443],
    "devices": [],
}

_DEVICE_DEFAULTS: dict[str, Any] = {
    "port": 443,
    "timeout": 10.0,
    "username": None,
    "password": None,
    "verify_tls": False,
}


def load_config(path: str) -> dict[str, Any]:
    """
    Load and validate the YAML configuration file.

    The ``devices`` list is optional; the exporter can run without any
    pre-configured devices when all targets are supplied via URL parameters.
    Missing top-level keys fall back to :data:`_DEFAULT_CONFIG`.
    """
    cfg = dict(_DEFAULT_CONFIG)

    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    cfg.update(data)

    # Normalise the auth section; env vars override the config file so the
    # password need not live in a file.
    auth = dict(_DEFAULT_CONFIG["auth"])
    auth.update(cfg.get("auth") or {})
    auth["username"] = os.environ.get("LW3_AUTH_USERNAME") or auth["username"]
    auth["password"] = os.environ.get("LW3_AUTH_PASSWORD") or auth["password"]
    if bool(auth["username"]) != bool(auth["password"]):
        raise ValueError("'auth' needs both 'username' and 'password' (or neither).")
    cfg["auth"] = auth

    # Compile allowed_hosts regexes up front so a bad pattern fails at startup.
    compiled = []
    for pattern in cfg["allowed_hosts"] or []:
        try:
            compiled.append(re.compile(str(pattern)))
        except re.error as exc:
            raise ValueError(f"Invalid allowed_hosts regex {pattern!r}: {exc}") from exc
    cfg["allowed_hosts"] = compiled
    cfg["allowed_ports"] = [int(p) for p in (cfg["allowed_ports"] or [])]

    for i, dev in enumerate(cfg["devices"]):
        if "name" not in dev:
            raise ValueError(f"Device #{i} is missing the required 'name' field.")
        if "host" not in dev:
            raise ValueError(f"Device '{dev.get('name', i)}' is missing the required 'host' field.")
        for key, default in _DEVICE_DEFAULTS.items():
            dev.setdefault(key, default)

    return cfg


def _device_from_query_params(query_params: dict[str, list[str]]) -> dict[str, Any]:
    """
    Build a device configuration dict from URL query parameters.

    :raises ValueError: If a parameter value cannot be converted to its expected type.
    """
    host = query_params["host"][0]
    return {
        "host": host,
        "name": query_params.get("name", [host])[0],
        "port": int(query_params.get("port", [_DEVICE_DEFAULTS["port"]])[0]),
        "timeout": float(query_params.get("timeout", [_DEVICE_DEFAULTS["timeout"]])[0]),
        "username": query_params.get("username", [_DEVICE_DEFAULTS["username"]])[0],
        "password": query_params.get("password", [_DEVICE_DEFAULTS["password"]])[0],
        "verify_tls": query_params.get("verify_tls", ["false"])[0].strip().lower() == "true",
    }


def _target_allowed(cfg: dict[str, Any], host: str, port: int) -> bool:
    """
    Check a URL-mode target against ``allowed_hosts``/``allowed_ports``.
    Matches the literal host string given (no DNS resolution). Always True
    when ``allowed_hosts`` is empty.
    """
    if not cfg["allowed_hosts"]:
        return True
    return (
        any(p.fullmatch(host) for p in cfg["allowed_hosts"])
        and port in cfg["allowed_ports"]
    )


def _resolve_credentials(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Return *devices* with each entry's username/password resolved: OpenBao
    first (keyed by hostname), falling back to whatever was already set from
    the config file or URL parameters. Devices are copied rather than
    mutated in place so a config-file device list can be resolved fresh on
    every scrape.
    """
    resolved = []
    for dev in devices:
        dev = dict(dev)
        creds = bao.get_credentials(dev["host"])
        if creds:
            dev["username"], dev["password"] = creds
            source = "openbao"
        else:
            source = "config/url-param" if dev.get("password") else "none"
        logger.debug(
            "Resolved credentials for %s (%s): source=%s", dev["name"], dev["host"], source
        )
        resolved.append(dev)
    return resolved


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

def _make_handler(cfg: dict[str, Any]) -> type[BaseHTTPRequestHandler]:
    """Return a :class:`BaseHTTPRequestHandler` subclass closed over *cfg*."""

    auth_user = cfg["auth"]["username"]
    auth_pass = cfg["auth"]["password"]
    expected_auth = (
        b"Basic " + base64.b64encode(f"{auth_user}:{auth_pass}".encode("utf-8"))
        if auth_user and auth_pass
        else None
    )

    class Handler(BaseHTTPRequestHandler):

        def _authorized(self) -> bool:
            """Constant-time check of the Basic auth header (always True if auth is off)."""
            if expected_auth is None:
                return True
            supplied = self.headers.get("Authorization", "").encode("utf-8")
            return hmac.compare_digest(supplied, expected_auth)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)

            # /healthz stays open for container/orchestrator liveness checks.
            if parsed.path != "/healthz" and not self._authorized():
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="lw3-exporter", charset="UTF-8"')
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"Unauthorized")
                return

            if parsed.path == "/":
                self._handle_index()
            elif parsed.path == "/probe":
                self._handle_probe(parse_qs(parsed.query))
            elif parsed.path == "/metrics":
                self._handle_metrics()
            elif parsed.path == "/healthz":
                self._handle_healthz()
            else:
                self.send_response(404)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"Not Found")

        def _handle_index(self) -> None:
            """Render a minimal HTML landing page with application info."""
            num_devices = len(cfg["devices"])
            device_rows = "".join(
                f"<tr><td>{dev['name']}</td><td>{dev['host']}</td><td>{dev['port']}</td></tr>"
                for dev in cfg["devices"]
            )
            device_section = (
                f"""
                <h2>Pre-configured devices ({num_devices})</h2>
                <table>
                  <thead><tr><th>Name</th><th>Host</th><th>Port</th></tr></thead>
                  <tbody>{device_rows}</tbody>
                </table>"""
                if num_devices
                else """
                <h2>Pre-configured devices</h2>
                <p>No devices configured. Supply a <code>host</code> query
                parameter to <a href="/probe">/probe</a> to target a device
                at scrape time.</p>"""
            )

            body = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>lw3-exporter</title>
  <style>
    body {{ font-family: sans-serif; max-width: 860px; margin: 2rem auto; padding: 0 1rem; color: #222; }}
    h1 {{ font-size: 1.4rem; margin-bottom: 0.25rem; }}
    h2 {{ font-size: 1rem; margin-top: 2rem; margin-bottom: 0.5rem; color: #555; text-transform: uppercase; letter-spacing: .05em; }}
    p  {{ margin: 0.4rem 0; }}
    a  {{ color: #0066cc; }}
    table {{ border-collapse: collapse; width: 100%; font-size: 0.9rem; }}
    th, td {{ text-align: left; padding: 0.35rem 0.6rem; border: 1px solid #ddd; }}
    th {{ background: #f5f5f5; }}
    .endpoints td:first-child {{ font-family: monospace; white-space: nowrap; }}
    code {{ background: #f0f0f0; padding: 0.1em 0.3em; border-radius: 3px; font-size: 0.9em; }}
  </style>
</head>
<body>
  <h1>lw3-exporter</h1>
  <p>Prometheus exporter for <a href="https://lightware.com/" target="_blank">Lightware</a>
     matrix switchers, extenders, and other LW3-capable devices, over their
     LW3-over-WebSocket management endpoint.</p>

  <h2>Endpoints</h2>
  <table class="endpoints">
    <thead><tr><th>Path</th><th>Description</th></tr></thead>
    <tbody>
      <tr><td><a href="/probe">/probe</a></td>
          <td>Probe LW3 device(s) and return their metrics.
              Accepts optional <code>host</code>, <code>name</code>,
              <code>port</code>, <code>timeout</code>, <code>username</code>,
              <code>password</code>, <code>verify_tls</code>
              query parameters.</td></tr>
      <tr><td><a href="/metrics">/metrics</a></td>
          <td>Exporter self-metrics (process info, probe request counters,
              probe duration histogram).</td></tr>
      <tr><td><a href="/healthz">/healthz</a></td>
          <td>Liveness check — always returns <code>200 OK</code>.</td></tr>
    </tbody>
  </table>
  {device_section}
</body>
</html>"""

            encoded = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _handle_probe(self, query_params: dict[str, list[str]]) -> None:
            """Probe one or more LW3 devices and return their metrics."""
            start = time.monotonic()

            # URL-parameter mode: at least `host` must be present.
            if "host" in query_params:
                try:
                    devices = [_device_from_query_params(query_params)]
                except ValueError as exc:
                    _PROBE_REQUESTS_TOTAL.labels(result="error").inc()
                    self.send_error(400, f"Invalid query parameter: {exc}")
                    return
                if not _target_allowed(cfg, devices[0]["host"], devices[0]["port"]):
                    _PROBE_REQUESTS_TOTAL.labels(result="error").inc()
                    logger.warning(
                        "Rejected URL-mode probe for %s:%d (not in allowed_hosts/allowed_ports)",
                        devices[0]["host"], devices[0]["port"],
                    )
                    self.send_error(403, "Target host/port not allowed")
                    return
                logger.debug(
                    "Probing device from URL parameters: %s (%s:%d)",
                    devices[0]["name"], devices[0]["host"], devices[0]["port"],
                )
            else:
                # Config-file mode: use the static device list from the config.
                if not cfg["devices"]:
                    _PROBE_REQUESTS_TOTAL.labels(result="error").inc()
                    self.send_error(
                        400,
                        "No devices configured. Either add devices to the config file "
                        "or supply a 'host' query parameter.",
                    )
                    return
                devices = cfg["devices"]
                logger.debug("Probing %d device(s) from config", len(devices))

            devices = _resolve_credentials(devices)

            registry = CollectorRegistry(auto_describe=True)
            registry.register(LW3Collector(devices))

            try:
                output = generate_latest(registry)
            except Exception as exc:
                logger.error("Failed to generate probe metrics: %s", exc)
                _PROBE_REQUESTS_TOTAL.labels(result="error").inc()
                self.send_error(500, "Internal server error")
                return

            _PROBE_DURATION_SECONDS.observe(time.monotonic() - start)
            _PROBE_REQUESTS_TOTAL.labels(result="success").inc()

            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(output)

        def _handle_metrics(self) -> None:
            """Serve the exporter's own application metrics from the default registry."""
            output = generate_latest(REGISTRY)
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(output)

        def _handle_healthz(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")

        def log_message(self, fmt: str, *args: Any) -> None:  # type: ignore[override]
            logger.debug(
                "%s - %s",
                self.address_string(),
                fmt % args,
            )

    return Handler


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lw3-exporter",
        description="Prometheus exporter for Lightware LW3-capable devices.",
    )
    parser.add_argument(
        "-c", "--config",
        default=os.environ.get("LW3_CONFIG", "config.yaml"),
        metavar="FILE",
        help="Path to the YAML configuration file (default: config.yaml, "
             "env: LW3_CONFIG).",
    )
    parser.add_argument(
        "--host",
        default=None,
        metavar="HOST",
        help="Override the HTTP listen host from the config file.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        metavar="PORT",
        help="Override the HTTP listen port from the config file.",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        metavar="LEVEL",
        help="Override the log level from the config file.",
    )
    return parser


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    # --- Load configuration ---
    try:
        cfg = load_config(args.config)
    except FileNotFoundError:
        print(
            f"ERROR: Configuration file not found: {args.config}\n"
            "Copy config.example.yaml to config.yaml and edit it.",
            file=sys.stderr,
        )
        sys.exit(1)
    except (ValueError, yaml.YAMLError) as exc:
        print(f"ERROR: Invalid configuration: {exc}", file=sys.stderr)
        sys.exit(1)

    # CLI overrides
    if args.host is not None:
        cfg["listen_host"] = args.host
    if args.port is not None:
        cfg["listen_port"] = args.port
    if args.log_level is not None:
        cfg["log_level"] = args.log_level

    # --- Logging ---
    logging.basicConfig(
        level=getattr(logging, cfg["log_level"].upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stderr,
    )

    if cfg["devices"]:
        logger.info(
            "Starting lw3-exporter on %s:%d — %d device(s) pre-configured",
            cfg["listen_host"], cfg["listen_port"], len(cfg["devices"]),
        )
        for dev in cfg["devices"]:
            logger.info(
                "  Device: %s  host=%s:%d",
                dev["name"], dev["host"], dev["port"],
            )
    else:
        logger.info(
            "Starting lw3-exporter on %s:%d — no pre-configured devices; "
            "targets must be supplied via URL parameters",
            cfg["listen_host"], cfg["listen_port"],
        )

    if cfg["auth"]["username"]:
        logger.info("HTTP Basic auth enabled (all endpoints except /healthz)")
    else:
        logger.warning("HTTP auth NOT configured — /probe and /metrics are open to anyone")

    if cfg["allowed_hosts"]:
        logger.info(
            "URL-mode targets restricted: %d host pattern(s), ports %s",
            len(cfg["allowed_hosts"]), cfg["allowed_ports"],
        )
    else:
        logger.warning(
            "allowed_hosts NOT configured — /probe?host=... can target any host "
            "(and will send OpenBao credentials for it)"
        )

    logger.info(
        "Listening on http://%s:%d  probe=>/probe  self-metrics=>/metrics",
        cfg["listen_host"], cfg["listen_port"],
    )

    # --- HTTP server ---
    httpd = ThreadingHTTPServer(
        (cfg["listen_host"], cfg["listen_port"]),
        _make_handler(cfg),
    )

    # --- Graceful shutdown ---
    def _handle_signal(signum: int, _frame: Any) -> None:
        logger.info("Received signal %d, shutting down…", signum)
        # shutdown() blocks until serve_forever() returns; calling it directly
        # here would deadlock since the handler runs on the serve_forever thread.
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    httpd.serve_forever()
    logger.info("lw3-exporter stopped.")


if __name__ == "__main__":
    main()
