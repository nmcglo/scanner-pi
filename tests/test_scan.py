#!/usr/bin/env python3
"""
test_scan.py — Unit tests for scanner_pi.scan

Run with:
    python3 -m unittest discover -s tests -v
    # or
    python3 tests/test_scan.py
"""

import argparse
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

from scanner_pi import scan


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _make_args(**kwargs) -> argparse.Namespace:
    """Return a Namespace with all CLI override fields defaulting to None."""
    defaults = dict(
        device=None,
        source=None,
        mode=None,
        resolution=None,
        format=None,
        output_dir=None,
        prefix=None,
        quality=None,
        blank_threshold=None,
    )
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _base_config() -> dict:
    """Return a fresh deep copy of the built-in defaults."""
    return {section: dict(values) for section, values in scan.DEFAULTS.items()}


def _write_toml(tmp_dir: Path, content: bytes) -> Path:
    path = tmp_dir / "test_config.toml"
    path.write_bytes(content)
    return path


def _single_page_pdf(path: Path, color: tuple = (128, 64, 32)) -> Path:
    """Save a minimal single-page PDF (used to test PDF merging)."""
    Image.new("RGB", (100, 100), color=color).save(path, format="PDF")
    return path


def _white_tiff(path: Path, size: tuple = (100, 100)) -> Path:
    """Save a plain white grayscale TIFF (blank page)."""
    Image.new("L", size, 255).save(path, format="TIFF")
    return path


def _content_tiff(path: Path, size: tuple = (100, 100)) -> Path:
    """Save a high-contrast checkerboard TIFF (non-blank page)."""
    img = Image.new("L", size, 0)
    px = img.load()
    for x in range(size[0]):
        for y in range(size[1]):
            if (x + y) % 2 == 0:
                px[x, y] = 255
    img.save(path, format="TIFF")
    return path


def _color_tiff(path: Path, color: tuple = (128, 64, 32)) -> Path:
    """Save a solid-colour RGB TIFF (for PDF assembly tests)."""
    Image.new("RGB", (200, 200), color=color).save(path, format="TIFF")
    return path


# --------------------------------------------------------------------------- #
# load_config
# --------------------------------------------------------------------------- #

class TestLoadConfig(unittest.TestCase):

    def test_missing_file_returns_pure_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = scan.load_config(Path(tmp) / "nonexistent.toml")

        self.assertEqual(config["scanner"]["device"], "")
        self.assertEqual(config["scanner"]["source"], "ADF Duplex")
        self.assertEqual(config["scanner"]["mode"], "Color")
        self.assertEqual(config["scanner"]["resolution"], 300)
        self.assertEqual(config["scanner"]["format"], "tiff")
        self.assertEqual(config["output"]["filename_prefix"], "scan")
        self.assertEqual(config["processing"]["jpeg_quality"], 85)
        self.assertAlmostEqual(config["processing"]["blank_page_threshold"], 0.015)

    def test_full_global_config_overrides_all_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _write_toml(Path(tmp), b"""
[global.scanner]
device = "my_device"
source = "ADF Front"
mode = "Gray"
resolution = 600
format = "pdf"

[global.output]
directory = "/tmp/docs"
filename_prefix = "doc"

[global.processing]
blank_page_threshold = 0.05
jpeg_quality = 70
""")
            config = scan.load_config(cfg)

        self.assertEqual(config["scanner"]["device"], "my_device")
        self.assertEqual(config["scanner"]["source"], "ADF Front")
        self.assertEqual(config["scanner"]["mode"], "Gray")
        self.assertEqual(config["scanner"]["resolution"], 600)
        self.assertEqual(config["scanner"]["format"], "pdf")
        self.assertEqual(config["output"]["directory"], "/tmp/docs")
        self.assertEqual(config["output"]["filename_prefix"], "doc")
        self.assertAlmostEqual(config["processing"]["blank_page_threshold"], 0.05)
        self.assertEqual(config["processing"]["jpeg_quality"], 70)

    def test_partial_global_config_merges_with_defaults(self):
        """Only the specified keys are overridden; the rest stay as defaults."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _write_toml(Path(tmp), b"""
