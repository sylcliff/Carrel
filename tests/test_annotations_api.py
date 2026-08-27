"""Tests for favorites, notes, and tags (user annotations)."""
from __future__ import annotations

from datetime import UTC, datetime

from carrel.models import Paper, PaperTag


def _paper(pid: str, **over) -> Paper:
    base = dict(
        id=pid,
        id_kind="openalex",
        title=f"Paper {pid}",
        venue="Nature",
        status="ready",
        oa_status="oa",
        source="openalex",
        in_library=True,
        discarded=False,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        authors=[{"name": "Jane Smith"}],
    )
    base.update(over)
    return Paper(**base)


# ---- favorites ----


def test_favorite_toggle_persists_and_bumps_updated_at(client, session):
    session.add(_paper("W1"))
    session.commit()
    before = session.get(Paper, "W1").updated_at

    r = client.post("/papers/W1/favorite", json={"favorite": True})
    assert r.status_code == 200, r.text
    assert r.json() == {"id": "W1", "favorite": True}

    p = session.get(Paper, "W1")
    assert p.favorite is True
    assert p.updated_at >= before

    r = client.post("/papers/W1/favorite", json={"favorite": False})
    assert r.status_code == 200
    session.expire_all()  # API session committed; drop the identity-map cache
    assert session.get(Paper, "W1").favorite is False


def test_list_filter_favorite(client, session):
    session.add(_paper("W1", favorite=True))
    session.add(_paper("W2"))
    session.commit()
    ids = {p["id"] for p in client.get("/papers?favorite=true").json()}
    assert ids == {"W1"}
    assert {p["id"] for p in client.get("/papers?favorite=false").json()} == {"W2"}


def test_summary_carries_favorite_flag(client, session):
    session.add(_paper("W1", favorite=True))
    session.commit()
    row = next(p for p in client.get("/papers").json() if p["id"] == "W1")
    assert row["favorite"] is True


# ---- notes ----


