"""Institutional SSH jump-host paper downloader.

When a paper has no open-access PDF (HTTP candidates fail), Carrel can fall
back to a generic SSH jump host that sits on an institutional/campus network
and runs a paper-download CLI such as ``scansci-pdf``. That CLI can get past
JS/Cloudflare challenges and paywalls that ``httpx`` cannot.

Design constraints (per user request):
  * The jump host is *generic* — no host/user/path is hardcoded. Everything
    comes from :class:`EnvSettings` (which reads ``.env``). No secrets in code.
  * This is a *fallback only*: callers try open-access HTTP first and only use
    this when that fails or there is no PDF URL at all.
  * The remote command is a user-supplied template; we only substitute
    whitelist-validated ``{id}`` plus ``{work_dir}``/``{timeout}``.

The remote CLI is expected to print a line ``OK: <remote-path>.pdf`` on success
(matching the scansci-pdf contract). We then SFTP the file back and validate
the ``%PDF-`` magic bytes. Paramiko is an optional dependency; if it is not
importable or the feature is not configured, everything degrades silently.
"""
from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from carrel.sources.pdf_download import looks_like_pdf

if TYPE_CHECKING:
    from carrel.config import EnvSettings

logger = logging.getLogger(__name__)

try:  # optional dependency
    import paramiko  # type: ignore
except ImportError:  # pragma: no cover - exercised only when paramiko absent
    paramiko = None  # type: ignore[assignment]

# Default remote command. Real deployments override via REMOTE_COMMAND_TEMPLATE
# (typically wrapping `conda activate`). Placeholders: {id} {work_dir} {timeout}.
DEFAULT_COMMAND_TEMPLATE = (
    "mkdir -p '{work_dir}'; timeout {timeout} scansci-pdf get "
    "'{id}' --output '{work_dir}' --strategy legal_only"
)

# The CLI prints this on success.
OK_RE = re.compile(r"OK:\s*(\S+\.pdf)", re.IGNORECASE)
# Fatal identifier errors from the remote CLI — do not waste retries on them.
PERMANENT_RE = re.compile(r"DOI not found|Invalid DOI|not a valid (DOI|arXiv)", re.IGNORECASE)
# Identifiers (DOIs / arXiv IDs) may only contain these characters. This is a
# shell-injection guard; the value is interpolated into a single-quoted command
# and we additionally reject single quotes / shell metacharacters.
_ID_RE = re.compile(r"^[A-Za-z0-9._:;()/-]+$")
_DOI_PREFIX_RE = re.compile(r"^https?://(?:dx\.)?doi\.org/", re.IGNORECASE)
# 10.48550/arXiv.<id> — the DOI registrar form arXiv uses for its own DOIs.
_ARXIV_DOI_RE = re.compile(r"^10\.48550/arxiv\.(.+)$", re.IGNORECASE)
# Trailing junk that clings to pasted DOIs.
_TRAILING_JUNK = " \t\r\n\"'`,;)]}>"


class RemoteError(Exception):
    """Base class for institutional-download failures."""


class RemoteNotConfigured(RemoteError):
    """The SSH jump host is not enabled or not fully configured."""


class RemotePermanentError(RemoteError):
    """The identifier is bad or missing on the remote; retries won't help."""


class RemoteTransientError(RemoteError):
    """SSH/network/timeout failure, or the CLI could not produce a PDF now."""


# ---------------------------------------------------------------------------
# configuration helpers
# ---------------------------------------------------------------------------


def is_configured(env: "EnvSettings | None" = None) -> bool:
    """True if Paramiko is importable and every required setting is present."""
    if paramiko is None:
        return False
    if env is None:
        from carrel.config import EnvSettings
        env = EnvSettings()
    return bool(
        env.remote_ssh_enabled
        and env.remote_ssh_host
        and env.remote_ssh_user
        and env.remote_ssh_key_path
        and env.remote_work_dir
        and env.remote_command_template
    )