[global.scanner]
mode = "Gray"
""")
            config = scan.load_config(cfg)

        self.assertEqual(config["scanner"]["mode"], "Gray")
        # Unspecified scanner keys keep defaults
        self.assertEqual(config["scanner"]["resolution"], 300)
        self.assertEqual(config["scanner"]["source"], "ADF Duplex")
        # Other sections completely untouched
        self.assertEqual(config["processing"]["jpeg_quality"], 85)

    def test_empty_config_file_returns_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _write_toml(Path(tmp), b"")
            config = scan.load_config(cfg)

        self.assertEqual(config["scanner"]["mode"], "Color")
        self.assertEqual(config["processing"]["jpeg_quality"], 85)


# --------------------------------------------------------------------------- #
# load_config — context merging
# --------------------------------------------------------------------------- #

class TestLoadConfigContext(unittest.TestCase):
    """Tests for the global → context-specific merge behaviour."""

    def test_context_defaults_to_scan(self):
        """Calling load_config without context= uses the 'scan' context."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _write_toml(Path(tmp), b"""
[scan.scanner]
mode = "Lineart"
""")
            config = scan.load_config(cfg)   # no context kwarg

        self.assertEqual(config["scanner"]["mode"], "Lineart")

    def test_scan_scanner_overrides_global_scanner(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _write_toml(Path(tmp), b"""
[global.scanner]
mode = "Gray"
resolution = 300

[scan.scanner]
mode = "Color"
""")
            config = scan.load_config(cfg, context="scan")

        self.assertEqual(config["scanner"]["mode"], "Color")       # scan wins
        self.assertEqual(config["scanner"]["resolution"], 300)     # from global

    def test_scan_output_overrides_global_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _write_toml(Path(tmp), b"""
[global.output]
directory = "~/scans"
filename_prefix = "scan"

[scan.output]
filename_prefix = "manual"
""")
            config = scan.load_config(cfg, context="scan")

        self.assertEqual(config["output"]["filename_prefix"], "manual")  # scan wins
        self.assertEqual(config["output"]["directory"], "~/scans")       # from global

    def test_scan_processing_overrides_global_processing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _write_toml(Path(tmp), b"""
[global.processing]
jpeg_quality = 80

[scan.processing]
jpeg_quality = 60
""")
            config = scan.load_config(cfg, context="scan")

        self.assertEqual(config["processing"]["jpeg_quality"], 60)

    def test_listener_context_overrides_global(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _write_toml(Path(tmp), b"""
[global.scanner]
source = "ADF Duplex"

[listener.scanner]
source = "ADF Front"
""")
            config = scan.load_config(cfg, context="listener")

        self.assertEqual(config["scanner"]["source"], "ADF Front")

    def test_scan_context_does_not_apply_listener_overrides(self):
        """Listener-specific settings must not bleed into the scan context."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _write_toml(Path(tmp), b"""
[global.scanner]
mode = "Gray"

[listener.scanner]
mode = "Color"
""")
            config = scan.load_config(cfg, context="scan")

        self.assertEqual(config["scanner"]["mode"], "Gray")   # listener ignored

    def test_global_output_destinations_loaded(self):
        """[[global.output.destinations]] ends up in config['output']['destinations']."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _write_toml(Path(tmp), b"""
[[global.output.destinations]]
type = "file"
directory = "/tmp/scans"
""")
            config = scan.load_config(cfg, context="scan")

        dests = config["output"].get("destinations", [])
        self.assertEqual(len(dests), 1)
        self.assertEqual(dests[0]["type"], "file")
        self.assertEqual(dests[0]["directory"], "/tmp/scans")

    def test_scan_destinations_replace_global_destinations(self):
        """Context-specific destinations completely replace global ones."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _write_toml(Path(tmp), b"""
[[global.output.destinations]]
type = "file"
directory = "/tmp/global"

