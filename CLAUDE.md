# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Prometheus exporter for Lightware LW3-capable devices — availability, identity (product name,
serial number, firmware version), and internal temperature sensors. [LW3](https://lightware.com/)
is Lightware's own text-based, tree-structured property protocol (not a cross-vendor standard,
unlike PJLink), implemented by virtually every Lightware device since around 2012: matrix
switchers, HDBaseT/fibre extenders, distribution amplifiers, videowall processors.

**Transport: LW3-over-WebSocket, not raw TCP 6107.** Lightware's manuals document LW3 over a raw
TCP socket (port 6107). This exporter deliberately does *not* use that. On UGent's network the raw
TCP port isn't reachable from wherever the exporter runs — only HTTPS (443) is. Every LW3 device's
built-in web management UI reaches the exact same property tree through a WebSocket endpoint at
`wss://<host>/lw3` (its "LW3 Console" widget talks to it directly — confirmed by reading the web
UI's own JavaScript module map, `dojoConfig.map.js`, which resolves `lowlevel/LW3Client` to
`taurus-common/lowlevel/LW3Client.js`). This exporter speaks that same WebSocket protocol instead.
**Everything in `prober.py` was verified live against a real production device**
(`matrix0409001.private.ugent.be`, a Lightware Taurus TX / UCX-4x3-TPX-TX30-R) with a throwaway
raw-WebSocket test client before any production code was written — not inferred from manuals
alone. See "Protocol reference" below for exactly what was confirmed and how.

Pure-Python stdlib HTTP server (`http.server.ThreadingHTTPServer`) for the exporter's own HTTP
surface — no web framework. The LW3 side uses the `websockets` library's *sync* client
(`websockets.sync.client.connect`) — a blocking API, no asyncio — since nothing else in this
codebase is async.

Sibling exporters in this fleet: `pearl-exporter` (Sanic + HTTP API, Epiphan Pearl),
`extronsis-exporter` (stdlib http.server, HTML scraping, Extron), and `pjlink-exporter` (stdlib
http.server, raw TCP socket, cross-vendor projector protocol). This repo follows pjlink-exporter's
architecture (package layout, config/URL dual-mode `/probe`, stdlib server,
fresh-connection-per-probe prober) most closely, and — like pearl-exporter/pjlink-exporter — has a
`bao.py` OpenBao credential lookup, since the LW3 WebSocket endpoint *does* require auth (HTTP
Basic, username + password) even though the raw TCP protocol in Lightware's manuals has none.
`bao.py` here is pearl-exporter's `get_credentials(hostname) -> (username, password)` shape, not
pjlink's trimmed single-password version, since LW3 needs both.

## Commands

```bash
# Install deps (PyYAML, prometheus-client, websockets, hvac)
pip install -r requirements.txt

# Run locally
cp config.example.yaml config.yaml   # edit devices
python -m lw3_exporter -c config.yaml

# CLI flags override config file values
python -m lw3_exporter -c config.yaml --host 0.0.0.0 --port 9879 --log-level DEBUG

# With auth enabled, scrape with -u (or set LW3_AUTH_USERNAME / LW3_AUTH_PASSWORD)
curl -u prometheus:secret 'localhost:9879/probe?host=...'

# Docker
docker build -t lw3-exporter .
docker compose up -d
```

Tooling: **ruff** (`ruff.toml`; lint only, no formatter enforced — `pip install ruff && ruff check .`),
`scripts/smoke-test.sh` (starts the exporter, checks auth, the host allowlist and clean SIGTERM
shutdown; needs no device), and Dependabot (`.github/dependabot.yml`: pip, github-actions,
docker). There is still **no pytest suite or type-checker**. CI (`.github/workflows/ci.yml`, on PRs
and pushes to `master`) runs ruff, the smoke test, `pip-audit` and a no-push Docker build.
`.github/workflows/docker.yml` builds and pushes the multiarch image to Docker Hub on push to
`master`. Beyond the smoke test, verify changes manually by running the exporter and curling its endpoints — ideally
against a real LW3 device (`GET /probe?host=...&username=...&password=...`). `prober.py`'s
`probe_device()` needs a real or fake LW3-over-WebSocket server to exercise; there's no pure
protocol-parsing entry point separate from the WebSocket I/O. A `websockets.sync.server` fixture
serving canned `n-`/`nE`/`pr`/`pE` lines is the way to unit test it if a test suite gets added.

## Architecture

Four files under `lw3_exporter/`, each with a single responsibility:

- **`prober.py`** — `probe_device()`: opens a `wss://<host>:<port>/lw3` WebSocket connection
  authenticated with HTTP Basic auth on the upgrade request (no separate login step, no `OPEN`
  before `GET`), queries `/.ProductName` (mandatory — this is what `success` gates on),
  `/.SerialNumber`, `/V1/MANAGEMENT/UID/PACKAGE.Version`, then walks `/V1/MANAGEMENT/HEALTH` to
  discover whatever temperature sensors the device actually has (`_discover_temperatures`), and
  returns a `ProbeResult`. Every query past `/.ProductName` is independently best-effort — a
  device lacking a property doesn't fail the whole probe. A fresh WebSocket connection is opened
  per probe rather than pooled, matching pjlink-exporter. `verify_tls` defaults to `False` since
  these devices typically ship a self-signed cert on the management interface.

