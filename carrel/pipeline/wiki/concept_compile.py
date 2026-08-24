"""Compile a concept wiki page from per-paper extractions (M8b).

For each normalized term the LLM extracted from at least one paper body, this
synthesizes a Markdown page that names the concept, summarizes how the library
talks about it, and links back to the contributing papers.  Inputs come from
:mod:`carrel.pipeline.paper_extract` (rows in ``paper_concepts``); the compiler
itself never re-reads paper text.
"""
from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from carrel import embeddings, llm, usage
from carrel.config import CarrelYAML
from carrel.models import WikiKind, WikiPage, WikiSource
from carrel.pipeline.summarize import _prepare_body
from carrel.pipeline.wiki import _frontmatter, _merge, _reindex, _slug
from carrel.pipeline.wiki._concepts_agg import (
    ConceptCandidate,
    EVIDENCE_THRESHOLD,
    aggregate,
    papers_for_term,
)
from carrel.pipeline.wiki._scholars_agg import (
    NAME_KEY_PREFIX,
    author_key,
)

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[dict], None]

COMPILER_VERSION = 1
_MAX_PAPERS = 25
_MAX_INPUT_CHARS = 8_000
_EMBED_CHARS = 1500


class ConceptError(Exception):
    """Concept compilation failed (no key, bad LLM output, IO error, etc.)."""


_SYSTEM_PROMPT = (
    "You are an expert research librarian writing a concept page for a "
    "personal research library. A concept is a technical term that "
    "recurs across multiple papers (e.g. Retrieval-Augmented Generation).\n"
    "Rules:\n"
    "- Ground every claim in the supplied paper snippets.\n"
    "- summary: one short paragraph (≤ 80 words) describing the concept.\n"
    "- tags: 3-8 short lowercase topic tags.\n"
    "- confidence: your estimate 0..1 (capped by evidence quantity later).\n"
    "- Respond with ONLY a JSON object of the form: "
    '{"summary": "...", "tags": [...], "confidence": 0.0}'
)


def _summarize_compile_io(data: dict, body: str) -> str:
    """Compact summary for the stepper IO panel."""
    summary = (data.get("summary") or "").strip()
    if summary:
        lines = [f"summary: {summary}"]
        for key in ("definition", "mechanism", "evidence", "open_questions"):
            v = data.get(key)
            if isinstance(v, list):
                for item in v[:3]:
                    if isinstance(item, str) and item.strip():
                        lines.append(f"{key}: {item.strip()}")
        return "\n".join(lines)
    # Fallback: first non-heading line of the body.
    for line in body.splitlines():
        s = line.strip()
        if s and not s.startswith("#") and not s.startswith("---"):
            return s
    return "(empty)"


def _paper_snippet(idx: int, paper: Any) -> str:
    bits = [f"[{idx}] {paper.title}"]
    venue = paper.venue or "unknown venue"
    year = paper.publication_date.year if paper.publication_date else "n.d."
    bits.append(f"    Venue/year: {venue} · {year}")
    body = paper.tldr_en or paper.abstract or ""
    if body:
        bits.append(f"    Abstract: {_prepare_body(body, 1000)}")
    return "\n".join(bits)