def test_put_notes_persists_and_bumps_updated_at(client, session):
    session.add(_paper("W1"))
    session.commit()
    before = session.get(Paper, "W1").updated_at

    r = client.put("/papers/W1/notes", json={"notes_markdown": "# Hello\nbody"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["notes_markdown"] == "# Hello\nbody"

    p = session.get(Paper, "W1")
    assert p.notes_markdown == "# Hello\nbody"
    assert p.updated_at >= before

    detail = client.get("/papers/W1").json()
    assert detail["notes_markdown"] == "# Hello\nbody"


def test_put_blank_notes_clears(client, session):
    session.add(_paper("W1", notes_markdown="keep me"))
    session.commit()
    r = client.put("/papers/W1/notes", json={"notes_markdown": "   "})
    assert r.status_code == 200
    assert r.json()["notes_markdown"] is None
    assert session.get(Paper, "W1").notes_markdown is None


# ---- tags ----


def test_add_tag_is_idempotent_and_case_insensitive(client, session):
    session.add(_paper("W1"))
    session.commit()

    r1 = client.post("/papers/W1/tags", json={"name": " NLP "})
    assert r1.status_code == 200, r1.text
    tag = r1.json()
    assert tag["name"] == "NLP"

    # Same spelling → idempotent, same id.
    r2 = client.post("/papers/W1/tags", json={"name": "NLP"})
    assert r2.json()["id"] == tag["id"]

    # Different case → reattaches existing tag, no second tag created.
    r3 = client.post("/papers/W1/tags", json={"name": "nlp"})
    assert r3.json()["id"] == tag["id"]

    tags = client.get("/papers/W1/tags").json()
    assert len(tags) == 1
    assert tags[0]["name"] == "NLP"

    # Paper detail carries the tag name.
    assert client.get("/papers/W1").json()["tags"] == ["NLP"]


def test_add_empty_tag_rejected(client, session):
    session.add(_paper("W1"))
    session.commit()
    r = client.post("/papers/W1/tags", json={"name": "   "})
    assert r.status_code == 422


def test_remove_paper_tag(client, session):
    session.add(_paper("W1"))
    session.commit()
    tag = client.post("/papers/W1/tags", json={"name": "todo"}).json()

    r = client.delete(f"/papers/W1/tags/{tag['id']}")
    assert r.status_code == 200, r.text
    assert r.json()["detached"] is True
    assert client.get("/papers/W1/tags").json() == []

    # Second delete is a 404.
    assert client.delete(f"/papers/W1/tags/{tag['id']}").status_code == 404


def test_list_tags_returns_counts(client, session):
    session.add(_paper("W1"))
    session.add(_paper("W2"))
    session.commit()
    client.post("/papers/W1/tags", json={"name": "nlp"})
    client.post("/papers/W2/tags", json={"name": "nlp"})
    client.post("/papers/W1/tags", json={"name": "todo"})

    tags = {t["name"]: t["paper_count"] for t in client.get("/tags").json()}
    assert tags == {"nlp": 2, "todo": 1}

    # Detaching the last paper still lists the tag with count 0 (outer join).
    tag_id = next(t["id"] for t in client.get("/tags").json() if t["name"] == "todo")
    client.delete(f"/papers/W1/tags/{tag_id}")
    tags = {t["name"]: t["paper_count"] for t in client.get("/tags").json()}
    assert tags["todo"] == 0


def test_delete_tag_cascades_detach(client, session):
    session.add(_paper("W1"))
    session.commit()
    tag = client.post("/papers/W1/tags", json={"name": "nlp"}).json()

    r = client.delete(f"/tags/{tag['id']}")
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] is True
    assert r.json()["detached"] == 1

    # Tag gone, paper remains, and no association rows linger.
    assert client.get("/tags").json() == []
    assert client.get("/papers/W1").status_code == 200
    assert session.get(PaperTag, ("W1", tag["id"])) is None


def test_list_filter_tag_any(client, session):
    session.add(_paper("W1"))
    session.add(_paper("W2"))
    session.add(_paper("W3"))
    session.commit()
    client.post("/papers/W1/tags", json={"name": "A"})
    client.post("/papers/W2/tags", json={"name": "B"})
    client.post("/papers/W3/tags", json={"name": "A"})
    client.post("/papers/W3/tags", json={"name": "B"})

    ids = {p["id"] for p in client.get("/papers?tag=A&tag=B").json()}
    assert ids == {"W1", "W2", "W3"}  # ANY (union)


def test_list_filter_q_title_author(client, session):
    session.add(_paper("W1", title="Smith on retrieval", authors=[{"name": "Other"}]))
    session.add(_paper("W2", title="Unrelated", authors=[{"name": "Jones"}]))
    session.commit()
    ids = {p["id"] for p in client.get("/papers?q=smith").json()}
    assert ids == {"W1"}


# ---- deletion cleanup ----


def test_delete_paper_removes_paper_tags(client, session):
    session.add(_paper("W1"))
    session.commit()
    client.post("/papers/W1/tags", json={"name": "nlp"})
    tag = client.get("/tags").json()[0]

    r = client.delete("/papers/W1")
    assert r.status_code == 200, r.text
    # The association row is gone (tag itself remains, now with count 0).
    assert session.get(PaperTag, ("W1", tag["id"])) is None
    tags = {t["name"]: t["paper_count"] for t in client.get("/tags").json()}
    assert tags["nlp"] == 0


def test_delete_paper_cascades_concept_question_chat_rows(client, session):
    """Regression: paper_concepts / paper_questions / chat_messages FK to
    papers.id without ON DELETE CASCADE. delete_paper must clean them up
    before the commit, otherwise the request 500s with IntegrityError.
    """
    from datetime import UTC, datetime
    from sqlmodel import select

    from carrel.models import ChatMessage, PaperConcept, PaperQuestion

    session.add(_paper("W1"))
    session.commit()

    # Seed child rows that previously blocked the delete.
    session.add(
        PaperConcept(
            paper_id="W1",
            term_normalized="rag",
            term_display="Retrieval-Augmented Generation",
        )
    )
    session.add(
        PaperQuestion(
            paper_id="W1",
            question_normalized="how does rag work",
            question_display="How does RAG work?",
        )
    )
    session.add(
        ChatMessage(
            paper_id="W1",
            role="user",
            content="hi",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    session.commit()

    r = client.delete("/papers/W1")
    assert r.status_code == 200, r.text
    assert session.get(Paper, "W1") is None
    assert session.exec(select(PaperConcept).where(PaperConcept.paper_id == "W1")).all() == []
    assert session.exec(select(PaperQuestion).where(PaperQuestion.paper_id == "W1")).all() == []
    assert session.exec(select(ChatMessage).where(ChatMessage.paper_id == "W1")).all() == []


# ---- notes in local search ----


def test_local_search_matches_notes(client, session):
    session.add(_paper("W1", title="Plain title", abstract="ordinary abstract"))
    session.commit()
    client.put("/papers/W1/notes", json={"notes_markdown": "zzqtestword appears here"})

    r = client.get(
        "/search/local", params={"q": "zzqtestword", "limit": 10, "correct": False}
    )
    assert r.status_code == 200, r.text
    ids = {hit.get("library_id") for hit in r.json()}
    assert "W1" in ids