- **`collector.py`** — `LW3Collector` (a plain duck-typed collector with `collect()`/`describe()`,
  not a subclass of `prometheus_client.registry.Collector`): turns `ProbeResult`s
  into Prometheus metric families (`probe_success`, `probe_duration_seconds`,
  `lw3_temperature_celsius` — one row per discovered sensor, labeled `sensor` — and `lw3_device`
  info). `collect()` runs fresh on every scrape (no caching), probing every configured device
  sequentially. Per-device failures don't abort the whole scrape. Purely a protocol-result-to-metric
  mapper; credential resolution lives in `__main__.py` (see below), not here — kept this way so the
  collector stays testable without touching `bao.py`.

- **`bao.py`** — OpenBao `(username, password)` lookup, `get_credentials(hostname)`. Copied from
  pearl-exporter's `bao.py` (not pjlink's trimmed one) since LW3 needs a username, unlike PJLink's
  single shared password. Returns `None` on any failure — never logs secret values.

- **`__main__.py`** — config loading (`load_config`, YAML + defaults merge), credential resolution
  (`_resolve_credentials`: OpenBao first per device by hostname, falling back to the
  config file's/URL's `username`/`password` fields), the HTTP handler (`_make_handler` closes over
  the loaded `cfg` dict), and `main()`/CLI argument parsing. Routes: `/` (HTML index), `/probe`,
  `/metrics`, `/healthz`. Also owns HTTP auth and URL-mode target restriction (see "Securing the
  HTTP surface"). The SIGTERM/SIGINT handler runs `httpd.shutdown()` in a separate thread — calling
  it inline deadlocks, since the handler runs on the same thread as `serve_forever()`.

### The two probing modes (both always active, can combine)

- **Config-file mode**: plain `GET /probe` scrapes every device listed under `devices:` in the
  YAML config.
- **URL-parameter mode**: `GET /probe?host=...&name=...&username=...&password=...` scrapes a
  single device described entirely by query params — no config-file entry needed.
  `_device_from_query_params()` in `__main__.py` builds the device dict; only `host` is required,
  everything else falls back to `_DEVICE_DEFAULTS`.

If a request supplies `host` as a query param, URL-parameter mode wins for that request even if
`devices:` is also configured — the two modes are per-request, not mutually exclusive globally.
URL mode is the only path where the caller picks the target, so it is the one `allowed_hosts`
restricts; config-file devices are never checked.

### Securing the HTTP surface

Both are implemented in `__main__.py` and both default to *off* (open + startup warning) so
existing deployments don't break. TLS is not handled here — run behind a TLS-terminating proxy.

- **Basic auth**: `auth.username`/`auth.password` in config, or `LW3_AUTH_USERNAME`/
  `LW3_AUTH_PASSWORD` env vars (env wins). Guards `/`, `/probe`, `/metrics`; `/healthz` is always
  open (Docker/Nomad liveness). Checked at the top of `Handler.do_GET` with `hmac.compare_digest`;
  401 + `WWW-Authenticate` on failure. Setting only one of username/password fails at startup.
- **Target allowlist**: `allowed_hosts` (regexes, `fullmatch`ed against the literal `host` string —
  no DNS resolution, so keep patterns anchored) and `allowed_ports` (default `[443]`, only enforced
  when `allowed_hosts` is set). Checked by `_target_allowed()` in `_handle_probe` *before*
  `_resolve_credentials`, so a rejected host (403) never triggers an OpenBao lookup. This matters:
  without it, any caller could point `host` at a server they control and receive the OpenBao
  credentials stored for that hostname in the Basic auth header. Regexes are compiled in
  `load_config`, so a bad pattern fails at startup.

### `/metrics` vs `/probe` — do not confuse these

- `/probe` returns **device** metrics (`probe_*`, `lw3_*`), built from a fresh `CollectorRegistry`
  + `LW3Collector` per request.
- `/metrics` returns the **exporter's own** self-metrics (`lw3_exporter_probe_requests_total`,
  `lw3_exporter_probe_duration_seconds`, plus default process/platform collectors) from
  `prometheus_client`'s global `REGISTRY`. It does not touch any device.

### Adding a new metric

1. If it requires a new LW3 property, add a query for it in `probe_device()` in `prober.py` and a
   new field on `ProbeResult`. Wrap it in `try/except LookupError` (the property may not exist on
   a given device/firmware) unless it's mandatory. If it needs tree discovery rather than a fixed
   path (like temperature), follow the `_discover_temperatures` pattern — `get_node()` to list
   children, don't hardcode a path that varies by product.
2. Yield a new `GaugeMetricFamily`/`InfoMetricFamily` in `LW3Collector.collect()`, and add the
   matching empty-family stub in `describe()`.