def _build_user_prompt(*, term_display, papers, old_body):
    parts = [f"Concept: {term_display}"]
    parts.append(
        f"Library papers mentioning this concept ({len(papers)} shown, newest first):"
    )
    parts.extend(_paper_snippet(i, p) for i, p in enumerate(papers, start=1))
    if old_body:
        parts.append(
            "Previous version of this page (revise and update):\n"
            + _prepare_body(old_body, 2500)
        )
    parts.append("\nReturn the JSON object now, with no commentary.")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _related_scholars(
    session: Session, papers: list[Any]
) -> list[tuple[str, str]]:
    """Return ``[(name, slug), ...]`` for scholars who authored these papers.

    Walks the author lists of the given papers, groups by aggregation key
    (A-ID when present, else name), and returns the most-frequent
    display name per key, sorted by paper-count desc then name.  ``slug``
    is what :func:`_slug.scholar_slug` produces so it matches the
    on-disk page address.  The same merge semantics as
    :func:`_scholars_agg.aggregate` apply, so an author with an A-ID in
    some papers and without in others collapses to a single entry.
    """
    from collections import Counter
    if not papers:
        return []
    name_counts: dict[str, Counter] = {}
    paper_counts: dict[str, set[str]] = {}
    for p in papers:
        for a in p.authors or []:
            if not isinstance(a, dict):
                continue
            key = author_key(a, session)
            if not key:
                continue
            name = (a.get("name") or "").strip()
            if not name:
                continue
            name_counts.setdefault(key, Counter())[name] += 1
            paper_counts.setdefault(key, set()).add(p.id)
    rows: list[tuple[int, str, str, str]] = []
    for key, counts in name_counts.items():
        aid = None if key.startswith(NAME_KEY_PREFIX) else key
        display = counts.most_common(1)[0][0]
        rows.append((
            -len(paper_counts.get(key, set())),
            display.lower(),
            display,
            _slug.scholar_slug(aid, display),
        ))
    rows.sort()
    return [(name, slug) for _neg, _lname, name, slug in rows[:_MAX_RELATED_SCHOLARS]]


# Cap on the deterministic "Related scholars" footer so a broad concept
# (e.g. "machine learning") does not bloat the page. 10 is enough to
# surface the principal contributors; the long tail is searchable
# via the /scholars browse.
_MAX_RELATED_SCHOLARS = 10


def _render_live_body(
    *,
    term_display,
    data,
    papers,
    related_scholars=None,
):
    summary = str(data.get("summary") or "").strip()
    lines = [f"# {term_display}", ""]
    if summary:
        lines += ["## Summary", summary, ""]
    if related_scholars:
        rendered = [
            f"- [[{name}]](../scholars/{slug}.md)"
            for name, slug in related_scholars
        ]
        lines += ["## Related scholars", "\n".join(rendered), ""]
    if papers:
        lines += ["## Sources", ""]
        for i, p in enumerate(papers, start=1):
            year = p.publication_date.year if p.publication_date else "n.d."
            lines.append(f"[^{i}]: [{p.title}](/papers/{p.id}) ({year})")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_stub_body(*, term_display, paper_count):
    # Stubs are written only when there is no evidence at all (defensive:
    # aggregation requires ≥1 backing paper, so a real stub with paper_count=0
    # should not happen in practice).  We keep this path so a future
    # zero-evidence entity is never an LLM call.
    noun = "paper" if paper_count == 1 else "papers"
    return (
        f"# {term_display}\n\n"
        f"_Not enough evidence to compile yet "
        f"(currently {paper_count} {noun})._\n"
    )


# ---------------------------------------------------------------------------
# Confidence
# ---------------------------------------------------------------------------


def _confidence(model_value, *, evidence_count):
    try:
        conf = float(model_value)
    except (TypeError, ValueError):
        conf = 0.4
    conf = max(0.0, min(1.0, conf))
    # M8c: with the evidence threshold dropped to 1, a single-paper concept
    # still gets a live LLM-compiled page; we no longer cap single-paper
    # confidence artificially low — let the LLM's own self-rating stand.
    # For 2-5 papers, gently cap so a single rogue high score can't dominate.
    if evidence_count <= 5:
        ceiling = 0.85
    else:
        ceiling = 0.95
    return round(min(conf, ceiling), 3)


# ---------------------------------------------------------------------------
# Atomic write + DB upsert
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".wiki-", suffix=".md", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _evidence_hash(paper_ids):
    return hashlib.sha256(",".join(sorted(paper_ids)).encode("utf-8")).hexdigest()


