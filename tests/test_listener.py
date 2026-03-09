#!/usr/bin/env python3
"""
test_listener.py — Unit tests for scanner_pi.listener

Run with:
    pytest
    # or
    python3 -m unittest discover -s tests -v
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from scanner_pi import listener, scan


# --------------------------------------------------------------------------- #
# Fake SANE device helpers
# (Avoids PropertyMock-on-MagicMock class pollution and keeps tests readable.)
# --------------------------------------------------------------------------- #

class _FakeDev:
    """
    Lightweight stand-in for a python-sane device object.

    The 'scan' property yields successive values from *scan_values*; raise
    StopIteration (which the test loop should never reach) if exhausted.
    """
    def __init__(self, scan_values):
        self._values = iter(scan_values)

    @property
    def scan(self):
        return next(self._values)


class _FakeDevNoScan:
    """Device whose 'scan' attribute raises AttributeError (sensor absent)."""
    @property
    def scan(self):
        raise AttributeError("no scan attribute on this device")


def _make_sane(dev):
    """Return a mock sane module that yields *dev* from sane.open()."""
    mock_sane = MagicMock()
    mock_sane.open.return_value = dev
    return mock_sane


# --------------------------------------------------------------------------- #
# await_button
# --------------------------------------------------------------------------- #

class TestAwaitButton(unittest.TestCase):

    def test_returns_immediately_when_scan_is_true(self):
        """If the button is already pressed, await_button returns on the first poll."""
        dev = _FakeDev([True])
        mock_sane = _make_sane(dev)

        with patch.object(listener, "sane", mock_sane, create=True):
            with patch("time.sleep") as mock_sleep:
                listener.await_button("test:device:1", poll_interval=0.1)

        mock_sleep.assert_not_called()  # no sleep needed before first True

    def test_polls_until_scan_becomes_true(self):
        """Returns on the third check; sleeps twice between False readings."""
        dev = _FakeDev([False, False, True])
        mock_sane = _make_sane(dev)

        with patch.object(listener, "sane", mock_sane, create=True):
            with patch("time.sleep") as mock_sleep:
                listener.await_button("test:device:1", poll_interval=0.2)

        self.assertEqual(mock_sleep.call_count, 2)
        mock_sleep.assert_called_with(0.2)

    def test_uses_custom_poll_interval(self):
        """The poll_interval argument is forwarded to time.sleep."""
        dev = _FakeDev([False, True])
        mock_sane = _make_sane(dev)

        with patch.object(listener, "sane", mock_sane, create=True):
            with patch("time.sleep") as mock_sleep:
                listener.await_button("test:device:1", poll_interval=0.75)

        mock_sleep.assert_called_with(0.75)

    def test_opens_correct_device(self):
        """sane.open() is called with the exact device string passed in."""
        dev = _FakeDev([True])
        mock_sane = _make_sane(dev)

        with patch.object(listener, "sane", mock_sane, create=True):
            with patch("time.sleep"):
                listener.await_button("fujitsu:ScanSnap iX1300:1718762")

        mock_sane.open.assert_called_once_with("fujitsu:ScanSnap iX1300:1718762")

    def test_calls_sane_init(self):
        dev = _FakeDev([True])
        mock_sane = _make_sane(dev)

        with patch.object(listener, "sane", mock_sane, create=True):
            with patch("time.sleep"):
                listener.await_button("test:device:1")

        mock_sane.init.assert_called_once()

    def test_calls_sane_exit_on_success(self):
        """sane.exit() is called in the finally block after a normal return."""
        dev = _FakeDev([True])
        mock_sane = _make_sane(dev)

        with patch.object(listener, "sane", mock_sane, create=True):
            with patch("time.sleep"):
                listener.await_button("test:device:1")

        mock_sane.exit.assert_called_once()

    def test_calls_sane_exit_even_on_error(self):
        """sane.exit() is called in the finally block even when an error is raised."""
        dev = _FakeDevNoScan()
        mock_sane = _make_sane(dev)

        with patch.object(listener, "sane", mock_sane, create=True):
            with self.assertRaises(RuntimeError):
                listener.await_button("test:device:1")

        mock_sane.exit.assert_called_once()

    def test_raises_runtime_error_on_missing_scan_attribute(self):
        """AttributeError from dev.scan → RuntimeError with a helpful message."""
        dev = _FakeDevNoScan()
        mock_sane = _make_sane(dev)

        with patch.object(listener, "sane", mock_sane, create=True):
            with self.assertRaises(RuntimeError) as ctx:
                listener.await_button("test:device:1")

        self.assertIn("scan", str(ctx.exception).lower())

    def test_runtime_error_message_mentions_sensor(self):
        """The error message should guide the user toward diagnosing the problem."""
        dev = _FakeDevNoScan()
        mock_sane = _make_sane(dev)

        with patch.object(listener, "sane", mock_sane, create=True):
            with self.assertRaises(RuntimeError) as ctx:
                listener.await_button("test:device:1")

        self.assertIn("sensor", str(ctx.exception).lower())


# --------------------------------------------------------------------------- #
# do_scan
# --------------------------------------------------------------------------- #

class TestDoScan(unittest.TestCase):

    def _mock_result(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)

    def test_invokes_scan_pi_command(self):
        """subprocess.run is called with 'scan-pi' as the first element."""
        with patch("subprocess.run", return_value=self._mock_result(0)) as mock_run:
            listener.do_scan(Path("/tmp/my.toml"))

        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[0], "scan-pi")

    def test_passes_config_flag_and_path(self):
        """--config and the config path are in the subprocess argument list."""
        with patch("subprocess.run", return_value=self._mock_result(0)) as mock_run:
            listener.do_scan(scan.DEFAULT_CONFIG_PATH)

        cmd = mock_run.call_args[0][0]
        self.assertIn("--config", cmd)
        config_idx = cmd.index("--config")
        self.assertEqual(cmd[config_idx + 1], scan.DEFAULT_CONFIG_PATH.as_posix())

    def test_uses_capture_output_and_text(self):
        """Output is captured as text so it can be logged."""
        with patch("subprocess.run", return_value=self._mock_result(0)) as mock_run:
            listener.do_scan(Path("/tmp/my.toml"))

        kwargs = mock_run.call_args.kwargs
        self.assertTrue(kwargs.get("capture_output"))
        self.assertTrue(kwargs.get("text"))

    def test_returns_path_on_success(self):
        """Returns the Path printed to stdout by scan-pi on a zero exit."""
        with patch("subprocess.run",
                   return_value=self._mock_result(0, stdout="/home/neil/scans/scan_20240101.pdf\n")):
            result = listener.do_scan(Path("/tmp/my.toml"))
        self.assertEqual(result, Path("/home/neil/scans/scan_20240101.pdf"))

    def test_returns_none_on_failure(self):
        """Returns None when scan-pi exits with a non-zero code."""
        with patch("subprocess.run", return_value=self._mock_result(1, stderr="something broke")):
            result = listener.do_scan(Path("/tmp/my.toml"))
        self.assertIsNone(result)

    def test_strips_trailing_newline_from_path(self):
        """Trailing whitespace in scan-pi stdout is stripped before Path construction."""
        with patch("subprocess.run",
                   return_value=self._mock_result(0, stdout="/tmp/scan.pdf\n")):
            result = listener.do_scan(Path("/tmp/my.toml"))
        self.assertEqual(result, Path("/tmp/scan.pdf"))

    def test_handles_nonzero_exit_without_raising(self):
        """Nonzero exit — error is logged but no exception propagates to the caller."""
        with patch("subprocess.run", return_value=self._mock_result(1, stderr="something broke")):
            listener.do_scan(Path("/tmp/my.toml"))  # should not raise

    def test_config_path_sent_as_posix_string(self):
        """Path is converted via as_posix() so forward slashes are used."""
        with patch("subprocess.run", return_value=self._mock_result(0)) as mock_run:
            listener.do_scan(Path("/tmp/my.toml"))

        cmd = mock_run.call_args[0][0]
        # All elements should be strings (subprocess requires this)
        for element in cmd:
            self.assertIsInstance(element, str)


# --------------------------------------------------------------------------- #
# listen  (the main daemon loop)
# --------------------------------------------------------------------------- #

class TestListen(unittest.TestCase):
    """
    listen() runs an infinite loop, so every test must arrange for it to stop.
    We use KeyboardInterrupt (which triggers sys.exit(0)) as the clean exit.
    """

    def _run_listen(self, await_side_effect, do_scan_side_effect=None,
                    poll_interval=0.2, retry_delay=5.0, debounce_delay=2.0):
        """
        Helper: run listen() with patched await_button / do_scan / time.sleep.
        Returns (mock_do_scan, mock_sleep, raised_SystemExit).
        """
        with patch.object(listener, "await_button", side_effect=await_side_effect) as mock_await:
            with patch.object(listener, "do_scan",
                              side_effect=do_scan_side_effect) as mock_do_scan:
                with patch("time.sleep") as mock_sleep:
                    with self.assertRaises(SystemExit) as ctx:
                        listener.listen(
                            device="test:device:1",
                            config_path=Path("/tmp/config.toml"),
                            poll_interval=poll_interval,
                            retry_delay=retry_delay,
                            debounce_delay=debounce_delay,
                        )
        return mock_await, mock_do_scan, mock_sleep, ctx.exception

    # ---- KeyboardInterrupt handling ----------------------------------------

    def test_keyboard_interrupt_exits_with_code_zero(self):
        """KeyboardInterrupt in await_button → sys.exit(0)."""
        _, _, _, exc = self._run_listen(await_side_effect=KeyboardInterrupt)
        self.assertEqual(exc.code, 0)

    def test_keyboard_interrupt_during_scan_exits_cleanly(self):
        """KeyboardInterrupt raised by do_scan also causes sys.exit(0)."""
        # First await_button call succeeds; do_scan raises KeyboardInterrupt
        awaits = [None, KeyboardInterrupt]  # second call exits

        def fake_do_scan(path):
            raise KeyboardInterrupt

        with patch.object(listener, "await_button", side_effect=awaits):
            with patch.object(listener, "do_scan", side_effect=fake_do_scan):
                with patch("time.sleep"):
                    with self.assertRaises(SystemExit) as ctx:
                        listener.listen("dev", Path("/tmp/config.toml"))

        self.assertEqual(ctx.exception.code, 0)

    # ---- Button → scan → debounce -----------------------------------------

    def test_button_press_triggers_do_scan(self):
        """After await_button returns, do_scan is called with the config path."""
        # First await_button succeeds; second raises KeyboardInterrupt to stop loop
        _, mock_do_scan, _, _ = self._run_listen(
            await_side_effect=[None, KeyboardInterrupt]
        )
        mock_do_scan.assert_called_once_with(Path("/tmp/config.toml"))

    def test_debounce_sleep_called_after_scan(self):
        """time.sleep is called with debounce_delay immediately after each scan."""
        _, _, mock_sleep, _ = self._run_listen(
            await_side_effect=[None, KeyboardInterrupt],
            debounce_delay=3.75,
        )
        mock_sleep.assert_called_once_with(3.75)

    def test_two_scans_in_sequence(self):
        """Two successful button presses → do_scan called twice."""
        _, mock_do_scan, _, _ = self._run_listen(
            await_side_effect=[None, None, KeyboardInterrupt]
        )
        self.assertEqual(mock_do_scan.call_count, 2)

    def test_two_debounce_sleeps_for_two_scans(self):
        """Debounce sleep happens after each of two scans."""
        _, _, mock_sleep, _ = self._run_listen(
            await_side_effect=[None, None, KeyboardInterrupt],
            debounce_delay=1.0,
        )
        self.assertEqual(mock_sleep.call_count, 2)
        mock_sleep.assert_called_with(1.0)

    # ---- Error recovery / retry --------------------------------------------

    def test_exception_in_await_button_triggers_retry_sleep(self):
        """A non-KeyboardInterrupt exception → sleep(retry_delay), then loop continues."""
        _, mock_do_scan, mock_sleep, _ = self._run_listen(
            await_side_effect=[RuntimeError("device gone"), KeyboardInterrupt],
            retry_delay=7.5,
        )
        # do_scan should NOT have been called (error before scan)
        mock_do_scan.assert_not_called()
        # Retry sleep should have been called with retry_delay
        mock_sleep.assert_called_once_with(7.5)

    def test_exception_in_do_scan_triggers_retry_sleep(self):
        """An exception inside do_scan also causes retry sleep."""
        _, _, mock_sleep, _ = self._run_listen(
            await_side_effect=[None, KeyboardInterrupt],
            do_scan_side_effect=[RuntimeError("scan failed")],
            retry_delay=3.0,
        )
        # No debounce sleep (scan failed), only retry sleep
        mock_sleep.assert_called_once_with(3.0)

    def test_loop_continues_after_error(self):
        """After a transient error the loop continues and can complete a scan."""
        # Iteration 1: await_button fails → retry
        # Iteration 2: await_button succeeds → scan
        # Iteration 3: KeyboardInterrupt
        _, mock_do_scan, _, _ = self._run_listen(
            await_side_effect=[RuntimeError("transient"), None, KeyboardInterrupt]
        )
        mock_do_scan.assert_called_once()

    # ---- Argument forwarding -----------------------------------------------

    def test_device_and_poll_interval_forwarded_to_await_button(self):
        """listen() passes device and poll_interval to each await_button call."""
        with patch.object(listener, "await_button",
                          side_effect=KeyboardInterrupt) as mock_await:
            with patch("time.sleep"):
                with self.assertRaises(SystemExit):
                    listener.listen("fujitsu:dev:1", Path("/tmp/c.toml"),
                                    poll_interval=0.5)

        mock_await.assert_called_once_with("fujitsu:dev:1", poll_interval=0.5)

    # ---- OutputHandler integration -----------------------------------------

    def test_handler_send_called_with_pdf_path_when_scan_succeeds(self):
        """If do_scan returns a Path and handler is provided, handler.send() is called."""
        from scanner_pi.output import OutputHandler
        from unittest.mock import MagicMock

        pdf_path = Path("/home/neil/scans/scan_001.pdf")
        mock_handler = MagicMock(spec=OutputHandler)

        awaits = [None, KeyboardInterrupt]

        with patch.object(listener, "await_button", side_effect=awaits):
            with patch.object(listener, "do_scan", return_value=pdf_path):
                with patch("time.sleep"):
                    with self.assertRaises(SystemExit):
                        listener.listen("dev", Path("/tmp/config.toml"),
                                        handler=mock_handler)

        mock_handler.send.assert_called_once_with(pdf_path)

    def test_handler_not_called_when_scan_returns_none(self):
        """If do_scan returns None (scan failed), handler.send() is NOT called."""
        from scanner_pi.output import OutputHandler

        mock_handler = MagicMock(spec=OutputHandler)

        awaits = [None, KeyboardInterrupt]

        with patch.object(listener, "await_button", side_effect=awaits):
            with patch.object(listener, "do_scan", return_value=None):
                with patch("time.sleep"):
                    with self.assertRaises(SystemExit):
                        listener.listen("dev", Path("/tmp/config.toml"),
                                        handler=mock_handler)

        mock_handler.send.assert_not_called()

    def test_handler_not_called_when_handler_is_none(self):
        """With handler=None (default), nothing extra is called after a scan."""
        pdf_path = Path("/home/neil/scans/scan_001.pdf")
        awaits = [None, KeyboardInterrupt]

        with patch.object(listener, "await_button", side_effect=awaits):
            with patch.object(listener, "do_scan", return_value=pdf_path):
                with patch("time.sleep"):
                    with self.assertRaises(SystemExit):
                        listener.listen("dev", Path("/tmp/config.toml"),
                                        handler=None)
        # no assertion needed — test just confirms no AttributeError / TypeError raised

    def test_output_error_from_handler_is_logged_not_raised(self):
        """OutputError from handler.send() does not interrupt the listen loop."""
        from scanner_pi.output import OutputError, OutputHandler

        pdf_path = Path("/tmp/scan.pdf")
        failing_handler = MagicMock(spec=OutputHandler)
        failing_handler.send.side_effect = OutputError([(MagicMock(), RuntimeError("net error"))])

        awaits = [None, KeyboardInterrupt]

        with patch.object(listener, "await_button", side_effect=awaits):
            with patch.object(listener, "do_scan", return_value=pdf_path):
                with patch("time.sleep"):
                    with self.assertRaises(SystemExit) as ctx:
                        listener.listen("dev", Path("/tmp/config.toml"),
                                        handler=failing_handler)

        # Loop must have exited cleanly via KeyboardInterrupt, not via the OutputError
        self.assertEqual(ctx.exception.code, 0)


# --------------------------------------------------------------------------- #
# build_parser (listener CLI)
# --------------------------------------------------------------------------- #

class TestBuildParserListener(unittest.TestCase):

    def _parse(self, argv: list[str]) -> object:
        return listener.build_parser().parse_args(argv)

    def test_default_config_matches_scan_default(self):
        """Default --config value is whatever scan.DEFAULT_CONFIG_PATH is."""
        args = self._parse([])
        self.assertEqual(args.config, scan.DEFAULT_CONFIG_PATH)

    def test_custom_config_accepted(self):
        args = self._parse(["--config", "/tmp/my.toml"])
        self.assertEqual(args.config, Path("/tmp/my.toml"))

    def test_device_flag_accepted(self):
        args = self._parse(["--device", "fujitsu:ScanSnap iX1300:1718762"])
        self.assertEqual(args.device, "fujitsu:ScanSnap iX1300:1718762")

    def test_device_defaults_to_none(self):
        args = self._parse([])
        self.assertIsNone(args.device)

    def test_poll_interval_parsed_as_float(self):
        args = self._parse(["--poll-interval", "0.5"])
        self.assertAlmostEqual(args.poll_interval, 0.5)

    def test_poll_interval_default(self):
        args = self._parse([])
        self.assertAlmostEqual(args.poll_interval, listener._POLL_INTERVAL)

    def test_retry_delay_parsed_as_float(self):
        args = self._parse(["--retry-delay", "10.0"])
        self.assertAlmostEqual(args.retry_delay, 10.0)

    def test_retry_delay_default(self):
        args = self._parse([])
        self.assertAlmostEqual(args.retry_delay, listener._RETRY_DELAY)

    def test_debounce_delay_parsed_as_float(self):
        args = self._parse(["--debounce-delay", "3.0"])
        self.assertAlmostEqual(args.debounce_delay, 3.0)

    def test_debounce_delay_default(self):
        args = self._parse([])
        self.assertAlmostEqual(args.debounce_delay, listener._DEBOUNCE)

    def test_verbose_short_flag(self):
        args = self._parse(["-v"])
        self.assertTrue(args.verbose)

    def test_verbose_long_flag(self):
        args = self._parse(["--verbose"])
        self.assertTrue(args.verbose)

    def test_verbose_default_false(self):
        args = self._parse([])
        self.assertFalse(args.verbose)

    def test_all_flags_together(self):
        """All flags can be combined without conflict."""
        args = self._parse([
            "--config", "/tmp/my.toml",
            "--device", "test:dev:1",
            "--poll-interval", "0.3",
            "--retry-delay", "8.0",
            "--debounce-delay", "1.5",
            "--verbose",
        ])
        self.assertEqual(args.config, Path("/tmp/my.toml"))
        self.assertEqual(args.device, "test:dev:1")
        self.assertAlmostEqual(args.poll_interval, 0.3)
        self.assertAlmostEqual(args.retry_delay, 8.0)
        self.assertAlmostEqual(args.debounce_delay, 1.5)
        self.assertTrue(args.verbose)


if __name__ == "__main__":
    unittest.main(verbosity=2)