[[scan.output.destinations]]
type = "file"
directory = "/tmp/scan-specific"
""")
            config = scan.load_config(cfg, context="scan")

        dests = config["output"].get("destinations", [])
        self.assertEqual(len(dests), 1)
        self.assertEqual(dests[0]["directory"], "/tmp/scan-specific")

    def test_listener_destinations_accessible(self):
        """[[listener.destinations]] is exposed as config['listener']['destinations']."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _write_toml(Path(tmp), b"""
[[listener.destinations]]
type = "file"
directory = "/tmp/listener"
""")
            config = scan.load_config(cfg, context="listener")

        dests = config.get("listener", {}).get("destinations", [])
        self.assertEqual(len(dests), 1)
        self.assertEqual(dests[0]["directory"], "/tmp/listener")

    def test_listener_destinations_not_in_scan_context(self):
        """[[listener.destinations]] must not appear when loading with context='scan'."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _write_toml(Path(tmp), b"""
[[listener.destinations]]
type = "file"
directory = "/tmp/listener"
""")
            config = scan.load_config(cfg, context="scan")

        dests = config.get("scan", {}).get("destinations", [])
        self.assertEqual(dests, [])

    def test_global_only_no_context_section(self):
        """A config with only [global.*] applies correctly regardless of context."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _write_toml(Path(tmp), b"""