3. Update the metrics table in `README.md`.

`InfoMetricFamily(name, ...)` renders as `<name>_info` in the exposition format — the family is
registered as `"lw3_device"` in `collector.py` but shows up as `lw3_device_info` in scrape output.

## Configuration

`config.yaml` is gitignored (contains internal hostnames and a plaintext credential fallback if
OpenBao isn't set up) — `config.example.yaml` is the tracked template and also what gets baked
into the Docker image as the built-in default (`/etc/lw3-exporter/config.yaml`, overridable via
the `LW3_CONFIG` env var or by bind-mounting over that path). The `devices:` list is optional at
every level — an exporter with zero configured devices is valid and simply requires every
`/probe` call to pass `host` (and credentials, one way or another). Top-level `auth`,
`allowed_hosts` and `allowed_ports` keys are documented in the example file and above; `auth` can
also come from env vars so the password needn't live in the file.

## Protocol reference — read this before touching `prober.py`

Lightware doesn't publish a standalone plain-text LW3 protocol spec, and the WebSocket bridge
described here isn't documented anywhere Lightware publishes either — it's the internal channel
their own web UI uses. Everything below was confirmed **live**, against a real device, with a
throwaway raw-WebSocket Python client, before writing any of `prober.py`. Don't trust the raw-TCP
framing shown in Lightware's manuals (`<seq>#GET ...` wrapped in `{<seq> ... }` blocks) for this
transport — the WebSocket bridge does not use it.

- **Endpoint**: `wss://<host>:<port>/lw3` (default port 443). Confirmed by finding `GET /lw3` with
  WebSocket upgrade headers returns `101 Switching Protocols`, discovered by reading the web UI's
  `dojoConfig.map.js` module map, which points `lowlevel/LW3Client` at
  `taurus-common/lowlevel/LW3Client.js` (that specific file wasn't fetchable — likely bundled into
  a built layer — but the `/lw3` path itself was confirmed directly).
- **Auth**: plain HTTP Basic auth (`Authorization: Basic base64(username:password)`) on the
  WebSocket upgrade request itself. No separate login/session step needed — the web UI's own
  cookie-based login (`POST /api/v1/management/session/login`) is unrelated browser-session
  plumbing, not required for this WebSocket. A missing/invalid `Authorization` header gets a plain
  `401` on the upgrade attempt.
- **Framing**: `\r\n`-terminated lines inside WebSocket *text* frames. No signature/sequence
  wrapper, no `{...}` block delimiters — the WebSocket's own message framing replaces that. A
  request is `GET <path>\r\n`; a response is one or more `\r\n`-terminated lines in a single text
  frame (all of a node's children arrive together in one frame, not one frame per line).
- **Two response shapes**:
    - Node (has children): one `n- <full-child-path>\r\n` line per child, or on error
      `nE <path> %E<code>: <message>\r\n` (e.g. `%E002: Not exists`).
    - Property (has a value): `pr <path>=<value>\r\n`, or on error
      `pE <path> %E<code>: <message>\r\n`.
- **Path addressing — the part that isn't obvious from the tree listing**: a node's own path is
  `/`-separated (e.g. `/V1/MANAGEMENT/UID/PACKAGE`). A *property* of that node is addressed by
  appending `.<PropertyName>` to the node's path, not another `/` segment — e.g.
  `/V1/MANAGEMENT/UID/PACKAGE.Version`, or `/.ProductName` for a property of the bare root. `GET`
  on a bare node path (no dot) lists its children; `GET` on a dotted property path reads its
  value. Getting this wrong silently returns nothing at all (not even an error line) rather than
  failing loudly — if a query you expect to work returns an empty response, check whether you
  need `.PropertyName` instead of `/PropertyName`.
- **Confirmed, standardized properties** (same on every LW3 device): `/.ProductName`,
  `/.SerialNumber`, `/V1/MANAGEMENT/UID/PACKAGE.Version` (overall firmware/package version).
- **No standard temperature node.** Confirmed live: temperature sensors live under
  `/V1/MANAGEMENT/HEALTH/<board>/TEMPERATURE/<sensor>.Value`, but which boards exist (`MB`, `RX`,
  `TX` on the Taurus TX tested; likely different on a matrix frame or videowall processor) and
  which sensors each board has (`CPU`, `AVX`, `10G` on this device) both vary by product. That's
  why `_discover_temperatures()` walks the tree (`get_node("/V1/MANAGEMENT/HEALTH")`, then for
  each board check for a `TEMPERATURE` child, then `.Value` on each sensor found) instead of
  hardcoding a path — do not replace this with a fixed path list; it will silently miss devices
  with a different board layout.
- **OPEN exists but isn't needed.** LW3 has an `OPEN <path>\r\n` → `o- <path>\r\n` command,
  presumably for subscribing to change notifications (`SIGNAL_CHANGE`-style pushes). It is *not*
  required before `GET` — confirmed live that `.Value` reads work on a fresh connection with no
  prior `OPEN`. Don't add an `OPEN` call to the read path; it was tested and found unnecessary.
