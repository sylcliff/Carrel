"""Second-pass scholar disambiguation beyond OpenAlex A-IDs.

OpenAlex frequently splits one real researcher across several Author IDs —
common for Chinese names and early-career authors. :mod:`carrel.pipeline.authors`
trusts whatever A-ID OpenAlex attaches to a Work; this module looks *across*
A-IDs and asks whether two of them are actually the same person.

Inputs are restricted to in-library papers, so the signal is cheap to gather:
for each same-display-name cluster of A-IDs we compare

  * **co-author overlap** — Jaccard over A-IDs of co-authors (strongest signal;
    two profiles of the same person tend to share collaborators),
  * **affiliation** — OpenAlex ``last_known_institution`` normalized equality,
  * **topic overlap** — Jaccard over the author's top OpenAlex topic IDs,
  * **works-count ratio** — a 1-paper ID next to a 50-paper ID is a more
    plausible duplicate than two equally-prolific IDs.

Pairwise scores feed a union-find; components above ``AUTO_CONFIDENCE`` are
persisted as :class:`ScholarAlias` rows with ``source="auto"``. Pairs the user
has explicitly rejected (``source="reject"``) are never rejoined. The remaining
clusters are returned as *suggestions* for the UI to expose with Accept/Reject.

The module never deletes or rewrites ``Paper.authors`` — aliases are an
indirection resolved by :func:`carrel.pipeline.wiki._scholars_agg.author_key`,
which keeps the original provenance intact and makes a merge reversible (delete
the alias row).
"""
from __future__ import annotations

import logging
import re
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session, select

from carrel.models import Paper, ScholarAlias
from carrel.sources import openalex_client as oa

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[dict[str, Any]], None]

# Auto-accept threshold. Below this the pair is surfaced as a suggestion only.
AUTO_CONFIDENCE = 0.55
# Strong-anchor gates — any one of these is sufficient evidence to auto-merge.
# We don't rely on the weighted total score alone because Jaccard punishes pairs
# where one A-ID has only 1-2 papers (small denominator = tiny overlap even
# when every coauthor matches). For those we use the *overlap coefficient*
# (intersection / min(|A|,|B|)) which is 1.0 when the smaller profile's
# collaborators are all subsumed by the larger one.
STRONG_COAUTHOR_OVERLAP = 0.6   # overlap coefficient on A-ID coauthors
STRONG_COAUTHOR_JACCARD = 0.34  # OR Jaccard (for two well-populated profiles)
STRONG_AFFIL_AND_COAUTHOR = (1.0, 0.05)  # OR same affiliation + any coauthor overlap
# Cap: when both A-IDs have this many in-library papers, require a stronger
# signal — two well-published namesakes at the same institution can be
# different people, so don't auto-merge on institution alone.
MANY_PAPERS = 5

# Polite pacing for OpenAlex Authors endpoint calls.
_REQUEST_SLEEP = 0.25
_PROFILE_TTL = 24 * 3600.0


# ---------------------------------------------------------------------------
# Cluster extraction (in-library evidence)
# ---------------------------------------------------------------------------


def _norm_name(name: str | None) -> str:
    if not name:
        return ""
    s = re.sub(r"[.\s]+", "", name).lower()
    # Strip occasional CJK spacing differences; OpenAlex sometimes returns
    # "WenHui DUAN" vs "Wenhui Duan".
    return s


def _iter_author_records(session: Session) -> Iterable[tuple[Paper, dict[str, Any]]]:
    papers = session.exec(
        select(Paper).where(Paper.in_library.is_(True), Paper.discarded.is_(False))
    ).all()
    for p in papers:
        for a in p.authors or []:
            if isinstance(a, dict) and a.get("openalex_author_id"):
                yield p, a


