import tomllib
import argparse
import logging
from pathlib import Path
import importlib.util
import os

log = logging.getLogger(__name__)


# the default config path is the config file in scanner_pi.config
# use importlib to obtain the file path
DEFAULT_CONFIG_PATH = Path(importlib.util.find_spec("scanner_pi.config").origin).parent / "default_config.toml"


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
    }
}


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

def load_config(config_path: Path, context: str = "scan") -> dict:
    """
    Load TOML config and merge with built-in defaults.

    Merge order (later entries win):

    1. Built-in ``DEFAULTS``
    2. ``[global.*]`` sections — settings shared by both scan-pi and
       scan-pi-listen
    3. ``[<context>.*]`` sub-sections — context-specific overrides
       (``context="scan"`` or ``context="listener"``)

    The returned dict always has the flat structure used by the rest of the
    module: ``{scanner: {...}, output: {...}, processing: {...}}``.

    Context-level destinations (e.g. ``[[listener.destinations]]``) are
    stored under ``config[context]["destinations"]`` for the caller to read.
    """
    config: dict = {section: dict(values) for section, values in DEFAULTS.items()}

    if not config_path.exists():
        log.debug("Config file not found at %s — using built-in defaults", config_path)
        return config

    with open(config_path, "rb") as fh:
        raw = tomllib.load(fh)
    log.debug("Loaded config from %s", config_path)

    # 1. Merge [global.*] sections — apply to both scan and listener.
    global_cfg = raw.get("global", {})
    for section in ("scanner", "output", "processing"):
        section_data = global_cfg.get(section, {})
        if section_data:
            log.debug("Merging [global.%s]", section)
            config[section].update(section_data)

    # 2. Merge context-specific sub-sections ([scan.*] or [listener.*]).
    #    These override any values already set from [global.*].
    ctx_cfg = raw.get(context, {})
    for section in ("scanner", "output", "processing"):
        section_data = ctx_cfg.get(section, {})
        if section_data:
            log.debug("Merging [%s.%s]", context, section)
            config[section].update(section_data)

    # 3. Expose context-level destinations (e.g. [[listener.destinations]]).
    #    These live directly under [<context>], not inside a sub-section.
    ctx_destinations = ctx_cfg.get("destinations", [])
    if ctx_destinations:
        config.setdefault(context, {})["destinations"] = ctx_destinations

    return config

def apply_env_overrides(config: dict) -> dict:
    """Apply environment variable overrides onto the config."""
    env_mapping = {
        "scanner": {
            "device": os.getenv("SCAN_PI_DEVICE"),
            "source": os.getenv("SCAN_PI_SOURCE"),
            "mode": os.getenv("SCAN_PI_MODE"),
            "resolution": os.getenv("SCAN_PI_RESOLUTION"),
            "format": os.getenv("SCAN_PI_FORMAT"),
            'driver_swskip': os.getenv("SCAN_PI_DRIVER_SWSKIP"),
            'driver_swcrop': os.getenv("SCAN_PI_DRIVER_SWCROP"),
        },
        "output": {
            "directory": os.getenv("SCAN_PI_OUTPUT_DIR"),
            "filename_prefix": os.getenv("SCAN_PI_FILENAME_PREFIX"),
        },
        "processing": {
            "jpeg_quality": os.getenv("SCAN_PI_JPEG_QUALITY"),
            "blank_page_threshold": os.getenv("SCAN_PI_BLANK_PAGE_THRESHOLD"),
        },
    }
    for section, values in env_mapping.items():
        for key, value in values.items():
            if value is not None:
                log.info("Overriding config from environment: [%s] %s = %s", section, key, value)
                config[section][key] = value
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
            'driver_swskip': args.swskip,
            'driver_swcrop': args.swcrop,
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
                log.info("Overriding config: [%s] %s = %s", section, key, value)
                config[section][key] = value
    return config