def _embed_page(cfg, title, summary, tags):
    text = " ".join([title, summary, " ".join(tags)]).strip()[:_EMBED_CHARS]
    if not text:
        return None
    try:
        vecs = embeddings.embed_texts([text], model=cfg.embeddings.model)
        return vecs[0] if vecs else None
    except Exception as e:  # noqa: BLE001
        logger.warning("wiki concept embed failed: %s", e)
        return None


def _existing_page(session, slug):
    return session.exec(
        select(WikiPage).where(
            WikiPage.kind == WikiKind.concept.value,
            WikiPage.slug == slug,
            WikiPage.redirects_to.is_(None),
        )
    ).first()


def _candidate_for_term(candidates, term_normalized):
    for c in candidates:
        if c.term_normalized == term_normalized:
            return c
    return None


# ---------------------------------------------------------------------------
# Per-concept compile
# ---------------------------------------------------------------------------


def compile_concept(
    session: Session,
    cfg: CarrelYAML,
    term_normalized: str,
    *,
    force: bool = False,
    on_progress: ProgressCallback | None = None,
) -> WikiPage:
    """Compile (or recompile) one concept's wiki page.  Raises ConceptError."""
    candidates = aggregate(session)
    candidate = _candidate_for_term(candidates, term_normalized)
    if candidate is None:
        raise ConceptError(f"concept not found in library: {term_normalized!r}")

    slug = _slug.slugify(candidate.term_display)
    rel_path = _slug.page_path(WikiKind.concept.value, slug)
    full_path = Path(cfg.storage.root) / rel_path

    papers = papers_for_term(session, term_normalized)
    if not papers:
        raise ConceptError(f"no in-library papers for {term_normalized!r}")

    def _emit(**progress):
        if on_progress is not None:
            on_progress({
                "term": term_normalized,
                "title": candidate.term_display,
                "stage": "concept_compile",
                **progress,
            })

    evidence_count = len(papers)
    evidence_hash = _evidence_hash([p.id for p in papers])
    existing = _existing_page(session, slug)

    # Hash-skip: if the existing live page already has the same evidence
    # hash, the page is up to date — no LLM call, no file write.
    if (
        not force
        and existing is not None
        and existing.compiled_at is not None
        and not existing.stub
    ):
        prev_hash_from_file = None
        if full_path.exists():
            try:
                meta, _body = _frontmatter.parse(
                    full_path.read_text(encoding="utf-8")
                )
                prev_hash_from_file = meta.get("evidence_hash")
            except OSError:
                prev_hash_from_file = None
        if prev_hash_from_file == evidence_hash:
            _emit(detail="Up to date")
            return existing

    # Stub path — below the evidence threshold we never call the LLM.
    if evidence_count < EVIDENCE_THRESHOLD:
        _emit(detail=f"Below threshold ({evidence_count}); writing stub")
        return _write_stub(
            session, cfg, slug, candidate.term_display,
            evidence_count, evidence_hash, papers,
        )

    if not (
        llm.has_key_for(cfg.llm.summarize_model)
        or llm.has_key_for(cfg.llm.fallback_model)
    ):
        raise ConceptError(
            "no LLM API key configured (set DEEPSEEK_API_KEY or VOLCANO_API_KEY)"
        )

    # Previous compiled prose (without the user section) for revision context.
    old_body = None
    if existing and full_path.exists():
        try:
            _old_meta, old_full = _frontmatter.parse(full_path.read_text(encoding="utf-8"))
            old_body = _merge.extract_user_section(old_full) or old_full
        except OSError:
            old_body = None

    prompt = _build_user_prompt(
        term_display=candidate.term_display,
        papers=papers[:_MAX_PAPERS],
        old_body=old_body,
    )
    prompt = prompt[:_MAX_INPUT_CHARS + 2000]

    _emit(detail=f"Synthesizing {candidate.term_display}…")
    try:
        data = llm.chat_json(
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            model=cfg.llm.summarize_model,
            fallback_model=cfg.llm.fallback_model,
            temperature=cfg.llm.temperature,
            timeout=cfg.llm.request_timeout_seconds,
            feature="wiki_concept",
            on_usage=usage.make_usage_callback(session, feature="wiki_concept"),
        )
    except llm.LLMError as e:
        raise ConceptError(str(e)) from e
    if not isinstance(data, dict) or not data.get("summary"):
        raise ConceptError("LLM returned no usable concept summary")

    body = _render_live_body(
        term_display=candidate.term_display,
        data=data,
        papers=papers,
        related_scholars=_related_scholars(session, papers),
    )

    # Preserve any prior user-authored section.
    old_text = full_path.read_text(encoding="utf-8") if full_path.exists() else None
    body = _merge.protect_user_section(old_text, body)

    tags = [str(t).strip().lower() for t in (data.get("tags") or []) if str(t).strip()]
    confidence = _confidence(data.get("confidence"), evidence_count=evidence_count)

    now = datetime.now(UTC)
    meta = {
        "kind": WikiKind.concept.value,
        "title": candidate.term_display,
        "slug": slug,
        "entity_key": f"concept:{slug}",
        "compiled_at": now.isoformat(),
        "compiler_version": COMPILER_VERSION,
        "confidence": confidence,
        "evidence_count": evidence_count,
        "source_paper_ids": [p.id for p in papers],
        "tags": tags,
        "evidence_hash": evidence_hash,
    }

    text = _frontmatter.dump(meta, body)
    _atomic_write(full_path, text)

    # Upsert the index row from what we just wrote.
    page = _reindex.upsert_page_from_disk(
        session, cfg, WikiKind.concept.value, slug
    )
    if page is None:
        raise ConceptError(f"failed to index written page: {rel_path}")
    if page.id is not None:
        for old in session.exec(
            select(WikiSource).where(WikiSource.wiki_page_id == page.id)
        ).all():
            session.delete(old)
        session.flush()
        for p in papers:
            session.add(
                WikiSource(
                    wiki_page_id=page.id,
                    paper_id=p.id,
                    chunk_id=None,
                    heading="abstract",
                    quote=(p.tldr_en or p.abstract or "")[:1000] or None,
                    role="context",
                )
            )
        vec = _embed_page(
            cfg, candidate.term_display, str(data.get("summary") or ""), tags
        )
        if vec is not None:
            page.embedding = vec
        page.confidence = confidence
        page.evidence_count = evidence_count
        page.stub = False
        session.add(page)
        session.commit()

    _reindex.recompute_backlinks(session)
    _emit(
        detail=f"Compiled {candidate.term_display}",
        io={
            "input": prompt,
            "output": _summarize_compile_io(data, body),
        },
    )
    logger.info("compiled concept page %s (%s)", slug, candidate.term_display)
    return page


