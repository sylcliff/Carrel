#!/usr/bin/env python3
"""One-shot cleanup of duplicate wiki pages.

Runs the same backfill + retire pass that :func:`carrel.db.init_db` runs at
startup, but explicitly.  Useful when:

  * the operator wants to see *what* was changed before letting the next
    ``init_db`` re-touch the table;
  * a developer wiped a recent change and wants to recompute the catalog
    from a known-good starting point.

Idempotent: running it twice in a row is a no-op the second time.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the project root importable when this script is run from
# ``scripts/`` directly (e.g. ``python scripts/cleanup_duplicate_wiki.py``).
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    from sqlmodel import Session, select
    from sqlalchemy import inspect

    from carrel.config import EnvSettings
    from carrel.db import (
        backfill_wiki_identity,
        retire_duplicate_wiki_pages,
        init_app_engine,
        get_app_engine,
    )
    from carrel.models import WikiPage
    from carrel.pipeline.wiki._entities import reconcile_scholars

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reconcile",
        action="store_true",
        help="Run the live scholar reconcile pass after the retire step",
    )
    args = parser.parse_args()

    env = EnvSettings()
    init_app_engine(env)
    engine = get_app_engine()

    # Print the index columns the migration expects to see; bail out early
    # if a user runs this against a pre-migration database.
    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("wiki_pages")}
    if "entity_key" not in cols or "redirects_to" not in cols:
        print(
            "wiki_pages is missing entity_key/redirects_to; "
            "run the app once (init_db) before this script.",
            file=sys.stderr,
        )
        return 2

    print("Step 1/3: backfill entity_key on rows with NULL...")
    counts = backfill_wiki_identity(engine)
    print(f"  {counts}")

    print("Step 2/3: retire duplicate rows into redirect shells...")
    counts = retire_duplicate_wiki_pages(engine)
    print(f"  {counts}")

    with Session(engine) as s:
        redirected = s.exec(
            select(WikiPage).where(WikiPage.redirects_to.is_not(None))
        ).all()
        print(f"  current redirect shells: {len(redirected)}")
        for row in redirected:
            print(
                f"    id={row.id} kind={row.kind} slug={row.slug} "
                f"-> {row.redirects_to}"
            )

    if args.reconcile:
        print("Step 3/3: run scholar reconcile against live aggregation...")
        with Session(engine) as s:
            r = reconcile_scholars(s)
        print(f"  {r.as_dict()}")
    else:
        print("(no --reconcile flag set; skipping step 3)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
