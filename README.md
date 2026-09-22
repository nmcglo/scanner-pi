# scanner-pi

Scan multi-page documents from a SANE scanner (developed against a ScanSnap iX1300) on a Raspberry Pi and deliver them as a single PDF — on demand or by pressing the scanner's hardware button.

## What it does

1. Drives `scanimage` over SANE to pull every sheet from the document feeder (duplex by default).
2. Discards blank pages — the scanner driver's own `--swskip`, plus a Pillow greyscale std-dev check on the resulting images.
3. Assembles the pages into one PDF (`pypdf` merge for native `pdf` output, Pillow otherwise).
4. Delivers the PDF through a configured output pipeline: local directory and/or upload to [Paperless-ngx](https://docs.paperless-ngx.com/).

## Commands

| Command | Purpose |
| --- | --- |
| `scan-pi` | One-shot scan. Prints the saved PDF path to stdout (when a file destination exists). |
| `scan-pi-listen` | Daemon: polls the scanner's button over SANE and runs a scan on each press. |

```sh
scan-pi                          # duplex, defaults from config
scan-pi --simplex --resolution 600 --mode Gray
scan-pi --output-dir ~/docs --prefix invoice
scan-pi-listen -v                # button daemon
```

`--help` lists every override (`--device`, `--source`, `--format`, `--swskip/--no-swcrop`, `--quality`, `--blank-threshold`, listener timings …).

## Requirements

- Python ≥ 3.11, `sane-utils` + `libsane1` (the `scanimage` CLI and backends)
- Membership in the `scanner` group for USB access
- Python deps: `pillow`, `pypdf`, `python-sane`, `requests`

```sh
uv sync            # or: pip install -e .
```

## Configuration

TOML, resolved in increasing precedence: built-in defaults → config file → `SCAN_PI_*` env vars → CLI flags.

Within the file, `[global.*]` applies to both entry points; `[scan.*]` and `[listener.*]` override it for their own context. Default file: `src/scanner_pi/config/default_config.toml` (override with `--config` or `SCAN_PI_CONFIG_PATH`).

```toml
[global.scanner]
device = ""              # "" = auto-detect
source = "ADF Duplex"    # or "ADF Front"
mode = "Color"           # Color | Gray | Lineart
resolution = 400
format = "tiff"          # tiff | png | jpeg | pnm | pdf
driver_swskip = 5        # 0–100 driver blank-page skip
driver_swcrop = false

[global.output]
directory = "~/scans"
filename_prefix = "scan" # → scan_YYYYMMDD_HHMMSS.pdf

[global.processing]
blank_page_threshold = 0.03  # greyscale std-dev, 0.0–1.0
jpeg_quality = 90            # 1–95
```

### Output destinations

`[[scan.output.destinations]]` *is* the output pipeline — configuring any destination disables the implicit local save, so include a `file` entry if you still want one.

```toml
[[scan.output.destinations]]
type = "file"
directory = "~/scans"

[[scan.output.destinations]]
type = "paperless_ngx"
base_url = "http://192.168.1.100:8000"
token = "…"                      # or SCAN_PI_PAPERLESS_NGX_TOKEN
title = "Scanned Document"       # optional
tag_names = ["scanner"]          # optional, resolved to IDs
correspondent_name = "My Bank"   # optional
document_type_name = "Invoice"   # optional
```

`[[listener.destinations]]` run *after* a button-triggered scan on the local PDF, letting manual scans stay local while button scans also upload. A failing destination is logged; the others still run.

Notes: `format = "pdf"` skips image-based blank-page detection (pages are merged as-is).

## Container

```sh
podman build -t scanner-pi .
SCANNER_GID=$(getent group scanner | cut -d: -f3)
podman run --device /dev/bus/usb --group-add "$SCANNER_GID" \
           -v ~/scans:/scans:Z scanner-pi
```

Default command is `scan-pi-listen`; output defaults to the `/scans` volume. Tune via `SCAN_PI_*` env vars (`SCAN_PI_DEVICE`, `SCAN_PI_RESOLUTION`, `SCAN_PI_OUTPUT_DIR`, `SCAN_PI_PAPERLESS_NGX_TOKEN`, …) or bind-mount a config file and pass `--config`.

## Tests

```sh
uv run pytest
```
