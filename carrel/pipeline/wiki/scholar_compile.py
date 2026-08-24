"""Compile a scholar wiki page from in-library paper metadata (M8a).

For each scholar (an author aggregated across the library's papers, keyed by
OpenAlex A-ID or ``name:<name>``), this synthesizes a Markdown page from
metadata and abstracts/tldrs only — it never reads the parsed PDF, so
metadata-only papers are fully covered and the LLM input stays small and cheap.

Design mirrors :mod:`carrel.pipeline.topics`:
  * **Reuses** :func:`carrel.llm.chat_json` / :func:`carrel.embeddings.embed_texts`.
  * **Idempotent** — a scholar with no newer papers is a no-op unless
    ``force=True``. Staleness is derived from ``Paper.updated_at`` vs
    ``WikiPage.compiled_at`` (no new per-paper queue column).
  * **Non-fatal** — failures raise :class:`ScholarError` and are caught per
    scholar by the batch driver.
  * **User sections preserved** — the compiler owns everything outside
    ``<section data-user="true">``; that block is spliced verbatim across
    recompiles.

The Markdown file on disk is the source of truth; the :class:`WikiPage` row
and :class:`WikiSource` rows are an index + per-paper provenance map rebuilt by
:mod:`carrel.pipeline.wiki._reindex`.
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
from carrel.pipeline.wiki._scholars_agg import (
    NAME_KEY_PREFIX,
    aggregate,
    get_profile,
    papers_for_key,
)

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[dict], None]

COMPILER_VERSION = 1
_MAX_PAPERS = 25
_MAX_INPUT_CHARS = 10_000
_EMBED_CHARS = 1500


class ScholarError(Exception):
    """Scholar compilation failed (no key, bad LLM output, IO error, etc.)."""


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are an expert research librarian writing a scholarly profile page "
    "about one researcher, based ONLY on the metadata and abstracts of papers "
    "by that researcher held in a personal library. Write in clear English.\n"
    "Rules:\n"
    "- Ground every claim in the supplied abstracts; never invent numbers, "
    "awards, or publications. Cite each synthesized claim with a footnote "
    "marker [^<n>] where <n> is the 1-based index of the supporting paper in "
    "the list.\n"
    "- Focus on research THEMES, how the work evolved over time, recurring "
    "methods or problems, and (only when the abstracts clearly show it) how "
    "the researcher's views or approach shifted.\n"
    "- For collaborators you can identify by name from the co-author lists, "
    'give key_collaborators as [{name, aid (OpenAlex A-ID if shown, else ""), '
    "reason}].\n"
    "- Do NOT propose concept links or open questions — those are derived "
    "deterministically from the per-paper extraction tables, not from your "
    "JSON response.\n"
    "- tags: 3-8 short lowercase topic tags.\n"
    "- confidence: your estimate 0..1, but it is only a starting point and is "
    "capped by evidence quantity later.\n"
    "- Respond with ONLY a JSON object, no prose or markdown fences, of the "
    'form: {"summary": "...", "research_lines": ["...", "..."], '
    '"trajectory": "...markdown...", "evolving_views": "...markdown or empty...", '
    '"key_collaborators": [...], '
    '"tags": [...], "confidence": 0.0}'
)


def _summarize_scholar_io(data: dict, body: str) -> str:
    """Compact summary of the LLM result + first lines of the rendered body."""
    bits: list[str] = []
    summary = (data.get("summary") or "").strip()
    if summary:
        bits.append(f"summary: {summary}")
    lines = (data.get("research_lines") or [])[:3]
    for line in lines:
        if line:
            bits.append(f"line: {line}")
    if not bits:
        # Fall back to the first 3 non-empty lines of the rendered markdown.
        for line in body.splitlines():
            s = line.strip()
            if s and not s.startswith("#") and not s.startswith("---"):
                bits.append(s)
                if len(bits) >= 3:
                    break
    return "\n".join(bits) if bits else "(empty)"


def _paper_snippet(idx: int, paper: Any) -> str:
    """One paper's metadata + tldr/abstract as a numbered prompt block."""
    bits = [f"[{idx}] {paper.title}"]
    venue = paper.venue or "unknown venue"
    year = paper.publication_date.year if paper.publication_date else "n.d."
    bits.append(f"    Venue/year: {venue} · {year}")
    coauthors = [
        a.get("name", "")
        for a in (paper.authors or [])
        if isinstance(a, dict) and a.get("name")
    ]
    if coauthors:
        bits.append(f"    Co-authors: {', '.join(coauthors[:12])}")
    if paper.keywords:
        bits.append(f"    Keywords: {', '.join(paper.keywords[:12])}")
    body = paper.tldr_en or paper.abstract or ""
    if body:
        bits.append(f"    Abstract: {_prepare_body(body, 1200)}")
    return "\n".join(bits)


