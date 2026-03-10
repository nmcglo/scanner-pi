# ============================================================================
# scanner-pi — Containerfile
# ============================================================================
#
# Default command: scan-pi-listen (hardware button → PDF pipeline)
# Also ships scan-pi for on-demand scanning.
#
# RUNTIME REQUIREMENTS
# --------------------
# The scanner is accessed via SANE over USB.  The container user must be able
# to reach the USB device, so pass the host's scanner GID at run time:
#
#   SCANNER_GID=$(getent group scanner | cut -d: -f3)
#   podman run --device /dev/bus/usb --group-add $SCANNER_GID \
#              -v ~/scans:/scans:Z \
#              scanner-pi
#
# CONFIGURATION
# -------------
# The bundled default_config.toml is used by default.  Override individual
# settings without touching the image by setting SCAN_PI_* environment
# variables (handled by apply_env_overrides).  The full list:
#
#   SCAN_PI_DEVICE              SANE device name (default: auto-detect)
#   SCAN_PI_SOURCE              ADF Duplex | ADF Front
#   SCAN_PI_MODE                Color | Gray | Lineart
#   SCAN_PI_RESOLUTION          DPI (e.g. 300, 400, 600)
#   SCAN_PI_FORMAT              tiff | png | jpeg | pnm | pdf
#   SCAN_PI_DRIVER_SWSKIP       0-100
#   SCAN_PI_DRIVER_SWCROP       true | false
#   SCAN_PI_OUTPUT_DIR          directory for saved PDFs  (default: /scans)
#   SCAN_PI_FILENAME_PREFIX     filename prefix           (default: scan)
#   SCAN_PI_JPEG_QUALITY        1-95
#   SCAN_PI_BLANK_PAGE_THRESHOLD  0.0-1.0
#   SCAN_PI_PAPERLESS_NGX_TOKEN   API token (keeps secrets out of config files)
#
# To use a fully custom config file instead, bind-mount it and pass --config:
#
#   podman run ... -v /path/to/config.toml:/config/config.toml:ro,Z \
#          scanner-pi scan-pi-listen --config /config/config.toml


# ---- build stage -----------------------------------------------------------
# Compile python-sane (C extension wrapping libsane) and
# assemble the virtual environment.  Build-time packages are not carried
# into the final image.
FROM python:3.13-slim-bookworm AS build

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libsane-dev \
    && rm -rf /var/lib/apt/lists/*

# Bring in uv from its official image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy files rather than hard-link — required for COPY --from to work correctly
ENV UV_LINK_MODE=copy

WORKDIR /app

# Install Python dependencies first so this layer is cached until the lock
# file changes.  Skip dev group (pytest etc. not needed at runtime).
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --no-install-project

# Copy source and install the project itself
COPY src/ ./src/
RUN uv sync --no-dev


# ---- runtime stage ---------------------------------------------------------
FROM python:3.13-slim-bookworm

LABEL org.opencontainers.image.title="scanner-pi" \
      org.opencontainers.image.description="Scan multi-page documents from a ScanSnap iX1300 to PDF"

# sane-utils → scanimage CLI (invoked by scan_pages())
# libsane1   → SANE runtime library + all hardware backends (incl. Fujitsu)
RUN apt-get update && apt-get install -y --no-install-recommends \
        sane-utils \
        libsane1 \
    && rm -rf /var/lib/apt/lists/*

# Non-root service user.  Home directory is /app so that ~ expands sensibly
# inside the process if the default config path is ever used directly.
RUN useradd -r -d /app -s /sbin/nologin scanner-pi

# Copy the assembled virtual environment from the build stage
COPY --from=build /app /app
ENV PATH="/app/.venv/bin:$PATH"

# Default output directory for scanned PDFs.
# Bind-mount a host directory or a named volume here to retrieve them:
#   podman run -v ~/scans:/scans:Z scanner-pi
ENV SCAN_PI_OUTPUT_DIR=/scans
RUN mkdir /scans && chown scanner-pi /scans
VOLUME /scans

USER scanner-pi
WORKDIR /app

CMD ["scan-pi-listen"]
