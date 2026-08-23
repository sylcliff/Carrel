"""Paper-dedup pipeline: deterministic scoring + union-find + auto-merge.

Mirror structure of :mod:`carrel.pipeline.scholar_dedup` (constants, dataclass,
scoring, union-find, canonical pick) but with paper-specific signals:

  * **Strong anchors** (any one is sufficient to auto-merge):
    - DOI exact match (post-normalization)
    - arXiv id exact match (post version-strip)
    - journal_doi bridge: A.arxiv_id == B.arxiv_id and (A.journal_doi == B.doi
      or B.journal_doi == A.doi)
    - S2 paperId exact match
    - LLM judge returned "same" with confidence >= LLM_SAME_THRESHOLD
  * **Soft signals** (weighted sum, only counts when no strong anchor):
    title Jaccard 0.45, author overlap 0.30, same year 0.10, same venue 0.10,
    abstract prefix overlap 0.05.

The LLM judge slot is optional — in :func:`run_dedup` we accept a
``judge`` callable; if not provided, the borderline path skips the LLM
and treats the deterministic verdict alone. The wiring is completed in
M10.6 by :mod:`carrel.pipeline.paper_dedup_judge`.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlmodel import Session, select

from carrel.models import Paper, PaperAlias
from carrel.pipeline.paper_dedup_ops import (
    apply_merge as _apply_merge,
    resolve_paper_id as _resolve_paper_id,
)
from carrel.sources.merge import (
    _clean_doi as _clean_doi,
    _normalize_title as _normalize_title,
    _strip_arxiv_version as _strip_arxiv_version,
)

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[dict[str, Any]], None]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Auto-accept threshold. Below this the pair is surfaced as a suggestion only.
AUTO_CONFIDENCE = 0.65
# Borderline band: pairs in [LLM_BORDERLINE_LO, LLM_BORDERLINE_HI) trigger
# the LLM judge (if a judge was passed in).
LLM_BORDERLINE_LO = 0.50
LLM_BORDERLINE_HI = AUTO_CONFIDENCE
# An LLM verdict with confidence >= this counts as a strong anchor.
LLM_SAME_THRESHOLD = 0.85
# Soft-signal weights.
_W_TITLE = 0.45
_W_AUTHORS = 0.30
_W_YEAR = 0.10
_W_VENUE = 0.10
_W_ABSTRACT = 0.05

# Cluster indexing: we look at the first N chars of the normalized title
# (after lowercase + strip) to bucket title-only candidates. The full
# normalized title is then Jaccard-scored. 20 chars is a compromise — short
# enough that real titles share a prefix, long enough that unrelated papers
# don't.
_TITLE_PREFIX = 20
_ABS_PREFIX = 200


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class PaperPairVerdict:
    """Verdict returned by an LLM judge (or a deterministic stand-in)."""

    verdict: str  # "same" | "different" | "uncertain"
    confidence: float
    reasons: list[str] = field(default_factory=list)
    model: str | None = None
    prompt_version: int | None = None


class PaperPairJudge(Protocol):
    """Pluggable judge interface (M10.6 will ship LLM + Composite impls)."""

    def judge(self, paper_a: Paper, paper_b: Paper) -> PaperPairVerdict: ...


@dataclass
class PairScore:
    a: str
    b: str
    score: float
    title: float
    authors: float
    year: float
    venue: float
    abstract: float
    strong_anchors: list[str] = field(default_factory=list)
    is_strong: bool = False
    reasons: list[str] = field(default_factory=list)
    llm_verdict: PaperPairVerdict | None = None


@dataclass
class DedupResult:
    candidates: int
    auto_merged: int
    suggested: int
    skipped_rejected: int
    components: list[dict[str, Any]] = field(default_factory=list)
    suggestions: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _norm_venue(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _author_keys(paper: Paper) -> set[str]:
    """Author identity set, preferring A-IDs over names (the A-ID is the
    stable key — names have spelling variants)."""
    out: set[str] = set()
    for a in paper.authors or []:
        if not isinstance(a, dict):
            continue
        aid = (a.get("openalex_author_id") or "").strip()
        if aid:
            out.add(f"aid:{aid}")
            continue
        name = (a.get("name") or "").strip().lower()
        if name:
            out.add(f"name:{name}")
    return out


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _year(paper: Paper) -> int | None:
    return paper.publication_date.year if paper.publication_date else None


def _abs_prefix(paper: Paper) -> str:
    return (paper.abstract or "")[:_ABS_PREFIX].lower()


# ---------------------------------------------------------------------------
# Strong anchor detection
# ---------------------------------------------------------------------------


def _detect_strong_anchors(a: Paper, b: Paper) -> list[str]:
    """Return the list of strong-anchor names that fire on this pair."""
    anchors: list[str] = []

    # 1. DOI exact match.
    a_doi = _clean_doi(a.doi)
    b_doi = _clean_doi(b.doi)
    if a_doi and b_doi and a_doi == b_doi:
        anchors.append("doi")

    # 2. arXiv id exact match.
    a_ax = _strip_arxiv_version(a.arxiv_id)
    b_ax = _strip_arxiv_version(b.arxiv_id)
    if a_ax and b_ax and a_ax == b_ax:
        anchors.append("arxiv")

    # 3. journal_doi bridge: an arxiv row whose journal_doi == the other row's doi
    #    (and they share an arxiv id) is the published version of the same paper.
    if a_ax and b_ax and a_ax == b_ax:
        a_j = _clean_doi(a.journal_doi)
        b_j = _clean_doi(b.journal_doi)
        if a_j and b_doi and a_j == b_doi:
            anchors.append("journal_doi_bridge")
        if b_j and a_doi and b_j == a_doi:
            anchors.append("journal_doi_bridge")
    # Also handle the looser case where the arxiv id is on only one side but
    # the journal_doi / doi of the other side matches.
    if a_ax and not b_ax and a.journal_doi and b_doi and a.journal_doi.lower() == b_doi:
        anchors.append("journal_doi_bridge")
    if b_ax and not a_ax and b.journal_doi and a_doi and b.journal_doi.lower() == a_doi:
        anchors.append("journal_doi_bridge")

    # 4. S2 paperId exact match.
    if a.s2_paper_id and b.s2_paper_id and a.s2_paper_id == b.s2_paper_id:
        anchors.append("s2")

    return anchors


# ---------------------------------------------------------------------------
# Soft signal scoring
# ---------------------------------------------------------------------------


def _soft_score(a: Paper, b: Paper) -> tuple[float, float, float, float, float, list[str]]:
    """Compute the (title, authors, year, venue, abstract, reasons) soft signals.

    Returns each component in [0, 1] and a list of human-readable reasons.
    """
    # Title Jaccard on the fully-stripped form.
    ta = _normalize_title(a.title)
    tb = _normalize_title(b.title)
    title = _jaccard(set(ta), set(tb)) if ta and tb else 0.0
    reasons: list[str] = []
    if title >= 0.85:
        reasons.append(f"title jaccard={title:.2f}")

    # Author overlap — Jaccard on A-ID/name keys.
    ak_a, ak_b = _author_keys(a), _author_keys(b)
    authors = _jaccard(ak_a, ak_b) if ak_a and ak_b else 0.0
    if authors >= 0.5:
        reasons.append(f"authors jaccard={authors:.2f}")

    # Same year.
    ya, yb = _year(a), _year(b)
    year = 1.0 if (ya is not None and yb is not None and ya == yb) else 0.0
    if year == 1.0:
        reasons.append(f"year={ya}")

    # Same venue.
    va = _norm_venue(a.venue)
    vb = _norm_venue(b.venue)
    venue = 1.0 if (va and vb and va == vb) else 0.0
    if venue == 1.0 and a.venue and b.venue:
        reasons.append(f"venue={a.venue!r}")

    # Abstract prefix Jaccard.
    aa, ab = _abs_prefix(a), _abs_prefix(b)
    if aa and ab:
        abstract = _jaccard(set(aa.split()), set(ab.split()))
    else:
        abstract = 0.0
    if abstract >= 0.7:
        reasons.append(f"abstract prefix jaccard={abstract:.2f}")

    return title, authors, year, venue, abstract, reasons


# ---------------------------------------------------------------------------
# Pair scoring
# ---------------------------------------------------------------------------


def score_pair(
    a: Paper,
    b: Paper,
    *,
    judge: PaperPairJudge | None = None,
) -> PairScore:
    """Score one pair of papers. Returns a :class:`PairScore`.

    Strong anchors dominate: if any fires and the pair is not a "reject"
    (which the caller should have pre-filtered), the pair is eligible for
    auto-merge. Soft signals are added to a weighted sum; an LLM judge
    (optional) is invoked only on the borderline band.
    """
    anchors = _detect_strong_anchors(a, b)
    title, authors, year, venue, abstract, soft_reasons = _soft_score(a, b)
    score = (
        _W_TITLE * title
        + _W_AUTHORS * authors
        + _W_YEAR * year
        + _W_VENUE * venue
        + _W_ABSTRACT * abstract
    )
    reasons = list(soft_reasons)

    llm_verdict: PaperPairVerdict | None = None
    # Only consult the LLM in the borderline band and only when no strong
    # anchor fired (the LLM is an arbitrator for ambiguous cases, not a
    # rubber-stamp for clear ones).
    if not anchors and judge is not None and LLM_BORDERLINE_LO <= score < LLM_BORDERLINE_HI:
        llm_verdict = judge.judge(a, b)
        if llm_verdict.verdict == "same" and llm_verdict.confidence >= LLM_SAME_THRESHOLD:
            anchors.append("llm")
            reasons.append(
                f"llm same (conf={llm_verdict.confidence:.2f}, model={llm_verdict.model})"
            )
        elif llm_verdict.verdict == "different" and llm_verdict.confidence >= LLM_SAME_THRESHOLD:
            # Strong negative signal — the score is wrong, do not merge.
            reasons.append(
                f"llm different (conf={llm_verdict.confidence:.2f}, model={llm_verdict.model})"
            )
            return PairScore(
                a=a.id, b=b.id, score=0.0, title=title, authors=authors,
                year=year, venue=venue, abstract=abstract,
                strong_anchors=[], is_strong=False, reasons=reasons,
                llm_verdict=llm_verdict,
            )
        else:
            reasons.append(
                f"llm {llm_verdict.verdict} (conf={llm_verdict.confidence:.2f})"
            )

    # A pair is "strong" (= auto-merge eligible) when any of:
    #   * a deterministic strong anchor fires (DOI / arXiv / journal_doi bridge / S2)
    #   * the soft score clears AUTO_CONFIDENCE on its own (catches clearly
    #     identical metadata that happens to lack a shared identifier, e.g.
    #     two openalex rows that lost their DOI)
    # LLM-only strong anchors are conservative: also require the soft score
    # to clear the bar so a single judge call can't rubber-stamp an unrelated pair.
    non_llm_anchors = [a for a in anchors if a != "llm"]
    is_strong = bool(non_llm_anchors) or score >= AUTO_CONFIDENCE
    if anchors == ["llm"] and not non_llm_anchors:
        is_strong = score >= AUTO_CONFIDENCE

    return PairScore(
        a=a.id, b=b.id, score=round(score, 3),
        title=round(title, 3), authors=round(authors, 3),
        year=year, venue=venue, abstract=round(abstract, 3),
        strong_anchors=anchors, is_strong=is_strong,
        reasons=reasons, llm_verdict=llm_verdict,
    )


def _should_auto_merge(ps: PairScore) -> bool:
    return ps.is_strong and not (
        ps.llm_verdict and ps.llm_verdict.verdict == "different"
    )


# ---------------------------------------------------------------------------
# Cluster indexing
# ---------------------------------------------------------------------------


@dataclass
class _Cluster:
    """A group of paper ids that may contain duplicates.

    The cluster is keyed by the strongest shared identifier (DOI > arXiv >
    S2 > journal_doi bridge > title). Within the cluster every pair is scored.
    """

    key: str
    kind: str  # "doi" | "arxiv" | "s2" | "journal_doi_bridge" | "title"
    paper_ids: set[str] = field(default_factory=set)


def _build_clusters(papers: list[Paper]) -> list[_Cluster]:
    """Group papers into candidate clusters by shared identifier.

    A paper that participates in multiple identifier buckets (e.g. has both
    DOI and arXiv id) will appear in multiple clusters — that's intentional;
    the union-find will collapse them. The cost is a few extra intra-cluster
    pair scorings, which are cheap.

    The ``doi`` bucket also collects arxiv rows whose ``journal_doi`` matches
    the bucket key — that's the journal-doi bridge (preprint vs journal
    version of the same paper).
    """
    by_doi: dict[str, set[str]] = defaultdict(set)
    by_arxiv: dict[str, set[str]] = defaultdict(set)
    by_s2: dict[str, set[str]] = defaultdict(set)
    by_title: dict[str, set[str]] = defaultdict(set)

    for p in papers:
        doi = _clean_doi(p.doi)
        ax = _strip_arxiv_version(p.arxiv_id)
        s2 = p.s2_paper_id
        jdoi = _clean_doi(p.journal_doi)
        # The doi bucket also pulls in arxiv rows whose journal_doi matches —
        # the journal-doi bridge between the published version (doi-only)
        # and the preprint (arxiv+journal_doi) of the same paper.
        if doi:
            by_doi[doi].add(p.id)
        if ax and jdoi:
            by_doi[jdoi].add(p.id)
        if ax:
            by_arxiv[ax].add(p.id)
        if s2:
            by_s2[s2].add(p.id)
        if not (doi or ax or s2):
            # Title bucket only for papers with no identifiers at all.
            tkey = _normalize_title(p.title)[:_TITLE_PREFIX]
            if tkey:
                by_title[tkey].add(p.id)

    clusters: list[_Cluster] = []
    seen_pairs: set[tuple[str, ...]] = set()

    def _emit(key: str, kind: str, ids: set[str]) -> None:
        if len(ids) < 2:
            return
        sig = tuple(sorted(ids))
        if sig in seen_pairs:
            return
        seen_pairs.add(sig)
        clusters.append(_Cluster(key=key, kind=kind, paper_ids=ids))

    for k, ids in by_doi.items():
        _emit(k, "doi", ids)
    for k, ids in by_arxiv.items():
        _emit(k, "arxiv", ids)
    for k, ids in by_s2.items():
        _emit(k, "s2", ids)
    for k, ids in by_title.items():
        _emit(k, "title", ids)
    return clusters


# ---------------------------------------------------------------------------
# Rejections and canonical pick
# ---------------------------------------------------------------------------


def _load_rejections(session: Session) -> set[tuple[str, str]]:
    rows = session.exec(
        select(PaperAlias).where(PaperAlias.source == "reject")
    ).all()
    out: set[tuple[str, str]] = set()
    for r in rows:
        out.add((r.alias_paper_id, r.canonical_paper_id))
        out.add((r.canonical_paper_id, r.alias_paper_id))
    return out


def _paper_completeness(paper: Paper) -> tuple[int, ...]:
    """Higher tuple = more authoritative. Used to pick canonical in a component."""
    return (
        1 if paper.id_kind == "openalex" else 0,
        1 if paper.doi else 0,
        1 if paper.arxiv_id else 0,
        1 if paper.s2_paper_id else 0,
        1 if paper.abstract else 0,
        1 if paper.pdf_path else 0,
        1 if paper.in_library else 0,
        # Earlier created_at is more "original" — we want to keep the older
        # row's provenance (notes, tags, etc.) when it is at least as complete.
        # Inverted so lexically smaller (earlier) wins.
        -paper.created_at.timestamp() if paper.created_at else 0,
    )


def _canonical_in_component(papers: list[Paper]) -> Paper:
    """Pick the most authoritative paper in a merge component."""
    return sorted(papers, key=_paper_completeness, reverse=True)[0]


# ---------------------------------------------------------------------------
# Union-find
# ---------------------------------------------------------------------------


class _UF:
    def __init__(self, items: Iterable[str]) -> None:
        self.parent = {x: x for x in items}

    def find(self, x: str) -> str:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _candidate_papers(session: Session) -> list[Paper]:
    """In-library, non-merged, non-discarded papers. The sync-time dedup
    (M10.3) catches same-as-it-arrives duplicates; the background scan
    catches pairs that slipped through (s2-only + W-only, or the gap
    between publication_check upgrading a row and the DOI being
    independently re-discovered)."""
    papers = session.exec(
        select(Paper).where(
            Paper.in_library.is_(True),
            Paper.discarded.is_(False),
            Paper.status != "merged",
        )
    ).all()
    return list(papers)


def run_dedup(
    session: Session,
    *,
    auto_apply: bool = True,
    judge: PaperPairJudge | None = None,
    on_progress: ProgressCallback | None = None,
) -> DedupResult:
    """Score in-library papers and apply high-confidence merges.

    With ``judge=None`` (default) the run is purely deterministic: any pair
    with no strong anchor and a soft score in the borderline band is left
    as a suggestion. With ``judge`` provided (M10.6), the borderline pairs
    are arbitrated by the LLM.
    """
    papers = _candidate_papers(session)
    if on_progress:
        on_progress({"stage": "load", "detail": f"Loaded {len(papers)} candidate papers"})

    by_id: dict[str, Paper] = {p.id: p for p in papers}

    rejections = _load_rejections(session)

    clusters = _build_clusters(papers)
    if on_progress:
        on_progress({
            "stage": "cluster",
            "detail": f"Built {len(clusters)} candidate clusters",
        })

    uf = _UF(set(by_id))
    pair_scores: list[PairScore] = []
    rejected_hits = 0
    n_clusters = len(clusters)
    for ci, cluster in enumerate(clusters, start=1):
        ids = sorted(cluster.paper_ids)
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a_id, b_id = ids[i], ids[j]
                if (a_id, b_id) in rejections or (b_id, a_id) in rejections:
                    rejected_hits += 1
                    continue
                a, b = by_id.get(a_id), by_id.get(b_id)
                if a is None or b is None:
                    continue
                ps = score_pair(a, b, judge=judge)
                pair_scores.append(ps)
                if _should_auto_merge(ps):
                    uf.union(a_id, b_id)
        if on_progress and n_clusters and ci % 50 == 0:
            on_progress({
                "stage": "score",
                "detail": f"Scored {ci}/{n_clusters} clusters",
            })

    # Group into components.
    components: dict[str, set[str]] = defaultdict(set)
    for pid in by_id:
        components[uf.find(pid)].add(pid)
    merged_components = [c for c in components.values() if len(c) > 1]

    result = DedupResult(
        candidates=len(pair_scores),
        auto_merged=0,
        suggested=0,
        skipped_rejected=rejected_hits,
    )

    for comp in merged_components:
        comp_papers = [by_id[pid] for pid in comp if pid in by_id]
        if len(comp_papers) < 2:
            continue
        canonical = _canonical_in_component(comp_papers)
        aliases = [p for p in comp_papers if p.id != canonical.id]
        comp_pairs = [
            ps for ps in pair_scores
            if {ps.a, ps.b} <= {p.id for p in comp_papers} and _should_auto_merge(ps)
        ]
        reasons = sorted({r for ps in comp_pairs for r in (ps.strong_anchors + ps.reasons)})
        avg_score = (
            round(sum(ps.score for ps in comp_pairs) / len(comp_pairs), 3)
            if comp_pairs else 0.0
        )
        component_out: dict[str, Any] = {
            "canonical_id": canonical.id,
            "alias_ids": [a.id for a in aliases],
            "display_label": canonical.title,
            "reasons": reasons,
            "avg_score": avg_score,
            "sources": list({p.id_kind for p in comp_papers}),
            "paper_titles": {p.id: p.title for p in comp_papers},
        }
        result.components.append(component_out)
        if auto_apply:
            for alias in aliases:
                _apply_merge(
                    session,
                    alias_paper_id=alias.id,
                    canonical_paper_id=canonical.id,
                    source="auto",
                    confidence=avg_score or 0.7,
                    reasons=reasons,
                    display_label=canonical.title,
                )
            result.auto_merged += len(aliases)

    if auto_apply:
        session.commit()

    # Remaining suggestions: pairs below threshold, after auto-merge.
    seen_sug: set[tuple[str, str]] = set()
    for ps in pair_scores:
        if _should_auto_merge(ps):
            continue  # already applied
        # Skip pairs that span different auto-merge components — the user
        # has nothing to compare; they collapse.
        ra, rb = uf.find(ps.a), uf.find(ps.b)
        if ra == rb:
            continue
        # Skip pairs where any side is a rejected alias.
        if (ps.a, ps.b) in rejections or (ps.b, ps.a) in rejections:
            continue
        a, b = by_id.get(ps.a), by_id.get(ps.b)
        if a is None or b is None:
            continue
        if a.status == "merged" or b.status == "merged":
            continue
        pair_key = tuple(sorted((ps.a, ps.b)))
        if pair_key in seen_sug:
            continue
        seen_sug.add(pair_key)
        result.suggestions.append({
            "a": ps.a,
            "b": ps.b,
            "score": ps.score,
            "title": ps.title,
            "authors": ps.authors,
            "strong_anchors": list(ps.strong_anchors),
            "reasons": list(ps.reasons),
            "llm_verdict": (
                {
                    "verdict": ps.llm_verdict.verdict,
                    "confidence": ps.llm_verdict.confidence,
                    "model": ps.llm_verdict.model,
                    "reasons": list(ps.llm_verdict.reasons),
                }
                if ps.llm_verdict else None
            ),
            "title_a": a.title,
            "title_b": b.title,
            "year_a": _year(a),
            "year_b": _year(b),
            "doi_a": a.doi,
            "doi_b": b.doi,
            "arxiv_id_a": a.arxiv_id,
            "arxiv_id_b": b.arxiv_id,
            "s2_paper_id_a": a.s2_paper_id,
            "s2_paper_id_b": b.s2_paper_id,
        })

    result.suggested = len(result.suggestions)

    if on_progress:
        on_progress({
            "stage": "done",
            "detail": (
                f"auto_merged={result.auto_merged} "
                f"suggested={result.suggested} "
                f"rejected={result.skipped_rejected}"
            ),
        })

    return result