# ---------------------------------------------------------------------------
# Stub helpers
# ---------------------------------------------------------------------------


def _write_stub(
    session,
    cfg,
    slug,
    term_display,
    evidence_count,
    evidence_hash,
    papers,
) -> WikiPage:
    rel_path = _slug.page_path(WikiKind.concept.value, slug)
    full_path = Path(cfg.storage.root) / rel_path
    body = _render_stub_body(term_display=term_display, paper_count=evidence_count)
    body = _merge.protect_user_section(
        full_path.read_text(encoding="utf-8") if full_path.exists() else None,
        body,
    )
    now = datetime.now(UTC)
    meta = {
        "kind": WikiKind.concept.value,
        "title": term_display,
        "slug": slug,
        "entity_key": f"concept:{slug}",
        "compiled_at": now.isoformat(),
        "compiler_version": COMPILER_VERSION,
        "confidence": 0.0,
        "evidence_count": evidence_count,
        "source_paper_ids": [p.id for p in papers],
        "tags": [],
        "evidence_hash": evidence_hash,
        "stub": True,
    }
    text = _frontmatter.dump(meta, body)
    _atomic_write(full_path, text)
    page = _reindex.upsert_page_from_disk(
        session, cfg, WikiKind.concept.value, slug
    )
    if page is not None:
        page.stub = True
        page.evidence_count = evidence_count
        session.add(page)
        session.commit()
    if page is None:
        raise ConceptError(f"failed to index written stub: {rel_path}")
    return page


