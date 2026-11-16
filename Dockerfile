# syntax=docker/dockerfile:1

# ── Build / dependency stage ──────────────────────────────────────────────────
FROM python:3.15-slim AS builder

WORKDIR /app

# Install dependencies into a separate prefix so we can copy them cleanly
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.15-slim

LABEL org.opencontainers.image.title="lw3-exporter" \
      org.opencontainers.image.description="Prometheus exporter for Lightware LW3-capable devices" \
      org.opencontainers.image.source="https://github.com/kristofkeppens/lw3-exporter"

WORKDIR /app

# Pick up Debian security fixes newer than the base image, and drop pip: the
# runtime never installs anything, and pip's vendored libraries (urllib3,
# msgpack, ...) would otherwise show up as separate, unfixable-by-us findings
# in image scans.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /usr/local/lib/python3.14/site-packages/pip \
              /usr/local/lib/python3.14/site-packages/pip-*.dist-info \
              /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.14

# Copy installed packages from the builder stage
COPY --from=builder /install /usr/local

# Copy the application source
COPY lw3_exporter/ ./lw3_exporter/

# Bundle the example config as the built-in default.
# A custom config can be injected at runtime by mounting a file over this path
# or by setting LW3_CONFIG to point elsewhere.
RUN mkdir -p /etc/lw3-exporter
COPY config.example.yaml /etc/lw3-exporter/config.yaml

ENV LW3_CONFIG=/etc/lw3-exporter/config.yaml

EXPOSE 9879

# Run as a non-root user for security
RUN useradd --no-create-home --shell /bin/false exporter
USER exporter

ENTRYPOINT ["python", "-m", "lw3_exporter"]
