"""
output.py — Pluggable output destinations for scanned PDFs.

Typical use:

    from scanner_pi.output import FileSpec, PaperlessNgxSpec, OutputHandler

    handler = OutputHandler([
        FileSpec(directory=Path("~/scans").expanduser()),
        PaperlessNgxSpec(
            base_url="http://paperless:8000",
            token="abc123",
            tag_names=["scanner"],
            correspondent_name="Bank",
        ),
    ])

    handler.send(pdf_path)   # delivers to all destinations; raises OutputError on failure
"""

from __future__ import annotations

import logging
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

import requests

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Exception
# --------------------------------------------------------------------------- #

class OutputError(Exception):
    """
    Raised by OutputHandler.send() when one or more destinations fail.

    All destinations are always attempted; every failure is collected and
    reported together so the caller can see the full picture at once.
    """

    def __init__(self, failures: list[tuple[OutputDestinationSpec, Exception]]) -> None:
        self.failures = failures
        summary = "\n".join(f"  {spec!r}: {exc}" for spec, exc in failures)
        super().__init__(f"{len(failures)} output destination(s) failed:\n{summary}")


# --------------------------------------------------------------------------- #
# Abstract base
# --------------------------------------------------------------------------- #

class OutputDestinationSpec(ABC):
    """
    Abstract description of one place a scanned PDF should be delivered.

    Subclass this to add new destination types; the only requirement is an
    implementation of send().
    """

    @abstractmethod
    def send(self, pdf_path: Path) -> None:
        """Deliver the PDF at *pdf_path* to this destination."""


# --------------------------------------------------------------------------- #
# File destination
# --------------------------------------------------------------------------- #

@dataclass
class FileSpec(OutputDestinationSpec):
    """
    Copy the scanned PDF into a directory on the local filesystem.

    Parameters
    ----------
    directory:
        Destination directory.  Created (including any missing parents) if it
        does not already exist.
    filename:
        Output filename.  If omitted, the original filename of the PDF is kept.
    """

    directory: Path
    filename: str | None = None

    def send(self, pdf_path: Path) -> None:
        dest_dir = Path(self.directory)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / (self.filename or pdf_path.name)
        shutil.copy2(pdf_path, dest)
        log.info("FileSpec: saved to %s", dest)


# --------------------------------------------------------------------------- #
# Paperless-ngx destination
# --------------------------------------------------------------------------- #