def _build_user_prompt(
    *,
    name: str,
    affiliation: str | None,
    profile: Any,
    papers: list[Any],
    old_body: str | None,
) -> str:
    parts = [f"Researcher: {name}"]
    if affiliation:
        parts.append(f"Affiliation: {affiliation}")
    if profile is not None:
        prof_bits = []
        if getattr(profile, "works_count", None):
            prof_bits.append(f"works_count={profile.works_count}")
        if getattr(profile, "h_index", None):
            prof_bits.append(f"h_index={profile.h_index}")
        if getattr(profile, "cited_by_count", None):
            prof_bits.append(f"cited_by_count={profile.cited_by_count}")
        if prof_bits:
            parts.append("OpenAlex profile: " + ", ".join(prof_bits))
    parts.append(
        f"Library papers by this researcher ({len(papers)} shown, newest first):"
    )
    parts.extend(_paper_snippet(i, p) for i, p in enumerate(papers, start=1))
    if old_body:
        parts.append(
            "Previous version of this page's compiled section (revise and "
            "update; keep what still holds, incorporate newer papers):\n"
            + _prepare_body(old_body, 2500)
        )
    parts.append("\nReturn the JSON object now, with no commentary.")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _md_list(items: list[str]) -> str:
    return "\n".join(f"- {x}" for x in items) if items else "_None identified._"


def _collab_link(name: str, aid: str) -> str:
    slug = _slug.scholar_slug(aid or None, name)
    return f"[[{name}]](../scholars/{slug}.md)"


def _concept_link(term: str) -> str:
    return f"[[{term}]](../concepts/{_slug.slugify(term)}.md)"


def _question_link(question: str) -> str:
    return f"[[{question}]](../questions/{_slug.slugify(question)}.md)"


# Cap on the deterministic link sections so a prolific scholar does not bloat
# the page. 10 each is plenty for a navigation footer; the long tail is on
# the concept/question pages themselves.
_MAX_DETERMINISTIC_LINKS = 10


def _concepts_for_papers(
    session: Session, paper_ids: list[str]
) -> list[tuple[str, int]]:
    """Return ``[(term_display, paper_count)]`` for concepts mentioned by these papers.

    Walks :class:`carrel.models.PaperConcept` joined to the given paper set,
    groups by ``term_normalized``, picks the most-common ``term_display`` per
    group, and returns the top entries by paper count desc then term. The
    result is the deterministic "Related concepts" footer of a scholar page.
    """
    from collections import Counter
    from carrel.models import PaperConcept
    if not paper_ids:
        return []
    rows = session.exec(
        select(PaperConcept).where(PaperConcept.paper_id.in_(paper_ids))
    ).all()
    displays: dict[str, Counter] = {}
    counts: dict[str, set[str]] = {}
    for pc in rows:
        tn = pc.term_normalized
        if not tn:
            continue
        displays.setdefault(tn, Counter())[pc.term_display or tn] += 1
        counts.setdefault(tn, set()).add(pc.paper_id)
    out: list[tuple[str, int]] = []
    for tn, paper_set in counts.items():
        display = displays[tn].most_common(1)[0][0]
        out.append((display, len(paper_set)))
    out.sort(key=lambda t: (-t[1], t[0]))
    return out[:_MAX_DETERMINISTIC_LINKS]


