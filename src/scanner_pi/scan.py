#!/usr/bin/env python3
"""
scan.py — Multi-page document scanner utility for ScanSnap iX1300

Scans all pages from the document feeder and combines them into a single PDF.
Blank pages (common with duplex scanning) are detected and discarded automatically.

Usage:
    scan-pi                          # use ~/.config/scanner-pi/config.toml
    scan-pi --config ~/my.toml      # custom config file
    scan-pi --output-dir ~/docs     # override output directory
    scan-pi --simplex               # single-sided scan
    scan-pi --mode Gray -v          # greyscale, verbose output
"""

import argparse
import logging
import subprocess
import sys
import tempfile
import tomllib
from datetime import datetime
from pathlib import Path
import importlib.util

from PIL import Image, ImageStat
from pypdf import PdfWriter

from scanner_pi.output import OutputError, build_handler

# Maps the scanimage --format value to the file extension it produces.
_FORMAT_EXT: dict[str, str] = {
    "tiff": "tif",
    "png":  "png",
    "jpeg": "jpg",
    "pnm":  "pnm",
    "pdf":  "pdf",
}

# --------------------------------------------------------------------------- #
# Built-in defaults (used when config file is absent or keys are missing)
# --------------------------------------------------------------------------- #

DEFAULTS: dict = {
    "scanner": {
        "device": "",           # empty string = auto-detect
        "source": "ADF Duplex",
        "mode": "Color",
        "resolution": 300,
        "format": "tiff",
        "driver_swskip": 20,  # strength of scanimage's built-in blank-page skipping (0 = off, 100 = max)
        "driver_swcrop": False, # whether to apply scanimage's built-in blank-page cropping (True or False)
    },
    "output": {
        "directory": str(Path.home() / "scans"),
        "filename_prefix": "scan",
    },
    "processing": {
        "blank_page_threshold": 0.015,  # normalised std-dev (0.0–1.0)
        "jpeg_quality": 85,
    },
}

# the default config path is the config file in scanner_pi.config
# use importlib to obtain the file path
DEFAULT_CONFIG_PATH = Path(importlib.util.find_spec("scanner_pi.config").origin).parent / "default_config.toml"

log = logging.getLogger("scan")


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

def load_config(config_path: Path) -> dict:
    """Load TOML config file and merge with built-in defaults."""
    config: dict = {section: dict(values) for section, values in DEFAULTS.items()}

    if config_path.exists():
        with open(config_path, "rb") as fh:
            user_config = tomllib.load(fh)
        for section, values in user_config.items():
            if section in config:
                log.debug("Merging config section [%s]", section)
                config[section].update(values)
            else:
                log.debug("Adding new config section [%s]", section)
                config[section] = values
        log.debug("Loaded config from %s", config_path)
    else:
        log.debug("Config file not found at %s — using built-in defaults", config_path)

    return config


def apply_overrides(config: dict, args: argparse.Namespace) -> dict:
    """Apply command-line argument overrides onto the loaded config."""
    mapping = {
        "scanner": {
            "device": args.device,
            "source": args.source,
            "mode": args.mode,
            "resolution": args.resolution,
            "format": args.format,
        },
        "output": {
            "directory": args.output_dir,
            "filename_prefix": args.prefix,
        },
        "processing": {
            "jpeg_quality": args.quality,
            "blank_page_threshold": args.blank_threshold,
        },
    }
    for section, values in mapping.items():
        for key, value in values.items():
            if value is not None:
                log.debug("Overriding config: [%s] %s = %s", section, key, value)
                config[section][key] = value
    return config


# --------------------------------------------------------------------------- #
# Scanner
# --------------------------------------------------------------------------- #

def detect_device() -> str:
    """Return the SANE name of the first detected scanner."""
    result = subprocess.run(["scanimage", "-L"], capture_output=True, text=True)
    for line in result.stdout.splitlines():
        # scanimage -L format:  device `name' is a ...
        if "device" in line.lower() and "`" in line and "'" in line:
            start = line.index("`") + 1
            end = line.index("'", start)
            return line[start:end]
    raise RuntimeError(
        "No scanner detected. Ensure it is connected, powered on, and that "
        "your user has permission to access it (check the 'scanner' group)."
    )