@dataclass
class AidEvidence:
    """Per-AID evidence gathered from in-library papers."""

    aid: str
    names: Counter = field(default_factory=Counter)
    paper_ids: set[str] = field(default_factory=set)
    coauthor_aids: set[str] = field(default_factory=set)
    coauthor_names: set[str] = field(default_factory=set)
    institutions: Counter = field(default_factory=Counter)
    years: list[int] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        return self.names.most_common(1)[0][0] if self.names else self.aid


def build_clusters(session: Session) -> dict[str, dict[str, AidEvidence]]:
    """Group A-IDs by normalized display name.

    Returns ``{normalized_name: {aid: AidEvidence}}`` for clusters with >1 A-ID.
    Singleton clusters are not interesting and are dropped.
    """
    clusters: dict[str, dict[str, AidEvidence]] = defaultdict(dict)
    name_index: dict[str, str] = {}  # aid -> norm name (first seen)

    for paper, a in _iter_author_records(session):
        aid = (a.get("openalex_author_id") or "").strip()
        name = (a.get("name") or "").strip()
        if not aid or not name:
            continue
        norm = _norm_name(name)
        if not norm:
            continue
        # If the same A-ID appears under different name spellings (rare but
        # possible — OpenAlex canonicalizes display_name per work), record all
        # variants but group it under the most common normalized form.
        prior_norm = name_index.get(aid)
        if prior_norm and prior_norm != norm:
            norm = prior_norm
        else:
            name_index[aid] = norm

        ev = clusters[norm].get(aid)
        if ev is None:
            ev = AidEvidence(aid=aid)
            clusters[norm][aid] = ev
        ev.names[name] += 1
        ev.paper_ids.add(paper.id)
        if paper.publication_date is not None:
            ev.years.append(paper.publication_date.year)
        affil = (a.get("affiliation") or "").strip()
        if affil:
            ev.institutions[_norm_affil(affil)] += 1
        for other in paper.authors or []:
            if not isinstance(other, dict):
                continue
            oa_id = (other.get("openalex_author_id") or "").strip()
            oname = (other.get("name") or "").strip()
            if oa_id and oa_id != aid:
                ev.coauthor_aids.add(oa_id)
            if oname and oname != name:
                ev.coauthor_names.add(_norm_name(oname))

    return {n: m for n, m in clusters.items() if len(m) > 1}


# ---------------------------------------------------------------------------
# OpenAlex author-profile enrichment
# ---------------------------------------------------------------------------