@dataclass
class PaperlessNgxSpec(OutputDestinationSpec):
    """
    Upload the scanned PDF to a Paperless-ngx instance via the REST API.

    Tag, correspondent, and document-type names are resolved to their integer
    IDs by querying the API before uploading, so the config stays human-readable.

    Parameters
    ----------
    base_url:
        Root URL of the Paperless-ngx instance, e.g. ``http://192.168.1.10:8000``.
        Trailing slashes are tolerated.
    token:
        API authentication token.  Generate one at Admin → Auth Token in the
        Paperless-ngx web interface.  Not included in repr() output.
    title:
        Document title sent to Paperless-ngx.  Paperless will infer a title
        from the content if this is omitted.
    tag_names:
        Display names of tags to apply (case-insensitive lookup).  Raises
        ValueError if a name is not found.
    correspondent_name:
        Display name of the correspondent to assign (case-insensitive lookup).
    document_type_name:
        Display name of the document type to assign (case-insensitive lookup).
    timeout:
        HTTP request timeout in seconds (applied to each individual request).
    """

    base_url: str
    token: str = field(repr=False)  # kept out of repr to avoid accidental logging
    title: str | None = None
    tag_names: list[str] = field(default_factory=list)
    correspondent_name: str | None = None
    document_type_name: str | None = None
    timeout: float = 60.0

    # ---- private helpers -------------------------------------------------- #

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Token {self.token}"}

    def _lookup_id(self, resource: str, name: str) -> int:
        """
        Resolve a Paperless-ngx resource name to its integer primary key.

        Parameters
        ----------
        resource:
            Plural API resource name: ``"tags"``, ``"correspondents"``, or
            ``"document_types"``.
        name:
            Display name to look up (matched case-insensitively).

        Raises
        ------
        ValueError
            If no matching resource is found in Paperless-ngx.
        requests.HTTPError
            If the API request itself fails.
        """
        url = f"{self.base_url.rstrip('/')}/api/{resource}/"
        resp = requests.get(
            url,
            params={"name__iexact": name},
            headers=self._headers(),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if not results:
            raise ValueError(
                f"Paperless-ngx: no {resource!r} resource found with name {name!r}"
            )
        return int(results[0]["id"])

    # ---- public interface ------------------------------------------------- #

    def send(self, pdf_path: Path) -> None:
        """Upload *pdf_path* to Paperless-ngx."""
        url = f"{self.base_url.rstrip('/')}/api/documents/post_document/"

        # Resolve names → IDs before opening the file so we fail fast on
        # bad names without having already started the multipart upload.
        tag_ids = [self._lookup_id("tags", n) for n in self.tag_names]
        correspondent_id = (
            self._lookup_id("correspondents", self.correspondent_name)
            if self.correspondent_name else None
        )
        document_type_id = (
            self._lookup_id("document_types", self.document_type_name)
            if self.document_type_name else None
        )

        data: dict = {}
        if self.title is not None:
            data["title"] = self.title
        if correspondent_id is not None:
            data["correspondent"] = correspondent_id
        if document_type_id is not None:
            data["document_type"] = document_type_id
        if tag_ids:
            data["tags"] = tag_ids

        with pdf_path.open("rb") as fh:
            resp = requests.post(
                url,
                headers=self._headers(),
                data=data,
                files={"document": (pdf_path.name, fh, "application/pdf")},
                timeout=self.timeout,
            )
        resp.raise_for_status()

        # Paperless-ngx returns the celery task UUID as a bare quoted string.
        task_id = resp.text.strip().strip('"')
        log.info("PaperlessNgxSpec: %s queued as task %s", pdf_path.name, task_id)


# --------------------------------------------------------------------------- #
# Output handler
# --------------------------------------------------------------------------- #

class OutputHandler:
    """
    Deliver a scanned PDF to one or more output destinations.

    Every spec is attempted even if an earlier one fails so that, for example,
    a local file copy failing does not prevent the Paperless-ngx upload.  All
    failures are collected and re-raised together as an OutputError.
    """

    def __init__(self, specs: list[OutputDestinationSpec]) -> None:
        self._specs = list(specs)

    def send(self, pdf_path: Path) -> None:
        """
        Send *pdf_path* to every configured destination.

        Raises
        ------
        OutputError
            If one or more destinations raise an exception.  All destinations
            are always attempted regardless of earlier failures.
        """
        failures: list[tuple[OutputDestinationSpec, Exception]] = []
        for spec in self._specs:
            try:
                log.info("Sending %s → %r", pdf_path.name, spec)
                spec.send(pdf_path)
            except Exception as exc:
                log.error("Output to %r failed: %s", spec, exc)
                failures.append((spec, exc))

        if failures:
            raise OutputError(failures)

    @property
    def specs(self) -> list[OutputDestinationSpec]:
        """Return a read-only snapshot of the configured destination specs."""
        return list(self._specs)

    def __len__(self) -> int:
        return len(self._specs)

    def __repr__(self) -> str:
        return f"OutputHandler({self._specs!r})"


# --------------------------------------------------------------------------- #
# Config-driven construction
# --------------------------------------------------------------------------- #

def _spec_from_config(d: dict) -> OutputDestinationSpec:
    """
    Build one OutputDestinationSpec from a config-dict entry.

    The dict must contain a ``type`` key; the remaining keys depend on the
    destination type:

    ``type = "file"``
        ``directory`` (required), ``filename`` (optional)

    ``type = "paperless_ngx"``
        ``base_url``, ``token`` (required);
        ``title``, ``tag_names``, ``correspondent_name``,
        ``document_type_name``, ``timeout`` (all optional)

    Raises
    ------
    ValueError
        If ``type`` is missing or not recognised.
    KeyError
        If a required key for the chosen type is absent.
    """
    spec_type = d.get("type", "")

    if spec_type == "file":
        return FileSpec(
            directory=Path(d["directory"]).expanduser(),
            filename=d.get("filename"),
        )

    if spec_type == "paperless_ngx":
        return PaperlessNgxSpec(
            base_url=d["base_url"],
            token=d["token"],
            title=d.get("title"),
            tag_names=list(d.get("tag_names", [])),
            correspondent_name=d.get("correspondent_name"),
            document_type_name=d.get("document_type_name"),
            timeout=float(d.get("timeout", 60.0)),
        )

    raise ValueError(
        f"Unknown output destination type {spec_type!r}. "
        f"Expected 'file' or 'paperless_ngx'."
    )


def build_handler(destinations: list[dict]) -> OutputHandler:
    """
    Build an OutputHandler from a list of destination config dicts.

    Each dict must have a ``type`` key understood by :func:`_spec_from_config`.
    An empty list produces an OutputHandler with no specs (a no-op send).

    Typical use — pass the ``[[output.destinations]]`` or
    ``[[listener.destinations]]`` array from the loaded config::

        handler = build_handler(config.get("output", {}).get("destinations", []))
        handler.send(pdf_path)
    """
    return OutputHandler([_spec_from_config(d) for d in destinations])