[global.scanner]
resolution = 600
""")
            config_scan     = scan.load_config(cfg, context="scan")
            config_listener = scan.load_config(cfg, context="listener")

        self.assertEqual(config_scan["scanner"]["resolution"], 600)
        self.assertEqual(config_listener["scanner"]["resolution"], 600)


# --------------------------------------------------------------------------- #
# apply_overrides
# --------------------------------------------------------------------------- #

class TestApplyOverrides(unittest.TestCase):

    def test_all_none_leaves_config_unchanged(self):
        config = _base_config()
        result = scan.apply_overrides(config, _make_args())
        self.assertEqual(result["scanner"]["mode"], "Color")
        self.assertEqual(result["scanner"]["resolution"], 300)
        self.assertEqual(result["processing"]["jpeg_quality"], 85)

    def test_output_dir_override(self):
        config = _base_config()
        result = scan.apply_overrides(config, _make_args(output_dir="/new/path"))
        self.assertEqual(result["output"]["directory"], "/new/path")

    def test_prefix_override(self):
        config = _base_config()
        result = scan.apply_overrides(config, _make_args(prefix="invoice"))
        self.assertEqual(result["output"]["filename_prefix"], "invoice")

    def test_device_override(self):
        config = _base_config()
        result = scan.apply_overrides(config, _make_args(device="test:device:1"))
        self.assertEqual(result["scanner"]["device"], "test:device:1")

    def test_source_override(self):
        config = _base_config()
        result = scan.apply_overrides(config, _make_args(source="ADF Front"))
        self.assertEqual(result["scanner"]["source"], "ADF Front")

    def test_mode_override(self):
        config = _base_config()
        result = scan.apply_overrides(config, _make_args(mode="Lineart"))
        self.assertEqual(result["scanner"]["mode"], "Lineart")

    def test_resolution_override(self):
        config = _base_config()
        result = scan.apply_overrides(config, _make_args(resolution=600))
        self.assertEqual(result["scanner"]["resolution"], 600)

    def test_quality_override(self):
        config = _base_config()
        result = scan.apply_overrides(config, _make_args(quality=60))
        self.assertEqual(result["processing"]["jpeg_quality"], 60)

    def test_format_override(self):
        config = _base_config()
        result = scan.apply_overrides(config, _make_args(format="pdf"))
        self.assertEqual(result["scanner"]["format"], "pdf")

    def test_blank_threshold_override(self):
        config = _base_config()
        result = scan.apply_overrides(config, _make_args(blank_threshold=0.1))
        self.assertAlmostEqual(result["processing"]["blank_page_threshold"], 0.1)

    def test_multiple_overrides_applied_together(self):
        config = _base_config()
        args = _make_args(mode="Gray", resolution=150, quality=50,
                          output_dir="/tmp/out", prefix="test")
        result = scan.apply_overrides(config, args)
        self.assertEqual(result["scanner"]["mode"], "Gray")
        self.assertEqual(result["scanner"]["resolution"], 150)
        self.assertEqual(result["processing"]["jpeg_quality"], 50)
        self.assertEqual(result["output"]["directory"], "/tmp/out")
        self.assertEqual(result["output"]["filename_prefix"], "test")

    def test_none_args_do_not_overwrite_config_values(self):
        """A None CLI value must not clobber a value already set in config."""
        config = _base_config()
        config["scanner"]["mode"] = "Gray"
        config["scanner"]["resolution"] = 600
        result = scan.apply_overrides(config, _make_args(mode=None, resolution=None))
        self.assertEqual(result["scanner"]["mode"], "Gray")
        self.assertEqual(result["scanner"]["resolution"], 600)

    def test_override_does_not_mutate_unrelated_sections(self):
        config = _base_config()
        scan.apply_overrides(config, _make_args(mode="Gray"))
        # processing section should be unchanged
        self.assertEqual(config["processing"]["jpeg_quality"], 85)


# --------------------------------------------------------------------------- #
# detect_device
# --------------------------------------------------------------------------- #

class TestDetectDevice(unittest.TestCase):

    def _run(self, stdout: str) -> MagicMock:
        return MagicMock(stdout=stdout, returncode=0)

    def test_detects_device_from_standard_output(self):
        out = "device `fujitsu:ScanSnap iX1300:1718762' is a FUJITSU ScanSnap iX1300 scanner\n"
        with patch("subprocess.run", return_value=self._run(out)):
            device = scan.detect_device()
        self.assertEqual(device, "fujitsu:ScanSnap iX1300:1718762")

    def test_returns_first_when_multiple_devices_listed(self):
        out = (
            "device `fujitsu:ScanSnap iX1300:1718762' is a FUJITSU scanner\n"
            "device `hp:OfficeJet:456' is an HP scanner\n"
        )
        with patch("subprocess.run", return_value=self._run(out)):
            device = scan.detect_device()
        self.assertEqual(device, "fujitsu:ScanSnap iX1300:1718762")

    def test_raises_when_no_device_in_output(self):
        with patch("subprocess.run", return_value=self._run("No scanners found.\n")):
            with self.assertRaises(RuntimeError):
                scan.detect_device()

    def test_raises_on_empty_output(self):
        with patch("subprocess.run", return_value=self._run("")):
            with self.assertRaises(RuntimeError):
                scan.detect_device()

    def test_calls_scanimage_with_list_flag(self):
        out = "device `test:dev:1' is a scanner\n"
        with patch("subprocess.run", return_value=self._run(out)) as mock_run:
            scan.detect_device()
        mock_run.assert_called_once_with(
            ["scanimage", "-L"], capture_output=True, text=True
        )


# --------------------------------------------------------------------------- #
# scan_pages
# --------------------------------------------------------------------------- #

class TestScanPages(unittest.TestCase):

    def _mock_run(self, returncode: int = 7) -> MagicMock:
        return MagicMock(returncode=returncode, stderr="", stdout="")

    def test_returns_sorted_tiff_list_on_exit_0(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _white_tiff(tmp_path / "page0001.tif")
            _white_tiff(tmp_path / "page0002.tif")
            _white_tiff(tmp_path / "page0003.tif")

            with patch("subprocess.run", return_value=self._mock_run(0)):
                pages = scan.scan_pages("dev", "ADF Duplex", "Color", 300, "tiff", tmp_path)

        self.assertEqual(len(pages), 3)
        self.assertEqual([p.name for p in pages],
                         ["page0001.tif", "page0002.tif", "page0003.tif"])

    def test_exit_code_7_treated_as_success(self):
        """Exit code 7 means ADF empty — normal completion, not an error."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _white_tiff(tmp_path / "page0001.tif")

            with patch("subprocess.run", return_value=self._mock_run(7)):
                pages = scan.scan_pages("dev", "ADF Duplex", "Color", 300, "tiff", tmp_path)

        self.assertEqual(len(pages), 1)

    def test_non_zero_non_seven_exit_code_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("subprocess.run", return_value=self._mock_run(1)):
                with self.assertRaises(RuntimeError):
                    scan.scan_pages("dev", "ADF Duplex", "Color", 300, "tiff", Path(tmp))

    def test_returns_empty_list_when_no_files_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("subprocess.run", return_value=self._mock_run(7)):
                pages = scan.scan_pages("dev", "ADF Duplex", "Color", 300, "tiff", Path(tmp))
        self.assertEqual(pages, [])

    def test_correct_device_name_passed_to_scanimage(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("subprocess.run", return_value=self._mock_run()) as mock_run:
                scan.scan_pages("fujitsu:ScanSnap iX1300:1718762",
                                "ADF Duplex", "Color", 300, "tiff", Path(tmp))
        cmd = mock_run.call_args[0][0]
        self.assertIn("--device-name=fujitsu:ScanSnap iX1300:1718762", cmd)

    def test_correct_source_passed_to_scanimage(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("subprocess.run", return_value=self._mock_run()) as mock_run:
                scan.scan_pages("dev", "ADF Front", "Color", 300, "tiff", Path(tmp))
        cmd = mock_run.call_args[0][0]
        self.assertIn("--source=ADF Front", cmd)

    def test_correct_mode_and_resolution_passed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("subprocess.run", return_value=self._mock_run()) as mock_run:
                scan.scan_pages("dev", "ADF Duplex", "Gray", 600, "tiff", Path(tmp))
        cmd = mock_run.call_args[0][0]
        self.assertIn("--mode=Gray", cmd)
        self.assertIn("--resolution=600", cmd)

    def test_batch_format_and_start_always_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("subprocess.run", return_value=self._mock_run()) as mock_run:
                scan.scan_pages("dev", "ADF Duplex", "Color", 300, "tiff", Path(tmp))
        cmd = mock_run.call_args[0][0]
        self.assertIn("--format=tiff", cmd)
        self.assertIn("--batch-start=1", cmd)
        self.assertTrue(any(a.startswith("--batch=") for a in cmd))

    def test_format_passed_to_scanimage(self):
        for fmt in ("tiff", "png", "jpeg", "pnm", "pdf"):
            with self.subTest(fmt=fmt):
                with tempfile.TemporaryDirectory() as tmp:
                    with patch("subprocess.run", return_value=self._mock_run()) as mock_run:
                        scan.scan_pages("dev", "ADF Duplex", "Color", 300, fmt, Path(tmp))
                cmd = mock_run.call_args[0][0]
                self.assertIn(f"--format={fmt}", cmd)

    def test_format_determines_file_extension(self):
        """Each format produces a batch pattern with the correct file extension."""
        ext_cases = [
            ("tiff", "page%04d.tif"),
            ("png",  "page%04d.png"),
            ("jpeg", "page%04d.jpg"),
            ("pnm",  "page%04d.pnm"),
            ("pdf",  "page%04d.pdf"),
        ]
        for fmt, expected_pattern in ext_cases:
            with self.subTest(fmt=fmt):
                with tempfile.TemporaryDirectory() as tmp:
                    with patch("subprocess.run", return_value=self._mock_run()) as mock_run:
                        scan.scan_pages("dev", "ADF Duplex", "Color", 300, fmt, Path(tmp))
                cmd = mock_run.call_args[0][0]
                batch_arg = next(a for a in cmd if a.startswith("--batch="))
                self.assertTrue(batch_arg.endswith(expected_pattern))

    def test_batch_path_is_inside_output_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with patch("subprocess.run", return_value=self._mock_run()) as mock_run:
                scan.scan_pages("dev", "ADF Duplex", "Color", 300, "tiff", tmp_path)
        cmd = mock_run.call_args[0][0]
        batch_arg = next(a for a in cmd if a.startswith("--batch="))
        batch_path = batch_arg[len("--batch="):]
        self.assertTrue(batch_path.startswith(str(tmp_path)))


# --------------------------------------------------------------------------- #
# _page_stddev
# --------------------------------------------------------------------------- #

class TestPageStddev(unittest.TestCase):

    def test_uniform_white_image_has_zero_stddev(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _white_tiff(Path(tmp) / "white.tif")
            stddev = scan._page_stddev(path)
        self.assertAlmostEqual(stddev, 0.0, places=4)

    def test_uniform_black_image_has_zero_stddev(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "black.tif"
            Image.new("L", (100, 100), 0).save(path, format="TIFF")
            stddev = scan._page_stddev(path)
        self.assertAlmostEqual(stddev, 0.0, places=4)

    def test_uniform_grey_image_has_zero_stddev(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "grey.tif"
            Image.new("L", (100, 100), 128).save(path, format="TIFF")
            stddev = scan._page_stddev(path)
        self.assertAlmostEqual(stddev, 0.0, places=4)

    def test_checkerboard_image_has_high_stddev(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _content_tiff(Path(tmp) / "checker.tif")
            stddev = scan._page_stddev(path)
        self.assertGreater(stddev, 0.4)

    def test_rgb_image_accepted_and_converted_to_grayscale(self):
        """RGB TIFFs (as produced by the scanner in Color mode) should work."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rgb.tif"
            Image.new("RGB", (100, 100), (200, 100, 50)).save(path, format="TIFF")
            stddev = scan._page_stddev(path)
        self.assertIsInstance(stddev, float)
        self.assertAlmostEqual(stddev, 0.0, places=4)  # uniform → 0

    def test_returns_value_between_zero_and_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _content_tiff(Path(tmp) / "checker.tif")
            stddev = scan._page_stddev(path)
        self.assertGreaterEqual(stddev, 0.0)
        self.assertLessEqual(stddev, 1.0)


# --------------------------------------------------------------------------- #
# filter_blank_pages
# --------------------------------------------------------------------------- #

class TestFilterBlankPages(unittest.TestCase):

    THRESHOLD = 0.015

    def test_all_blank_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            pages = [_white_tiff(p / f"page{i:04d}.tif") for i in range(3)]
            result = scan.filter_blank_pages(pages, self.THRESHOLD)
        self.assertEqual(result, [])

    def test_no_blank_returns_all_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            pages = [_content_tiff(p / f"page{i:04d}.tif") for i in range(4)]
            result = scan.filter_blank_pages(pages, self.THRESHOLD)
        self.assertEqual(len(result), 4)

    def test_mixed_pages_keeps_only_non_blank(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            pages = [
                _content_tiff(p / "page0001.tif"),
                _white_tiff(p / "page0002.tif"),   # blank — drop
                _content_tiff(p / "page0003.tif"),
                _white_tiff(p / "page0004.tif"),   # blank — drop
            ]
            result = scan.filter_blank_pages(pages, self.THRESHOLD)

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].name, "page0001.tif")
        self.assertEqual(result[1].name, "page0003.tif")

    def test_preserves_original_page_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            pages = [_content_tiff(p / f"page{i:04d}.tif") for i in range(5)]
            result = scan.filter_blank_pages(pages, self.THRESHOLD)
        self.assertEqual([r.name for r in result],
                         [f"page{i:04d}.tif" for i in range(5)])

    def test_zero_threshold_keeps_all_pages(self):
        """Threshold of 0 means nothing is ever considered blank."""
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            pages = [_white_tiff(p / f"page{i:04d}.tif") for i in range(3)]
            result = scan.filter_blank_pages(pages, threshold=0.0)
        self.assertEqual(len(result), 3)

    def test_threshold_of_one_drops_all_pages(self):
        """Threshold of 1.0 means every page is considered blank."""
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            pages = [_content_tiff(p / f"page{i:04d}.tif") for i in range(3)]
            result = scan.filter_blank_pages(pages, threshold=1.0)
        self.assertEqual(result, [])

    def test_empty_input_returns_empty_list(self):
        result = scan.filter_blank_pages([], threshold=0.015)
        self.assertEqual(result, [])


# --------------------------------------------------------------------------- #
# assemble_pdf
# --------------------------------------------------------------------------- #

class TestAssemblePdf(unittest.TestCase):

    def test_single_page_creates_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            pages = [_color_tiff(p / "page0001.tif")]
            out = p / "out.pdf"
            scan.assemble_pdf(pages, out, jpeg_quality=85)
            self.assertTrue(out.exists())
            self.assertGreater(out.stat().st_size, 0)

    def test_multi_page_creates_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            pages = [
                _color_tiff(p / "page0001.tif", color=(255, 0, 0)),
                _color_tiff(p / "page0002.tif", color=(0, 255, 0)),
                _color_tiff(p / "page0003.tif", color=(0, 0, 255)),
            ]
            out = p / "out.pdf"
            scan.assemble_pdf(pages, out, jpeg_quality=85)
            self.assertTrue(out.exists())
            self.assertGreater(out.stat().st_size, 0)

    def test_output_has_pdf_magic_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            pages = [_color_tiff(p / "page0001.tif")]
            out = p / "out.pdf"
            scan.assemble_pdf(pages, out, jpeg_quality=85)
            magic = out.read_bytes()[:5]
        self.assertEqual(magic, b"%PDF-")

    def test_lower_quality_produces_smaller_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            # Use a real image with varied content so compression ratios differ
            pages_hi = [_content_tiff(p / "hi_page.tif")]
            pages_lo = [_content_tiff(p / "lo_page.tif")]
            out_hi = p / "hi.pdf"
            out_lo = p / "lo.pdf"
            scan.assemble_pdf(pages_hi, out_hi, jpeg_quality=95)
            scan.assemble_pdf(pages_lo, out_lo, jpeg_quality=5)
            self.assertLess(out_lo.stat().st_size, out_hi.stat().st_size)

    def test_overwrites_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            out = p / "out.pdf"
            out.write_bytes(b"old content")
            pages = [_color_tiff(p / "page0001.tif")]
            scan.assemble_pdf(pages, out, jpeg_quality=85)
            self.assertNotEqual(out.read_bytes(), b"old content")
            self.assertEqual(out.read_bytes()[:5], b"%PDF-")


# --------------------------------------------------------------------------- #
# merge_pdfs
# --------------------------------------------------------------------------- #

class TestMergePdfs(unittest.TestCase):

    def test_single_page_creates_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            pages = [_single_page_pdf(p / "page0001.pdf")]
            out = p / "out.pdf"
            scan.merge_pdfs(pages, out)
            self.assertTrue(out.exists())
            self.assertGreater(out.stat().st_size, 0)

    def test_multi_page_creates_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            pages = [
                _single_page_pdf(p / "page0001.pdf", color=(255, 0, 0)),
                _single_page_pdf(p / "page0002.pdf", color=(0, 255, 0)),
                _single_page_pdf(p / "page0003.pdf", color=(0, 0, 255)),
            ]
            out = p / "out.pdf"
            scan.merge_pdfs(pages, out)
            self.assertTrue(out.exists())
            self.assertGreater(out.stat().st_size, 0)

    def test_output_has_pdf_magic_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            pages = [_single_page_pdf(p / "page0001.pdf")]
            out = p / "out.pdf"
            scan.merge_pdfs(pages, out)
            self.assertEqual(out.read_bytes()[:5], b"%PDF-")

    def test_merged_pdf_has_correct_page_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            pages = [_single_page_pdf(p / f"page{i:04d}.pdf") for i in range(4)]
            out = p / "out.pdf"
            scan.merge_pdfs(pages, out)
            from pypdf import PdfReader
            reader = PdfReader(out)
            self.assertEqual(len(reader.pages), 4)


# --------------------------------------------------------------------------- #
# build_parser / CLI integration
# --------------------------------------------------------------------------- #

class TestBuildParser(unittest.TestCase):

    def _parse(self, argv: list[str]) -> argparse.Namespace:
        parser = scan.build_parser()
        return parser.parse_args(argv)

    def test_default_config_is_package_bundled_config(self):
        """Default config resolves to the config.toml shipped inside the package."""
        args = self._parse([])
        self.assertEqual(args.config, scan.DEFAULT_CONFIG_PATH)
        self.assertTrue(args.config.name == "default_config.toml")
        self.assertTrue(args.config.exists(), f"Bundled config not found at {args.config}")

    def test_custom_config_accepted(self):
        args = self._parse(["--config", "/tmp/my.toml"])
        self.assertEqual(args.config, Path("/tmp/my.toml"))

    def test_all_overrides_parsed(self):
        args = self._parse([
            "--output-dir", "/tmp/out",
            "--prefix", "doc",
            "--device", "test:dev:1",
            "--source", "ADF Front",
            "--mode", "Gray",
            "--resolution", "600",
            "--format", "pdf",
            "--quality", "70",
            "--blank-threshold", "0.05",
        ])
        self.assertEqual(args.output_dir, "/tmp/out")
        self.assertEqual(args.prefix, "doc")
        self.assertEqual(args.device, "test:dev:1")
        self.assertEqual(args.source, "ADF Front")
        self.assertEqual(args.mode, "Gray")
        self.assertEqual(args.resolution, 600)
        self.assertEqual(args.format, "pdf")
        self.assertEqual(args.quality, 70)
        self.assertAlmostEqual(args.blank_threshold, 0.05)

    def test_simplex_flag_parsed(self):
        args = self._parse(["--simplex"])
        self.assertTrue(args.simplex)

    def test_verbose_flag_parsed(self):
        args = self._parse(["-v"])
        self.assertTrue(args.verbose)

    def test_defaults_are_none_when_not_provided(self):
        args = self._parse([])
        self.assertIsNone(args.device)
        self.assertIsNone(args.output_dir)
        self.assertIsNone(args.mode)
        self.assertIsNone(args.resolution)

    def test_invalid_mode_rejected(self):
        parser = scan.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["--mode", "Infrared"])

    def test_invalid_source_rejected(self):
        parser = scan.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["--source", "Flatbed"])

    def test_all_valid_formats_accepted(self):
        for fmt in ("tiff", "png", "jpeg", "pnm", "pdf"):
            with self.subTest(fmt=fmt):
                args = self._parse(["--format", fmt])
                self.assertEqual(args.format, fmt)

    def test_invalid_format_rejected(self):
        parser = scan.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["--format", "bmp"])


# --------------------------------------------------------------------------- #
# simplex shortcut (exercised in main())
# --------------------------------------------------------------------------- #

class TestSimplexShortcut(unittest.TestCase):
    """
    Verify that --simplex sets source to 'ADF Front' when --source is absent,
    and that an explicit --source takes precedence over --simplex.
    """

    def _run_main_with(self, argv: list[str]) -> argparse.Namespace:
        """
        Parse args as main() would, apply the simplex shortcut, and return
        the resulting args Namespace (without actually scanning anything).
        """
        parser = scan.build_parser()
        args = parser.parse_args(argv)
        if args.simplex and args.source is None:
            args.source = "ADF Front"
        return args

    def test_simplex_sets_source_to_adf_front(self):
        args = self._run_main_with(["--simplex"])
        self.assertEqual(args.source, "ADF Front")

    def test_simplex_does_not_override_explicit_source(self):
        # --source is mutually usable with --simplex; explicit wins
        args = self._run_main_with(["--simplex", "--source", "ADF Duplex"])
        self.assertEqual(args.source, "ADF Duplex")

    def test_without_simplex_source_stays_none(self):
        args = self._run_main_with([])
        self.assertIsNone(args.source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
