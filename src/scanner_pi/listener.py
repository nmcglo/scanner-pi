#!/usr/bin/env python3
"""
listener.py — Hardware button listener for the ScanSnap scanner.

Runs as a daemon, opening the SANE device and polling the hardware scan
button.  When the button is pressed, scan-pi is invoked to perform the
scan and produce a PDF.

The ScanSnap iX1300 exposes its button as a read-only SANE sensor option
named 'scan'.  Verify available sensor options on your device with:

    scanimage --device-name=<device> --all-options

Usage:
    scan-pi-listen                      # use default config, auto-detect device
    scan-pi-listen -v                   # verbose / debug logging
    scan-pi-listen --config ~/my.toml
    scan-pi-listen --device "fujitsu:ScanSnap iX1300:1718762"
"""

import argparse
import logging
import subprocess
import sys
import time
from pathlib import Path
import sane 

from scanner_pi import util
util.monkey_patch_sane_scan(sane)  # avoid dumb collision between sane.SaneDev.scan and sane.SaneDev.options["scan"]

from scanner_pi import scan
from scanner_pi.config.configuration import apply_env_overrides
from scanner_pi.output import OutputError, OutputHandler, build_handler

log = logging.getLogger("listener")

# Default timing constants — all overridable via CLI.
_POLL_INTERVAL = 0.2  # seconds between reads of the button sensor
_RETRY_DELAY   = 5.0  # seconds to wait after a connection error before re-opening
_DEBOUNCE      = 2.0  # seconds to ignore the button after a scan fires


# --------------------------------------------------------------------------- #
# Button detection
# --------------------------------------------------------------------------- #

def await_button(device: str, poll_interval: float = _POLL_INTERVAL) -> None:
    """
    Open the SANE device and block until the hardware scan button is pressed.

    The iX1300 exposes its scan button as the read-only SANE sensor option
    'scan' (visible as '--scan[=(yes|no)]' in --all-options).  We keep the
    device open and poll this attribute at *poll_interval* second intervals.

    Raises RuntimeError if the device cannot be opened or the 'scan' sensor
    option is absent on the attached scanner model.
    """

    sane.init()
    try:
        dev = sane.open(device)
        log.debug("Opened SANE device: %s", device)
        while True:
            try:
                if dev.scan:
                    return  # button pressed
            except AttributeError:
                raise RuntimeError(
                    "The 'scan' button sensor was not found on this device. "
                    "Run 'scanimage --device-name=<dev> --all-options' and look "
                    "for a sensor option to use for button detection."
                )
            time.sleep(poll_interval)
    finally:
        sane.exit()


# --------------------------------------------------------------------------- #
# Scan trigger
# --------------------------------------------------------------------------- #

def do_scan(config_path: Path) -> Path | None:
    """
    Invoke scan-pi as a subprocess and return the path of the assembled PDF.

    Running as a subprocess (rather than importing scan.main directly) gives
    us a clean process boundary — any crash in the scan does not bring down
    the listener daemon.

    Returns the Path printed to stdout by scan-pi on success, or None if
    scan-pi exits with a non-zero code.
    """
    result = subprocess.run(
        ["scan-pi", "--config", config_path.as_posix()],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        out = result.stdout.strip()
        if out:
            pdf_path = Path(out)
            log.info("Scan complete: %s", pdf_path)
            return pdf_path
        # scan-pi printed nothing — no FileSpec destination configured
        log.info("Scan complete (no local file produced)")
        return None
    else:
        log.error(
            "scan-pi failed (exit %d):\n%s",
            result.returncode,
            result.stderr.strip(),
        )
        return None


# --------------------------------------------------------------------------- #
# Main listen loop
# --------------------------------------------------------------------------- #

def listen(
    device: str,
    config_path: Path,
    poll_interval: float = _POLL_INTERVAL,
    retry_delay: float   = _RETRY_DELAY,
    debounce_delay: float = _DEBOUNCE,
    handler: OutputHandler | None = None,
) -> None:
    """
    Loop forever: wait for a button press, trigger a scan, repeat.

    If the scanner becomes unavailable (e.g. USB reconnect), the error is
    logged and the loop retries after *retry_delay* seconds.  After each
    successful scan, the button is ignored for *debounce_delay* seconds to
    prevent double-triggers from a single press.

    Parameters
    ----------
    handler:
        Optional OutputHandler to run after each successful scan.  Configured
        via ``[[listener.destinations]]`` in the config file.  These run
        against the local PDF path returned by scan-pi; they are only invoked
        when scan-pi produces a local file (i.e. at least one FileSpec is in
        the scan output pipeline).  OutputErrors are logged but do not
        interrupt the listen loop.
    """
    log.info("Watching for button press on: %s", device)
    while True:
        try:
            log.debug("Awaiting button press...")
            await_button(device, poll_interval=poll_interval)
            log.info("Button pressed — starting scan")
            pdf_path = do_scan(config_path)
            if pdf_path is not None and handler is not None:
                try:
                    handler.send(pdf_path)
                except OutputError as exc:
                    for spec, err in exc.failures:
                        log.error("Listener output to %r failed: %s", spec, err)
            log.debug("Debounce: waiting %.1f s", debounce_delay)
            time.sleep(debounce_delay)
        except KeyboardInterrupt:
            log.info("Listener stopped")
            sys.exit(0)
        except Exception as e:
            log.error("Error: %s — retrying in %.0f s", e, retry_delay)
            time.sleep(retry_delay)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Watch the scanner's hardware button and trigger scan-pi when pressed."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    parser.add_argument(
        "--config", type=Path, default=scan.DEFAULT_CONFIG_PATH, metavar="FILE",
        help=f"Config file passed through to scan-pi (default: {scan.DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--device", metavar="NAME",
        help="SANE device name (default: read from config, then auto-detect)",
    )
    parser.add_argument(
        "--poll-interval", type=float, default=_POLL_INTERVAL, metavar="SECS",
        help=f"Seconds between button-state polls (default: {_POLL_INTERVAL})",
    )
    parser.add_argument(
        "--retry-delay", type=float, default=_RETRY_DELAY, metavar="SECS",
        help=f"Seconds to wait after an error before retrying (default: {_RETRY_DELAY})",
    )
    parser.add_argument(
        "--debounce-delay", type=float, default=_DEBOUNCE, metavar="SECS",
        help=f"Seconds to ignore button after a scan fires (default: {_DEBOUNCE})",
    )
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

    config = scan.load_config(args.config, context="listener")
    config = apply_env_overrides(config)

    device = args.device or config["scanner"]["device"] or scan.detect_device()

    # Build an OutputHandler from [[listener.destinations]] if configured.
    # These run after each button-triggered scan, independently of any
    # [[output.destinations]] that scan-pi itself processes.
    listener_dests = config.get("listener", {}).get("destinations", [])
    handler = build_handler(listener_dests) if listener_dests else None

    listen(
        device=device,
        config_path=args.config,
        poll_interval=args.poll_interval,
        retry_delay=args.retry_delay,
        debounce_delay=args.debounce_delay,
        handler=handler,
    )


if __name__ == "__main__":
    main()
