"""Tests for pipeline.runner: partition, merge/dedup, upsert, run_sync."""
from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import patch

from carrel.models import Job, JobKind, JobStatus, Paper, PaperStatus, Subscription
from carrel.pipeline.runner import (
    _is_stronger,
    _merge_record,
    fetch_candidates,
    partition_subscriptions,
    run_sync,
    upsert_records,
)
from carrel.sources.normalize import PaperRecord
from sqlmodel import select


def _rec(**over) -> PaperRecord:
    base = dict(
        id="W1",
        id_kind="openalex",
        title="Paper One",
        abstract="abstract",
        publication_date=date(2026, 8, 1),
        venue="Nature",
        authors=[{"name": "A", "openalex_author_id": "", "affiliation": None}],
        doi="10.1/abc",
        arxiv_id=None,
        pdf_url="https://x/p.pdf",
        oa_status="oa",
        source="openalex",
        raw_meta={},
    )
    base.update(over)
    return PaperRecord(**base)


# ---- partition_subscriptions ----

def test_partition_groups_by_kind(session):
    subs = [
        Subscription(kind="keyword", value="rag"),
        Subscription(kind="author", value="A1"),
        Subscription(kind="venue", value="S1"),
        Subscription(kind="arxiv_category", value="cs.CL"),
        Subscription(kind="keyword", value="diffusion"),
    ]
    kws, authors, venues, cats = partition_subscriptions(subs)
    assert [s.value for s in kws] == ["rag", "diffusion"]
    assert [s.value for s in authors] == ["A1"]
    assert [s.value for s in venues] == ["S1"]
    assert [s.value for s in cats] == ["cs.CL"]


# ---- _is_stronger ----

def test_is_stronger_prefers_openalex_id():
    a = _rec(id_kind="openalex")
    b = _rec(id="arxiv:1", id_kind="arxiv", venue=None, doi=None, abstract=None)
    assert _is_stronger(a, b)
    assert not _is_stronger(b, a)


def test_is_stronger_tie_uses_fields():
    a = _rec(id_kind="arxiv", venue="V", doi="d", abstract="x")
    b = _rec(id="arxiv:2", id_kind="arxiv", venue=None, doi=None, abstract=None)
    assert _is_stronger(a, b)


# ---- _merge_record / dedup ----

def test_merge_evicts_arxiv_placeholder_when_canonical_arrives():
    records = {}
    arxiv_placeholder = _rec(
        id="arxiv:2401.00001", id_kind="arxiv", source="arxiv",
        arxiv_id="2401.00001", pdf_url="https://arxiv.org/pdf/2401.00001",
    )
    canonical = _rec(
        id="W100", id_kind="openalex", arxiv_id="2401.00001", source="openalex",
    )
    _merge_record(records, arxiv_placeholder)
    _merge_record(records, canonical)
    assert "arxiv:2401.00001" not in records
    assert list(records) == ["W100"]
    assert records["W100"].source == "openalex"


def test_merge_keeps_stronger_of_same_id():
    records = {}
    weak = _rec(venue=None, doi=None, abstract=None)
    strong = _rec(venue="Nature", doi="10.1/x", abstract="full abstract")
    _merge_record(records, weak)
    _merge_record(records, strong)
    assert records["W1"].venue == "Nature"
    assert records["W1"].abstract == "full abstract"


def test_merge_skips_record_with_no_id(caplog):
    records = {}
    _merge_record(records, _rec(id=""))
    assert records == {}


# ---- upsert_records ----

def test_upsert_inserts_new(session):
    counts = upsert_records(session, [_rec(), _rec(id="W2", title="Two")])
    assert counts == {"new": 2, "updated": 0, "skipped": 0, "new_ids": ["W1", "W2"]}
    assert session.get(Paper, "W1").title == "Paper One"
    assert session.get(Paper, "W2").status == PaperStatus.pending.value


def test_upsert_skips_no_id(session):
    counts = upsert_records(session, [_rec(id="")])
    assert counts == {"new": 0, "updated": 0, "skipped": 1, "new_ids": []}
    assert len(session.exec(select(Paper)).all()) == 0