def _questions_for_papers(
    session: Session, paper_ids: list[str]
) -> list[tuple[str, int]]:
    """Return ``[(question_display, paper_count)]`` for questions raised by these papers.

    Mirror of :func:`_concepts_for_papers` for :class:`carrel.models.PaperQuestion`.
    The result is the deterministic "Open questions" footer of a scholar page.
    """
    from collections import Counter
    from carrel.models import PaperQuestion
    if not paper_ids:
        return []
    rows = session.exec(
        select(PaperQuestion).where(PaperQuestion.paper_id.in_(paper_ids))
    ).all()
    displays: dict[str, Counter] = {}
    counts: dict[str, set[str]] = {}
    for pq in rows:
        qn = pq.question_normalized
        if not qn:
            continue
        displays.setdefault(qn, Counter())[pq.question_display or qn] += 1
        counts.setdefault(qn, set()).add(pq.paper_id)
    out: list[tuple[str, int]] = []
    for qn, paper_set in counts.items():
        display = displays[qn].most_common(1)[0][0]
        out.append((display, len(paper_set)))
    out.sort(key=lambda t: (-t[1], t[0]))
    return out[:_MAX_DETERMINISTIC_LINKS]


def _render_body(
    *,
    name: str,
    data: dict[str, Any],
    papers: list[Any],
    profile: Any,
    related_concepts: list[tuple[str, int]] | None = None,
    open_questions: list[tuple[str, int]] | None = None,
) -> str:
    """Assemble the compiled Markdown body (title + sections + sources)."""
    summary = str(data.get("summary") or "").strip()
    research_lines = [str(x).strip() for x in (data.get("research_lines") or []) if str(x).strip()]
    trajectory = str(data.get("trajectory") or "").strip()
    evolving = str(data.get("evolving_views") or "").strip()

    lines: list[str] = [f"# {name}", ""]

    if summary:
        lines += ["## Summary", summary, ""]

    if research_lines:
        lines += ["## Research lines", _md_list(research_lines), ""]

    if trajectory:
        lines += ["## Research trajectory", trajectory, ""]

    if evolving:
        lines += ["## Evolving views", evolving, ""]

    collabs = data.get("key_collaborators") or []
    if isinstance(collabs, list) and collabs:
        rendered = []
        for c in collabs:
            if not isinstance(c, dict):
                continue
            cname = str(c.get("name") or "").strip()
            if not cname:
                continue
            aid = str(c.get("aid") or "").strip()
            reason = str(c.get("reason") or "").strip()
            link = _collab_link(cname, aid)
            rendered.append(f"- {link}" + (f" — {reason}" if reason else ""))
        if rendered:
            lines += ["## Key collaborations", "\n".join(rendered), ""]

    concepts = related_concepts or []
    if concepts:
        rendered = [
            f"- {_concept_link(term)} — {n} paper{'s' if n != 1 else ''}"
            for term, n in concepts
        ]
        lines += ["## Related concepts", "\n".join(rendered), ""]

    questions = open_questions or []
    if questions:
        rendered = [
            f"- {_question_link(qtext)} — {n} paper{'s' if n != 1 else ''}"
            for qtext, n in questions
        ]
        lines += ["## Open questions", "\n".join(rendered), ""]

    # Sources: footnote definitions linking back to each paper.
    if papers:
        lines += ["## Sources", ""]
        for i, p in enumerate(papers, start=1):
            year = p.publication_date.year if p.publication_date else "n.d."
            lines.append(f"[^{i}]: [{p.title}](/papers/{p.id}) ({year})")
        lines.append("")

    if profile is not None and (
        getattr(profile, "works_count", None)
        or getattr(profile, "h_index", None)
    ):
        bits = []
        if getattr(profile, "works_count", None):
            bits.append(f"{profile.works_count} OpenAlex works")
        if getattr(profile, "h_index", None):
            bits.append(f"h-index {profile.h_index}")
        if getattr(profile, "cited_by_count", None):
            bits.append(f"{profile.cited_by_count:,} citations")
        if bits:
            lines += [
                "---",
                f"<small>OpenAlex: {', '.join(bits)} (library subset may differ).</small>",
                "",
            ]

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Confidence
# ---------------------------------------------------------------------------


