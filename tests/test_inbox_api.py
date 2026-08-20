"""Tests for the sync-discover → inbox → import flow."""
from __future__ import annotations

from datetime import UTC, datetime

from carrel.models import Paper
from carrel.pipeline.embed import select_pending_embed
from carrel.pipeline.process import select_pending


def _paper(pid: str, **over) -> Paper:
    base = dict(
        id=pid,
        id_kind="openalex",
        title=f"Paper {pid}",
        venue="Nature",
        status="pending",
        oa_status="oa",
        pdf_url="https://example.com/p.pdf",
        source="openalex",
        in_library=False,
        discarded=False,
        discovered_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        authors=[],
    )
    base.update(over)
    return Paper(**base)


# ---- list filtering ----

def test_list_default_returns_only_library(client, session):
    session.add(_paper("W_inbox"))
    session.add(_paper("W_lib", in_library=True))
    session.commit()

    listing = client.get("/papers").json()
    ids = {p["id"] for p in listing}
    assert ids == {"W_lib"}


def test_list_in_library_false_returns_inbox_excluding_discarded(client, session):
    session.add(_paper("W_inbox"))
    session.add(_paper("W_discarded", discarded=True))
    session.add(_paper("W_lib", in_library=True))
    session.commit()

    listing = client.get("/papers?in_library=false").json()
    ids = {p["id"] for p in listing}
    assert ids == {"W_inbox"}


# ---- import / discard ----

def test_import_moves_inbox_paper_to_library(client, session):
    session.add(_paper("W1"))
    session.commit()

    r = client.post("/papers/W1/import")
    assert r.status_code == 200, r.text
    assert r.json()["imported"] is True

    session.refresh(session.get(Paper, "W1"))
    p = session.get(Paper, "W1")
    assert p.in_library is True
    assert p.discarded is False
    # Still pending — import is metadata-only, no auto-download.
    assert p.status == "pending"

    # It now appears in the default library listing.
    assert {x["id"] for x in client.get("/papers").json()} == {"W1"}
    # And no longer in the inbox.
    assert client.get("/papers?in_library=false").json() == []


def test_import_un_discards(client, session):
    session.add(_paper("W1", discarded=True))
    session.commit()
    client.post("/papers/W1/import")
    p = session.get(Paper, "W1")
    assert p.in_library is True
    assert p.discarded is False


def test_discard_hides_inbox_paper(client, session):
    session.add(_paper("W1"))
    session.commit()

    r = client.post("/papers/W1/discard")
    assert r.status_code == 200, r.text
    assert r.json()["discarded"] is True
    assert client.get("/papers?in_library=false").json() == []


def test_discard_rejects_library_paper(client, session):
    session.add(_paper("W1", in_library=True))
    session.commit()
    r = client.post("/papers/W1/discard")
    assert r.status_code == 409


# ---- pipelines ignore the inbox ----

def test_select_pending_excludes_inbox(client, session):
    session.add(_paper("W_inbox", pdf_url="https://x/p.pdf"))
    session.add(_paper(
        "W_lib", in_library=True, pdf_url="https://x/p.pdf",
        md_path="papers/W_lib/paper.md", status="parsed",
    ))
    session.commit()
    pending_ids = {p.id for p in select_pending(session, limit=10)}
    assert "W_inbox" not in pending_ids


def test_select_pending_embed_excludes_inbox(client, session):
    session.add(_paper("W_inbox", md_path="papers/W_inbox/paper.md", status="parsed"))
    session.add(_paper(
        "W_lib", in_library=True, md_path="papers/W_lib/paper.md", status="parsed",
    ))
    session.commit()
    ids = {p.id for p in select_pending_embed(session, limit=10)}
    assert ids == {"W_lib"}
