"""Tests for paper dedup operations: alias resolution, state migration, reject/undo."""
from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlmodel import Session, select

from carrel.models import (
    ChatMessage,
    Chunk,
    Paper,
    PaperAlias,
    PaperMergeEvent,
    PaperStatus,
    PaperTag,
    PaperTopic,
    Tag,
    Topic,
    WikiSource,
)
from carrel.pipeline import paper_dedup_ops as ops


def _make_paper(
    pid: str,
    *,
    title: str = "T",
    doi: str | None = None,
    arxiv_id: str | None = None,
    s2_paper_id: str | None = None,
    journal_doi: str | None = None,
    in_library: bool = True,
    favorite: bool = False,
    notes: str | None = None,
    tldr_en: str | None = None,
    keywords: list[str] | None = None,
    status: str = "ready",
) -> Paper:
    return Paper(
        id=pid, id_kind="openalex", title=title,
        doi=doi, arxiv_id=arxiv_id, s2_paper_id=s2_paper_id,
        journal_doi=journal_doi,
        publication_date=date(2024, 1, 1),
        in_library=in_library, favorite=favorite, notes_markdown=notes,
        tldr_en=tldr_en, keywords=keywords, status=status,
        oa_status="oa", source="openalex",
        created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# resolve_paper_id
# ---------------------------------------------------------------------------


def test_resolve_paper_id_no_alias_returns_same(session: Session):
    p = _make_paper("W1")
    session.add(p)
    session.commit()
    assert ops.resolve_paper_id(session, "W1") == "W1"


def test_resolve_paper_id_one_hop(session: Session):
    session.add(_make_paper("W1"))
    session.add(_make_paper("W2"))
    session.add(PaperAlias(
        alias_paper_id="W2", canonical_paper_id="W1",
        source="auto", confidence=1.0, reasons=["doi-match"],
    ))
    session.commit()
    assert ops.resolve_paper_id(session, "W2") == "W1"
    assert ops.resolve_paper_id(session, "W1") == "W1"


def test_resolve_paper_id_reject_is_not_followed(session: Session):
    """A 'reject' alias must not be followed — it suppresses auto-merge, doesn't redirect."""
    session.add(_make_paper("W1"))
    session.add(_make_paper("W2"))
    session.add(PaperAlias(
        alias_paper_id="W2", canonical_paper_id="W1",
        source="reject", confidence=1.0, reasons=["user-rejected"],
    ))
    session.commit()
    # Reject is invisible to the resolver.
    assert ops.resolve_paper_id(session, "W2") == "W2"


def test_resolve_paper_id_chain_loops_safely(session: Session):
    """A pathological cycle (W1 -> W2 -> W1) must not infinite-loop."""
    session.add(_make_paper("W1"))
    session.add(_make_paper("W2"))
    session.add(PaperAlias(
        alias_paper_id="W1", canonical_paper_id="W2",
        source="auto", confidence=1.0, reasons=["x"],
    ))
    session.add(PaperAlias(
        alias_paper_id="W2", canonical_paper_id="W1",
        source="auto", confidence=1.0, reasons=["x"],
    ))
    session.commit()
    # Should return *some* value, never crash.
    out = ops.resolve_paper_id(session, "W1")
    assert out in ("W1", "W2")


# ---------------------------------------------------------------------------
# apply_merge
# ---------------------------------------------------------------------------


def test_apply_merge_self_raises(session: Session):
    session.add(_make_paper("W1"))
    session.commit()
    with pytest.raises(ops.PaperMergeError):
        ops.apply_merge(
            session, alias_paper_id="W1", canonical_paper_id="W1",
            source="user", confidence=1.0,
        )


def test_apply_merge_missing_paper_raises(session: Session):
    session.add(_make_paper("W1"))
    session.commit()
    with pytest.raises(ops.PaperMergeError):
        ops.apply_merge(
            session, alias_paper_id="DOES_NOT_EXIST", canonical_paper_id="W1",
            source="auto", confidence=1.0,
        )


def test_apply_merge_basic_alias_row(session: Session):
    session.add(_make_paper("W1", title="Canonical"))
    session.add(_make_paper("W2", title="Alias", favorite=True, notes="from W2"))
    session.commit()

    row = ops.apply_merge(
        session, alias_paper_id="W2", canonical_paper_id="W1",
        source="user", confidence=1.0, reasons=["user-confirmed"],
    )
    session.commit()

    # Alias row is written.
    assert row.source == "user"
    assert row.canonical_paper_id == "W1"
    assert row.alias_paper_id == "W2"

    # resolve_paper_id routes W2 -> W1.
    assert ops.resolve_paper_id(session, "W2") == "W1"

    # Loser is flagged.
    loser = session.get(Paper, "W2")
    assert loser is not None
    assert loser.status == PaperStatus.merged.value

    # Audit event recorded.
    events = session.exec(select(PaperMergeEvent)).all()
    assert len(events) == 1
    assert events[0].source == "user"
    assert events[0].user_state_migrated is True


def test_apply_merge_migrates_favorite_notes_tldr(session: Session):
    session.add(_make_paper("W1", title="Canon"))
    session.add(_make_paper(
        "W2", title="Alias",
        favorite=True, notes="notes from W2",
        tldr_en="A TLDR from W2", keywords=["a", "b"],
    ))
    session.commit()

    ops.apply_merge(
        session, alias_paper_id="W2", canonical_paper_id="W1",
        source="auto", confidence=1.0, reasons=["doi"],
    )
    session.commit()

    winner = session.get(Paper, "W1")
    assert winner.favorite is True
    assert winner.notes_markdown == "notes from W2"
    assert winner.tldr_en == "A TLDR from W2"
    assert set(winner.keywords or []) == {"a", "b"}

    loser = session.get(Paper, "W2")
    assert loser.favorite is False
    assert loser.notes_markdown is None
    assert loser.tldr_en is None
    assert loser.keywords is None
    assert loser.status == PaperStatus.merged.value


def test_apply_merge_concatenates_dual_notes(session: Session):
    session.add(_make_paper("W1", notes="from W1"))
    session.add(_make_paper("W2", notes="from W2"))
    session.commit()
    ops.apply_merge(
        session, alias_paper_id="W2", canonical_paper_id="W1",
        source="user", confidence=1.0,
    )
    session.commit()
    winner = session.get(Paper, "W1")
    assert "from W1" in winner.notes_markdown
    assert "from W2" in winner.notes_markdown
    assert "---" in winner.notes_markdown  # separator present


def test_apply_merge_rebinds_chunks_chat_wiki(session: Session):
    session.add(_make_paper("W1"))
    session.add(_make_paper("W2"))
    session.commit()

    # Two chunks on W2, one chunk on W1.
    session.add(Chunk(paper_id="W2", chunk_index=0, content_md="x", token_count=1))
    session.add(Chunk(paper_id="W2", chunk_index=1, content_md="y", token_count=1))
    session.add(Chunk(paper_id="W1", chunk_index=0, content_md="z", token_count=1))
    # Chat history on W2.
    session.add(ChatMessage(paper_id="W2", role="user", content="hi",
                            created_at=datetime.now(UTC), updated_at=datetime.now(UTC)))
    # Wiki source on W2.
    session.add(WikiSource(wiki_page_id=1, paper_id="W2", chunk_id=None,
                            heading="h", quote="q", role="context"))
    session.commit()

    ops.apply_merge(
        session, alias_paper_id="W2", canonical_paper_id="W1",
        source="auto", confidence=1.0,
    )
    session.commit()

    # All FKs rebind to W1.
    chunk_papers = {c.paper_id for c in session.exec(select(Chunk)).all()}
    assert chunk_papers == {"W1"}, f"chunks not rebind: {chunk_papers}"
    chat_papers = {c.paper_id for c in session.exec(select(ChatMessage)).all()}
    assert chat_papers == {"W1"}
    wiki_papers = {w.paper_id for w in session.exec(select(WikiSource)).all()}
    assert wiki_papers == {"W1"}


def test_apply_merge_dedups_paper_tags_and_topics(session: Session):
    session.add(_make_paper("W1"))
    session.add(_make_paper("W2"))
    session.commit()
    t1 = Tag(name="ml")
    t2 = Tag(name="llm")
    session.add(t1); session.add(t2)
    session.commit()
    top1 = Topic(name="Agents")
    top2 = Topic(name="RAG")
    session.add(top1); session.add(top2)
    session.commit()

    # W1 has {t1, top1}; W2 has {t1, t2, top1, top2}. t1 and top1 collide.
    session.add(PaperTag(paper_id="W1", tag_id=t1.id))
    session.add(PaperTag(paper_id="W2", tag_id=t1.id))
    session.add(PaperTag(paper_id="W2", tag_id=t2.id))
    session.add(PaperTopic(paper_id="W1", topic_id=top1.id))
    session.add(PaperTopic(paper_id="W2", topic_id=top1.id))
    session.add(PaperTopic(paper_id="W2", topic_id=top2.id))
    session.commit()

    ops.apply_merge(
        session, alias_paper_id="W2", canonical_paper_id="W1",
        source="user", confidence=1.0,
    )
    session.commit()

    tag_pairs = {(r.paper_id, r.tag_id) for r in session.exec(select(PaperTag)).all()}
    assert tag_pairs == {("W1", t1.id), ("W1", t2.id)}
    topic_pairs = {(r.paper_id, r.topic_id) for r in session.exec(select(PaperTopic)).all()}
    assert topic_pairs == {("W1", top1.id), ("W1", top2.id)}


def test_apply_merge_unions_citation_lists(session: Session):
    session.add(_make_paper("W1"))
    session.add(_make_paper("W2"))
    session.commit()
    common = {"title": "Shared Ref", "doi": "10.1/shared"}
    p1 = session.get(Paper, "W1")
    p2 = session.get(Paper, "W2")
    p1.references = [common, {"title": "Only in W1", "doi": "10.1/only1"}]
    p2.references = [common, {"title": "Only in W2", "doi": "10.1/only2"}]
    session.add(p1); session.add(p2)
    session.commit()

    ops.apply_merge(
        session, alias_paper_id="W2", canonical_paper_id="W1",
        source="auto", confidence=1.0,
    )
    session.commit()

    winner = session.get(Paper, "W1")
    dois = {ref.get("doi") for ref in (winner.references or [])}
    assert dois == {"10.1/shared", "10.1/only1", "10.1/only2"}


def test_apply_merge_idempotent(session: Session):
    """Calling apply_merge twice with the same args doesn't double-migrate."""
    session.add(_make_paper("W1", title="C"))
    session.add(_make_paper("W2", title="A", notes="from W2", favorite=True))
    session.commit()

    ops.apply_merge(
        session, alias_paper_id="W2", canonical_paper_id="W1",
        source="auto", confidence=1.0,
    )
    session.commit()

    ops.apply_merge(
        session, alias_paper_id="W2", canonical_paper_id="W1",
        source="auto", confidence=1.0,
    )
    session.commit()

    # Notes should not be duplicated.
    winner = session.get(Paper, "W1")
    assert winner.notes_markdown == "from W2"
    assert winner.favorite is True

    # Only one alias row.
    assert len(session.exec(select(PaperAlias)).all()) == 1


def test_apply_merge_resolves_existing_alias_chain(session: Session):
    """If A->B already exists and we ask to merge B->C, the result is A->C and B->C."""
    session.add(_make_paper("W1"))
    session.add(_make_paper("W2"))
    session.add(_make_paper("W3"))
    session.add(PaperAlias(
        alias_paper_id="W1", canonical_paper_id="W2",
        source="auto", confidence=1.0, reasons=["x"],
    ))
    session.commit()

    ops.apply_merge(
        session, alias_paper_id="W2", canonical_paper_id="W3",
        source="user", confidence=1.0,
    )
    session.commit()

    # W1 now resolves to W3 (chain), W2 to W3.
    assert ops.resolve_paper_id(session, "W1") == "W3"
    assert ops.resolve_paper_id(session, "W2") == "W3"


# ---------------------------------------------------------------------------
# apply_reject
# ---------------------------------------------------------------------------


def test_apply_reject_writes_reject_alias(session: Session):
    session.add(_make_paper("W1"))
    session.add(_make_paper("W2"))
    session.commit()

    row = ops.apply_reject(session, a="W1", b="W2", display_label="two papers")
    session.commit()

    assert row.source == "reject"
    assert {row.alias_paper_id, row.canonical_paper_id} == {"W1", "W2"}


def test_apply_reject_drops_prior_auto_merge(session: Session):
    """A reject must override any prior auto-merge between the pair."""
    session.add(_make_paper("W1"))
    session.add(_make_paper("W2"))
    session.add(PaperAlias(
        alias_paper_id="W2", canonical_paper_id="W1",
        source="auto", confidence=0.9, reasons=["doi"],
    ))
    session.commit()

    ops.apply_reject(session, a="W1", b="W2")
    session.commit()

    aliases = session.exec(select(PaperAlias)).all()
    # Only the reject survives.
    assert len(aliases) == 1
    assert aliases[0].source == "reject"


def test_apply_reject_self_raises(session: Session):
    session.add(_make_paper("W1"))
    session.commit()
    with pytest.raises(ops.PaperMergeError):
        ops.apply_reject(session, a="W1", b="W1")


# ---------------------------------------------------------------------------
# undo_alias
# ---------------------------------------------------------------------------


def test_undo_alias_deletes_row_and_unflags_loser(session: Session):
    session.add(_make_paper("W1", title="C"))
    session.add(_make_paper("W2", title="A", notes="to migrate"))
    session.commit()

    ops.apply_merge(
        session, alias_paper_id="W2", canonical_paper_id="W1",
        source="user", confidence=1.0,
    )
    session.commit()
    assert ops.resolve_paper_id(session, "W2") == "W1"

    deleted = ops.undo_alias(
        session, alias_paper_id="W2", canonical_paper_id="W1",
    )
    session.commit()

    assert deleted is True
    assert ops.resolve_paper_id(session, "W2") == "W2"
    loser = session.get(Paper, "W2")
    # Status un-flagged; user_state was migrated and is NOT put back.
    assert loser.status == PaperStatus.ready.value
    assert loser.notes_markdown is None  # not restored


def test_undo_alias_missing_returns_false(session: Session):
    session.add(_make_paper("W1"))
    session.add(_make_paper("W2"))
    session.commit()
    deleted = ops.undo_alias(session, alias_paper_id="W2", canonical_paper_id="W1")
    assert deleted is False


# ---------------------------------------------------------------------------
# list_aliases / is_merged_away
# ---------------------------------------------------------------------------


def test_list_aliases_filter_by_source(session: Session):
    session.add(_make_paper("W1"))
    session.add(_make_paper("W2"))
    session.add(_make_paper("W3"))
    session.add(PaperAlias(alias_paper_id="W2", canonical_paper_id="W1",
                            source="auto", confidence=1.0))
    session.add(PaperAlias(alias_paper_id="W3", canonical_paper_id="W1",
                            source="reject", confidence=1.0))
    session.commit()

    auto = ops.list_aliases(session, source="auto")
    assert len(auto) == 1 and auto[0].alias_paper_id == "W2"
    rejects = ops.list_aliases(session, source="reject")
    assert len(rejects) == 1 and rejects[0].alias_paper_id == "W3"
    all_aliases = ops.list_aliases(session)
    assert len(all_aliases) == 2


def test_is_merged_away(session: Session):
    session.add(_make_paper("W1"))
    session.add(_make_paper("W2"))
    session.commit()
    assert ops.is_merged_away(session, "W2") is False
    ops.apply_merge(
        session, alias_paper_id="W2", canonical_paper_id="W1",
        source="user", confidence=1.0,
    )
    session.commit()
    assert ops.is_merged_away(session, "W2") is True
    assert ops.is_merged_away(session, "W1") is False
