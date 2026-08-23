"""Tests for paper dedup API: scan, accept, reject, undo, snapshot shape."""
from __future__ import annotations

from datetime import UTC, date, datetime

from sqlmodel import Session, select

from carrel.models import Paper, PaperAlias
from carrel.pipeline import paper_dedup_ops as ops


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _paper(
    pid: str,
    *,
    title: str = "T",
    doi: str | None = None,
    arxiv_id: str | None = None,
    s2_paper_id: str | None = None,
    journal_doi: str | None = None,
    authors: list[dict] | None = None,
    abstract: str | None = None,
    venue: str | None = None,
    year: int = 2024,
    in_library: bool = True,
    status: str = "ready",
    id_kind: str = "openalex",
    favorite: bool = False,
    notes: str | None = None,
) -> Paper:
    return Paper(
        id=pid, id_kind=id_kind, title=title,
        doi=doi, arxiv_id=arxiv_id, s2_paper_id=s2_paper_id,
        journal_doi=journal_doi,
        publication_date=date(year, 1, 1),
        authors=authors or [],
        abstract=abstract, venue=venue,
        in_library=in_library, status=status, oa_status="oa", source="openalex",
        favorite=favorite, notes_markdown=notes,
        created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# GET /paper-dedup/suggestions — empty
# ---------------------------------------------------------------------------


def test_suggestions_empty(client, session: Session):
    resp = client.get("/paper-dedup/suggestions")
    assert resp.status_code == 200
    snap = resp.json()
    assert snap == {
        "suggestions": [],
        "applied": [],
        "rejected": [],
        "components": [],
    }


# ---------------------------------------------------------------------------
# POST /paper-dedup/run — auto-merge path (background=False for determinism)
# ---------------------------------------------------------------------------


def test_run_auto_merges_strong_doi_pair(client, session: Session):
    session.add(_paper("W1", doi="10.1234/abc", title="Canonical Title"))
    session.add(_paper("W2", doi="10.1234/abc", title="Same Canonical Title"))
    session.commit()

    resp = client.post(
        "/paper-dedup/run", json={"auto_apply": True, "background": False}
    )
    assert resp.status_code == 200, resp.text
    job = resp.json()
    assert job["status"] == "done"
    assert job["stats"]["auto_merged"] == 1
    assert job["stats"]["suggested"] == 0

    aliases = session.exec(select(PaperAlias)).all()
    assert len(aliases) == 1
    assert aliases[0].source == "auto"


def test_run_emits_suggestion_for_soft_match(client, session: Session):
    """Two papers with borderline title overlap but no shared id land in the
    panel as a suggestion, not an auto-merge."""
    session.add(_paper(
        "W1", title="Alpha Studies on Transformers v1",
        authors=[{"name": "A One", "openalex_author_id": "A1"}], year=2024,
    ))
    session.add(_paper(
        "W2", title="Alpha Studies on Transformers v2",
        authors=[{"name": "B Two", "openalex_author_id": "B2"}], year=2024,
    ))
    session.commit()

    resp = client.post(
        "/paper-dedup/run", json={"auto_apply": True, "background": False}
    )
    assert resp.status_code == 200, resp.text
    job = resp.json()
    assert job["stats"]["auto_merged"] == 0
    assert job["stats"]["suggested"] >= 1

    # GET /suggestions now serves the cached snapshot.
    snap = client.get("/paper-dedup/suggestions").json()
    assert any(
        {s["a"], s["b"]} == {"W1", "W2"} for s in snap["suggestions"]
    )
    # The suggestion carries the score breakdown fields the frontend needs.
    s = next(s for s in snap["suggestions"] if {s["a"], s["b"]} == {"W1", "W2"})
    assert "title" in s
    assert "authors" in s
    assert "strong_anchors" in s
    assert "reasons" in s
    assert s["title_a"] == "Alpha Studies on Transformers v1"
    assert s["title_b"] == "Alpha Studies on Transformers v2"


# ---------------------------------------------------------------------------
# POST /paper-dedup/merge
# ---------------------------------------------------------------------------


def test_merge_pair_migrates_user_state_and_writes_alias(client, session: Session):
    session.add(_paper("W1", title="Canonical"))
    session.add(_paper(
        "W2", title="Alias", favorite=True, notes="from W2",
    ))
    session.commit()

    resp = client.post(
        "/paper-dedup/merge",
        json={"alias_paper_id": "W2", "canonical_paper_id": "W1",
              "display_label": "manual merge"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "user"
    assert body["alias_paper_id"] == "W2"
    assert body["canonical_paper_id"] == "W1"
    assert body["display_label"] == "manual merge"

    # W2 is the loser; status becomes "merged", user_state migrated to W1.
    loser = session.get(Paper, "W2")
    assert loser.status == "merged"
    assert loser.favorite is False
    assert loser.notes_markdown is None
    winner = session.get(Paper, "W1")
    assert winner.favorite is True
    assert winner.notes_markdown == "from W2"

    # resolve_paper_id now routes W2 -> W1.
    assert ops.resolve_paper_id(session, "W2") == "W1"


def test_merge_rejects_self_merge(client, session: Session):
    session.add(_paper("W1"))
    session.commit()
    resp = client.post(
        "/paper-dedup/merge",
        json={"alias_paper_id": "W1", "canonical_paper_id": "W1"},
    )
    assert resp.status_code == 422


def test_merge_rejects_missing_paper(client, session: Session):
    session.add(_paper("W1"))
    session.commit()
    resp = client.post(
        "/paper-dedup/merge",
        json={"alias_paper_id": "DOES_NOT_EXIST", "canonical_paper_id": "W1"},
    )
    assert resp.status_code == 422


def test_merge_appears_in_snapshot_applied(client, session: Session):
    session.add(_paper("W1"))
    session.add(_paper("W2"))
    session.commit()
    client.post(
        "/paper-dedup/merge",
        json={"alias_paper_id": "W2", "canonical_paper_id": "W1"},
    )
    snap = client.get("/paper-dedup/suggestions").json()
    assert any(
        a["alias_paper_id"] == "W2" and a["canonical_paper_id"] == "W1"
        for a in snap["applied"]
    )


# ---------------------------------------------------------------------------
# POST /paper-dedup/reject
# ---------------------------------------------------------------------------


def test_reject_pair_writes_reject_alias_and_blocks_future_merge(
    client, session: Session
):
    session.add(_paper("W1", doi="10.1234/abc"))
    session.add(_paper("W2", doi="10.1234/abc"))
    session.commit()

    # User rejects the pair.
    resp = client.post(
        "/paper-dedup/reject",
        json={"a": "W1", "b": "W2", "display_label": "different papers"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["source"] == "reject"

    # Now run a scan — the pair is suppressed.
    job = client.post(
        "/paper-dedup/run", json={"auto_apply": True, "background": False}
    ).json()
    assert job["stats"]["auto_merged"] == 0
    assert job["stats"]["skipped_rejected"] >= 1

    aliases = session.exec(select(PaperAlias)).all()
    assert len(aliases) == 1
    assert aliases[0].source == "reject"


def test_reject_pair_rejects_self(client, session: Session):
    session.add(_paper("W1"))
    session.commit()
    resp = client.post(
        "/paper-dedup/reject", json={"a": "W1", "b": "W1"},
    )
    assert resp.status_code == 422


def test_reject_appears_in_snapshot_rejected(client, session: Session):
    session.add(_paper("W1"))
    session.add(_paper("W2"))
    session.commit()
    client.post("/paper-dedup/reject", json={"a": "W1", "b": "W2"})
    snap = client.get("/paper-dedup/suggestions").json()
    assert any(
        r["alias_paper_id"] in ("W1", "W2")
        and r["canonical_paper_id"] in ("W1", "W2")
        and r["source"] == "reject"
        for r in snap["rejected"]
    )


# ---------------------------------------------------------------------------
# DELETE /paper-dedup/aliases/{a}/{c}
# ---------------------------------------------------------------------------


def test_delete_alias_unflags_loser_and_returns_404_when_missing(
    client, session: Session
):
    session.add(_paper("W1", title="C"))
    session.add(_paper("W2", title="A", notes="to migrate"))
    session.commit()

    client.post(
        "/paper-dedup/merge",
        json={"alias_paper_id": "W2", "canonical_paper_id": "W1"},
    )
    assert ops.resolve_paper_id(session, "W2") == "W1"

    resp = client.delete("/paper-dedup/aliases/W2/W1")
    assert resp.status_code == 200
    assert resp.json() == {"deleted": True}

    # Loser is back to independent (status=ready), but the migrated notes
    # are NOT restored.
    loser = session.get(Paper, "W2")
    assert loser.status == "ready"
    assert loser.notes_markdown is None
    assert ops.resolve_paper_id(session, "W2") == "W2"

    # Deleting a non-existent alias returns 404.
    resp404 = client.delete("/paper-dedup/aliases/NOPE/NOPE")
    assert resp404.status_code == 404


# ---------------------------------------------------------------------------
# Components surface
# ---------------------------------------------------------------------------


def test_run_reports_components_in_snapshot(client, session: Session):
    session.add(_paper("W1", doi="10.1234/abc", title="Canon", in_library=True))
    session.add(_paper("W2", doi="10.1234/abc", title="Dup", in_library=True))
    session.add(_paper("W3", arxiv_id="2401.00005", journal_doi="10.1234/abc"))
    session.commit()

    client.post(
        "/paper-dedup/run", json={"auto_apply": True, "background": False}
    )

    snap = client.get("/paper-dedup/suggestions").json()
    assert snap["components"], "expected at least one component in snapshot"
    # W1 should be the canonical (openalex id wins over arxiv placeholder).
    canon = next(c for c in snap["components"] if "W1" in [c["canonical_id"], *c["alias_ids"]])
    assert canon["canonical_id"] == "W1"
    assert "doi" in canon["reasons"]


# ---------------------------------------------------------------------------
# In-process suggestion cache is invalidated on actions
# ---------------------------------------------------------------------------


def test_merge_invalidates_cached_suggestions(client, session: Session):
    # Start with a borderline pair so /suggestions has a cached entry.
    session.add(_paper(
        "W1", title="Alpha Studies on Transformers v1",
        authors=[{"name": "A", "openalex_author_id": "A1"}], year=2024,
    ))
    session.add(_paper(
        "W2", title="Alpha Studies on Transformers v2",
        authors=[{"name": "B", "openalex_author_id": "B2"}], year=2024,
    ))
    session.commit()
    client.post(
        "/paper-dedup/run", json={"auto_apply": True, "background": False}
    )
    assert client.get("/paper-dedup/suggestions").json()["suggestions"]

    # User accepts the suggestion; the cache should be invalidated.
    client.post(
        "/paper-dedup/merge",
        json={"alias_paper_id": "W2", "canonical_paper_id": "W1"},
    )
    snap = client.get("/paper-dedup/suggestions").json()
    # W2 is now merged away; the open pair is gone.
    assert all(
        {s["a"], s["b"]} != {"W1", "W2"} for s in snap["suggestions"]
    )
    assert any(
        a["alias_paper_id"] == "W2" and a["canonical_paper_id"] == "W1"
        for a in snap["applied"]
    )


# ---------------------------------------------------------------------------
# POST /paper-dedup/judge — on-demand LLM judge (M10.6)
# ---------------------------------------------------------------------------


def test_judge_route_short_circuits_on_strong_doi_anchor(client, session: Session):
    """Strong-anchor pairs should never reach the LLM; response is instant."""
    from unittest.mock import patch

    session.add(_paper("W1", doi="10.1234/abc", title="Canonical"))
    session.add(_paper("W2", doi="10.1234/abc", title="Canonical"))
    session.commit()

    with patch("carrel.pipeline.paper_dedup_judge.chat_json") as chat:
        resp = client.post("/paper-dedup/judge", json={"a": "W1", "b": "W2"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["verdict"] == "same"
    assert body["confidence"] == 1.0
    assert body["model"] == "deterministic"
    assert chat.call_count == 0, "strong-anchor pair must not hit chat_json"


def test_judge_route_caches_verdict_in_db(client, session: Session):
    """A borderline pair that calls the LLM should land a verdict in paper_dedup_verdicts."""
    from unittest.mock import patch
    from sqlmodel import select as _sel

    from carrel.config import load_settings
    from carrel.models import PaperDedupVerdict

    # Same title, different DOIs — no strong anchor, falls into the
    # composite's borderline path. With the LLM mocked to "same" the
    # response is "same" and the verdict row is persisted.
    session.add(_paper("W1", doi="10.1/a", title="Borderline"))
    session.add(_paper("W2", doi="10.1/b", title="Borderline"))
    session.commit()

    cfg, _ = load_settings()
    with patch(
        "carrel.pipeline.paper_dedup_judge.chat_json",
        return_value={"verdict": "same", "confidence": 0.9, "reasons": ["r"]},
    ):
        resp = client.post("/paper-dedup/judge", json={"a": "W1", "b": "W2"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["verdict"] == "same"
    assert body["cached"] is False
    # The cached response is recorded.
    rows = session.exec(_sel(PaperDedupVerdict)).all()
    assert len(rows) == 1
    a_key, b_key = sorted(("W1", "W2"))
    assert rows[0].paper_a_id == a_key
    assert rows[0].paper_b_id == b_key

    # A follow-up judge call with the same pair hits the cache.
    with patch("carrel.pipeline.paper_dedup_judge.chat_json") as chat2:
        resp2 = client.post("/paper-dedup/judge", json={"a": "W1", "b": "W2"})
    assert resp2.status_code == 200
    assert resp2.json()["cached"] is True
    assert chat2.call_count == 0


def test_judge_route_self_pair_rejected(client, session: Session):
    session.add(_paper("W1"))
    session.commit()
    resp = client.post("/paper-dedup/judge", json={"a": "W1", "b": "W1"})
    assert resp.status_code == 422


def test_judge_route_missing_paper_returns_404(client, session: Session):
    session.add(_paper("W1"))
    session.commit()
    resp = client.post("/paper-dedup/judge", json={"a": "W1", "b": "DOES_NOT_EXIST"})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /paper-dedup/run — passes a composite judge into run_dedup (M10.6)
# ---------------------------------------------------------------------------


def test_run_invokes_composite_judge_for_borderline_pair(client, session: Session):
    """The /run background task should build a composite judge and let it
    decide borderline pairs. A strong-DOI pair still auto-merges; this
    test focuses on whether the judge is wired in (no exception, the run
    completes)."""
    from unittest.mock import patch

    session.add(_paper("W1", doi="10.1234/abc", title="X"))
    session.add(_paper("W2", doi="10.1234/abc", title="X"))
    session.commit()

    with patch("carrel.pipeline.paper_dedup_judge.chat_json") as chat:
        resp = client.post(
            "/paper-dedup/run", json={"auto_apply": True, "background": False}
        )
    assert resp.status_code == 200, resp.text
    # Strong anchor — the composite should not call the LLM at all.
    assert chat.call_count == 0
    job = resp.json()
    assert job["status"] == "done"
    assert job["stats"]["auto_merged"] >= 1