def _confidence(
    model_value: Any,
    *,
    evidence_count: int,
    has_aid: bool,
    sections_with_citations: int,
    sections_total: int,
) -> float:
    try:
        conf = float(model_value)
    except (TypeError, ValueError):
        conf = 0.4
    conf = max(0.0, min(1.0, conf))

    # Evidence ceilings: corroboration across distinct papers is the hard cap.
    if evidence_count < 3:
        ceiling = 0.45
    elif evidence_count <= 5:
        ceiling = 0.7
    else:
        ceiling = 0.9
    conf = min(conf, ceiling)

    if not has_aid:
        conf *= 0.8  # name-only identity is ambiguous.
    if sections_total and sections_with_citations < sections_total:
        conf -= 0.1  # a major section has no citation
    return round(max(0.0, min(1.0, conf)), 3)


def _count_footnotes(text: str) -> set[int]:
    """Footnote indices actually referenced inline (not definitions)."""
    import re

    refs: set[int] = set()
    for m in re.finditer(r"\[\^(\d+)\](?!\:)", text):
        refs.add(int(m.group(1)))
    return refs


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


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _embed_page(
    cfg: CarrelYAML, title: str, summary: str, research_lines: list[str], tags: list[str]
) -> list[float] | None:
    text = " ".join([title, summary, " ".join(research_lines), " ".join(tags)])
    text = text.strip()[:_EMBED_CHARS]
    if not text:
        return None
    try:
        vecs = embeddings.embed_texts([text], model=cfg.embeddings.model)
        return vecs[0] if vecs else None
    except Exception as e:  # noqa: BLE001 - embedding is best-effort
        logger.warning("wiki scholar embed failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Per-scholar compile
# ---------------------------------------------------------------------------


def _aid_for_key(key: str) -> str | None:
    return None if key.startswith(NAME_KEY_PREFIX) else key


def _existing_page(session: Session, key: str, name: str) -> WikiPage | None:
    """Look up the canonical (non-redirect) page for a scholar key.

    Lookup is by ``entity_key`` so the function is invariant to which slug
    was previously used.  A redirect shell for the same entity is ignored —
    we want the live page, never the shell (reconcile has already
    consolidated shells before the compiler runs).

    Falls back to the (kind, slug) only when ``entity_key`` is missing on
    the row (a row that predates the identity migration; the next reconcile
    pass will assign it a real key).
    """
    from carrel.pipeline.wiki._scholars_agg import NAME_KEY_PREFIX as _NP
    aid = _aid_for_key(key)
    if aid:
        entity_key = f"scholar:{aid}"
    else:
        entity_key = f"scholar:name:{key[len(_NP):]}"
    page = session.exec(
        select(WikiPage).where(
            WikiPage.entity_key == entity_key,
            WikiPage.redirects_to.is_(None),
        )
    ).first()
    if page is not None:
        return page
    # Fallback for legacy rows without entity_key: match by slug.
    slug = _slug.scholar_slug(aid, name)
    return session.exec(
        select(WikiPage).where(
            WikiPage.kind == WikiKind.scholar.value,
            WikiPage.slug == slug,
            WikiPage.redirects_to.is_(None),
        )
    ).first()


def compile_scholar(
    session: Session,
    cfg: CarrelYAML,
    scholar_key: str,
    *,
    force: bool = False,
    on_progress: ProgressCallback | None = None,
) -> WikiPage:
    """Compile (or recompile) one scholar's wiki page. Raises ScholarError."""
    summary = next((s for s in aggregate(session) if s.key == scholar_key), None)
    if summary is None:
        raise ScholarError(f"scholar not found in library: {scholar_key}")

    aid = _aid_for_key(scholar_key)
    name = summary.name
    slug = _slug.scholar_slug(aid, name)
    rel_path = _slug.page_path(WikiKind.scholar.value, slug)
    full_path = Path(cfg.storage.root) / rel_path

    # Defensive: if the target slug is currently a redirect shell (the
    # reconcile pass has not yet caught up), bail rather than clobber the
    # shell's frontmatter.  The next reconcile + compile will re-evaluate.
    from sqlmodel import select as _sel
    existing_at_slug = session.exec(
        _sel(WikiPage).where(
            WikiPage.kind == WikiKind.scholar.value,
            WikiPage.slug == slug,
            WikiPage.redirects_to.is_not(None),
        )
    ).first()
    if existing_at_slug is not None and not force:
        raise ScholarError(
            f"scholar slug {slug!r} is a redirect shell — reconcile first"
        )

    papers = papers_for_key(session, scholar_key)[:_MAX_PAPERS]
    if not papers:
        raise ScholarError(f"no in-library papers for {scholar_key}")

    def _emit(**progress: Any) -> None:
        if on_progress is not None:
            on_progress({
                "key": scholar_key,
                "name": name,
                "stage": "scholar_compile",
                **progress,
            })

    latest_update = max((p.updated_at for p in papers if p.updated_at), default=None)
    existing = _existing_page(session, scholar_key, name)
    # If the existing canonical page sits at a *different* slug than the one
    # the current author key would synthesize (e.g. the author acquired an
    # A-ID after being compiled as a name-only page), retire the old file
    # to a redirect shell before we write the new one.  Without this, a
    # reader who hits the old URL would see a 404, and the new page would
    # be written at the new slug with no forward link from the old one.
    if existing is not None and existing.slug != slug:
        try:
            from carrel.pipeline.wiki._frontmatter import dump
            old_path = Path(cfg.storage.root) / existing.path
            if old_path.exists():
                meta = {"redirects_to": f"scholar:{aid}" if aid else f"scholar:name:{scholar_key[len(NAME_KEY_PREFIX):]}"}
                body = (
                    f"# Redirected\n\n"
                    f"This page moved to "
                    f"[[{name}]](../scholars/{slug}.md).\n"
                )
                text = dump(meta, body)
                tmp = old_path.with_suffix(old_path.suffix + ".tmp")
                tmp.write_text(text, encoding="utf-8")
                tmp.replace(old_path)
            # Convert the old DB row to a redirect shell so it stops
            # showing up in API lists / select_stale_scholars.
            existing.entity_key = None
            existing.redirects_to = f"scholar:{aid}" if aid else f"scholar:name:{scholar_key[len(NAME_KEY_PREFIX):]}"
            existing.title = name
            existing.summary = None
            existing.confidence = 0.0
            existing.evidence_count = 0
            existing.compiled_at = datetime.now(UTC)
            session.add(existing)
            session.commit()
        except OSError:
            logger.warning("scholar compile: could not retire old slug %s", existing.slug)
        existing = None  # treat as fresh compile
    if (
        not force
        and existing is not None
        and existing.compiled_at is not None
        and latest_update is not None
        and latest_update <= existing.compiled_at
    ):
        _emit(detail="Up to date")
        return existing

    if not (
        llm.has_key_for(cfg.llm.summarize_model)
        or llm.has_key_for(cfg.llm.fallback_model)
    ):
        raise ScholarError(
            "no LLM API key configured (set DEEPSEEK_API_KEY or VOLCANO_API_KEY)"
        )

    profile = get_profile(scholar_key)

    # Previous compiled prose (without the user section) for revision context.
    old_body: str | None = None
    if existing and full_path.exists():
        try:
            _old_meta, old_full = _frontmatter.parse(full_path.read_text(encoding="utf-8"))
            old_body = _merge.extract_user_section(old_full) or old_full
        except OSError:
            old_body = None

    prompt = _build_user_prompt(
        name=name,
        affiliation=summary.affiliation,
        profile=profile,
        papers=papers,
        old_body=old_body,
    )
    prompt = prompt[:_MAX_INPUT_CHARS + 2000]

    _emit(detail=f"Synthesizing {name}…")
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
            feature="wiki_scholar",
            on_usage=usage.make_usage_callback(session, feature="wiki_scholar"),
        )
    except llm.LLMError as e:
        raise ScholarError(str(e)) from e
    if not isinstance(data, dict) or not data.get("summary"):
        raise ScholarError("LLM returned no usable scholar summary")

    body = _render_body(
        name=name,
        data=data,
        papers=papers,
        profile=profile,
        related_concepts=_concepts_for_papers(session, [p.id for p in papers]),
        open_questions=_questions_for_papers(session, [p.id for p in papers]),
    )

    # Preserve any prior user-authored section.
    old_text = full_path.read_text(encoding="utf-8") if full_path.exists() else None
    body = _merge.protect_user_section(old_text, body)

    evidence_count = sum(
        1 for p in papers if (p.tldr_en or p.abstract or "")
    )
    cited = _count_footnotes(body)
    sections_total = sum(
        1 for h in ("Summary", "Research trajectory", "Evolving views")
        if f"## {h}" in body
    )
    sections_cited = sum(
        1 for h in ("Summary", "Research trajectory", "Evolving views")
        if f"## {h}" in body and cited
    )
    confidence = _confidence(
        data.get("confidence"),
        evidence_count=evidence_count,
        has_aid=bool(aid),
        sections_with_citations=sections_cited,
        sections_total=sections_total,
    )

    tags = [str(t).strip().lower() for t in (data.get("tags") or []) if str(t).strip()]
    research_lines = [
        str(x).strip() for x in (data.get("research_lines") or []) if str(x).strip()
    ]

    now = datetime.now(UTC)
    meta: dict[str, Any] = {
        "kind": WikiKind.scholar.value,
        "title": name,
        "slug": slug,
        "compiled_at": now.isoformat(),
        "compiler_version": COMPILER_VERSION,
        "confidence": confidence,
        "evidence_count": evidence_count,
        "source_paper_ids": [p.id for p in papers],
        "tags": tags,
    }
    if aid:
        meta["openalex_id"] = aid
    if summary.affiliation:
        meta["affiliation"] = summary.affiliation
    if summary.first_year:
        meta["first_year"] = summary.first_year
    if summary.last_year:
        meta["last_year"] = summary.last_year

    text = _frontmatter.dump(meta, body)
    _atomic_write(full_path, text)

    # Upsert the index row from what we just wrote.
    page = _reindex.upsert_page_from_disk(
        session, cfg, WikiKind.scholar.value, slug
    )
    if page is None:
        raise ScholarError(f"failed to index written page: {rel_path}")
    if page.id is not None:
        # Replace this page's source rows for the current paper set.
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
        vec = _embed_page(cfg, name, str(data.get("summary") or ""), research_lines, tags)
        if vec is not None:
            page.embedding = vec
        page.confidence = confidence
        page.evidence_count = evidence_count
        session.add(page)
        session.commit()

    _reindex.recompute_backlinks(session)
    _emit(
        detail=f"Compiled {name}",
        io={
            "input": prompt,
            "output": _summarize_scholar_io(data, body),
        },
    )
    logger.info("compiled scholar page %s (%s)", slug, name)
    return page


