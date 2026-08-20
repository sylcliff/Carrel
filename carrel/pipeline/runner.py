"""End-to-end sync pipeline.

Inspired by galleonli/paper-agent (MIT) — same order, same lookback/de-dup
discipline, same kind of log line at the end. Differences:
  - Two sources (arXiv, OpenAlex) instead of arXiv only
  - State is in PostgreSQL/SQLite (not a seen.json file)
  - The selection / bandit / autotune layer is gone — we keep it simple
  - We do not write markdown notes; the DB is the source of truth

This module is intentionally synchronous. A single-user daily sync is tens
to hundreds of papers; we don't need Celery/RQ.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, select

from carrel.config import CarrelYAML
from carrel.models import Job, JobStatus, Paper, PaperStatus, Subscription
from carrel.sources import arxiv as arxiv_src
from carrel.sources import openalex_client as oa
from carrel.sources.normalize import (
    PaperRecord,
    enrich_with_openalex,
    from_arxiv,
    from_openalex,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------


def list_enabled_subscriptions(session: Session) -> list[Subscription]:
    return list(
        session.exec(
            select(Subscription).where(Subscription.enabled.is_(True))
        ).all()
    )


def partition_subscriptions(
    subs: Iterable[Subscription],
) -> tuple[list[Subscription], list[Subscription], list[Subscription], list[Subscription]]:
    keywords, authors, venues, arxiv_cats = [], [], [], []
    for s in subs:
        if s.kind == "keyword":
            keywords.append(s)
        elif s.kind == "author":
            authors.append(s)
        elif s.kind == "venue":
            venues.append(s)
        elif s.kind == "arxiv_category":
            arxiv_cats.append(s)
    return keywords, authors, venues, arxiv_cats


# ---------------------------------------------------------------------------
# Source dispatch
# ---------------------------------------------------------------------------


def fetch_candidates(
    cfg: CarrelYAML,
    subs: list[Subscription],
    *,
    lookback_hours: int = 24,
) -> tuple[list[PaperRecord], dict[str, str]]:
    """Run all enabled subscriptions and return (records, per_source_errors).

    Per-source errors are collected, not raised, so a single upstream failure
    (e.g. arXiv 429/503) does not kill the rest of the sync. The caller can
    surface them in job stats.
    """
    oa.configure(cfg)
    since = datetime.now(UTC) - timedelta(hours=lookback_hours)
    keywords, authors, venues, arxiv_cats = partition_subscriptions(subs)

    records: dict[str, PaperRecord] = {}  # keyed by normalized id

    # ponytail: isolate each source so a single upstream failure (e.g. arXiv
    # 429/503, OpenAlex rate-limit) doesn't kill the whole sync.
    errors: dict[str, str] = {}

    # --- arXiv category + keyword subscriptions --------------------------------
    # Categories and keywords are independent subscription types: a keyword
    # search is NOT restricted to subscribed categories (PLAN §3), so we run
    # them as two arXiv sweeps and merge results by id.
    arxiv_cats_str = [s.value for s in arxiv_cats]
    arxiv_queries = [s.value for s in keywords]
    if arxiv_cats_str:
        try:
            entries = arxiv_src.fetch_recent(
                lookback_hours=lookback_hours,
                categories=arxiv_cats_str,
                max_results=cfg.arxiv.max_results_per_query,
                timeout=cfg.arxiv.request_timeout_seconds,
                delay_between_requests=cfg.arxiv.delay_between_requests_seconds,
            )
        except Exception as e:  # noqa: BLE001 - log and continue
            logger.warning("arxiv category fetch failed: %s", e)
            errors["arxiv_categories"] = f"{type(e).__name__}: {e}"
            entries = []
        for e in entries:
            rec = enrich_with_openalex(from_arxiv(e))
            _merge_record(records, rec)
    if arxiv_queries:
        try:
            entries = arxiv_src.fetch_recent(
                lookback_hours=lookback_hours,
                queries=arxiv_queries,
                max_results=cfg.arxiv.max_results_per_query,
                timeout=cfg.arxiv.request_timeout_seconds,
                delay_between_requests=cfg.arxiv.delay_between_requests_seconds,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("arxiv keyword fetch failed: %s", e)
            errors["arxiv_keywords"] = f"{type(e).__name__}: {e}"
            entries = []
        for e in entries:
            rec = enrich_with_openalex(from_arxiv(e))
            _merge_record(records, rec)

    # --- OpenAlex: author subscriptions -----------------------------------------
    for s in authors:
        try:
            works = oa.fetch_recent_by_author(
                s.value, since=since, max_results=50
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("openalex author fetch failed for %s: %s", s.value, e)
            errors[f"openalex_author:{s.value}"] = f"{type(e).__name__}: {e}"
            works = []
        for w in works:
            rec = from_openalex(w)
            if rec is not None:
                _merge_record(records, rec)

    # --- OpenAlex: venue subscriptions ------------------------------------------
    for s in venues:
        try:
            works = oa.fetch_recent_by_venue(
                s.value, since=since, max_results=100
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("openalex venue fetch failed for %s: %s", s.value, e)
            errors[f"openalex_venue:{s.value}"] = f"{type(e).__name__}: {e}"
            works = []
        for w in works:
            rec = from_openalex(w)
            if rec is not None:
                _merge_record(records, rec)

    # --- OpenAlex: keyword subscriptions (in addition to arXiv) ---------------
    keyword_strs = {s.value for s in keywords}
    for q in keyword_strs:
        try:
            works = oa.fetch_recent_by_keyword(q, since=since, max_results=30)
        except Exception as e:  # noqa: BLE001
            logger.warning("openalex keyword fetch failed for %s: %s", q, e)
            errors[f"openalex_keyword:{q}"] = f"{type(e).__name__}: {e}"
            works = []
        for w in works:
            rec = from_openalex(w)
            if rec is not None:
                _merge_record(records, rec)

    return list(records.values()), errors


def _merge_record(records: dict[str, PaperRecord], rec: PaperRecord) -> None:
    """Merge `rec` into the in-memory dedup map.

    A paper can arrive twice — once from arXiv (keyed `arxiv:<id>` when OpenAlex
    enrichment failed) and once from an OpenAlex author/venue/keyword search
    (keyed `W...`). When an OpenAlex record carries an arXiv id we also evict
    the weaker `arxiv:` placeholder so the same paper is not inserted twice.
    """
    if not rec.id:
        logger.warning("Skipping candidate with no id: %r", rec.title)
        return

    # If this canonical record supersedes an earlier arxiv:<id> placeholder,
    # drop the placeholder so it isn't upserted as a separate row.
    if rec.id_kind == "openalex" and rec.arxiv_id:
        records.pop(f"arxiv:{rec.arxiv_id}", None)

    existing = records.get(rec.id)
    if existing is None or _is_stronger(rec, existing):
        records[rec.id] = rec


def _is_stronger(a: PaperRecord, b: PaperRecord) -> bool:
    """'Stronger' = has OpenAlex ID, or has more complete fields."""

    def score(r: PaperRecord) -> tuple[bool, bool, bool, bool]:
        return (
            r.id_kind == "openalex",
            bool(r.venue),
            bool(r.doi),
            bool(r.abstract),
        )

    return score(a) > score(b)



def upsert_records(session: Session, records: list[PaperRecord]) -> dict[str, object]:
    """Insert or update each record.

    Returns counters ``{new, updated, skipped, new_ids}`` where ``new_ids`` is
    the list of paper IDs inserted in this pass (used by downstream enrichment
    such as citation lookups).
    """
    now = datetime.now(UTC)
    new_count = 0
    updated_count = 0
    skipped_count = 0
    new_ids: list[str] = []

    for rec in records:
        if not rec.id:
            logger.warning("Skipping record with no id: %r", rec.title)
            skipped_count += 1
            continue

        existing = session.get(Paper, rec.id)
        if existing is None and rec.id_kind == "openalex" and rec.arxiv_id:
            # A previous sync may have stored this paper under `arxiv:<id>`
            # because OpenAlex enrichment failed then. Promote the placeholder
            # to the canonical OpenAlex ID rather than inserting a duplicate.
            # No chunks/notes exist for a pending placeholder, so delete+insert
            # is safe; preserve any fields (e.g. the arXiv PDF URL) the
            # canonical record lacks.
            placeholder = session.get(Paper, f"arxiv:{rec.arxiv_id}")
            if placeholder is not None:
                logger.info(
                    "promoting placeholder arxiv:%s -> %s", rec.arxiv_id, rec.id
                )
                if not rec.pdf_url and placeholder.pdf_url:
                    rec.pdf_url = placeholder.pdf_url
                    rec.oa_status = "oa"
                if not rec.abstract and placeholder.abstract:
                    rec.abstract = placeholder.abstract
                session.delete(placeholder)

        if existing is None:
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
                source=rec.source,
                status=PaperStatus.pending.value,
                authors=rec.authors,
                raw_meta=rec.raw_meta,
                created_at=now,
                updated_at=now,
            )
            session.add(paper)
            new_count += 1
            new_ids.append(rec.id)
        else:
            # Refresh metadata if our new record is stronger
            changed = False
            if rec.venue and not existing.venue:
                existing.venue = rec.venue
                changed = True
            if rec.doi and not existing.doi:
                existing.doi = rec.doi
                changed = True
            if rec.arxiv_id and not existing.arxiv_id:
                existing.arxiv_id = rec.arxiv_id
                changed = True
            if rec.abstract and not existing.abstract:
                existing.abstract = rec.abstract
                changed = True
            if rec.pdf_url and not existing.pdf_url:
                existing.pdf_url = rec.pdf_url
                existing.oa_status = rec.oa_status
                changed = True
            if rec.source == "both" and existing.source != "both":
                existing.source = "both"
                changed = True
            if changed:
                existing.updated_at = now
                updated_count += 1
            else:
                skipped_count += 1

    session.commit()
    return {
        "new": new_count,
        "updated": updated_count,
        "skipped": skipped_count,
        "new_ids": new_ids,
    }


# ---------------------------------------------------------------------------
# Top-level run
# ---------------------------------------------------------------------------


def run_sync(
    session: Session,
    cfg: CarrelYAML,
    *,
    lookback_hours: int = 24,
    job: Job | None = None,
) -> dict[str, object]:
    """Run a single sync pass; update job record (if provided) in-place."""
    if job is not None:
        job.status = JobStatus.running.value
        job.started_at = datetime.now(UTC)
        session.add(job)
        session.commit()

    subs = list_enabled_subscriptions(session)
    logger.info("sync start: subs=%d lookback_h=%d", len(subs), lookback_hours)

    try:
        records, fetch_errors = fetch_candidates(cfg, subs, lookback_hours=lookback_hours)
        logger.info("fetched %d candidate records", len(records))
        counts = upsert_records(session, records)
        counts["fetched"] = len(records)
        counts["subscriptions"] = len(subs)
        if fetch_errors:
            counts["source_errors"] = fetch_errors

        # Best-effort citation enrichment for newly inserted papers, plus a
        # bounded backfill of papers enriched before the references-list feature
        # shipped (reference_count set but references still NULL). Failures here
        # (rate limits, S2 downtime) must not fail the whole sync.
        new_ids = counts.get("new_ids") or []
        if cfg.semantic_scholar.fetch_on_sync:
            from carrel.pipeline import citations as cite_pipe

            backfill = [
                p.id for p in cite_pipe.select_missing_references(
                    session, limit=cfg.semantic_scholar.references_backfill_batch
                )
            ]
            # Preserve order, dedup (a backfill id could coincide with a new id).
            seen: set[str] = set()
            cite_ids: list[str] = []
            for pid in [*list(new_ids), *backfill]:
                if pid not in seen:
                    seen.add(pid)
                    cite_ids.append(pid)
            counts["references_backfilled"] = len(backfill)

            def _cite_progress(progress: dict) -> None:
                if job is not None:
                    job.stats = {**(job.stats or {}), **progress, **counts}
                    session.add(job)
                    session.commit()

            if cite_ids:
                cite_counts = cite_pipe.enrich_papers(
                    session, cfg, cite_ids, on_progress=_cite_progress
                )
                counts["citations_enriched"] = cite_counts["enriched"]
                counts["citations_failed"] = cite_counts["failed"]
                logger.info(
                    "citation enrichment: enriched=%d failed=%d (new=%d, reference-backfill=%d)",
                    cite_counts["enriched"], cite_counts["failed"],
                    len(new_ids), len(backfill),
                )

        logger.info(
            "sync done: fetched=%d new=%d updated=%d skipped=%d",
            counts["fetched"],
            counts["new"],
            counts["updated"],
            counts["skipped"],
        )
        if job is not None:
            job.status = JobStatus.done.value
            job.finished_at = datetime.now(UTC)
            job.message = "ok"
            job.stats = counts
            session.add(job)
            session.commit()
        return counts
    except Exception as e:
        logger.exception("sync failed: %s", e)
        if job is not None:
            job.status = JobStatus.failed.value
            job.finished_at = datetime.now(UTC)
            job.message = f"{type(e).__name__}: {e}"
            session.add(job)
            session.commit()
        raise
