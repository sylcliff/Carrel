"""Tests for GET /papers/{id}/sections."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from carrel.models import Paper, PaperStatus


def _now() -> datetime:
    return datetime.now(UTC)


def _seed_paper(session, **kw) -> Paper:
    base = dict(
        id="W300",
        id_kind="openalex",
        title="Sectioned Paper",
        status=PaperStatus.parsed.value,
        oa_status="oa",
        source="openalex",
        md_path="papers/W300/paper.md",
        created_at=_now(),
        updated_at=_now(),
    )
    base.update(kw)
    p = Paper(**base)
    session.add(p)
    session.commit()
    return p


def _write_md(storage_root: Path, body: str) -> None:
    pdir = storage_root / "papers" / "W300"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "paper.md").write_text(body, encoding="utf-8")


def test_sections_returns_in_document_order(
    client, session, tmp_path, monkeypatch
):
    from carrel.main import app_config
    monkeypatch.setattr(app_config.storage, "root", str(tmp_path))
    _seed_paper(session)
    _write_md(
        tmp_path,
        (
            "Some preamble text.\n\n"
            "## Introduction\n\nIntro body.\n\n"
            "## Methods\n\nMethods body.\n\n"
            "## Conclusion\n\nConclusion body.\n"
        ),
    )

    r = client.get("/papers/W300/sections")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["id"] == "W300"
    assert [s["heading"] for s in data["sections"]] == [
        "",
        "Introduction",
        "Methods",
        "Conclusion",
    ]
    # Document order: preamble is first, then headings in source order.
    assert data["sections"][1]["body"].startswith("Intro body.")
    assert data["sections"][3]["body"].startswith("Conclusion body.")
    # Preamble is rendered with empty heading and an empty heading_path.
    assert data["sections"][0]["heading_path"] == ""


def test_sections_nested_heading_keeps_ancestor_path(
    client, session, tmp_path, monkeypatch
):
    from carrel.main import app_config
    monkeypatch.setattr(app_config.storage, "root", str(tmp_path))
    _seed_paper(session)
    _write_md(
        tmp_path,
        (
            "## Experiments\n\n"
            "Intro to experiments.\n\n"
            "### Setup\n\nSetup body.\n\n"
            "### Results\n\nResults body.\n"
        ),
    )

    r = client.get("/papers/W300/sections")
    data = r.json()
    paths = [s["heading_path"] for s in data["sections"]]
    assert "Experiments" in paths
    assert "Experiments / Setup" in paths
    assert "Experiments / Results" in paths
    # The leaf heading is just the trailing path segment.
    leaves = {s["heading_path"]: s["heading"] for s in data["sections"]}
    assert leaves["Experiments / Setup"] == "Setup"
    assert leaves["Experiments / Results"] == "Results"


def test_sections_404(client, monkeypatch):
    r = client.get("/papers/nope/sections")
    assert r.status_code == 404


def test_sections_409_when_no_markdown(client, session, tmp_path, monkeypatch):
    from carrel.main import app_config
    monkeypatch.setattr(app_config.storage, "root", str(tmp_path))
    _seed_paper(session, md_path=None)
    r = client.get("/papers/W300/sections")
    assert r.status_code == 409


def test_sections_304_on_etag_hit(client, session, tmp_path, monkeypatch):
    from carrel.main import app_config
    monkeypatch.setattr(app_config.storage, "root", str(tmp_path))
    _seed_paper(session)
    _write_md(tmp_path, "## A\n\nA body.\n")
    r1 = client.get("/papers/W300/sections")
    etag = r1.headers.get("etag")
    assert etag
    r2 = client.get("/papers/W300/sections", headers={"If-None-Match": etag})
    assert r2.status_code == 304


def test_sections_invalidated_on_parse(client, session, tmp_path, monkeypatch):
    from carrel.main import app_config
    from carrel.api._invalidation import invalidate_paper_mutated
    from carrel.api._app_cache import get_cache

    monkeypatch.setattr(app_config.storage, "root", str(tmp_path))
    _seed_paper(session)
    _write_md(tmp_path, "## A\n\nA body.\n")

    # Warm the L2 entry.
    r1 = client.get("/papers/W300/sections")
    assert r1.status_code == 200
    assert r1.json()["sections"]

    cache = get_cache()
    # The L2 key is `paper_sections:{...}` — built by `_stable_key` from
    # the route + key_params. We assert by prefix rather than reconstruct
    # the exact JSON-serialized params dict.
    has_sections_before = any(
        k.startswith("paper_sections:") for k in cache._data  # noqa: SLF001
    )
    assert has_sections_before, "expected sections entry in L2 cache"

    invalidate_paper_mutated("W300", mutate={"parse"})

    has_sections_after = any(
        k.startswith("paper_sections:") for k in cache._data  # noqa: SLF001
    )
    assert not has_sections_after, "sections entry should be dropped on parse"

    # And the next read rebuilds it from the file.
    r2 = client.get("/papers/W300/sections")
    assert r2.status_code == 200
    assert r2.json()["sections"]


def test_sections_empty_when_md_file_missing(
    client, session, tmp_path, monkeypatch
):
    from carrel.main import app_config
    monkeypatch.setattr(app_config.storage, "root", str(tmp_path))
    _seed_paper(session)  # md_path set but file not written
    r = client.get("/papers/W300/sections")
    assert r.status_code == 200
    assert r.json()["sections"] == []
