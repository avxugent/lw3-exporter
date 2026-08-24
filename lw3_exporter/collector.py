"""
Prometheus collector for Lightware LW3 devices.

Probes one or more devices over LW3 and exposes connectivity, identity
(product name, serial number, firmware version), and internal temperature
sensors as Prometheus metrics.
"""

from __future__ import annotations

import logging
from typing import Any, Generator

from prometheus_client.core import GaugeMetricFamily, InfoMetricFamily, Metric

from .prober import ProbeResult, probe_device

logger = logging.getLogger(__name__)


class LW3Collector:
    """
    A Prometheus custom collector that probes LW3 devices.

    Each call to :meth:`collect` probes every configured device fresh (no
    caching) and yields the results.
    """

    def __init__(self, devices: list[dict[str, Any]]) -> None:
        """
        :param devices: List of device configuration dicts. Each dict must
            contain at least ``name``, ``host``, ``username``, and
            ``password``; optional keys are ``port``, ``timeout``, and
            ``verify_tls``.
        """
        self._devices = devices

    # ------------------------------------------------------------------
    # prometheus_client collector interface
    # ------------------------------------------------------------------

    def describe(self) -> Generator[Metric, None, None]:
        yield GaugeMetricFamily("probe_success", "")
        yield GaugeMetricFamily("probe_duration_seconds", "")
        yield GaugeMetricFamily("lw3_temperature_celsius", "")
        yield InfoMetricFamily("lw3_device", "")

    def collect(self) -> Generator[Metric, None, None]:
        """Probe all devices and yield Prometheus metric families."""
        results: list[tuple[dict[str, Any], ProbeResult]] = [
            (dev_cfg, self._probe(dev_cfg)) for dev_cfg in self._devices
        ]
        base_labels = ["device", "host"]

        # --- probe_success / probe_duration_seconds ---
        success_family = GaugeMetricFamily(
            "probe_success",
            "1 if the device responded to LW3 queries, 0 otherwise.",
            labels=base_labels,
        )
        duration_family = GaugeMetricFamily(
            "probe_duration_seconds",
            "Duration of the probe in seconds.",
            labels=base_labels,
        )
        for dev_cfg, r in results:
            labels = [dev_cfg["name"], dev_cfg["host"]]
            success_family.add_metric(labels, 1.0 if r.success else 0.0)
            duration_family.add_metric(labels, r.duration_seconds)
        yield success_family
        yield duration_family

        # --- lw3_temperature_celsius ---
        temperature_family = GaugeMetricFamily(
            "lw3_temperature_celsius",
            "Internal temperature sensor readings, discovered at probe time "
            "by walking /V1/MANAGEMENT/HEALTH/*/TEMPERATURE (sensor set is "
            "device-specific; see the 'sensor' label).",
            labels=base_labels + ["sensor"],
        )
        for dev_cfg, r in results:
            for sensor, celsius in r.temperatures.items():
                temperature_family.add_metric(
                    [dev_cfg["name"], dev_cfg["host"], sensor], celsius
                )
        yield temperature_family

        # --- lw3_device (info) ---
        device_family = InfoMetricFamily(
            "lw3_device",
            "Device identity reported over LW3 (/.ProductName, /.SerialNumber, "
            "/MANAGEMENT/UID/PACKAGE.Version).",
            labels=base_labels,
        )
        for dev_cfg, r in results:
            info: dict[str, str] = {}
            if r.product_name:
                info["product_name"] = r.product_name
            if r.serial_number:
                info["serial_number"] = r.serial_number
            if r.firmware_version:
                info["firmware_version"] = r.firmware_version
            if info:
                device_family.add_metric([dev_cfg["name"], dev_cfg["host"]], info)
        yield device_family

    # ------------------------------------------------------------------
    # Device probing
    # ------------------------------------------------------------------

    def _probe(self, dev_cfg: dict[str, Any]) -> ProbeResult:
        """Probe a single device and return the result."""
        return probe_device(
            host=dev_cfg["host"],
            username=dev_cfg.get("username") or "",
            password=dev_cfg.get("password") or "",
            port=int(dev_cfg.get("port", 443)),
            timeout=float(dev_cfg.get("timeout", 10.0)),
            verify_tls=bool(dev_cfg.get("verify_tls", False)),
        )
