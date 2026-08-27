"""One-shot backfill: copy ``raw_meta.ids.arxiv`` into the ``arxiv_id`` column
for in-library papers that have an arXiv id in raw_meta but a NULL column.

Background
----------
The bulk-import fast path (see :func:`carrel.api.import_bulk._search_result_to_work`)
builds an OA-shaped Work dict and only writes the arXiv id under
``work["ids"]["arxiv"]``. The S2 import branch
(:func:`carrel.api.search._import_from_s2`) used to read ``work["arxiv_id"]``
(top-level) and silently dropped the id, so the ``arxiv_id`` column stayed
NULL even though ``raw_meta.ids.arxiv`` carried the value. The 2026-08
search-page bulk import hit this for ~408 fresh-arXiv papers.

This script walks every affected row, copies the id from ``raw_meta``,
synthesizes the arXiv PDF URL when ``pdf_url`` is empty, and stamps
``oa_status='oa'`` so download jobs pick the right URL.

Idempotent: re-running is a no-op (the column match check skips rows that
are already populated).

Usage::

    # Local dev (uses configured DATABASE_URL):
    uv run python scripts/backfill_arxiv_id_from_raw_meta.py

    # Dry-run — count only, write nothing:
    uv run python scripts/backfill_arxiv_id_from_raw_meta.py --dry-run
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Make `carrel` importable when run from the project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from carrel.config import load_settings  # noqa: E402
from carrel.db import init_app_engine  # noqa: E402
from carrel.models import Paper  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

logger = logging.getLogger("backfill_arxiv_id")


_ARXIV_VERSION_RE = __import__("re").compile(r"v\d+$")


def _strip(arxiv_id: str | None) -> str | None:
    """Drop version suffix and ``arxiv:`` prefix; the same logic
    :func:`carrel.sources.merge._strip_arxiv_version` uses."""
    if not arxiv_id:
        return None
    a = arxiv_id.strip().lower()
    a = _ARXIV_VERSION_RE.sub("", a)
    if a.startswith("arxiv:"):
        a = a[len("arxiv:"):]
    return a or None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Count affected rows and exit without writing.",
    )
    parser.add_argument(
        "--in-library-only", action="store_true", default=True,
        help="Only fix in_library=True rows (the default; pass --all to include inbox).",
    )
    parser.add_argument(
        "--all", dest="in_library_only", action="store_false",
        help="Also fix inbox rows.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    engine = init_app_engine(load_settings()[1])
    with Session(engine) as session:
        # Pre-flight count for the log line.
        scope_clause = "AND in_library = true" if args.in_library_only else ""
        total_affected = session.exec(text(f"""
            SELECT COUNT(*)
            FROM papers
            WHERE (arxiv_id IS NULL OR arxiv_id = '')
              AND raw_meta->'ids'->>'arxiv' IS NOT NULL
              AND raw_meta->'ids'->>'arxiv' <> ''
              {scope_clause}
        """)).one()[0]
        logger.info("affected rows: %d (dry_run=%s)", total_affected, args.dry_run)
        if total_affected == 0 or args.dry_run:
            return 0

        # Stream the affected rows; for each, copy the id + heal the pdf_url.
        rows = session.exec(select(Paper).where(
            (Paper.arxiv_id.is_(None)) | (Paper.arxiv_id == ""),
        )).all()
        # Filter in Python because raw_meta is JSON; cheaper to filter here
        # than push JSON SQL into the WHERE.
        fixed = 0
        for p in rows:
            if args.in_library_only and not p.in_library:
                continue
            raw = p.raw_meta or {}
            raw_ids = raw.get("ids") or {}
            arxiv_raw = raw_ids.get("arxiv")
            if not arxiv_raw:
                continue
            arxiv_id = _strip(arxiv_raw)
            if not arxiv_id:
                continue
            changed = False
            p.arxiv_id = arxiv_id
            changed = True
            if not p.pdf_url:
                p.pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
                p.oa_status = "oa"
            if changed:
                session.add(p)
                fixed += 1
                if fixed % 50 == 0:
                    session.commit()
                    logger.info("committed %d / %d", fixed, total_affected)
        session.commit()
        logger.info("backfilled %d rows", fixed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
