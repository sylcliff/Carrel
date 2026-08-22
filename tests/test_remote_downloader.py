"""Tests for the generic institutional SSH jump-host downloader.

The SSH/SFTP layer is faked; no real network or key is used.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from carrel.config import EnvSettings
from carrel.sources import remote_downloader as rd


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class _FakeChannel:
    def __init__(self, status: int = 0) -> None:
        self._status = status

    def recv_exit_status(self) -> int:
        return self._status


class _FakeStream:
    def __init__(self, text: str = "") -> None:
        self._text = text

    def read(self) -> bytes:
        return self._text.encode("utf-8")


class _FakeSFTP:
    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.files = files or {}
        self.gets: list[tuple[str, str]] = []
        self.closed = False

    def get(self, remote: str, local: str) -> None:
        self.gets.append((remote, local))
        Path(local).parent.mkdir(parents=True, exist_ok=True)
        Path(local).write_bytes(self.files.get(remote, b"%PDF-1.7\nfake"))

    def close(self) -> None:
        self.closed = True


class _FakeClient:
    """Stand-in for paramiko.SSHClient."""

    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        status: int = 0,
        sftp: _FakeSFTP | None = None,
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self._status = status
        self._sftp = sftp or _FakeSFTP()
        self.commands: list[str] = []
        self.closed = False

    def exec_command(self, command: str, timeout: int | None = None):  # noqa: ARG002
        self.commands.append(command)
        return (
            _FakeStream(""),
            _FakeStream(self._stdout),
            _FakeStream(self._stderr),
            _FakeChannel(self._status),
        )

    # The module unpacks (stdin, stdout, stderr) and calls stdout.channel, so
    # attach a channel to the stdout stream.
    def open_sftp(self) -> _FakeSFTP:
        return self._sftp

    def close(self) -> None:
        self.closed = True


# Patch the stdout stream to carry a .channel, like paramiko does.
class _StdoutWithChannel(_FakeStream):
    def __init__(self, text: str, status: int) -> None:
        super().__init__(text)
        self.channel = _FakeChannel(status)


class _FakeClientWithChannel(_FakeClient):
    def exec_command(self, command: str, timeout: int | None = None):  # noqa: ARG002
        self.commands.append(command)
        return (
            _FakeStream(""),
            _StdoutWithChannel(self._stdout, self._status),
            _FakeStream(self._stderr),
        )


@pytest.fixture()
def configured_env(tmp_path, monkeypatch) -> EnvSettings:
    """An EnvSettings that reports the remote as fully configured.

    We also stub the key-loading in ``_connect`` so no real key file is needed.
    """
    key = tmp_path / "id_ed25519"
    key.write_bytes(b"fake-key-bytes")
    env = EnvSettings(
        remote_ssh_enabled=True,
        remote_ssh_host="jump.example.edu",
        remote_ssh_port=22,
        remote_ssh_user="carrel",
        remote_ssh_key_path=str(key),
        remote_ssh_known_hosts_path=None,
        remote_ssh_connect_timeout=5,
        remote_work_dir="/tmp/carrel-remote",
        remote_command_template="mkdir -p '{work_dir}'; get '{id}'",
        remote_dl_timeout=30,
        remote_retries=3,
    )
    return env


def _patch_connect(monkeypatch, client: _FakeClient) -> None:
    """Replace _connect so it returns ``client`` (a fresh one per attempt)."""
    clients = [client]
    # Each call to _connect returns the client; for multi-attempt tests the
    # caller can pass a list via the clients closure instead.
    def _fake(_env):
        return clients.pop(0) if len(clients) > 1 else client

    # Simpler: always return the same client.
    monkeypatch.setattr(rd, "_connect", lambda _env: client)


# ---------------------------------------------------------------------------
# identifier helpers
# ---------------------------------------------------------------------------


def test_normalize_doi_strips_prefix_and_junk():
    assert rd.normalize_doi("https://doi.org/10.1021/acs.jctc.6c01122") == "10.1021/acs.jctc.6c01122"
    assert rd.normalize_doi("http://dx.doi.org/10.1/X  ") == "10.1/X"
    assert rd.normalize_doi("  10.1/X,") == "10.1/X"
    assert rd.normalize_doi("") is None
    assert rd.normalize_doi(None) is None


def test_arxiv_doi_helpers():
    assert rd.arxiv_doi("2301.01234") == "10.48550/arXiv.2301.01234"
    assert rd.is_arxiv_doi("10.48550/arXiv.2301.01234") is True
    assert rd.is_arxiv_doi("10.48550/arxiv.2301.01234v2") is True
    assert rd.is_arxiv_doi("10.1021/acs.jctc.6c01122") is False
    assert rd.is_arxiv_doi(None) is False


def test_validate_identifier_rejects_shell_metacharacters():
    with pytest.raises(rd.RemotePermanentError):
        rd._validate_identifier("10.1/x; rm -rf /")
    with pytest.raises(rd.RemotePermanentError):
        rd._validate_identifier("$(whoami)")
    with pytest.raises(rd.RemotePermanentError):
        rd._validate_identifier("")
    # Valid DOI / arXiv forms pass.
    assert rd._validate_identifier("10.1021/acs.jctc.6c01122") == "10.1021/acs.jctc.6c01122"
    assert rd._validate_identifier("2301.01234") == "2301.01234"


# ---------------------------------------------------------------------------
# is_configured
# ---------------------------------------------------------------------------


def test_is_configured_true_when_all_fields_present(configured_env):
    assert rd.is_configured(configured_env) is True


def test_is_configured_false_when_disabled(configured_env):
    configured_env.remote_ssh_enabled = False
    assert rd.is_configured(configured_env) is False


def test_is_configured_false_when_field_missing(configured_env):
    configured_env.remote_work_dir = None
    assert rd.is_configured(configured_env) is False


# ---------------------------------------------------------------------------
# download_paper
# ---------------------------------------------------------------------------


def test_download_paper_success(configured_env, tmp_path, monkeypatch):
    dest = tmp_path / "papers" / "W1"
    remote_pdf = "/tmp/carrel-remote/10.1021_acs.pdf"
    sftp = _FakeSFTP(files={remote_pdf: b"%PDF-1.7\n% real pdf bytes"})
    client = _FakeClientWithChannel(stdout=f"OK: {remote_pdf}\n", sftp=sftp)
    _patch_connect(monkeypatch, client)

    out = rd.download_paper(
        "10.1021/acs.jctc.6c01122", dest, filename="paper.pdf", env=configured_env
    )

    assert out == dest / "paper.pdf"
    assert out.read_bytes().startswith(b"%PDF-")
    # The identifier was interpolated into the command, single-quoted.
    assert any("'10.1021/acs.jctc.6c01122'" in c for c in client.commands)
    # SFTP fetched the remote path. No .part file left behind.
    assert sftp.gets and sftp.gets[0][0] == remote_pdf
    assert not (dest / "paper.pdf.part").exists()


def test_download_paper_permanent_error_not_retried(configured_env, tmp_path, monkeypatch):
    dest = tmp_path / "papers" / "W2"
    client = _FakeClientWithChannel(stdout="DOI not found\n", stderr="")
    connections = {"n": 0}

    def _fake_connect(_env):
        connections["n"] += 1
        return client

    monkeypatch.setattr(rd, "_connect", _fake_connect)
    # Avoid real sleep between (non-existent) retries.
    monkeypatch.setattr(rd.time, "sleep", lambda *_a, **_k: None)

    with pytest.raises(rd.RemotePermanentError):
        rd.download_paper("10.1/missing", dest, env=configured_env)

    # Tried exactly once — permanent errors short-circuit.
    assert connections["n"] == 1


def test_download_paper_transient_retried_then_fails(configured_env, tmp_path, monkeypatch):
    dest = tmp_path / "papers" / "W3"
    configured_env.remote_retries = 3
    client = _FakeClientWithChannel(stdout="", stderr="network blip")
    connections = {"n": 0}

    def _fake_connect(_env):
        connections["n"] += 1
        return client

    monkeypatch.setattr(rd, "_connect", _fake_connect)
    monkeypatch.setattr(rd.time, "sleep", lambda *_a, **_k: None)

    with pytest.raises(rd.RemoteTransientError):
        rd.download_paper("10.1/x", dest, env=configured_env)

    assert connections["n"] == 3  # retried the configured number of times


def test_download_paper_bad_magic_cleans_part(configured_env, tmp_path, monkeypatch):
    dest = tmp_path / "papers" / "W4"
    remote_pdf = "/tmp/carrel-remote/x.pdf"
    # SFTP returns HTML, not a PDF.
    sftp = _FakeSFTP(files={remote_pdf: b"<html>cloudflare</html>"})
    client = _FakeClientWithChannel(stdout=f"OK: {remote_pdf}\n", sftp=sftp)
    _patch_connect(monkeypatch, client)

    with pytest.raises(rd.RemoteTransientError):
        rd.download_paper("10.1/x", dest, env=configured_env)

    assert not (dest / "paper.pdf").exists()
    assert not (dest / "paper.pdf.part").exists()


def test_download_paper_not_configured_raises(tmp_path):
    env = EnvSettings(remote_ssh_enabled=False)
    with pytest.raises(rd.RemoteNotConfigured):
        rd.download_paper("10.1/x", tmp_path, env=env)


def test_download_paper_unsafe_identifier_raises_before_connect(
    configured_env, tmp_path, monkeypatch
):
    def _never(_env):  # pragma: no cover - must not be called
        raise AssertionError("connect must not be called for an unsafe identifier")

    monkeypatch.setattr(rd, "_connect", _never)
    # Single quote would break out of the single-quoted remote command; it must
    # be rejected before any SSH connection is attempted.
    with pytest.raises(rd.RemotePermanentError):
        rd.download_paper("10.1/x'y", tmp_path, env=configured_env)


# ---------------------------------------------------------------------------
# test_connection
# ---------------------------------------------------------------------------


def test_test_connection_not_configured():
    env = EnvSettings(remote_ssh_enabled=False)
    res = rd.test_connection(env)
    assert res["ok"] is False
    assert res["message"] == "not configured"


def test_test_connection_ok(configured_env, monkeypatch):
    client = _FakeClientWithChannel(stdout="carrel-ok\n")
    monkeypatch.setattr(rd, "_connect", lambda _env: client)
    res = rd.test_connection(configured_env)
    assert res["ok"] is True
    assert res["host"] == "jump.example.edu"
    assert res["user"] == "carrel"
    assert "echo carrel-ok" in client.commands[0]