def _settings(env: "EnvSettings | None") -> "EnvSettings":
    if env is not None:
        return env
    from carrel.config import EnvSettings
    return EnvSettings()


# ---------------------------------------------------------------------------
# identifier helpers
# ---------------------------------------------------------------------------


def normalize_doi(doi: str | None) -> str | None:
    """Strip a ``https://doi.org/`` prefix and surrounding whitespace/junk."""
    if not doi:
        return None
    value = doi.strip()
    value = _DOI_PREFIX_RE.sub("", value)
    value = value.strip(_TRAILING_JUNK)
    return value or None


def is_arxiv_doi(doi: str | None) -> bool:
    """True if ``doi`` is arXiv's own DOI form (10.48550/arXiv.<id>)."""
    return bool(doi and _ARXIV_DOI_RE.match(doi.strip()))


def arxiv_doi(arxiv_id: str) -> str:
    """Synthesize the registrar DOI for an arXiv id (10.48550/arXiv.<id>)."""
    return f"10.48550/arXiv.{arxiv_id}"


def _validate_identifier(identifier: str) -> str:
    ident = identifier.strip()
    if not ident or not _ID_RE.match(ident):
        raise RemotePermanentError(f"unsafe or malformed identifier: {identifier!r}")
    return ident


# ---------------------------------------------------------------------------
# SSH
# ---------------------------------------------------------------------------


def _connect(env: "EnvSettings") -> Any:
    """Open an SSHClient using the configured key. Caller must close it."""
    assert paramiko is not None
    key_path = env.remote_ssh_key_path
    if not key_path or not Path(key_path).expanduser().exists():
        raise RemoteNotConfigured(f"SSH key not found: {key_path!r}")

    key: Any = None
    last_err: Exception | None = None
    for key_cls in (paramiko.Ed25519Key, paramiko.RSAKey):
        try:
            key = key_cls.from_private_key_file(str(Path(key_path).expanduser()))
            break
        except paramiko.SSHException as exc:  # key type mismatch / bad format
            last_err = exc
    if key is None:
        raise RemoteNotConfigured(
            f"could not load SSH key {key_path!r} as Ed25519 or RSA: {last_err}"
        )

    client = paramiko.SSHClient()
    if env.remote_ssh_known_hosts_path and Path(
        env.remote_ssh_known_hosts_path
    ).expanduser().exists():
        client.load_system_host_keys(
            str(Path(env.remote_ssh_known_hosts_path).expanduser())
        )
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
    else:
        logger.warning(
            "remote SSH known_hosts not configured; accepting unknown host key "
            "for %s (set REMOTE_SSH_KNOWN_HOSTS_PATH to verify)",
            env.remote_ssh_host,
        )
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        client.connect(
            hostname=env.remote_ssh_host,
            port=env.remote_ssh_port,
            username=env.remote_ssh_user,
            pkey=key,
            timeout=env.remote_ssh_connect_timeout,
            allow_agent=False,
            look_for_keys=False,
        )
    except Exception as exc:  # paramiko.SSHException, socket.gaierror, OSError, ...
        client.close()
        raise RemoteTransientError(f"SSH connect failed: {exc}") from exc
    return client


