"""Minimal HTTP client for a self-hosted MinerU API.

We talk to the official ``mineru-api`` FastAPI service (see
``opendatalab/MinerU`` → ``mineru/cli/fast_api.py``) over HTTP and never import
its code, so its AGPL license does not propagate to Carrel.

The synchronous ``POST /file_parse`` endpoint accepts a multipart upload and,
when ``response_format_zip=true``, streams back a ZIP containing
``<name>.md`` plus an ``images/`` directory. We use that ZIP form (rather than
the JSON form with base64-embedded images) because it keeps large image
payloads out of memory.
"""
from __future__ import annotations

import io
import logging
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

UPLOAD_NAME = "paper.pdf"  # name MinerU keys the result by (also the .md stem)


class MinerUError(Exception):
    """Raised when MinerU is unreachable or returns an error."""


@dataclass
class MinerUResult:
    md_path: Path
    images: list[Path] = field(default_factory=list)

    @property
    def markdown(self) -> str:
        return self.md_path.read_text(encoding="utf-8")


def is_healthy(
    base_url: str, *, timeout: float = 10.0, client: httpx.Client | None = None
) -> bool:
    own_client = client is None
    httpx_client = client or httpx.Client(timeout=timeout)
    try:
        r = httpx_client.get(f"{base_url.rstrip('/')}/health")
    except httpx.HTTPError:
        return False
    finally:
        if own_client:
            httpx_client.close()
    if r.status_code != 200:
        return False
    try:
        return r.json().get("status") == "healthy"
    except ValueError:
        return False


def parse_pdf(
    pdf_path: Path,
    dest_dir: Path,
    *,
    base_url: str = "http://127.0.0.1:8000",
    timeout: float = 900.0,
    backend: str = "pipeline",
    parse_method: str = "auto",
    lang_list: Iterable[str] = ("en",),
    formula_enable: bool = True,
    table_enable: bool = True,
    client: httpx.Client | None = None,
    on_progress=None,
    poll_interval: float = 3.0,
) -> MinerUResult:
    """Send ``pdf_path`` to MinerU and write markdown + images into ``dest_dir``.

    Uses MinerU's async task API (``POST /tasks`` → poll ``GET /tasks/{id}`` →
    ``GET /tasks/{id}/result``) so callers can report progress. ``on_progress``
    is called with ``(event, info_dict)``:
      - ``("submitted", {"task_id": ...})``
      - ``("status", {"status": "processing", "queued_ahead": 0})``
      - ``("fetching", {})``

    Returns a :class:`MinerUResult`. Raises :class:`MinerUError` on transport
    failure, a non-2xx response, or a result ZIP missing the markdown.
    """
    if not pdf_path.exists():
        raise MinerUError(f"PDF not found: {pdf_path}")

    own_client = client is None
    httpx_client = client or httpx.Client(timeout=timeout)
    try:
        task_id = _submit_task(
            httpx_client,
            base_url,
            pdf_path,
            backend=backend,
            parse_method=parse_method,
            lang_list=lang_list,
            formula_enable=formula_enable,
            table_enable=table_enable,
        )
        if on_progress is not None:
            on_progress("submitted", {"task_id": task_id})

        import time

        while True:
            status, info = _get_task(httpx_client, base_url, task_id)
            if on_progress is not None:
                on_progress("status", info)
            if status in ("completed", "failed"):
                break
            time.sleep(poll_interval)

        if status == "failed":
            err = info.get("error") or "MinerU task failed"
            raise MinerUError(f"MinerU task {task_id} failed: {err}")

        if on_progress is not None:
            on_progress("fetching", {})
        zip_bytes = _fetch_result(httpx_client, base_url, task_id)
        return _extract_result_zip(zip_bytes, dest_dir)
    finally:
        if own_client:
            httpx_client.close()


def _form_data(
    backend: str,
    parse_method: str,
    lang_list: Iterable[str],
    formula_enable: bool,
    table_enable: bool,
) -> dict[str, object]:
    return {
        "backend": backend,
        "parse_method": parse_method,
        "lang_list": list(lang_list),
        "formula_enable": str(formula_enable).lower(),
        "table_enable": str(table_enable).lower(),
        "return_md": "true",
        "return_images": "true",
        "response_format_zip": "true",
    }