def test_upsert_backfills_missing_fields_on_existing(session):
    upsert_records(session, [_rec(venue=None, doi=None, arxiv_id=None, abstract=None)])
    # A richer second record for the same id should backfill, not duplicate.
    counts = upsert_records(session, [_rec(
        venue="Nature", doi="10.1/x", arxiv_id="2401.00001",
        abstract="now with abstract",
    )])
    assert counts["updated"] == 1
    p = session.get(Paper, "W1")
    assert p.venue == "Nature"
    assert p.doi == "10.1/x"
    assert p.arxiv_id == "2401.00001"
    assert p.abstract == "now with abstract"


def test_upsert_counts_skip_when_nothing_new(session):
    upsert_records(session, [_rec()])
    counts = upsert_records(session, [_rec()])
    assert counts == {"new": 0, "updated": 0, "skipped": 1, "new_ids": []}


def test_upsert_promotes_arxiv_placeholder_to_canonical(session):
    """If an earlier sync stored a paper as arxiv:<id> (OpenAlex enrichment
    failed then) and a later sync finds the canonical OpenAlex work, the
    placeholder is removed and the canonical row is inserted — no duplicate."""
    placeholder = _rec(
        id="arxiv:2401.00001", id_kind="arxiv", source="arxiv",
        arxiv_id="2401.00001", venue=None, doi=None, abstract=None,
        pdf_url="https://arxiv.org/pdf/2401.00001",
    )
    upsert_records(session, [placeholder])

    canonical = _rec(
        id="W500", id_kind="openalex", source="openalex",
        arxiv_id="2401.00001", venue="arXiv",
    )
    counts = upsert_records(session, [canonical])

    assert counts["new"] == 1
    assert session.get(Paper, "arxiv:2401.00001") is None
    assert session.get(Paper, "W500") is not None
    assert len(session.exec(select(Paper)).all()) == 1


# ---- Zenodo records are filtered out at the source ----


def test_from_openalex_skips_zenodo_by_doi():
    from carrel.sources.normalize import from_openalex

    work = {
        "id": "https://openalex.org/W9001",
        "title": "A Tool, Not a Paper",
        "doi": "https://doi.org/10.5281/zenodo.1000001",
        "primary_location": {"source": {"display_name": "Some venue"}},
    }
    assert from_openalex(work) is None


def test_from_openalex_skips_zenodo_by_venue():
    from carrel.sources.normalize import from_openalex

    work = {
        "id": "https://openalex.org/W9002",
        "title": "Another Deposit",
        "doi": "https://doi.org/10.1000/xyz",
        "primary_location": {"source": {"display_name": "Zenodo"}},
    }
    assert from_openalex(work) is None


# ---- run_sync (with mocked sources) ----

def _sub(kind, value):
    return Subscription(kind=kind, value=value, enabled=True,
                        created_at=datetime.now(UTC))


def test_run_sync_persists_papers_and_updates_job(session, cfg):
    from carrel.sources.arxiv import ArxivEntry

    entry = ArxivEntry(
        arxiv_id="2401.00001", title="Fetched From arXiv", summary="abs",
        authors=["A"], categories=["cs.CL"], updated="2026-08-01T00:00:00Z",
        abs_url="https://arxiv.org/abs/2401.00001",
        pdf_url="https://arxiv.org/pdf/2401.00001",
    )
    job = Job(kind=JobKind.sync.value, status=JobStatus.queued.value,
              created_at=datetime.now(UTC))
    session.add(job)
    session.commit()

    with patch("carrel.pipeline.runner.arxiv_src.fetch_recent", return_value=[entry]), \
         patch("carrel.pipeline.runner.oa.lookup_by_arxiv_id", return_value=None), \
         patch("carrel.pipeline.runner.oa.configure"), \
         patch("carrel.pipeline.citations.enrich_papers",
               return_value={"enriched": 0, "failed": 0, "skipped": 0}):
        session.add(_sub("arxiv_category", "cs.CL"))
        session.commit()
        counts = run_sync(session, cfg, lookback_hours=24, job=job)

    assert counts["new"] == 1
    p = session.get(Paper, "arxiv:2401.00001")
    assert p is not None
    assert p.title == "Fetched From arXiv"
    session.refresh(job)
    assert job.status == JobStatus.done.value
    assert job.stats["new"] == 1


