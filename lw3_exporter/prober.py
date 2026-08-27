"""
Lightware LW3 device prober.

Speaks LW3 over the device's WebSocket bridge (``wss://<host>/lw3``) rather
than the raw TCP transport (port 6107) documented in Lightware's "LW3
Programmers' Reference" manuals. That's a deliberate choice, not a shortcut:
on UGent's network the raw TCP port isn't reachable from wherever this
exporter runs — only HTTPS (443) is — while every device's built-in web
management UI reaches the same property tree through this WebSocket endpoint
(its "LW3 Console" widget uses it directly, confirmed by reading the UI's
own JavaScript module map). Verified live against a production Taurus TX
unit (matrix0409001.private.ugent.be) with a raw WebSocket client before
writing this module — nothing here is guessed from the manuals alone:

- ``wss://<host>/lw3`` upgrades cleanly with a plain HTTP Basic
  ``Authorization`` header on the upgrade request itself — no separate
  session/cookie login step (the web UI's cookie-based
  ``/api/v1/management/session/login`` flow is unrelated plumbing for
  browser sessions, not required here), and no ``OPEN`` needed before
  ``GET``. Each probe opens one WebSocket connection, issues its queries,
  and closes it — no pooling, matching the other exporters in this fleet.
- Wire framing over the WebSocket is *not* what the raw-TCP manuals show
  (``<seq>#GET ...`` requests wrapped in ``{<seq> ... }`` response blocks) —
  the bridge strips that framing since the WebSocket already delimits
  messages. Requests are plain ``GET <path>\\r\\n`` text frames; responses
  are one or more ``\\r\\n``-terminated lines in a single text frame.
- Two response shapes, both confirmed live:

    - Node (has children):    ``n- <child-path>\\r\\n`` (one line per child)
                               or ``nE <path> %E<code>: <message>\\r\\n``
    - Property (has a value): ``pr <path>=<value>\\r\\n``
                               or ``pE <path> %E<code>: <message>\\r\\n``

  A node's path is ``/``-separated; a *property* of that node is addressed
  by appending ``.<PropertyName>`` to the node's own path (not a further
  ``/`` segment) — e.g. the node ``/V1/MANAGEMENT/UID/PACKAGE`` has a
  property read as ``/V1/MANAGEMENT/UID/PACKAGE.Version``.
- ``/.ProductName`` and ``/.SerialNumber`` (properties of the bare root
  ``/``, not ``/V1``) and ``/V1/MANAGEMENT/UID/PACKAGE.Version`` are
  standardized across LW3 devices. Internal temperature sensors are not —
  they live under per-board subtrees of ``/V1/MANAGEMENT/HEALTH`` (e.g.
  ``MB``, ``RX``, ``TX``; which boards exist varies by product), so this
  prober walks the tree at probe time (see ``_discover_temperatures``)
  instead of hardcoding a sensor path: list ``HEALTH``'s children, and for
  each one that has a ``TEMPERATURE`` child node, read ``.Value`` on every
  sensor listed under it.

TLS: devices carry a self-signed certificate on their management interface
by default, so verification defaults off (``verify_tls=False``); set it per
device once real CA-issued certs are in place.
"""

from __future__ import annotations

import base64
import logging
import ssl
import time
from dataclasses import dataclass, field
from typing import Optional

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect as ws_connect

logger = logging.getLogger(__name__)

DEFAULT_PORT = 443

_TERMINATOR = "\r\n"


@dataclass
class ProbeResult:
    """Result of probing a single LW3 device."""

    success: bool = False
    error: Optional[str] = None
    duration_seconds: float = 0.0

    product_name: Optional[str] = None
    serial_number: Optional[str] = None
    version: Optional[str] = None
    # sensor label (e.g. "MB_CPU") -> reading in Celsius. Populated by
    # walking /V1/MANAGEMENT/HEALTH at probe time -- see module docstring.
    temperatures: dict[str, float] = field(default_factory=dict)


class _ProtocolError(ConnectionError):
    """Malformed or unexpected data from the device (not a plain socket/WS error)."""


