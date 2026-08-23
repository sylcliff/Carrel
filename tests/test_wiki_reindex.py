"""Tests for the dead-link cleanup pass on concept/question pages.

Covers:
  * Dead links to missing concept/question pages are dropped from links_out.
  * Scholar pages' links_out is preserved even when the target is missing
    (user notes may legitimately reference concepts that haven't compiled).
  * Live links are preserved.
  * Redirect shells are NOT pruned (they're identified by redirects_to).
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from carrel.models import WikiPage
from carrel.pipeline.wiki import _reindex


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _page(**kw) -> WikiPage:
    now = datetime.now(UTC)
    base = dict(
        kind=kw.pop("kind", "concept"),
        slug=kw.pop("slug", "x"),
        title=kw.pop("title", "X"),
        path=kw.pop("path", f"wiki/{kw.get('kind', 'concept')}/{kw.pop('slug', 'x')}.md"),
        links_out=kw.pop("links_out", []),
        created_at=now,
        updated_at=now,
    )
    base.update(kw)
    return WikiPage(**base)


def test_dead_concept_link_pruned_from_concept_page(session):
    """A concept page linking to a non-existent concept page has that link dropped."""
    page = _page(
        kind="concept", slug="live",
        path="wiki/concepts/live.md",
        links_out=["../concepts/dead.md", "../concepts/live.md"],
    )
    session.add(page); session.commit()
    pruned = _reindex.prune_dead_links(session)
    assert pruned == 1
    session.refresh(page)
    assert page.links_out == ["../concepts/live.md"]


def test_scholar_page_links_preserved_even_when_target_missing(session):
    """Scholar pages are never touched — user notes may link to anything."""
    page = _page(
        kind="scholar", slug="A1", path="wiki/scholars/A1.md",
        title="Scholar", links_out=["../concepts/never-existed.md"],
    )
    session.add(page); session.commit()
    pruned = _reindex.prune_dead_links(session)
    assert pruned == 0
    session.refresh(page)
    assert page.links_out == ["../concepts/never-existed.md"]


def test_question_page_drops_dead_link(session):
    """Question pages: dead links get pruned just like concept pages."""
    page = _page(
        kind="question", slug="will-it-work",
        path="wiki/questions/will-it-work.md",
        title="Will it work?",
        links_out=["../concepts/missing.md", "../scholars/A1.md"],
    )
    scholar = _page(
        kind="scholar", slug="A1", path="wiki/scholars/A1.md",
        title="Scholar A1",
    )
    session.add(page); session.add(scholar); session.commit()
    pruned = _reindex.prune_dead_links(session)
    assert pruned == 1
    session.refresh(page)
    # The scholar link is fine — the page exists; the concept link is dropped.
    assert page.links_out == ["../scholars/A1.md"]


def test_live_target_link_kept(session):
    """A live target page is not considered dead — its link is preserved."""
    src = _page(
        kind="concept", slug="src", path="wiki/concepts/src.md",
        links_out=["../concepts/dst.md"],
    )
    dst = _page(
        kind="concept", slug="dst", path="wiki/concepts/dst.md",
    )
    session.add(src); session.add(dst); session.commit()
    pruned = _reindex.prune_dead_links(session)
    assert pruned == 0
    session.refresh(src)
    assert src.links_out == ["../concepts/dst.md"]


def test_redirect_shell_target_treated_as_dead(session):
    """A link to a redirect shell resolves through resolve_target; if the
    target entity is missing entirely, the link is dead."""
    # Source page links to a redirect shell whose target entity doesn't exist.
    src = _page(
        kind="concept", slug="src", path="wiki/concepts/src.md",
        links_out=["../concepts/shell.md"],
    )
    shell = _page(
        kind="concept", slug="shell", path="wiki/concepts/shell.md",
        entity_key="concept:shell", redirects_to="concept:missing",
    )
    session.add(src); session.add(shell); session.commit()
    pruned = _reindex.prune_dead_links(session)
    assert pruned == 1
    session.refresh(src)
    assert src.links_out == []
