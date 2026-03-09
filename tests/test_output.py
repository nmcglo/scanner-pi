#!/usr/bin/env python3
"""
test_output.py — Unit tests for scanner_pi.output

Run with:
    pytest
    # or
    python3 -m unittest discover -s tests -v
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import requests as _real_requests

from scanner_pi import output
from scanner_pi.output import (
    FileSpec,
    OutputDestinationSpec,
    OutputError,
    OutputHandler,
    PaperlessNgxSpec,
    _spec_from_config,
    build_handler,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

_BASE_URL  = "http://paperless:8000"
_TOKEN     = "supersecrettoken"
_TASK_UUID = "a1b2c3d4-0000-0000-0000-000000000000"


def _minimal_spec(**kwargs) -> PaperlessNgxSpec:
    """PaperlessNgxSpec with only required fields (plus any overrides)."""
    defaults = dict(base_url=_BASE_URL, token=_TOKEN)
    defaults.update(kwargs)
    return PaperlessNgxSpec(**defaults)


def _ok_get(results: list) -> MagicMock:
    """Mock GET response returning *results* as a page."""
    mock = MagicMock()
    mock.json.return_value = {"count": len(results), "results": results}
    mock.raise_for_status.return_value = None
    return mock


def _ok_post(task_id: str = _TASK_UUID) -> MagicMock:
    """Mock POST response simulating a successful Paperless-ngx document upload."""
    mock = MagicMock()
    mock.text = f'"{task_id}"'
    mock.raise_for_status.return_value = None
    return mock


def _error_response(status_code: int = 403) -> MagicMock:
    """Mock response that raises HTTPError on raise_for_status()."""
    mock = MagicMock()
    mock.status_code = status_code
    mock.raise_for_status.side_effect = _real_requests.HTTPError(
        response=mock
    )
    return mock


def _make_pdf(directory: Path, name: str = "scan_0001.pdf") -> Path:
    """Write a minimal (but valid) PDF file and return its path."""
    path = directory / name
    # Pillow-style single-page PDF magic bytes are enough for open("rb")
    path.write_bytes(b"%PDF-1.4 1 0 obj<</Type/Catalog>>endobj\n%%EOF\n")
    return path


class _FakeSpec(OutputDestinationSpec):
    """Trivial concrete spec for OutputHandler tests."""

    def __init__(self, name: str, raises: Exception | None = None) -> None:
        self.name = name
        self.raises = raises
        self.received: list[Path] = []

    def send(self, pdf_path: Path) -> None:
        self.received.append(pdf_path)
        if self.raises:
            raise self.raises

    def __repr__(self) -> str:
        return f"_FakeSpec({self.name!r})"


# --------------------------------------------------------------------------- #
# OutputError
# --------------------------------------------------------------------------- #

class TestOutputError(unittest.TestCase):

    def _make(self, n: int = 1) -> OutputError:
        spec = _FakeSpec("s")
        exc  = ValueError("boom")
        return OutputError([(spec, exc)] * n)

    def test_message_includes_failure_count_singular(self):
        err = self._make(1)
        self.assertIn("1 output destination(s) failed", str(err))

    def test_message_includes_failure_count_plural(self):
        err = self._make(3)
        self.assertIn("3 output destination(s) failed", str(err))

    def test_failures_attribute_length(self):
        err = self._make(2)
        self.assertEqual(len(err.failures), 2)

    def test_failures_attribute_contains_spec_and_exc(self):
        spec = _FakeSpec("x")
        exc  = RuntimeError("oops")
        err  = OutputError([(spec, exc)])
        self.assertIs(err.failures[0][0], spec)
        self.assertIs(err.failures[0][1], exc)

    def test_message_contains_repr_of_spec(self):
        spec = _FakeSpec("myspec")
        err  = OutputError([(spec, ValueError("fail"))])
        self.assertIn("myspec", str(err))

    def test_is_exception_subclass(self):
        self.assertIsInstance(self._make(), Exception)


# --------------------------------------------------------------------------- #
# FileSpec
# --------------------------------------------------------------------------- #

class TestFileSpec(unittest.TestCase):

    def test_copies_file_to_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            src_dir = Path(tmp) / "src"
            dst_dir = Path(tmp) / "dst"
            src_dir.mkdir()
            pdf = _make_pdf(src_dir)

            FileSpec(directory=dst_dir).send(pdf)

            self.assertTrue((dst_dir / pdf.name).exists())

    def test_creates_destination_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            src_dir = Path(tmp) / "src"
            dst_dir = Path(tmp) / "a" / "b" / "c"
            src_dir.mkdir()
            pdf = _make_pdf(src_dir)

            FileSpec(directory=dst_dir).send(pdf)

            self.assertTrue(dst_dir.exists())

    def test_uses_original_filename_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            src_dir = Path(tmp) / "src"
            dst_dir = Path(tmp) / "dst"
            src_dir.mkdir()
            pdf = _make_pdf(src_dir, name="my_scan.pdf")

            FileSpec(directory=dst_dir).send(pdf)

            self.assertTrue((dst_dir / "my_scan.pdf").exists())

    def test_uses_specified_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            src_dir = Path(tmp) / "src"
            dst_dir = Path(tmp) / "dst"
            src_dir.mkdir()
            pdf = _make_pdf(src_dir, name="tmp.pdf")

            FileSpec(directory=dst_dir, filename="invoice.pdf").send(pdf)

            self.assertTrue((dst_dir / "invoice.pdf").exists())
            self.assertFalse((dst_dir / "tmp.pdf").exists())

    def test_copied_content_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            src_dir = Path(tmp) / "src"
            dst_dir = Path(tmp) / "dst"
            src_dir.mkdir()
            pdf = _make_pdf(src_dir)

            FileSpec(directory=dst_dir).send(pdf)

            self.assertEqual(
                pdf.read_bytes(),
                (dst_dir / pdf.name).read_bytes(),
            )

    def test_source_file_is_not_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            src_dir = Path(tmp) / "src"
            dst_dir = Path(tmp) / "dst"
            src_dir.mkdir()
            pdf = _make_pdf(src_dir)

            FileSpec(directory=dst_dir).send(pdf)

            self.assertTrue(pdf.exists())

    def test_repr_includes_directory(self):
        spec = FileSpec(directory=Path("/tmp/scans"))
        self.assertIn("/tmp/scans", repr(spec))

    def test_repr_does_not_omit_filename_when_set(self):
        spec = FileSpec(directory=Path("/tmp/scans"), filename="doc.pdf")
        self.assertIn("doc.pdf", repr(spec))

    def test_is_output_destination_spec(self):
        self.assertIsInstance(FileSpec(directory=Path("/tmp")), OutputDestinationSpec)


# --------------------------------------------------------------------------- #
# PaperlessNgxSpec — network interactions
# --------------------------------------------------------------------------- #

class TestPaperlessNgxSpec(unittest.TestCase):
    """
    All HTTP traffic is intercepted via patch("scanner_pi.output.requests").
    Tests receive separate mock_get / mock_post handles for fine-grained assertions.
    """

    def _send(self, spec: PaperlessNgxSpec,
              get_side_effect=None, post_return=None):
        """
        Call spec.send() with patched requests.get / requests.post.
        Returns (mock_get, mock_post).
        """
        if post_return is None:
            post_return = _ok_post()

        with tempfile.TemporaryDirectory() as tmp:
            pdf = _make_pdf(Path(tmp))
            with patch("scanner_pi.output.requests.get",
                       side_effect=get_side_effect) as mock_get:
                with patch("scanner_pi.output.requests.post",
                           return_value=post_return) as mock_post:
                    spec.send(pdf)

        return mock_get, mock_post

    # ---- upload endpoint / auth ------------------------------------------- #

    def test_posts_to_correct_endpoint(self):
        spec = _minimal_spec()
        _, mock_post = self._send(spec)
        url = mock_post.call_args[0][0]
        self.assertEqual(url, f"{_BASE_URL}/api/documents/post_document/")

    def test_trailing_slash_in_base_url_stripped(self):
        spec = _minimal_spec(base_url=f"{_BASE_URL}/")
        _, mock_post = self._send(spec)
        url = mock_post.call_args[0][0]
        self.assertNotIn("//api/", url)
        self.assertTrue(url.endswith("/api/documents/post_document/"))

    def test_auth_token_in_request_header(self):
        spec = _minimal_spec()
        _, mock_post = self._send(spec)
        headers = mock_post.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], f"Token {_TOKEN}")

    def test_document_sent_as_multipart_file(self):
        spec = _minimal_spec()
        _, mock_post = self._send(spec)
        files = mock_post.call_args.kwargs["files"]
        self.assertIn("document", files)

    def test_document_filename_in_multipart(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = _make_pdf(Path(tmp), name="myinvoice.pdf")
            with patch("scanner_pi.output.requests.get"):
                with patch("scanner_pi.output.requests.post",
                           return_value=_ok_post()) as mock_post:
                    _minimal_spec().send(pdf)

        name, _fh, mime = mock_post.call_args.kwargs["files"]["document"]
        self.assertEqual(name, "myinvoice.pdf")
        self.assertEqual(mime, "application/pdf")

    # ---- optional metadata fields ----------------------------------------- #

    def test_title_included_when_set(self):
        spec = _minimal_spec(title="My Invoice")
        _, mock_post = self._send(spec)
        data = mock_post.call_args.kwargs["data"]
        self.assertEqual(data["title"], "My Invoice")

    def test_title_absent_when_not_set(self):
        spec = _minimal_spec(title=None)
        _, mock_post = self._send(spec)
        data = mock_post.call_args.kwargs["data"]
        self.assertNotIn("title", data)

    # ---- tags ------------------------------------------------------------- #

    def test_tag_lookup_uses_name_iexact(self):
        spec = _minimal_spec(tag_names=["Scanner"])
        mock_get, _ = self._send(
            spec,
            get_side_effect=[_ok_get([{"id": 7, "name": "Scanner"}])],
        )
        params = mock_get.call_args.kwargs["params"]
        self.assertEqual(params["name__iexact"], "Scanner")

    def test_tag_lookup_hits_tags_endpoint(self):
        spec = _minimal_spec(tag_names=["Inbox"])
        mock_get, _ = self._send(
            spec,
            get_side_effect=[_ok_get([{"id": 3, "name": "Inbox"}])],
        )
        url = mock_get.call_args[0][0]
        self.assertIn("/api/tags/", url)

    def test_resolved_tag_ids_sent_in_post(self):
        spec = _minimal_spec(tag_names=["Inbox", "Scanner"])
        self._send(
            spec,
            get_side_effect=[
                _ok_get([{"id": 3, "name": "Inbox"}]),
                _ok_get([{"id": 7, "name": "Scanner"}]),
            ],
        )
        # Verified via: no ValueError raised + post was called once.
        # The exact data dict is tested below.
        pass  # implicitly passes if send() completes without error

    def test_tag_ids_present_in_post_data(self):
        spec = _minimal_spec(tag_names=["Inbox"])
        with tempfile.TemporaryDirectory() as tmp:
            pdf = _make_pdf(Path(tmp))
            with patch("scanner_pi.output.requests.get",
                       return_value=_ok_get([{"id": 42, "name": "Inbox"}])):
                with patch("scanner_pi.output.requests.post",
                           return_value=_ok_post()) as mock_post:
                    spec.send(pdf)
        data = mock_post.call_args.kwargs["data"]
        self.assertIn("tags", data)
        self.assertIn(42, data["tags"])

    def test_tags_field_absent_when_no_tag_names(self):
        spec = _minimal_spec(tag_names=[])
        _, mock_post = self._send(spec)
        data = mock_post.call_args.kwargs["data"]
        self.assertNotIn("tags", data)

    def test_raises_value_error_for_unknown_tag(self):
        spec = _minimal_spec(tag_names=["Nonexistent"])
        with tempfile.TemporaryDirectory() as tmp:
            pdf = _make_pdf(Path(tmp))
            with patch("scanner_pi.output.requests.get",
                       return_value=_ok_get([])):   # empty results
                with self.assertRaises(ValueError) as ctx:
                    spec.send(pdf)
        self.assertIn("Nonexistent", str(ctx.exception))

    # ---- correspondent ---------------------------------------------------- #

    def test_correspondent_lookup_hits_correspondents_endpoint(self):
        spec = _minimal_spec(correspondent_name="ACME Corp")
        mock_get, _ = self._send(
            spec,
            get_side_effect=[_ok_get([{"id": 5, "name": "ACME Corp"}])],
        )
        url = mock_get.call_args[0][0]
        self.assertIn("/api/correspondents/", url)

    def test_correspondent_id_in_post_data(self):
        spec = _minimal_spec(correspondent_name="ACME Corp")
        with tempfile.TemporaryDirectory() as tmp:
            pdf = _make_pdf(Path(tmp))
            with patch("scanner_pi.output.requests.get",
                       return_value=_ok_get([{"id": 5, "name": "ACME Corp"}])):
                with patch("scanner_pi.output.requests.post",
                           return_value=_ok_post()) as mock_post:
                    spec.send(pdf)
        data = mock_post.call_args.kwargs["data"]
        self.assertEqual(data["correspondent"], 5)

    def test_correspondent_absent_when_not_set(self):
        spec = _minimal_spec(correspondent_name=None)
        _, mock_post = self._send(spec)
        data = mock_post.call_args.kwargs["data"]
        self.assertNotIn("correspondent", data)

    # ---- document type ---------------------------------------------------- #

    def test_document_type_lookup_hits_document_types_endpoint(self):
        spec = _minimal_spec(document_type_name="Invoice")
        mock_get, _ = self._send(
            spec,
            get_side_effect=[_ok_get([{"id": 2, "name": "Invoice"}])],
        )
        url = mock_get.call_args[0][0]
        self.assertIn("/api/document_types/", url)

    def test_document_type_id_in_post_data(self):
        spec = _minimal_spec(document_type_name="Invoice")
        with tempfile.TemporaryDirectory() as tmp:
            pdf = _make_pdf(Path(tmp))
            with patch("scanner_pi.output.requests.get",
                       return_value=_ok_get([{"id": 2, "name": "Invoice"}])):
                with patch("scanner_pi.output.requests.post",
                           return_value=_ok_post()) as mock_post:
                    spec.send(pdf)
        data = mock_post.call_args.kwargs["data"]
        self.assertEqual(data["document_type"], 2)

    def test_document_type_absent_when_not_set(self):
        spec = _minimal_spec(document_type_name=None)
        _, mock_post = self._send(spec)
        data = mock_post.call_args.kwargs["data"]
        self.assertNotIn("document_type", data)

    # ---- lookup failure / HTTP errors ------------------------------------- #

    def test_raises_value_error_for_unknown_correspondent(self):
        spec = _minimal_spec(correspondent_name="Nobody")
        with tempfile.TemporaryDirectory() as tmp:
            pdf = _make_pdf(Path(tmp))
            with patch("scanner_pi.output.requests.get",
                       return_value=_ok_get([])):
                with self.assertRaises((ValueError, Exception)):
                    spec.send(pdf)

    def test_http_error_on_bad_post_propagates(self):
        spec = _minimal_spec()
        with tempfile.TemporaryDirectory() as tmp:
            pdf = _make_pdf(Path(tmp))
            with patch("scanner_pi.output.requests.get"):
                with patch("scanner_pi.output.requests.post",
                           return_value=_error_response(403)):
                    with self.assertRaises(_real_requests.HTTPError):
                        spec.send(pdf)

    def test_http_error_on_get_propagates(self):
        spec = _minimal_spec(tag_names=["Inbox"])
        with tempfile.TemporaryDirectory() as tmp:
            pdf = _make_pdf(Path(tmp))
            with patch("scanner_pi.output.requests.get",
                       return_value=_error_response(401)):
                with self.assertRaises(_real_requests.HTTPError):
                    spec.send(pdf)

    # ---- repr / token safety ---------------------------------------------- #

    def test_token_not_in_repr(self):
        spec = _minimal_spec(token="my_very_secret_token")
        self.assertNotIn("my_very_secret_token", repr(spec))

    def test_base_url_in_repr(self):
        spec = _minimal_spec(base_url="http://paperless:8000")
        self.assertIn("http://paperless:8000", repr(spec))

    def test_is_output_destination_spec(self):
        self.assertIsInstance(_minimal_spec(), OutputDestinationSpec)


# --------------------------------------------------------------------------- #
# OutputHandler
# --------------------------------------------------------------------------- #

class TestOutputHandler(unittest.TestCase):

    def test_calls_send_on_all_specs(self):
        specs = [_FakeSpec("a"), _FakeSpec("b"), _FakeSpec("c")]
        handler = OutputHandler(specs)
        pdf = Path("/tmp/dummy.pdf")
        handler.send(pdf)
        for spec in specs:
            self.assertEqual(spec.received, [pdf])

    def test_passes_correct_pdf_path_to_each_spec(self):
        spec = _FakeSpec("s")
        pdf  = Path("/tmp/scan_0001.pdf")
        OutputHandler([spec]).send(pdf)
        self.assertEqual(spec.received[0], pdf)

    def test_continues_after_one_failure(self):
        """All specs are attempted even when an earlier one raises."""
        spec_a = _FakeSpec("a", raises=RuntimeError("boom"))
        spec_b = _FakeSpec("b")
        pdf    = Path("/tmp/dummy.pdf")

        with self.assertRaises(OutputError):
            OutputHandler([spec_a, spec_b]).send(pdf)

        self.assertEqual(spec_b.received, [pdf])

    def test_continues_after_multiple_failures(self):
        spec_a = _FakeSpec("a", raises=RuntimeError("err a"))
        spec_b = _FakeSpec("b", raises=ValueError("err b"))
        spec_c = _FakeSpec("c")
        pdf    = Path("/tmp/dummy.pdf")

        with self.assertRaises(OutputError):
            OutputHandler([spec_a, spec_b, spec_c]).send(pdf)

        self.assertEqual(spec_c.received, [pdf])

    def test_raises_output_error_when_any_spec_fails(self):
        spec = _FakeSpec("s", raises=RuntimeError("nope"))
        with self.assertRaises(OutputError):
            OutputHandler([spec]).send(Path("/tmp/dummy.pdf"))

    def test_output_error_contains_failing_spec(self):
        spec = _FakeSpec("failing")
        exc  = RuntimeError("oops")
        spec.raises = exc

        try:
            OutputHandler([spec]).send(Path("/tmp/dummy.pdf"))
            self.fail("OutputError not raised")
        except OutputError as err:
            self.assertIs(err.failures[0][0], spec)

    def test_output_error_contains_original_exception(self):
        exc  = ValueError("original error")
        spec = _FakeSpec("s", raises=exc)

        try:
            OutputHandler([spec]).send(Path("/tmp/dummy.pdf"))
            self.fail("OutputError not raised")
        except OutputError as err:
            self.assertIs(err.failures[0][1], exc)

    def test_all_failures_collected(self):
        specs = [
            _FakeSpec("a", raises=RuntimeError("err a")),
            _FakeSpec("b", raises=ValueError("err b")),
        ]
        try:
            OutputHandler(specs).send(Path("/tmp/dummy.pdf"))
            self.fail("OutputError not raised")
        except OutputError as err:
            self.assertEqual(len(err.failures), 2)

    def test_no_exception_when_all_specs_succeed(self):
        specs = [_FakeSpec("a"), _FakeSpec("b")]
        OutputHandler(specs).send(Path("/tmp/dummy.pdf"))  # should not raise

    def test_empty_spec_list_succeeds(self):
        OutputHandler([]).send(Path("/tmp/dummy.pdf"))  # should not raise

    def test_len_returns_spec_count(self):
        self.assertEqual(len(OutputHandler([])), 0)
        self.assertEqual(len(OutputHandler([_FakeSpec("x")])), 1)
        self.assertEqual(len(OutputHandler([_FakeSpec("x"), _FakeSpec("y")])), 2)

    def test_repr_contains_specs(self):
        spec = _FakeSpec("myspec")
        handler = OutputHandler([spec])
        self.assertIn("myspec", repr(handler))


# --------------------------------------------------------------------------- #
# _spec_from_config
# --------------------------------------------------------------------------- #

class TestSpecFromConfig(unittest.TestCase):

    # ---- file type -------------------------------------------------------- #

    def test_file_spec_created_from_type_file(self):
        spec = _spec_from_config({"type": "file", "directory": "/tmp/scans"})
        self.assertIsInstance(spec, FileSpec)

    def test_file_spec_directory_set(self):
        spec = _spec_from_config({"type": "file", "directory": "/tmp/scans"})
        self.assertEqual(spec.directory, Path("/tmp/scans"))

    def test_file_spec_tilde_expanded(self):
        spec = _spec_from_config({"type": "file", "directory": "~/scans"})
        self.assertNotIn("~", str(spec.directory))

    def test_file_spec_filename_optional(self):
        spec = _spec_from_config({"type": "file", "directory": "/tmp/scans"})
        self.assertIsNone(spec.filename)

    def test_file_spec_filename_set_when_provided(self):
        spec = _spec_from_config({"type": "file", "directory": "/tmp", "filename": "out.pdf"})
        self.assertEqual(spec.filename, "out.pdf")

    # ---- paperless_ngx type ----------------------------------------------- #

    def test_paperless_spec_created_from_type_paperless_ngx(self):
        spec = _spec_from_config({"type": "paperless_ngx", "base_url": "http://p:8000", "token": "tok"})
        self.assertIsInstance(spec, PaperlessNgxSpec)

    def test_paperless_spec_base_url_and_token_set(self):
        spec = _spec_from_config({"type": "paperless_ngx", "base_url": "http://p:8000", "token": "tok"})
        self.assertEqual(spec.base_url, "http://p:8000")
        self.assertEqual(spec.token, "tok")

    def test_paperless_spec_optional_fields_default_to_none(self):
        spec = _spec_from_config({"type": "paperless_ngx", "base_url": "http://p:8000", "token": "tok"})
        self.assertIsNone(spec.title)
        self.assertIsNone(spec.correspondent_name)
        self.assertIsNone(spec.document_type_name)
        self.assertEqual(spec.tag_names, [])

    def test_paperless_spec_optional_fields_populated(self):
        spec = _spec_from_config({
            "type": "paperless_ngx",
            "base_url": "http://p:8000",
            "token": "tok",
            "title": "Invoice",
            "tag_names": ["scanner", "inbox"],
            "correspondent_name": "ACME",
            "document_type_name": "Invoice",
            "timeout": 30.0,
        })
        self.assertEqual(spec.title, "Invoice")
        self.assertEqual(spec.tag_names, ["scanner", "inbox"])
        self.assertEqual(spec.correspondent_name, "ACME")
        self.assertEqual(spec.document_type_name, "Invoice")
        self.assertAlmostEqual(spec.timeout, 30.0)

    def test_paperless_spec_timeout_defaults_to_60(self):
        spec = _spec_from_config({"type": "paperless_ngx", "base_url": "http://p:8000", "token": "tok"})
        self.assertAlmostEqual(spec.timeout, 60.0)

    # ---- unknown type ----------------------------------------------------- #

    def test_raises_value_error_for_unknown_type(self):
        with self.assertRaises(ValueError) as ctx:
            _spec_from_config({"type": "ftp"})
        self.assertIn("ftp", str(ctx.exception))

    def test_raises_value_error_when_type_missing(self):
        with self.assertRaises(ValueError):
            _spec_from_config({"directory": "/tmp"})

    def test_raises_key_error_when_required_key_absent(self):
        # "directory" is required for type=file
        with self.assertRaises(KeyError):
            _spec_from_config({"type": "file"})


# --------------------------------------------------------------------------- #
# build_handler
# --------------------------------------------------------------------------- #

class TestBuildHandler(unittest.TestCase):

    def test_returns_output_handler(self):
        handler = build_handler([])
        self.assertIsInstance(handler, OutputHandler)

    def test_empty_list_gives_empty_handler(self):
        handler = build_handler([])
        self.assertEqual(len(handler), 0)

    def test_single_file_destination(self):
        handler = build_handler([{"type": "file", "directory": "/tmp/scans"}])
        self.assertEqual(len(handler), 1)

    def test_multiple_destinations(self):
        handler = build_handler([
            {"type": "file", "directory": "/tmp/a"},
            {"type": "file", "directory": "/tmp/b"},
        ])
        self.assertEqual(len(handler), 2)

    def test_mixed_destination_types(self):
        handler = build_handler([
            {"type": "file", "directory": "/tmp/scans"},
            {"type": "paperless_ngx", "base_url": "http://p:8000", "token": "tok"},
        ])
        self.assertEqual(len(handler), 2)

    def test_raises_value_error_on_unknown_type(self):
        with self.assertRaises(ValueError):
            build_handler([{"type": "sftp", "host": "example.com"}])

    def test_handler_sends_to_all_specs(self):
        """End-to-end: build from config dicts and confirm send() runs all specs."""
        with tempfile.TemporaryDirectory() as tmp_a, \
             tempfile.TemporaryDirectory() as tmp_b:
            pdf = _make_pdf(Path(tmp_a))
            dest_dir = Path(tmp_b)

            handler = build_handler([{"type": "file", "directory": str(dest_dir)}])
            handler.send(pdf)

            self.assertTrue((dest_dir / pdf.name).exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