def scan_pages(
    device: str,
    source: str,
    mode: str,
    resolution: int,
    fmt: str,
    output_dir: Path,
    swskip: int = 0,
    swcrop: bool = True,
) -> list[Path]:
    """
    Invoke scanimage in batch mode and return a sorted list of scanned page paths.

    Exit code 7 is treated as success: it means the ADF ran out of paper, which
    is the normal end-of-document condition for the Fujitsu SANE backend.
    """
    ext = _FORMAT_EXT[fmt]
    batch_pattern = str(output_dir / f"page%04d.{ext}")
    cmd = [
        "scanimage",
        f"--device-name={device}",
        f"--source={source}",
        f"--mode={mode}",
        f"--resolution={resolution}",
        f"--format={fmt}",
        f"--batch={batch_pattern}",
        "--batch-start=1",
        f"--swskip={swskip}",
        f"--swcrop={"yes" if swcrop else "no"}",
    ]

    log.info(
        "Scanning — device: %s | source: %s | mode: %s | resolution: %d dpi | format: %s | swskip: %d | swcrop: %s | output: %s",
        device, source, mode, resolution, fmt, swskip, swcrop, output_dir
    )
    log.debug("Command: %s", " ".join(cmd))

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode not in (0, 7):
        if result.stderr:
            log.error("scanimage stderr:\n%s", result.stderr.strip())
        raise RuntimeError(
            f"scanimage exited with unexpected code {result.returncode}."
        )

    pages = sorted(output_dir.glob(f"page*.{ext}"))
    log.info("Scanned %d raw page(s)", len(pages))
    return pages


# --------------------------------------------------------------------------- #
# Blank-page detection
# --------------------------------------------------------------------------- #

def _page_stddev(image_path: Path) -> float:
    """Return the normalised greyscale standard deviation (0.0–1.0) of an image."""
    img = Image.open(image_path).convert("L")
    return ImageStat.Stat(img).stddev[0] / 255.0


def filter_blank_pages(pages: list[Path], threshold: float) -> list[Path]:
    """Discard pages whose pixel std-dev falls below *threshold* and return the rest."""
    kept: list[Path] = []
    for page in pages:
        stddev = _page_stddev(page)
        if stddev < threshold:
            log.info("  Dropping blank page: %s (stddev=%.4f)", page.name, stddev)
        else:
            log.debug("  Keeping page:        %s (stddev=%.4f)", page.name, stddev)
            kept.append(page)
    log.info(
        "Kept %d non-blank page(s) out of %d scanned", len(kept), len(pages)
    )
    return kept


# --------------------------------------------------------------------------- #
# PDF assembly
# --------------------------------------------------------------------------- #

def merge_pdfs(pages: list[Path], output_path: Path) -> None:
    """Merge per-page PDF files produced by scanimage into a single PDF."""
    writer = PdfWriter()
    for page in pages:
        writer.append(page)
    with open(output_path, "wb") as fh:
        writer.write(fh)
    log.info("PDF written: %s", output_path)