# ---------------------------------------------------------------------------
# Staleness + batch
# ---------------------------------------------------------------------------


def select_stale_concepts(session, *, limit: int = 20) -> list[str]:
    """Return concept terms that need (re)compilation.

    A concept is stale when:
      * no WikiPage row exists, OR
      * the page is a stub AND the paper set is now at/above the
        threshold (promote to live), OR
      * ``latest_paper_update`` exceeds ``page.compiled_at``.

    Sorted by paper-count desc so the most-evidenced concepts compile
    first.
    """
    candidates = aggregate(session)
    page_by_slug = {}
    for page in session.exec(
        select(WikiPage).where(
            WikiPage.kind == WikiKind.concept.value,
            WikiPage.redirects_to.is_(None),
        )
    ).all():
        if page.slug:
            page_by_slug[page.slug] = page

    stale = []
    for c in candidates:
        slug = _slug.slugify(c.term_display)
        page = page_by_slug.get(slug)
        if page is None:
            stale.append((c.paper_count, c.term_normalized))
            continue
        if page.stub and c.paper_count >= EVIDENCE_THRESHOLD:
            # Stub promotion — threshold now met, recompile.
            stale.append((c.paper_count, c.term_normalized))
            continue
        if page.compiled_at is not None and c.latest_paper_update is not None:
            if c.latest_paper_update > page.compiled_at:
                stale.append((c.paper_count, c.term_normalized))
    stale.sort(key=lambda t: (-t[0], t[1]))
    return [term for _count, term in stale[:limit]]


def compile_concepts_pending(
    session: Session,
    cfg: CarrelYAML,
    *,
    limit: int = 20,
    force: bool = False,
    on_progress: ProgressCallback | None = None,
) -> dict[str, int]:
    """Compile stale concept pages; returns counts."""
    # Reconcile the concept catalog against the live aggregation.  Today
    # concepts have no alias layer, so this only catches "page exists at
    # (kind, slug) but the aggregation no longer surfaces the term" —
    # we leave those alone for ops.  Cheap to run; failure is non-fatal.
    try:
        from carrel.pipeline.wiki._entities import reconcile_kind
        from carrel.pipeline.wiki._concepts_agg import enumerate_entities
        reconcile_kind(
            session, kind="concept", enumerate_fn=enumerate_entities
        )
    except Exception:
        logger.exception("concept compile: reconcile failed (continuing)")

    terms = select_stale_concepts(session, limit=limit)
    counts = {
        "candidates": len(terms),
        "compiled": 0,
        "stubbed": 0,
        "failed": 0,
    }
    total = len(terms)

    def _wrap(i, term, title):
        def _cb(progress):
            if on_progress is not None:
                on_progress({**progress, "index": i, "total": total, "name": title})
        return _cb

    candidates = aggregate(session)
    for i, term in enumerate(terms, start=1):
        cand = _candidate_for_term(candidates, term)
        label = cand.term_display if cand else term
        try:
            page = compile_concept(
                session, cfg, term, force=force, on_progress=_wrap(i, term, label)
            )
            if page.stub:
                counts["stubbed"] += 1
            else:
                counts["compiled"] += 1
        except ConceptError as e:
            logger.info("concept %s failed: %s", term, e)
            counts["failed"] += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("concept %s crashed: %s", term, e)
            counts["failed"] += 1

    logger.info(
        "concept wiki batch done: candidates=%d compiled=%d stubbed=%d failed=%d",
        counts["candidates"], counts["compiled"], counts["stubbed"], counts["failed"],
    )
    return counts

