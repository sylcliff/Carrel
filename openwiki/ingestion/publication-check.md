---
type: pipeline
title: arXiv-to-journal publication check
description: Detects when an arXiv preprint has been formally published, records the journal DOI, keeps the journal PDF alongside the arXiv PDF, promotes and re-parses it when the institutional downloader can fetch it.
tags: [publication-check, arxiv, journal-doi, remote-download, pdf]
---

# arXiv-to-journal publication check

`carrel/pipeline/publication_check.py` watches old arXiv papers in the
library for a peer-reviewed journal version. It runs as a scheduled job
(`publication_check`, Mondays 10:00 by default) and as a per-paper API
endpoint (`POST /papers/{id}/check-publication`).

## Selection

`select_candidates(session, limit, min_age_days=180, throttle_days=30)`
returns arXiv papers that:

- have an `arxiv_id` and no `journal_doi`,
- are in the library and not discarded,
- have an arXiv `<published>` date (or `publication_date`) at least
  `min_age_days` old,
- have not been checked in the last `throttle_days` (or never) based on
  `published_checked_at`.

## Detection

`detect_publication(paper, min_age_days)` is metadata-only:

1. Refetch the arXiv Atom record to read the authoritative first-version
   `<published>` date. Papers younger than `min_age_days` short-circuit
   with `reason="too young"` without hitting S2/OA.
2. Query Semantic Scholar at `ARXIV:<bare-id>` as the primary source
   (`publicationVenue.type` is the journal signal). A non-arXiv DOI whose
   venue type is in `JOURNAL_VENUE_TYPES = {"journal", "conference",
   "book series", "book"}` (or whose venue name does not contain "arxiv")
   returns `PublicationInfo(found=True, source="semanticscholar", ...)`.
3. Fall back to OpenAlex by arXiv id (with title hint). The same
   venue-type/name test applies against `primary_location.source.type`.
4. Otherwise `found=False, reason="no journal DOI found"` (or
   `"not an arXiv paper"`).

Detection runs even without the institutional SSH host configured — only
the PDF fetch needs it.

## Applying a result

When a journal DOI is found, the pipeline:

1. Records `journal_doi` and `published_checked_at` on the Paper.
2. If the institutional downloader is configured, tries to download the
   journal PDF via the SSH jump host (see
   [pdf-processing.md](pdf-processing.md#institutional-ssh-fallback)).
3. On a successful fetch, **keeps both PDFs on disk**:
   - `arxiv.pdf` — backup of the active `paper.pdf` (the arXiv version).
   - `journal.pdf` — the newly downloaded file.
   - `paper.pdf` is overwritten with the journal version.
   - The map is stored in `Paper.pdf_files` (e.g.
     `{"arxiv": "papers/<slug>/arxiv.pdf",
     "journal": "papers/<slug>/journal.pdf"}`).
   - `pdf_origin="journal"`.
4. Clears parse artifacts: deletes the old `paper.md`, any `images/`,
   and all `Chunk` rows for the paper, then re-invokes
   `process.process_paper` to parse and (best-effort) summarize the
   journal version.

### Safety invariants

- If the journal PDF fetch fails, the arXiv version stays active; the
  detected `journal_doi` is still recorded so the UI can show it and a
  later run can retry without re-detecting.
- Files are swapped only after a successful download + validation:
  download → validate `%PDF-` → back up arXiv → overwrite `paper.pdf` →
  only then clear parse artifacts. The download is written to
  `journal.pdf.tmp` first and renamed into place.
- `published_checked_at` is always bumped (success or "not found") so
  the throttle is respected.

## Entry points

- `check_pending(session, cfg, limit)` — the scheduler body; selects
  candidates, detects, and applies for each. Returns
  `{candidates, found, parsed, failed}`.
- `fill_closed_papers(session, cfg, limit)` — sibling function used by
  the `remote_fill` scheduled job; finds library papers with no
  open-access PDF and tries the institutional SSH downloader without
  the publication-detection step.
- `POST /papers/{paper_id}/check-publication` in
  `carrel/api/publication.py` wraps a single paper in one
  `Job(kind='publication_check')` and supports inline/background
  execution like the other per-paper endpoints.

## Configuration

- `schedule.publication_check_enabled` / `publication_check_cron`
  (default `0 10 * * 1`, off by default).
- `EnvSettings.remote_journal_min_age_days` (default 180).
- `EnvSettings.remote_journal_check_throttle_days` (default 30).
- Institutional SSH settings are shared with the remote-fill fallback
  (`remote_ssh_*`, `remote_command_template`, etc.) — see
  [../architecture/configuration.md](../architecture/configuration.md).

## Focused tests

- `tests/test_publication_check.py` — age gate, S2 and OA detection
  paths, arXiv-DOI rejection, file-swap ordering, failure leaves arXiv
  version active, throttle semantics.
- `tests/test_publication_api.py` — `POST /papers/{id}/check-publication`
  job creation and the inline/background contract.

## Validation

```bash
.venv/bin/python -m pytest tests/test_publication_check.py tests/test_publication_api.py -q
```

## Evidence

- Pipeline: `carrel/pipeline/publication_check.py`.
- API: `carrel/api/publication.py`.
- Scheduler wiring: `carrel/scheduler.py` (`_scheduled_publication_check`).
- Remote downloader: `carrel/sources/remote_downloader.py`.
- Process pipeline for re-parse: `carrel/pipeline/process.py`; see
  [pdf-processing.md](pdf-processing.md).
- Data model: `Paper.journal_doi`, `Paper.pdf_files`,
  `Paper.pdf_origin`, `Paper.published_checked_at` in
  [../architecture/data-model.md](../architecture/data-model.md).