def assemble_pdf(pages: list[Path], output_path: Path, jpeg_quality: int) -> None:
    """Combine page images into a single PDF using Pillow."""
    images = [Image.open(p).convert("RGB") for p in pages]
    first, rest = images[0], images[1:]
    first.save(
        output_path,
        format="PDF",
        save_all=True,
        append_images=rest,
        quality=jpeg_quality,
    )
    log.info("PDF written: %s", output_path)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Scan a multi-page document from the feeder and save it as a PDF."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Config file
    parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG_PATH, metavar="FILE",
        help=f"Path to TOML config file (default: {DEFAULT_CONFIG_PATH})",
    )

    # Output overrides
    out = parser.add_argument_group("output options")
    out.add_argument(
        "--output-dir", metavar="DIR",
        help="Directory to save scanned PDFs (overrides config)",
    )
    out.add_argument(
        "--prefix", metavar="PREFIX",
        help="Output filename prefix; files are named <prefix>_YYYYMMDD_HHMMSS.pdf",
    )

    # Scanner overrides
    scan = parser.add_argument_group("scanner options")
    scan.add_argument(
        "--device", metavar="NAME",
        help="SANE device name (default: auto-detect)",
    )
    scan.add_argument(
        "--source", metavar="SOURCE",
        choices=["ADF Duplex", "ADF Front"],
        help="Feed source: 'ADF Duplex' (both sides) or 'ADF Front' (one side)",
    )
    scan.add_argument(
        "--simplex", action="store_true",
        help="Scan one side per sheet only — shortcut for --source 'ADF Front'",
    )
    scan.add_argument(
        "--mode", metavar="MODE",
        choices=["Color", "Gray", "Lineart"],
        help="Scan mode: Color, Gray, or Lineart",
    )
    scan.add_argument(
        "--resolution", type=int, metavar="DPI",
        help="Scan resolution in DPI (e.g. 300, 600)",
    )
    scan.add_argument(
        "--format", metavar="FMT",
        choices=list(_FORMAT_EXT),
        help="Per-page format: tiff, png, jpeg, pnm, or pdf (overrides config)",
    )

    # Processing overrides
    proc = parser.add_argument_group("processing options")
    proc.add_argument(
        "--quality", type=int, metavar="N",
        help="JPEG quality for PDF image compression, 1–95 (overrides config)",
    )
    proc.add_argument(
        "--blank-threshold", type=float, metavar="F",
        help=(
            "Greyscale std-dev threshold for blank-page detection, 0.0–1.0 "
            "(overrides config)"
        ),
    )

    # Verbosity
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable verbose/debug logging",
    )

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    # --simplex is a shortcut for --source "ADF Front"
    if args.simplex and args.source is None:
        args.source = "ADF Front"

    config = load_config(args.config)
    config = apply_overrides(config, args)

    scanner_cfg = config["scanner"]
    output_cfg = config["output"]
    proc_cfg = config["processing"]

    # Resolve device (auto-detect if not set)
    device: str = scanner_cfg["device"] or detect_device()

    # Resolve and create output directory
    output_dir = Path(output_cfg["directory"]).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_pdf = output_dir / f"{output_cfg['filename_prefix']}_{timestamp}.pdf"

    with tempfile.TemporaryDirectory(prefix="scanner-pi-") as tmp:
        tmp_path = Path(tmp)

        fmt = scanner_cfg["format"]

        # 1. Scan
        pages = scan_pages(
            device=device,
            source=scanner_cfg["source"],
            mode=scanner_cfg["mode"],
            resolution=int(scanner_cfg["resolution"]),
            fmt=fmt,
            output_dir=tmp_path,
            swskip=int(scanner_cfg["driver_swskip"]),
            swcrop=bool(scanner_cfg["driver_swcrop"]),
        )

        if not pages:
            log.error("No pages scanned — is the document feeder loaded?")
            sys.exit(1)

        # 2. Assemble — path depends on whether scanimage produced images or PDFs
        if fmt == "pdf":
            # Per-page PDFs: merge directly; blank detection is not available
            log.info("Native PDF format: skipping blank-page detection")
            merge_pdfs(pages, output_pdf)
        else:
            # Image format: detect and discard blank pages, then build PDF
            pages = filter_blank_pages(pages, float(proc_cfg["blank_page_threshold"]))
            if not pages:
                log.error("All pages were blank — aborting.")
                sys.exit(1)
            assemble_pdf(pages, output_pdf, int(proc_cfg["jpeg_quality"]))

    # 3. Deliver to any extra output destinations configured under
    #    [[output.destinations]] in the config file.
    extra_dests = config.get("output", {}).get("destinations", [])
    if extra_dests:
        handler = build_handler(extra_dests)
        try:
            handler.send(output_pdf)
        except OutputError as exc:
            # Log every failure but don't abort — the PDF is already saved locally.
            for spec, err in exc.failures:
                log.error("Output to %r failed: %s", spec, err)

    # Print the output path so callers (scripts, button handlers, etc.) can use it
    print(output_pdf)


if __name__ == "__main__":
    main()