def _run_remote(
    client: Any, command: str, *, exec_timeout: int
) -> tuple[int, str, str]:
    """Run a command, gather stdout/stderr, return (exit_status, out, err)."""
    _stdin, stdout, stderr = client.exec_command(command, timeout=exec_timeout)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    status = stdout.channel.recv_exit_status()
    return status, out, err


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def download_paper(
    identifier: str,
    dest_dir: str | Path,
    *,
    filename: str = "paper.pdf",
    env: "EnvSettings | None" = None,
) -> Path:
    """Download a paper via the SSH jump host into ``dest_dir/filename``.

    Returns the local :class:`Path`. Raises :class:`RemotePermanentError` for
    bad/missing identifiers (not retried), :class:`RemoteTransientError` for
    SSH/network failures or when the CLI cannot produce a PDF after retries.
    """
    settings = _settings(env)
    if not is_configured(settings):
        raise RemoteNotConfigured("institutional SSH download is not configured")

    ident = _validate_identifier(identifier)
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    final_path = dest / filename
    tmp_path = dest / f"{filename}.part"

    template = settings.remote_command_template or DEFAULT_COMMAND_TEMPLATE
    command = template.format(
        id=ident,
        work_dir=settings.remote_work_dir,
        timeout=settings.remote_dl_timeout,
    )
    exec_timeout = settings.remote_dl_timeout + 60  # channel timeout > remote timeout

    last_err: Exception | None = None
    remote_pdf: str | None = None
    log_tail = ""
    for attempt in range(1, settings.remote_retries + 1):
        client = None
        try:
            client = _connect(settings)
            _status, out, err = _run_remote(client, command, exec_timeout=exec_timeout)
            log_tail = (out + "\n" + err).strip()[-2000:]
            if PERMANENT_RE.search(out) or PERMANENT_RE.search(err):
                raise RemotePermanentError(
                    f"remote rejected identifier {ident!r}: {log_tail}"
                )
            match = OK_RE.search(out)
            if match:
                remote_pdf = match.group(1)
                break
            last_err = RemoteTransientError(
                f"no 'OK:' line for {ident}: {log_tail}"
            )
        except RemotePermanentError:
            raise
        except Exception as exc:
            if isinstance(exc, RemoteError):
                last_err = exc
            else:
                last_err = RemoteTransientError(f"SSH error: {exc}")
            logger.info(
                "remote download attempt %d/%d for %s failed: %s",
                attempt,
                settings.remote_retries,
                ident,
                last_err,
            )
        finally:
            if client is not None:
                client.close()
        if attempt < settings.remote_retries:
            time.sleep(min(2 ** (attempt - 1), 8))

    if not remote_pdf:
        if isinstance(last_err, RemotePermanentError):
            raise last_err
        raise RemoteTransientError(
            f"institutional download failed for {ident} after "
            f"{settings.remote_retries} attempts: {last_err}"
        )

    # SFTP the produced PDF back, via a temp file, then validate magic bytes.
    client = _connect(settings)
    try:
        sftp = client.open_sftp()
        try:
            sftp.get(remote_pdf, str(tmp_path))
        finally:
            sftp.close()
    except Exception as exc:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        client.close()
        raise RemoteTransientError(
            f"SFTP fetch failed for {remote_pdf!r}: {exc}"
        ) from exc
    finally:
        client.close()

    try:
        with tmp_path.open("rb") as f:
            if not looks_like_pdf(f):
                raise RemoteTransientError(
                    f"remote file {remote_pdf!r} is not a PDF (bad magic)"
                )
        os.replace(tmp_path, final_path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise

    logger.info("institutional download ok: %s -> %s", ident, final_path)
    return final_path


def test_connection(env: "EnvSettings | None" = None) -> dict[str, Any]:
    """Verify the SSH connection works. Used by health checks / troubleshooting.

    Never returns key material. Returns ``{"ok", "host", "user", "message"}``.
    """
    settings = _settings(env)
    if not is_configured(settings):
        return {"ok": False, "host": None, "user": None, "message": "not configured"}
    client = None
    try:
        client = _connect(settings)
        _status, out, _err = _run_remote(
            client, "echo carrel-ok", exec_timeout=settings.remote_ssh_connect_timeout
        )
        ok = "carrel-ok" in out
        return {
            "ok": ok,
            "host": settings.remote_ssh_host,
            "user": settings.remote_ssh_user,
            "message": "connected" if ok else "unexpected response",
        }
    except Exception as exc:
        return {
            "ok": False,
            "host": settings.remote_ssh_host,
            "user": settings.remote_ssh_user,
            "message": str(exc),
        }
    finally:
        if client is not None:
            client.close()