# ---------------------------------------------------------------------------
# Staleness + batch
# ---------------------------------------------------------------------------


def select_stale_scholars(session: Session, *, limit: int = 20) -> list[str]:
    """Return scholar keys that need (re)compilation.

    Stale when there is no page, or ``max(Paper.updated_at)`` across the
    scholar's papers exceeds ``WikiPage.compiled_at``. Sorted by the number of
    *new/updated* papers (descending) so backfills prioritize active scholars.

    Pages are looked up by ``entity_key`` (unique per scholar) so a stale
    check never returns a redirect shell — the shell is bookkeeping, not
    a page that needs recompiling.

    Also skipped: aggregator keys whose slug is occupied by a redirect
    shell (the canonical for that person lives at a different slug). The
    reconcile pass handles those.
    """
    from carrel.pipeline.wiki._scholars_agg import NAME_KEY_PREFIX as _NP
    scholars = aggregate(session)
    page_by_entity: dict[str, WikiPage] = {}
    # Slugs that point at redirect shells — a key whose canonical slug is
    # one of these must be ignored (the canonical is at a different slug,
    # already known to page_by_entity).
    shell_slugs: set[str] = set()
    for page in session.exec(
        select(WikiPage).where(
            WikiPage.kind == WikiKind.scholar.value,
            WikiPage.redirects_to.is_(None),
        )
    ).all():
        if page.entity_key:
            page_by_entity[page.entity_key] = page
    for shell in session.exec(
        select(WikiPage).where(
            WikiPage.kind == WikiKind.scholar.value,
            WikiPage.redirects_to.is_not(None),
        )
    ).all():
        if shell.slug:
            shell_slugs.add(shell.slug)

    stale: list[tuple[int, str]] = []
    for s in scholars:
        aid = _aid_for_key(s.key)
        entity_key = f"scholar:{aid}" if aid else f"scholar:name:{s.key[len(_NP):]}"
        page = page_by_entity.get(entity_key)
        papers = papers_for_key(session, s.key)
        if not papers:
            continue
        # If the canonical slug for this key happens to be a redirect
        # shell (the rare case where the file already exists from a
        # previous compile but the row was just retired), skip —
        # rewriting it would erase the shell's frontmatter.
        from carrel.pipeline.wiki import _slug as _ws
        canonical_slug = _ws.scholar_slug(s.key if not aid else aid, s.name)
        if canonical_slug in shell_slugs:
            continue
        latest = max((p.updated_at for p in papers if p.updated_at), default=None)
        if page is None or page.compiled_at is None:
            delta = len(papers)
        elif latest is not None and latest > page.compiled_at:
            delta = sum(1 for p in papers if p.updated_at and p.updated_at > page.compiled_at)
        else:
            continue
        stale.append((delta, s.key))

    stale.sort(key=lambda t: (-t[0], t[1]))
    return [key for _delta, key in stale[:limit]]


