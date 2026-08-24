# syntax=docker/dockerfile:1

# ── Build / dependency stage ──────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /app

# Install dependencies into a separate prefix so we can copy them cleanly
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.12-slim

LABEL org.opencontainers.image.title="lw3-exporter" \
      org.opencontainers.image.description="Prometheus exporter for Lightware LW3-capable devices" \
      org.opencontainers.image.source="https://github.com/kristofkeppens/lw3-exporter"

WORKDIR /app

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