def _norm_affil(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"\buniversity\b", "univ", s)
    s = re.sub(r"\b(institute|institution)\b", "inst", s)
    s = re.sub(r"\b(the|of|and|at)\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return s


@dataclass
class Profile:
    aid: str
    display_name: str | None = None
    affiliation: str | None = None
    affiliation_norm: str | None = None
    works_count: int = 0
    h_index: int = 0
    cited_by_count: int = 0
    topic_ids: set[str] = field(default_factory=set)
    fetched: bool = False


_profile_cache: dict[str, tuple[float, Profile]] = {}


def _fetch_profile(aid: str) -> Profile:
    now = time.monotonic()
    cached = _profile_cache.get(aid)
    if cached and now - cached[0] < _PROFILE_TTL:
        return cached[1]
    prof = Profile(aid=aid)
    try:
        raw = oa.fetch_author(aid)
    except Exception as e:  # noqa: BLE001 - best-effort enrichment
        logger.warning("scholar_dedup: fetch_author(%s) failed: %s", aid, e)
        raw = None
    if raw:
        prof.fetched = True
        prof.display_name = raw.get("name") or raw.get("display_name")
        prof.works_count = int(raw.get("works_count") or 0)
        prof.h_index = int(raw.get("h_index") or 0)
        prof.cited_by_count = int(raw.get("cited_by_count") or 0)
        affil = raw.get("affiliation")
        if isinstance(affil, str) and affil.strip():
            prof.affiliation = affil.strip()
            prof.affiliation_norm = _norm_affil(prof.affiliation)
        for topic in raw.get("topics") or []:
            tid = topic.get("id") if isinstance(topic, dict) else None
            if tid:
                prof.topic_ids.add(str(tid))
    _profile_cache[aid] = (now, prof)
    time.sleep(_REQUEST_SLEEP)
    return prof


# ---------------------------------------------------------------------------
# Pairwise scoring
# ---------------------------------------------------------------------------


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / len(a | b)


def _overlap(a: set[str], b: set[str]) -> float:
    """Overlap coefficient: intersection / min(|A|, |B|).

    This is 1.0 when every collaborator on the smaller profile also appears on
    the larger one — a strong containment signal even with a 1-paper duplicate.
    """
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / min(len(a), len(b)) if inter else 0.0


def _name_overlap(ea: AidEvidence, eb: AidEvidence) -> float:
    """Reward exact display-name agreement (cluster key is a normalized match,
    but OpenAlex display_name can still differ, e.g. initials vs full)."""
    na = {n.lower() for n in ea.names}
    nb = {n.lower() for n in eb.names}
    if na & nb:
        return 1.0
    # Token-subset match ("Y. Xu" vs "Yong Xu")
    def toks(names: set[str]) -> set[str]:
        out: set[str] = set()
        for n in names:
            out.update(t for t in re.split(r"\s+", n) if t)
        return out
    ta, tb = toks(na), toks(nb)
    if ta and tb and (ta <= tb or tb <= ta):
        return 0.6
    return 0.3  # same normalized name, different surface form — already clustered


@dataclass
class PairScore:
    a: str
    b: str
    score: float
    coauthor: float       # Jaccard (used for the weighted score)
    coauthor_overlap: float  # overlap coefficient (used for strong-anchor test)
    affiliation: float
    topic: float
    name: float
    papers_a: int
    papers_b: int
    reasons: list[str]


def score_pair(ea: AidEvidence, eb: AidEvidence, pa: Profile, pb: Profile) -> PairScore:
    # Jaccard feeds the weighted total score; overlap coefficient is the
    # "is the smaller profile subsumed by the larger" test for auto-merge.
    coauthor = _jaccard(ea.coauthor_aids, eb.coauthor_aids)
    coauthor_ov = _overlap(ea.coauthor_aids, eb.coauthor_aids)
    if coauthor == 0 and coauthor_ov == 0:
        # Fall back to name-based coauthors when A-IDs are sparse (e.g. the
        # papers were imported from arXiv without author resolution).
        coauthor = _jaccard(ea.coauthor_names, eb.coauthor_names) * 0.7
        coauthor_ov = _overlap(ea.coauthor_names, eb.coauthor_names) * 0.7

    if pa.affiliation_norm and pb.affiliation_norm:
        affiliation = 1.0 if pa.affiliation_norm == pb.affiliation_norm else 0.0
    else:
        # Fall back to in-library affiliations when OpenAlex profile has none.
        aa = set(ea.institutions)
        bb = set(eb.institutions)
        affiliation = 1.0 if aa and bb and (aa & bb) else 0.0
    topic = _jaccard(pa.topic_ids, pb.topic_ids)
    name = _name_overlap(ea, eb)

    # Weighted sum — coauthors are the anchor; affiliation corroborates; topic
    # is weak (two people in the same field are often different people).
    score = 0.55 * coauthor + 0.25 * affiliation + 0.10 * topic + 0.10 * name

    reasons: list[str] = []
    if coauthor_ov >= STRONG_COAUTHOR_OVERLAP:
        reasons.append(f"shared co-authors (overlap={coauthor_ov:.2f})")
    elif coauthor >= STRONG_COAUTHOR_JACCARD:
        reasons.append(f"shared co-authors (J={coauthor:.2f})")
    if affiliation >= 1.0:
        reasons.append("same affiliation")
    if topic >= 0.5:
        reasons.append(f"shared topics (J={topic:.2f})")
    if name >= 1.0:
        reasons.append("identical display name")

    return PairScore(
        a=ea.aid, b=eb.aid, score=round(score, 3),
        coauthor=round(coauthor, 3),
        coauthor_overlap=round(coauthor_ov, 3),
        affiliation=affiliation,
        topic=round(topic, 3), name=name,
        papers_a=len(ea.paper_ids), papers_b=len(eb.paper_ids),
        reasons=reasons,
    )


def _is_strong(ps: PairScore) -> bool:
    """At least one strong anchor beyond topic/name resemblance.

    Coauthor containment is the most reliable signal (when one A-ID has only a
    paper or two, the overlap coefficient catches that every coauthor on that
    paper is already in the larger profile's network). Same affiliation plus
    even a sliver of coauthor overlap is also accepted. We reject the
    institution-only match when both profiles are well-published, since large
    departments have many namesakes.
    """
    if ps.coauthor_overlap >= STRONG_COAUTHOR_OVERLAP:
        return True
    if ps.coauthor >= STRONG_COAUTHOR_JACCARD:
        return True
    affil_thr, coauthor_thr = STRONG_AFFIL_AND_COAUTHOR
    if ps.affiliation >= affil_thr and ps.coauthor >= coauthor_thr:
        return True
    return False


def _should_auto_merge(ps: PairScore) -> bool:
    """Decide whether a pair merges without asking the user.

    A strong coauthor anchor (high overlap *or* high Jaccard) is sufficient on
    its own — pairs are already clustered by identical name, so "every coauthor
    on the smaller profile appears in the larger one" is a near-certainty
    duplicate signal even when the smaller profile has only one paper (where
    Jaccard is structurally tiny). The weaker affiliation+coauthor anchor still
    needs the blended confidence threshold to avoid merging large-department
    namesakes.
    """
    if ps.coauthor_overlap >= STRONG_COAUTHOR_OVERLAP:
        return True
    if ps.coauthor >= STRONG_COAUTHOR_JACCARD:
        return True
    if ps.score >= AUTO_CONFIDENCE and _is_strong(ps):
        return True
    return False


# ---------------------------------------------------------------------------
# Union-find + alias persistence
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


def _load_rejections(session: Session) -> set[tuple[str, str]]:
    rows = session.exec(
        select(ScholarAlias).where(ScholarAlias.source == "reject")
    ).all()
    out: set[tuple[str, str]] = set()
    for r in rows:
        out.add((r.alias_aid, r.canonical_aid))
        out.add((r.canonical_aid, r.alias_aid))
    return out


def _canonical_in_component(
    aids: set[str], evidence: dict[str, AidEvidence], profiles: dict[str, Profile]
) -> str:
    """Pick the most authoritative A-ID in a merge component.

    Priority: most in-library papers → highest OpenAlex works_count → highest
    h-index → lexical (stable tiebreak).
    """
    def key(aid: str) -> tuple[int, int, int, str]:
        ev = evidence.get(aid)
        prof = profiles.get(aid)
        return (
            len(ev.paper_ids) if ev else 0,
            prof.works_count if prof else 0,
            prof.h_index if prof else 0,
            aid,
       )
    return sorted(aids, key=key, reverse=True)[0]


@dataclass
class DedupResult:
    candidates: int
    auto_merged: int
    suggested: int
    skipped_rejected: int
    components: list[dict[str, Any]] = field(default_factory=list)
    suggestions: list[dict[str, Any]] = field(default_factory=list)


def run_dedup(
    session: Session,
    *,
    auto_apply: bool = True,
    limit_clusters: int | None = None,
    on_progress: ProgressCallback | None = None,
) -> DedupResult:
    """Score same-name A-ID clusters and persist high-confidence aliases.

    Returns a :class:`DedupResult` with applied merges and remaining
    suggestions for the UI.
    """
    clusters = build_clusters(session)
    rejections = _load_rejections(session)

    # Flatten clusters but remember name grouping (for display + canonical pick).
    all_aids: set[str] = set()
    evidence: dict[str, AidEvidence] = {}
    name_of: dict[str, str] = {}
    for norm, group in clusters.items():
        for aid, ev in group.items():
            all_aids.add(aid)
            evidence[aid] = ev
            name_of[aid] = ev.display_name

    if on_progress:
        on_progress({"stage": "profiles", "detail": f"Fetching {len(all_aids)} OpenAlex profiles…"})
    profiles: dict[str, Profile] = {}
    for i, aid in enumerate(sorted(all_aids), start=1):
        profiles[aid] = _fetch_profile(aid)
        if on_progress and i % 10 == 0:
            on_progress({"stage": "profiles", "detail": f"Fetched {i}/{len(all_aids)} profiles"})

    # Pairwise scoring within each same-name cluster, union-find across all.
    uf = _UF(all_aids)
    pair_scores: list[PairScore] = []
    rejected_hits = 0
    for norm, group in clusters.items():
        aids = sorted(group.keys())
        for i in range(len(aids)):
            for j in range(i + 1, len(aids)):
                a, b = aids[i], aids[j]
                if (a, b) in rejections:
                    rejected_hits += 1
                    continue
                ps = score_pair(evidence[a], evidence[b], profiles[a], profiles[b])
                pair_scores.append(ps)
                if _should_auto_merge(ps):
                    uf.union(a, b)

    # Group into components.
    components: dict[str, set[str]] = defaultdict(set)
    for aid in all_aids:
        components[uf.find(aid)].add(aid)
    merged_components = [c for c in components.values() if len(c) > 1]

    result = DedupResult(
        candidates=len(pair_scores),
        auto_merged=0,
        suggested=0,
        skipped_rejected=rejected_hits,
    )

    for comp in merged_components:
        canonical = _canonical_in_component(comp, evidence, profiles)
        aliases = sorted(a for a in comp if a != canonical)
        display = name_of.get(canonical) or evidence[canonical].display_name
        comp_pairs = [
            ps for ps in pair_scores
            if {ps.a, ps.b} <= comp and _should_auto_merge(ps)
        ]
        reasons = sorted({r for ps in comp_pairs for r in ps.reasons})
        avg_score = (
            round(sum(ps.score for ps in comp_pairs) / len(comp_pairs), 3)
            if comp_pairs else 0.0
        )
        result.components.append({
            "canonical_aid": canonical,
            "alias_aids": aliases,
            "display_name": display,
            "reasons": reasons,
            "avg_score": avg_score,
            "paper_counts": {a: len(evidence[a].paper_ids) for a in comp},
        })
        if auto_apply:
            for alias in aliases:
                _upsert_alias(
                    session,
                    alias_aid=alias,
                    canonical_aid=canonical,
                    display_name=display,
                    source="auto",
                    confidence=avg_score or 0.7,
                    reasons=reasons,
                )
            result.auto_merged += len(aliases)

    session.commit()

    # Remaining suggestions: pairs below threshold across same-name clusters.
    seen_sug: set[tuple[str, str]] = set()
    for ps in pair_scores:
        if _should_auto_merge(ps):
            continue
        if (ps.a, ps.b) in rejections:
            continue
        # Don't suggest pairs the auto-merge already joined.
        if uf.find(ps.a) == uf.find(ps.b):
            continue
        key = tuple(sorted((ps.a, ps.b)))
        if key in seen_sug:
            continue
        seen_sug.add(key)
        result.suggestions.append({
            "a": ps.a, "b": ps.b,
            "display_name": name_of.get(ps.a) or evidence[ps.a].display_name,
            "score": ps.score,
            "coauthor": ps.coauthor,
            "affiliation": ps.affiliation,
            "topic": ps.topic,
            "reasons": ps.reasons,
            "paper_counts": {
                ps.a: len(evidence[ps.a].paper_ids),
                ps.b: len(evidence[ps.b].paper_ids),
            },
            "affiliations": {
                ps.a: profiles[ps.a].affiliation or evidence[ps.a].institutions.most_common(1)[0][0] if evidence[ps.a].institutions else None,
                ps.b: profiles[ps.b].affiliation or evidence[ps.b].institutions.most_common(1)[0][0] if evidence[ps.b].institutions else None,
            },
        })
    result.suggestions.sort(key=lambda s: -s["score"])
    result.suggested = len(result.suggestions)
    return result


def _upsert_alias(
    session: Session,
    *,
    alias_aid: str,
    canonical_aid: str,
    display_name: str | None,
    source: str,
    confidence: float,
    reasons: list[str] | None,
) -> ScholarAlias:
    existing = session.exec(
        select(ScholarAlias).where(
            ScholarAlias.alias_aid == alias_aid,
            ScholarAlias.canonical_aid == canonical_aid,
        )
    ).first()
    if existing is None:
        existing = ScholarAlias(
            alias_aid=alias_aid,
            canonical_aid=canonical_aid,
        )
    existing.display_name = display_name
    existing.source = source
    existing.confidence = confidence
    existing.reasons = reasons
    session.add(existing)
    return existing


# ---------------------------------------------------------------------------
# Resolution (used by the aggregator)
# ---------------------------------------------------------------------------


def resolve_aid(session: Session, aid: str) -> str:
    """Follow alias chain ``alias_aid -> canonical_aid`` to its root.

    Chains are short (one hop in practice) but we loop defensively against
    future re-pointing. A rejected alias is never a resolution (treated as no
    mapping).
    """
    if not aid:
        return aid
    seen: set[str] = set()
    current = aid
    for _ in range(8):
        if current in seen:
            return current
        seen.add(current)
        row = session.exec(
            select(ScholarAlias).where(
                ScholarAlias.alias_aid == current,
                ScholarAlias.source != "reject",
            )
        ).first()
        if row is None:
            return current
        current = row.canonical_aid
    return current


def apply_user_merge(
    session: Session, *, alias_aid: str, canonical_aid: str, display_name: str | None
) -> ScholarAlias:
    """Record a user-confirmed merge and clear any prior rejection between them."""
    # If the target itself is an alias, resolve first so we don't nest.
    canonical_aid = resolve_aid(session, canonical_aid)
    alias_root = resolve_aid(session, alias_aid)
    if alias_root == canonical_aid:
        # Already merged; return existing row if any.
        existing = session.exec(
            select(ScholarAlias).where(
                ScholarAlias.alias_aid == alias_aid,
                ScholarAlias.canonical_aid == canonical_aid,
            )
        ).first()
        if existing:
            return existing
    # Drop any prior rejection in either direction.
    for r in session.exec(
        select(ScholarAlias).where(ScholarAlias.source == "reject")
    ).all():
        if {r.alias_aid, r.canonical_aid} == {alias_aid, canonical_aid}:
            session.delete(r)
    row = _upsert_alias(
        session,
        alias_aid=alias_aid,
        canonical_aid=canonical_aid,
        display_name=display_name,
        source="user",
        confidence=1.0,
        reasons=["user-confirmed"],
    )
    session.commit()
    return row


def apply_user_reject(
    session: Session, *, a: str, b: str, display_name: str | None
) -> ScholarAlias:
    """Record that two A-IDs are NOT the same person (suppresses auto-merge)."""
    row = _upsert_alias(
        session,
        alias_aid=a,
        canonical_aid=b,
        display_name=display_name,
        source="reject",
        confidence=1.0,
        reasons=["user-rejected"],
    )
    # If an auto/user merge existed in either direction, remove it.
    for r in session.exec(
        select(ScholarAlias).where(
            ScholarAlias.alias_aid.in_([a, b]),
            ScholarAlias.canonical_aid.in_([a, b]),
            ScholarAlias.source != "reject",
        )
    ).all():
        session.delete(r)
    session.commit()
    return row