def compile_scholars_pending(
    session: Session,
    cfg: CarrelYAML,
    *,
    limit: int = 20,
    force: bool = False,
    on_progress: ProgressCallback | None = None,
) -> dict[str, int]:
    """Compile stale scholar pages; returns counts."""
    # Reconcile the scholar catalog against the live aggregation.  This
    # retires any page whose entity_key no longer corresponds to a live
    # author (e.g. a name-only author who later acquired an A-ID, or two
    # A-IDs that were merged via scholar_aliases).  Without this, stale
    # lookups by entity_key would silently skip the orphan and the next
    # compile would resurrect the duplicate.
    #
    # Failure is non-fatal: a reconcile error must not block compiles.
    # The next pass will retry.
    try:
        from carrel.pipeline.wiki._entities import reconcile_scholars
        reconcile_scholars(session)
    except Exception:
        logger.exception("scholar compile: reconcile failed (continuing)")

    keys = select_stale_scholars(session, limit=limit)
    counts = {"candidates": len(keys), "compiled": 0, "failed": 0}
    total = len(keys)

    def _wrap(i: int, name: str):
        def _cb(progress: dict) -> None:
            if on_progress is not None:
                on_progress({**progress, "index": i, "total": total, "name": name})
        return _cb

    for i, key in enumerate(keys, start=1):
        scholar = next((s for s in aggregate(session) if s.key == key), None)
        label = scholar.name if scholar else key
        try:
            compile_scholar(
                session, cfg, key, force=force, on_progress=_wrap(i, label)
            )
            counts["compiled"] += 1
        except ScholarError as e:
            logger.info("scholar %s failed: %s", key, e)
            counts["failed"] += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("scholar %s crashed: %s", key, e)
            counts["failed"] += 1

    logger.info(
        "scholar wiki batch done: candidates=%d compiled=%d failed=%d",
        counts["candidates"], counts["compiled"], counts["failed"],
    )
    return counts


def reindex_and_seed_scholars(
    session: Session,
    cfg: CarrelYAML,
    *,
    limit: int = 20,
    force: bool = False,
    on_progress: ProgressCallback | None = None,
) -> dict[str, int]:
    """One-click upgrade: reindex any on-disk pages, then compile stale ones."""
    reindexed = _reindex.reindex_wiki(session, cfg).get("indexed", 0)
    counts = compile_scholars_pending(
        session, cfg, limit=limit, force=force, on_progress=on_progress
    )
    counts["reindexed"] = reindexed
    return counts