class _LW3Session:
    """One LW3 WebSocket connection: issue a sequence of GET queries against it."""

    def __init__(self, ws, timeout: float) -> None:
        self._ws = ws
        self._timeout = timeout

    def _request(self, path: str) -> list[str]:
        self._ws.send(f"GET {path}\r\n")
        message = self._ws.recv(timeout=self._timeout)
        if isinstance(message, bytes):
            message = message.decode("utf-8", errors="replace")
        lines = [line for line in message.split(_TERMINATOR) if line]
        if not lines:
            raise _ProtocolError(f"empty response to GET {path}")
        return lines

    def get_node(self, path: str) -> list[str]:
        """
        List a node's children as full child paths.

        :raises LookupError: The path doesn't exist as a node (``nE``) —
            expected when walking an optional subtree, not a connection
            failure.
        """
        children = []
        for line in self._request(path):
            prefix, _, rest = line.partition(" ")
            if prefix == "n-":
                children.append(rest)
            elif prefix == "nE":
                _, _, message = rest.partition(": ")
                raise LookupError(message or rest)
            else:
                raise _ProtocolError(f"unexpected response to GET {path}: {line!r}")
        return children

    def get_property(self, path: str) -> str:
        """
        Read a single property's value.

        :raises LookupError: The device answered with an LW3 error line
            (e.g. the property doesn't exist on this device/firmware) —
            expected for optional/non-standard properties, not a connection
            failure.
        """
        line = self._request(path)[0]
        prefix, _, rest = line.partition(" ")
        if prefix == "pr":
            resp_path, eq, value = rest.partition("=")
            if not eq:
                raise _ProtocolError(f"unexpected response to GET {path}: {line!r}")
            return value
        if prefix == "pE":
            _, _, message = rest.partition(": ")
            raise LookupError(message or rest)
        raise _ProtocolError(f"unexpected response to GET {path}: {line!r}")


def _discover_temperatures(session: _LW3Session, host: str) -> dict[str, float]:
    """
    Walk ``/V1/MANAGEMENT/HEALTH`` for temperature sensors.

    Sensor names, and even which boards exist (``MB``, ``RX``, ``TX``, ...),
    vary by product, so this discovers them at probe time rather than
    hardcoding a path — see module docstring.
    """
    temperatures: dict[str, float] = {}
    try:
        boards = session.get_node("/V1/MANAGEMENT/HEALTH")
    except LookupError as exc:
        logger.debug("HEALTH node query failed for %s: %s", host, exc)
        return temperatures

    for board_path in boards:
        try:
            sensors = session.get_node(f"{board_path}/TEMPERATURE")
        except LookupError:
            continue  # this board has no TEMPERATURE subtree
        board = board_path.rsplit("/", 1)[-1]
        for sensor_path in sensors:
            sensor = sensor_path.rsplit("/", 1)[-1]
            try:
                temperatures[f"{board}_{sensor}"] = float(
                    session.get_property(f"{sensor_path}.Value")
                )
            except (LookupError, ValueError) as exc:
                logger.debug(
                    "Temperature query (%s) failed for %s: %s", sensor_path, host, exc
                )

    return temperatures


def probe_device(
    host: str,
    username: str,
    password: str,
    port: int = DEFAULT_PORT,
    timeout: float = 10.0,
    verify_tls: bool = False,
) -> ProbeResult:
    """
    Query an LW3 device for identity (product name, serial number, firmware
    version) and internal temperature sensors, over its LW3-over-WebSocket
    management endpoint.

    Connectivity, auth, and protocol failures are caught and reported via
    :attr:`ProbeResult.success` / :attr:`ProbeResult.error` rather than
    raised, so callers can always emit a ``probe_success`` metric. The probe
    is only considered successful once the mandatory ``/.ProductName`` query
    succeeds, since that's the minimum needed to say the device is up and
    speaking LW3. Every other query (serial number, firmware version,
    temperature sensors) is independently best-effort.
    """
    start = time.monotonic()
    result = ProbeResult()

    ssl_context = ssl.create_default_context()
    if not verify_tls:
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

    auth = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")

    try:
        with ws_connect(
            f"wss://{host}:{port}/lw3",
            ssl_context=ssl_context,
            additional_headers={"Authorization": f"Basic {auth}"},
            open_timeout=timeout,
            close_timeout=min(timeout, 5.0),
        ) as ws:
            session = _LW3Session(ws, timeout)

            result.product_name = session.get_property("/.ProductName")
            result.success = True

            try:
                result.serial_number = session.get_property("/.SerialNumber")
            except LookupError as exc:
                logger.debug("SerialNumber query failed for %s: %s", host, exc)

            try:
                result.version = session.get_property(
                    "/V1/MANAGEMENT/UID/PACKAGE.Version"
                )
            except LookupError as exc:
                logger.debug("Firmware version query failed for %s: %s", host, exc)

            result.temperatures = _discover_temperatures(session, host)

    except (OSError, WebSocketException, _ProtocolError, LookupError, TimeoutError) as exc:
        result.error = str(exc)
        logger.warning("Probe failed for %s:%d: %s", host, port, exc)
    finally:
        result.duration_seconds = time.monotonic() - start

    return result
