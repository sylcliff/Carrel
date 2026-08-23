"""One-shot migration: scan the library and auto-merge strong-anchor duplicates.

Useful right after upgrading an existing Carrel install to the M10 release,
where Paper rows were imported with no cross-id dedup. This script:

1. Iterates every in-library, non-merged, non-discarded Paper row.
2. Runs ``run_dedup(auto_apply=True)`` (no LLM — strong-anchor only, plus
   the deterministic soft band if it clears the threshold).
3. Prints a one-line summary: how many strong-anchor pairs were auto-merged
   and how many borderline pairs were left as suggestions for the user to
   review in the Library → Duplicates panel.

The whole run executes inside one SQLModel session. The merge migration is
idempotent at the row level (re-applying is a no-op on the alias), but
because the migration physically clears loser's user_state into the
canonical, do not re-run after a partial failure without restoring the
``PaperMergeEvent`` log first.

Pre-flight: take a database snapshot. Suggested: ``pg_dump -Fc > pre-m10.dump``.

Usage::

    # Local dev (uses the configured DATABASE_URL):
    uv run python scripts/migrate_paper_dedup.py

    # Dry run — score only, write nothing:
    uv run python scripts/migrate_paper_dedup.py --dry-run
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Make `carrel` importable when run from the project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import Session, select  # noqa: E402

from carrel.config import load_settings  # noqa: E402
from carrel.db import init_app_engine  # noqa: E402
from carrel.models import Paper, PaperAlias, PaperMergeEvent  # noqa: E402
from carrel.pipeline import paper_dedup as dedup  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("migrate_paper_dedup")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "One-shot paper dedup migration. Scans in-library papers, "
            "auto-merges strong-anchor pairs, leaves the rest for review."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Score only; do not write any aliases or merge events.",
    )
    args = parser.parse_args(argv)

    cfg, env = load_settings()
    engine = init_app_engine(env)

    # Sanity check: the papers table must already exist. This script is
    # meant to run against an existing Carrel install, not a fresh database.
    # create_all() would mask a missing schema with an empty one and turn
    # a migration into a no-op, so fail loudly instead.
    from sqlalchemy import inspect

    insp = inspect(engine)
    if "papers" not in insp.get_table_names():
        log.error(
            "papers table not found in the configured database. "
            "This script is a one-shot migration over an existing Carrel "
            "library; run the app at least once to create the schema, "
            "then re-run."
        )
        return 2

    with Session(engine) as session:
        candidates = session.exec(
            select(Paper).where(
                Paper.in_library.is_(True),
                Paper.discarded.is_(False),
                Paper.status != "merged",
            )
        ).all()
        n_in = len(candidates)
        log.info("Found %d in-library candidate papers", n_in)

        n_aliases_before = len(
            session.exec(select(PaperAlias)).all()
        )
        n_events_before = len(
            session.exec(select(PaperMergeEvent)).all()
        )

        if args.dry_run:
            log.info("DRY RUN — not writing any aliases or merge events")
            result = dedup.run_dedup(
                session,
                auto_apply=False,
                on_progress=lambda p: log.info(
                    "progress: %s", p.get("detail")
                ),
            )
            session.rollback()
            log.info(
                "DRY RUN complete: candidates=%d auto_merged=%d suggested=%d "
                "skipped_rejected=%d",
                result.candidates,
                result.auto_merged,
                result.suggested,
                result.skipped_rejected,
            )
            return 0

        # Real run. Commit at the end; if any merge raises, the whole
        # transaction rolls back so the database is never left in a
        # half-migrated state.
        result = dedup.run_dedup(
            session,
            auto_apply=True,
            on_progress=lambda p: log.info("progress: %s", p.get("detail")),
        )
        session.commit()

        n_aliases_after = len(session.exec(select(PaperAlias)).all())
        n_events_after = len(session.exec(select(PaperMergeEvent)).all())

        n_new_aliases = n_aliases_after - n_aliases_before
        n_new_events = n_events_after - n_events_before

        log.info(
            "MIGRATION COMPLETE — papers_considered=%d pairs_scored=%d "
            "auto_merged=%d suggested=%d skipped_rejected=%d "
            "new_alias_rows=%d new_merge_events=%d",
            n_in,
            result.candidates,
            result.auto_merged,
            result.suggested,
            result.skipped_rejected,
            n_new_aliases,
            n_new_events,
        )
        log.info(
            "Next: open the Library page, click Duplicates, and review the "
            "%d suggested pair(s).",
            result.suggested,
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
