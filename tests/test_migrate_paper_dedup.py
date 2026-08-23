"""Tests for scripts/migrate_paper_dedup.py — one-shot strong-anchor migration.

Exercises the script's main() directly against an in-memory engine, so the
test never touches a real Postgres database. The script uses the
``init_app_engine`` global, so we monkey-patch it to point at the test
session's engine. Each test gets its own in-memory engine so the data is
isolated.
"""
from __future__ import annotations

import importlib.util
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(eng)

    @event.listens_for(eng, "connect")
    def _fk_on(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    yield eng
    eng.dispose()


@pytest.fixture
def patch_init_app_engine(monkeypatch, engine):
    """Make the migration script (and carrel.db) use ``engine``."""

    def _init_app_engine(_env):
        return engine

    monkeypatch.setattr("carrel.db.init_app_engine", _init_app_engine)
    from scripts import migrate_paper_dedup as mig

    monkeypatch.setattr(mig, "init_app_engine", _init_app_engine)
    return engine


def _paper(
    pid: str,
    *,
    doi: str | None = None,
    arxiv_id: str | None = None,
    s2_paper_id: str | None = None,
    journal_doi: str | None = None,
    title: str = "T",
    in_library: bool = True,
    status: str = "ready",
    discarded: bool = False,
):
    from carrel.models import Paper

    return Paper(
        id=pid, id_kind="openalex", title=title,
        doi=doi, arxiv_id=arxiv_id, s2_paper_id=s2_paper_id,
        journal_doi=journal_doi,
        publication_date=date(2024, 1, 1),
        authors=[],
        in_library=in_library, status=status, discarded=discarded,
        oa_status="oa", source="openalex",
        created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
    )


def _load_script():
    """Load scripts/migrate_paper_dedup.py without going through the
    ``scripts`` package (no __init__.py)."""
    spec = importlib.util.spec_from_file_location(
        "migrate_paper_dedup",
        Path(__file__).resolve().parent.parent
        / "scripts"
        / "migrate_paper_dedup.py",
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_dry_run_scores_without_writing(engine, patch_init_app_engine):
    from sqlmodel import select

    from carrel.models import PaperAlias, PaperMergeEvent

    with Session(engine) as s:
        s.add(_paper("W1", doi="10.1/a", title="Same"))
        s.add(_paper("W2", doi="10.1/a", title="Same"))
        s.commit()

    mig = _load_script()
    rc = mig.main(["--dry-run"])
    assert rc == 0

    with Session(engine) as s:
        assert len(s.exec(select(PaperAlias)).all()) == 0
        assert len(s.exec(select(PaperMergeEvent)).all()) == 0


def test_real_run_auto_merges_strong_doi_pair(engine, patch_init_app_engine):
    from sqlmodel import select

    from carrel.models import PaperAlias, PaperMergeEvent

    with Session(engine) as s:
        s.add(_paper("W1", doi="10.1/a", title="Same Paper"))
        s.add(_paper("W2", doi="10.1/a", title="Same Paper"))
        s.commit()

    mig = _load_script()
    rc = mig.main([])
    assert rc == 0

    with Session(engine) as s:
        aliases = s.exec(select(PaperAlias)).all()
        assert len(aliases) == 1
        assert aliases[0].source == "auto"
        # confidence is the pipeline's weighted soft score (0.55 = AUTO_CONFIDENCE
        # floor), not 1.0 — the strong anchor just guarantees a merge, not
        # maximum confidence.
        assert aliases[0].confidence >= 0.5
        assert any("doi" in (r or "") for r in (aliases[0].reasons or []))
        events = s.exec(select(PaperMergeEvent)).all()
        assert len(events) == 1


def test_real_run_skips_discarded_and_out_of_library(engine, patch_init_app_engine):
    from sqlmodel import select

    from carrel.models import PaperAlias

    with Session(engine) as s:
        s.add(_paper("W1", doi="10.1/a", title="Library"))
        s.add(_paper("W2", doi="10.1/a", title="Library", discarded=True))
        s.add(_paper("W3", doi="10.1/a", title="Inbox", in_library=False))
        s.commit()

    mig = _load_script()
    rc = mig.main([])
    assert rc == 0

    with Session(engine) as s:
        # Only W1 is a candidate; no merge should happen.
        assert len(s.exec(select(PaperAlias)).all()) == 0
