# LW3 Exporter

[![CI](https://github.com/avxugent/lw3-exporter/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/avxugent/lw3-exporter/actions/workflows/ci.yml)
[![Dependabot](https://img.shields.io/badge/dependabot-enabled-025e8c?logo=dependabot)](https://github.com/avxugent/lw3-exporter/network/updates)

A Prometheus exporter for Lightware LW3-capable devices, written in Python.

[LW3](https://lightware.com/) is Lightware's own text-based, tree-structured property protocol,
implemented by virtually every Lightware device since around 2012 — matrix switchers,
HDBaseT/fibre extenders, distribution amplifiers, videowall processors. Lightware's manuals
document LW3 over a raw TCP socket (port 6107), but this exporter instead speaks it over the
device's **LW3-over-WebSocket management endpoint** (`wss://<host>/lw3`, HTTPS port 443) — the
same channel the device's own web UI uses for its built-in "LW3 Console" widget. That's a
deliberate choice: on networks where the raw TCP port isn't reachable through the firewall (as at
UGent) but the web management UI is, this is the only channel actually available — and it carries
the exact same LW3 property tree.

## Usage

### Docker

#### Build

```bash
docker build -t lw3-exporter .
```

#### Run

```bash
docker run -p 9879:9879 lw3-exporter
```

### Manual

1.  Install dependencies:
    ```bash
    pip install -r requirements.txt
    ```

2.  Copy the example config and edit it:
    ```bash
    cp config.example.yaml config.yaml
    ```

3.  Run the exporter:
    ```bash
    python -m lw3_exporter -c config.yaml
    ```

    CLI flags override config file values:
    ```bash
    python -m lw3_exporter -c config.yaml --host 0.0.0.0 --port 9879 --log-level DEBUG
    ```

## Credentials (OpenBao)

The exporter looks up each device's LW3 username/password in [OpenBao](https://openbao.org/)
(Vault-compatible), keyed by the device hostname, so credentials don't have to live in the config
file or scrape URL. The LW3 WebSocket endpoint authenticates with plain HTTP Basic auth on the
connection itself — the same username/password used to log into the device's web UI.

### Secret layout

All devices live under one common KV **v2** path, with the device hostname as the final path
segment:

```bash
bao kv put secret/lw3/192.168.1.10 username=admin password=secret
```

Here the mount is `secret`, the prefix is `lw3`, and `192.168.1.10` is the device hostname.

### Configuration

Configure the exporter via environment variables (`BAO_*` take precedence, `VAULT_*` are accepted
as fallbacks so Nomad's native integration vars work directly):

| Variable | Default | Description |
|----------|---------|--------------|
| `BAO_ADDR` / `VAULT_ADDR` | — | OpenBao server address |
| `BAO_TOKEN` / `VAULT_TOKEN` | — | Token used to authenticate to OpenBao |
| `BAO_KV_MOUNT` | `secret` | KV v2 mount point |
| `BAO_PATH_PREFIX` | (empty) | Common path under the mount before the hostname |
| `BAO_CACERT` / `VAULT_CACERT` | — | Optional CA bundle for TLS verification |

If the OpenBao lookup returns nothing (not configured, secret missing, network error), the
exporter falls back to the device's `username`/`password` fields in `config.yaml`, or the matching
URL query parameters.

### Nomad

```hcl
vault {}

template {
  data        = "VAULT_TOKEN={{ env \"VAULT_TOKEN\" }}"
  destination = "secrets/bao.env"
  env         = true
}
```

## Authentication (exporter's HTTP endpoints)

Optional HTTP Basic auth protects `/`, `/probe` and `/metrics` (`/healthz` always stays open).
Set `auth.username`/`auth.password` in `config.yaml`, or the `LW3_AUTH_USERNAME` /
`LW3_AUTH_PASSWORD` env vars (these override the file). Unset = no auth, and a warning is logged
at startup. The exporter serves plain HTTP, so run it behind a TLS-terminating proxy.

```yaml
scrape_configs:
  - job_name: lw3
    metrics_path: /probe
    basic_auth:
      username: prometheus
      password_file: /etc/prometheus/lw3.pass
```

## Restricting URL-parameter targets

`/probe?host=...` lets the caller choose where the exporter connects, and OpenBao credentials for
that hostname are sent along. Set `allowed_hosts` (regexes, fullmatched against the literal `host`)
and optionally `allowed_ports` (default `[443]`) in `config.yaml` to restrict this; other targets
get `403` before any credential lookup. Unset = unrestricted, with a startup warning. Devices under
`devices:` are not checked.

```yaml
allowed_hosts:
  - '^matrix\d+\.private\.ugent\.be$'
```

## Metrics

The exporter exposes device metrics on the `/probe` endpoint.

**Config-file mode** — scrapes every device listed in `config.yaml`:

```bash
curl "http://localhost:9879/probe"
```

**URL-parameter mode** — probes a single device by query parameter, no config-file entry needed
(useful for driving many devices via Prometheus `relabel_configs`, blackbox-exporter style):

```bash
curl "http://localhost:9879/probe?host=192.168.1.10&name=matrix-101&username=admin&password=secret"
```

Both modes are always available; a request with a `host` parameter always wins for that request.

### Exposed Metrics

-   `probe_success`: 1 if the device responded to LW3 queries, 0 otherwise. This is the exporter's
    online/offline signal for the device.
-   `probe_duration_seconds`: How long the probe took to complete.
-   `lw3_temperature_celsius` (label: `sensor`): Internal temperature sensor readings. LW3 has no
    single standardized temperature node, so the exporter discovers sensors at probe time by
    walking `/V1/MANAGEMENT/HEALTH/*/TEMPERATURE` — the sensor set is device-specific (a small
    extender might report `MB_CPU`/`MB_AVX`, a matrix frame might report per-board sensors like
    `TX_CPU`/`RX_10G`). No configuration needed; whatever the device exposes shows up automatically.
-   `lw3_device_info` (info metric, labels: `product_name`, `serial_number`, `version`):
    Device identity reported over LW3 (`/.ProductName`, `/.SerialNumber`,
    `/MANAGEMENT/UID/PACKAGE.Version`). Registered as `lw3_device` in code — `InfoMetricFamily`
    appends the `_info` suffix in the exposition format.

All device metrics carry `device` (the configured name) and `host` labels.

### Exporter self-metrics

`/metrics` returns the exporter's own process metrics (not any device's), for monitoring the
exporter itself: `lw3_exporter_probe_requests_total`, `lw3_exporter_probe_duration_seconds`, plus
the default Python/process collectors.

## Notes

- Devices are probed fresh on every scrape — no caching, no persistent connections. A fresh
  WebSocket connection is opened per probe, matching pjlink-exporter/extronsis-exporter's approach.
- A device is only reported as `probe_success=0` if the mandatory `/.ProductName` query fails —
  serial number, firmware version, and temperature sensors are each independently best-effort, so
  a device lacking one of them simply omits that field/metric rather than failing the whole probe.
- TLS certificate verification defaults to **off** (`verify_tls: false`), since these devices
  typically carry a self-signed certificate on their management interface out of the box. Set
  `verify_tls: true` per device once a trusted certificate is installed.
- If your network can reach the device's raw LW3 TCP port (6107) directly, you don't need this
  exporter's WebSocket approach at all — see Lightware's own manuals for that protocol. This
  exporter targets the WebSocket bridge specifically because that's what was reachable in this
  fleet's deployment environment.