def test_run_sync_swallows_per_source_errors_and_records_them(session, cfg):
    """A single source's failure must not fail the whole sync; the error is
    recorded in the job stats under `source_errors` so the UI can surface it.
    """
    job = Job(kind=JobKind.sync.value, status=JobStatus.queued.value,
              created_at=datetime.now(UTC))
    session.add(job)
    session.add(_sub("keyword", "anything"))
    session.commit()

    with patch("carrel.pipeline.runner.arxiv_src.fetch_recent",
               side_effect=RuntimeError("boom")), \
         patch("carrel.pipeline.runner.oa.configure"), \
         patch(
             "carrel.pipeline.citations.enrich_papers",
             return_value={"enriched": 0, "failed": 0, "skipped": 0},
         ):
        run_sync(session, cfg, lookback_hours=24, job=job)

    session.refresh(job)
    assert job.status == JobStatus.done.value
    assert job.stats.get("source_errors", {}).get("arxiv_keywords", "").endswith("boom")


def test_fetch_candidates_dedups_arxiv_and_openalert_same_paper(session, cfg):
    """The same paper arriving from arXiv (enrichment failed) and an OpenAlex
    keyword search must collapse to one record keyed by the OpenAlex ID."""
    from carrel.sources.arxiv import ArxivEntry

    entry = ArxivEntry(
        arxiv_id="2401.00001", title="Same Paper", summary="abs",
        authors=["A"], categories=[], updated="2026-08-01T00:00:00Z",
        abs_url="https://arxiv.org/abs/2401.00001",
        pdf_url="https://arxiv.org/pdf/2401.00001",
    )
    oa_work = {
        "id": "https://openalex.org/W999",
        "title": "Same Paper",
        "doi": "https://doi.org/10.48550/arXiv.2401.00001",
        "publication_date": "2026-08-01",
        "abstract_inverted_index": None,
        "open_access": {"is_oa": True},
        "best_oa_location": {
            "pdf_url": "https://arxiv.org/pdf/2401.00001.pdf",
            "landing_page_url": "https://arxiv.org/abs/2401.00001",
        },
        "locations": [],
        "primary_location": {"source": {"display_name": "arXiv"}},
        "authorships": [],
    }

    subs = [_sub("keyword", "same paper")]
    with patch("carrel.pipeline.runner.arxiv_src.fetch_recent", return_value=[entry]), \
         patch("carrel.pipeline.runner.oa.lookup_by_arxiv_id", return_value=None), \
         patch("carrel.pipeline.runner.oa.fetch_recent_by_keyword", return_value=[oa_work]), \
         patch("carrel.pipeline.runner.oa.configure"):
        records, errors = fetch_candidates(cfg, subs, lookback_hours=24)

    assert errors == {}
    ids = {r.id for r in records}
    assert ids == {"W999"}, f"expected single canonical record, got {ids}"


# ---- select_missing_references backfill selector ----

def test_select_missing_references_picks_count_without_list(session):
    from carrel.pipeline.citations import select_missing_references

    # stale: count set, references NULL, has identifier -> selected
    stale = Paper(id="W_stale", id_kind="openalex", title="Stale",
                  reference_count=51, references=None, doi="10.1/stale")
    # already backfilled: references present -> skipped
    filled = Paper(id="W_filled", id_kind="openalex", title="Filled",
                   reference_count=10, references=[{"title": "x"}], doi="10.1/filled")
    # genuinely empty after fetch: references == [] -> skipped (not NULL)
    emptied = Paper(id="W_empty", id_kind="openalex", title="Empty",
                    reference_count=3, references=[], doi="10.1/empty")
    # no count yet -> skipped
    nocnt = Paper(id="W_nocnt", id_kind="openalex", title="NoCount",
                  reference_count=None, references=None, doi="10.1/nocnt")
    # count but no resolvable identifier -> skipped
    noid = Paper(id="W_noid", id_kind="openalex", title="NoId",
                 reference_count=7, references=None,
                 doi=None, arxiv_id=None, s2_paper_id=None)
    session.add_all([stale, filled, emptied, nocnt, noid])
    session.commit()

    found = select_missing_references(session, limit=50)
    assert [p.id for p in found] == ["W_stale"]