def _submit_task(
    httpx_client: httpx.Client,
    base_url: str,
    pdf_path: Path,
    **opts,
) -> str:
    """POST /tasks; return the task id from the 202 response."""
    url = f"{base_url.rstrip('/')}/tasks"
    data = _form_data(
        opts["backend"], opts["parse_method"], opts["lang_list"],
        opts["formula_enable"], opts["table_enable"],
    )
    with pdf_path.open("rb") as fh:
        files = {"files": (UPLOAD_NAME, fh, "application/pdf")}
        try:
            resp = httpx_client.post(url, data=data, files=files)
        except httpx.HTTPError as e:
            raise MinerUError(f"MinerU request failed: {e}") from e

    if resp.status_code != 202:
        snippet = resp.text[:500] if resp.text else resp.reason_phrase
        raise MinerUError(f"MinerU submit returned HTTP {resp.status_code}: {snippet}")
    try:
        payload = resp.json()
    except ValueError as e:
        raise MinerUError("MinerU submit returned non-JSON response") from e
    task_id = payload.get("task_id")
    if not task_id:
        raise MinerUError(f"MinerU submit response had no task_id: {payload!r}")
    return str(task_id)


def _get_task(
    httpx_client: httpx.Client, base_url: str, task_id: str
) -> tuple[str, dict]:
    """GET /tasks/{id}; return (status, info_dict)."""
    url = f"{base_url.rstrip('/')}/tasks/{task_id}"
    try:
        resp = httpx_client.get(url)
    except httpx.HTTPError as e:
        raise MinerUError(f"MinerU status request failed: {e}") from e
    if resp.status_code != 200:
        raise MinerUError(f"MinerU status HTTP {resp.status_code}: {resp.text[:300]}")
    payload = resp.json()
    return str(payload.get("status", "unknown")), {
        "status": payload.get("status"),
        "queued_ahead": payload.get("queued_ahead"),
        "error": payload.get("error"),
    }


def _fetch_result(httpx_client: httpx.Client, base_url: str, task_id: str) -> bytes:
    """GET /tasks/{id}/result; return the ZIP bytes."""
    url = f"{base_url.rstrip('/')}/tasks/{task_id}/result"
    try:
        resp = httpx_client.get(url)
    except httpx.HTTPError as e:
        raise MinerUError(f"MinerU result request failed: {e}") from e
    if resp.status_code != 200:
        raise MinerUError(f"MinerU result HTTP {resp.status_code}: {resp.text[:500]}")
    ctype = resp.headers.get("content-type", "").lower()
    # Some MinerU builds send application/octet-stream for the zip; verify magic.
    if "zip" not in ctype and not resp.content.startswith(b"PK\x03\x04"):
        raise MinerUError(f"expected a ZIP result, got content-type={ctype!r}")
    return resp.content


def _extract_result_zip(zip_bytes: bytes, dest_dir: Path) -> MinerUResult:
    """Extract ``<name>.md`` and its ``images/`` out of a MinerU result ZIP.

    The archive layout is ``<pdf_name>/<parse_dir>/<pdf_name>.md`` and
    ``<pdf_name>/<parse_dir>/images/*`` (see MinerU ``build_zip_arcname``).
    We locate the single ``.md`` and flatten the parse_dir level so the result
    lands at ``dest_dir/paper.md`` / ``dest_dir/images/*`` — matching the
    relative ``images/...`` links MinerU writes into the markdown.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    images_dir = dest_dir / "images"
    images_dir.mkdir(exist_ok=True)

    md_written: Path | None = None
    images: list[Path] = []

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        # Guard against path traversal in archive member names.
        for member in zf.infolist():
            name = member.filename
            if member.is_dir():
                continue
            norm = Path(name)
            if norm.is_absolute() or ".." in norm.parts:
                logger.warning("skipping unsafe zip member: %s", name)
                continue

            lower = name.lower()
            if lower.endswith(".md") and md_written is None:
                # The markdown content is always named after the uploaded file
                # ("paper.md"). Write it there regardless of its archive prefix.
                md_written = dest_dir / f"{Path(UPLOAD_NAME).stem}.md"
                md_written.write_bytes(zf.read(member))
            elif "/images/" in lower.replace("\\", "/"):
                out = images_dir / Path(name).name
                out.write_bytes(zf.read(member))
                images.append(out)

    if md_written is None:
        raise MinerUError("MinerU result ZIP contained no markdown (.md) file")

    logger.info(
        "MinerU parsed -> %s (%d image(s))", md_written, len(images)
    )
    return MinerUResult(md_path=md_written, images=images)
