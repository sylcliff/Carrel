"""One-off: inject a well-known paper so the user can see citations work.

Usage:  uv run python scripts/seed_demo_paper.py
"""
from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlmodel import Session, SQLModel, create_engine

# Make `carrel` importable when run from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from carrel.config import EnvSettings, load_settings  # noqa: E402
from carrel.db import init_app_engine  # noqa: E402
from carrel.models import Paper, PaperStatus, SourceKind  # noqa: E402
from carrel.sources.normalize import from_openalex  # noqa: E402

OPENALEX_ID = "W2626778328"  # "Attention Is All You Need"


def fetch_openalex(work_id: str) -> dict:
    url = f"https://api.openalex.org/works/{work_id}"
    r = httpx.get(url, timeout=15)
    r.raise_for_status()
    return r.json()


def upsert(engine, rec) -> Paper:
    """Insert the record into `papers`, preserving any existing row's id."""
    now = datetime.now(UTC)
    with Session(engine) as session:
        existing = session.get(Paper, rec.id)
        if existing is not None:
            existing.title = rec.title
            existing.abstract = rec.abstract
            existing.publication_date = rec.publication_date
            existing.venue = rec.venue
            existing.doi = rec.doi
            existing.arxiv_id = rec.arxiv_id
            existing.pdf_url = rec.pdf_url
            existing.oa_status = rec.oa_status
            existing.authors = rec.authors
            existing.raw_meta = rec.raw_meta
            existing.updated_at = now
            session.add(existing)
            session.commit()
            session.refresh(existing)
            return existing
        paper = Paper(
            id=rec.id,
            id_kind=rec.id_kind,
            title=rec.title,
            abstract=rec.abstract,
            publication_date=rec.publication_date,
            venue=rec.venue,
            doi=rec.doi,
            arxiv_id=rec.arxiv_id,
            pdf_url=rec.pdf_url,
            oa_status=rec.oa_status,
            source=SourceKind.openalex.value,
            status=PaperStatus.pending.value,
            authors=rec.authors,
            raw_meta=rec.raw_meta,
            created_at=now,
            updated_at=now,
        )
        session.add(paper)
        session.commit()
        session.refresh(paper)
        return paper


def main() -> int:
    cfg, env = load_settings(Path("data/config.yaml"))
    engine = init_app_engine(env)
    # In case tables haven't been created yet
    SQLModel.metadata.create_all(engine)

    work = fetch_openalex(OPENALEX_ID)
    rec = from_openalex(work)
    if rec is None:
        print("from_openalex returned None; aborting")
        return 1
    paper = upsert(engine, rec)
    print(f"inserted/updated: id={paper.id} title={paper.title!r}")
    print(f"  doi={paper.doi}  arxiv_id={paper.arxiv_id}  venue={paper.venue}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
