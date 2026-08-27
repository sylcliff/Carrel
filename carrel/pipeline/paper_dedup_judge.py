"""LLM judge for paper deduplication (M10.6).

The deterministic pipeline in :mod:`carrel.pipeline.paper_dedup` handles
strong-anchor matches (DOI / arXiv / s2 / journal-doi bridge) and auto-merges
them. The remaining borderline pairs land in a soft band that the
deterministic score alone can't resolve — two papers with the same title
prefix and overlapping authors, but different DOIs and missing s2 ids. That
band is what this module judges.

Three implementations of the :class:`PaperPairJudge` protocol are provided:

- :class:`DeterministicJudge` — uses only the existing strong-anchor signals
  (no LLM). Returns "same" / "different" / "uncertain" based on whether any
  strong anchor fires. CI-friendly; no network.
- :class:`LLMJudge` — calls the configured LLM with a structured prompt and
  caches the verdict by (paper_a, paper_b, prompt_hash) in
  ``paper_dedup_verdicts``. Honors a per-run call budget so a large
  borderline queue can't run the meter away.
- :class:`CompositeJudge` — strong anchors short-circuit to "same";
  borderline pairs are routed to the LLM. If no LLM is configured, the
  composite is a pure deterministic judge and borderline pairs flow through
  unchanged.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from sqlmodel import Session, select

from carrel import prompts_runtime
from carrel.config import LLMConfig
from carrel.llm import LLMError, chat_json
from carrel.models import Paper, PaperDedupVerdict
from carrel.pipeline.paper_dedup import (
    PaperPairJudge,
    PaperPairVerdict,
    _detect_strong_anchors,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

#: System prompt for the LLM judge. Bump
#: :attr:`LLMConfig.paper_dedup_judge_prompt_version` when this string
#: changes — cached verdicts are keyed on (model, version, content) so a
#: bumped version transparently invalidates them.
_SYSTEM_PROMPT = """\
You are an expert research librarian judging whether two academic paper \
records refer to the SAME paper (not just similar topics).

Use ALL the metadata available: DOIs, arXiv ids, Semantic Scholar paper ids, \
titles, authors, abstracts, venues, and publication dates.

Rules:
- "same" = these are the same work. A preprint and its journal version count \
  as the same work. arXiv v1 vs v2 of the same preprint counts as the same \
  work. A conference paper and its journal extension (different title) does \
  NOT count as same.
- "different" = these are distinct works even if titles overlap (a paper and \
  its follow-up; a paper and a translation; two unrelated papers with the \
  same first-author surname; a paper and an unrelated survey by the same \
  authors).
- "uncertain" = the metadata alone is not enough to decide; lean uncertain \
  rather than guessing.

Respond with ONLY a JSON object in this exact shape:
{"verdict": "same"|"different"|"uncertain", "confidence": 0.0-1.0, "reasons": ["...", "..."]}
"""


def _paper_block(p: Paper) -> str:
    """Render one paper's metadata as a structured block for the user prompt."""
    return (
        f"id: {p.id}\n"
        f"doi: {p.doi or ''}\n"
        f"arxiv_id: {p.arxiv_id or ''}\n"
        f"s2_paper_id: {p.s2_paper_id or ''}\n"
        f"journal_doi: {p.journal_doi or ''}\n"
        f"title: {(p.title or '')[:300]}\n"
        f"authors: {_fmt_authors(p)}\n"
        f"venue: {p.venue or ''}\n"
        f"year: {_year_str(p)}\n"
        f"abstract: {(p.abstract or '')[:1200]}\n"
    )


_USER_TEMPLATE = (
    "PAPER A\n{paper_a_block}\n\n"
    "PAPER B\n{paper_b_block}\n\n"
    "{trailer}"
)

_DEFAULT_TRAILER = "Reply with JSON only — no prose, no markdown."


def _build_user_prompt(a: Paper, b: Paper) -> str:
    """Render the two papers' metadata as a structured prompt.

    The trailer (final instruction line) is editable via the user template
    override; if the user removes the ``{trailer}`` placeholder, we fall
    back to a no-trailer render so a missing placeholder doesn't crash.
    """
    template = prompts_runtime.get_user_template("dedup_judge", _USER_TEMPLATE)
    try:
        return template.format(
            paper_a_block=_paper_block(a),
            paper_b_block=_paper_block(b),
            trailer=_DEFAULT_TRAILER,
        )
    except KeyError:
        return template.replace("{trailer}", _DEFAULT_TRAILER).format(
            paper_a_block=_paper_block(a),
            paper_b_block=_paper_block(b),
        )


def _fmt_authors(p: Paper) -> str:
    parts = []
    for a in p.authors or []:
        if not isinstance(a, dict):
            continue
        name = (a.get("name") or "").strip()
        affil = (a.get("affiliation") or "").strip()
        if name and affil:
            parts.append(f"{name} ({affil})")
        elif name:
            parts.append(name)
    return "; ".join(parts)


def _year_str(p: Paper) -> str:
    return str(p.publication_date.year) if p.publication_date else ""


def _prompt_hash(
    a: Paper,
    b: Paper,
    model: str,
    prompt_version: int,
    *,
    system: str,
    user_template: str,
) -> str:
    """Stable sha256 of (paper content + model + prompt_version + prompt text).

    The pair is sorted before hashing so the LLM and the lookup key agree on
    order. Bumping the prompt version yields a fresh hash automatically;
    editing the system prompt or user template in the UI also yields a fresh
    hash (via the SHA256 prefixes of the resolved texts) so cached verdicts
    transparently invalidate without DB surgery.
    """
    a_sig, b_sig = sorted(
        [_paper_signature(a), _paper_signature(b)],
        key=lambda s: (s.get("id") or ""),
    )
    payload = {
        "v": prompt_version,
        "model": model,
        "sys_sha": hashlib.sha256(system.encode("utf-8")).hexdigest()[:16],
        "ut_sha": hashlib.sha256(user_template.encode("utf-8")).hexdigest()[:16],
        "a": a_sig,
        "b": b_sig,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _paper_signature(p: Paper) -> dict[str, Any]:
    return {
        "id": p.id,
        "doi": p.doi,
        "arxiv_id": p.arxiv_id,
        "s2_paper_id": p.s2_paper_id,
        "journal_doi": p.journal_doi,
        "title": p.title,
        "abstract": p.abstract,
        "venue": p.venue,
        "year": _year_str(p),
    }


# ---------------------------------------------------------------------------
# Deterministic judge (no LLM)
# ---------------------------------------------------------------------------


class DeterministicJudge:
    """Strong-anchor-only judge. No network calls; safe for CI / offline runs.

    A pair is "same" if any of the strong anchors detected in
    :func:`carrel.pipeline.paper_dedup._detect_strong_anchors` fires (DOI,
    arXiv, journal-doi bridge, s2). Otherwise the verdict is "uncertain" with
    confidence 0.5 — borderline pairs flow through unchanged so the user
    can review them in the panel.
    """

    def judge(self, a: Paper, b: Paper) -> PaperPairVerdict:
        anchors = _detect_strong_anchors(a, b)
        if anchors:
            return PaperPairVerdict(
                verdict="same",
                confidence=1.0,
                reasons=[f"strong anchor: {x}" for x in anchors],
                model="deterministic",
                prompt_version=0,
            )
        return PaperPairVerdict(
            verdict="uncertain",
            confidence=0.5,
            reasons=["no strong anchor; deterministic judge stays neutral"],
            model="deterministic",
            prompt_version=0,
        )


# ---------------------------------------------------------------------------
# LLM judge (with cache)
# ---------------------------------------------------------------------------


class LLMJudge:
    """Calls the LLM and caches the verdict in :class:`PaperDedupVerdict`.

    The cache key is ``(min(a,b), max(b,a), prompt_hash)`` so the same pair
    does not double-bill the LLM within a prompt_version. Cached verdicts
    carry the model that produced them, so switching models gives a fresh
    hash and a fresh LLM call.

    A per-run ``calls_remaining`` counter caps the LLM call count so a large
    borderline queue cannot run the meter. The caller (the dedup run) tops
    it up at the start of each scan with the configured
    ``paper_dedup_judge_max_calls_per_run``.
    """

    def __init__(
        self,
        session: Session,
        cfg: LLMConfig,
        *,
        calls_remaining: int = 200,
    ) -> None:
        self.session = session
        self.cfg = cfg
        self.model = (
            cfg.paper_dedup_judge_model
            or cfg.chat_model
            or cfg.summarize_model
        )
        self.fallback = (
            cfg.paper_dedup_judge_fallback
            or cfg.chat_fallback_model
            or cfg.fallback_model
        )
        self.prompt_version = cfg.paper_dedup_judge_prompt_version
        self.calls_remaining = calls_remaining

    def budget_left(self) -> int:
        return max(0, self.calls_remaining)

    def judge(self, a: Paper, b: Paper) -> PaperPairVerdict:
        system = prompts_runtime.get_system("dedup_judge", _SYSTEM_PROMPT, session=self.session)
        user_template = prompts_runtime.get_user_template("dedup_judge", _USER_TEMPLATE, session=self.session)
        ph = _prompt_hash(
            a, b, self.model, self.prompt_version,
            system=system, user_template=user_template,
        )
        a_key, b_key = sorted((a.id, b.id))

        cached = self.session.exec(
            select(PaperDedupVerdict).where(
                PaperDedupVerdict.paper_a_id == a_key,
                PaperDedupVerdict.paper_b_id == b_key,
                PaperDedupVerdict.prompt_hash == ph,
            )
        ).first()
        if cached is not None:
            return PaperPairVerdict(
                verdict=cached.verdict,
                confidence=cached.confidence,
                reasons=list(cached.reasons or []),
                model=cached.model,
                prompt_version=cached.prompt_version,
            )

        if self.calls_remaining <= 0:
            return PaperPairVerdict(
                verdict="uncertain",
                confidence=0.5,
                reasons=["llm budget exhausted for this run"],
                model=self.model,
                prompt_version=self.prompt_version,
            )

        self.calls_remaining -= 1
        verdict = self._call_llm(a, b)
        # Persist (cache). Even "uncertain" verdicts are cached so we don't
        # hammer the LLM on the same pair every run.
        try:
            self.session.add(PaperDedupVerdict(
                paper_a_id=a_key,
                paper_b_id=b_key,
                prompt_hash=ph,
                model=verdict.model or self.model,
                prompt_version=verdict.prompt_version or self.prompt_version,
                verdict=verdict.verdict,
                confidence=verdict.confidence,
                reasons=list(verdict.reasons),
            ))
            self.session.commit()
        except Exception as e:  # noqa: BLE001 - cache write is best-effort
            logger.warning("could not cache LLM verdict: %s", e)
            self.session.rollback()
        return verdict

    def _call_llm(self, a: Paper, b: Paper) -> PaperPairVerdict:
        from carrel import usage as _usage
        system = prompts_runtime.get_system("dedup_judge", _SYSTEM_PROMPT, session=self.session)
        try:
            raw = chat_json(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": _build_user_prompt(a, b)},
                ],
                model=self.model,
                fallback_model=self.fallback,
                temperature=0.0,
                feature="dedup_judge",
                on_usage=_usage.make_usage_callback(
                    self.session, feature="dedup_judge",
                ),
            )
        except LLMError as e:
            logger.warning("LLM judge call failed: %s", e)
            return PaperPairVerdict(
                verdict="uncertain",
                confidence=0.5,
                reasons=[f"llm error: {e}"],
                model=self.model,
                prompt_version=self.prompt_version,
            )

        verdict = str(raw.get("verdict", "uncertain")).lower().strip()
        if verdict not in ("same", "different", "uncertain"):
            verdict = "uncertain"
        try:
            conf = float(raw.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        reasons = raw.get("reasons") or []
        if not isinstance(reasons, list):
            reasons = [str(reasons)]
        return PaperPairVerdict(
            verdict=verdict,
            confidence=max(0.0, min(1.0, conf)),
            reasons=[str(r) for r in reasons],
            model=self.model,
            prompt_version=self.prompt_version,
        )


# ---------------------------------------------------------------------------
# Composite judge
# ---------------------------------------------------------------------------


class CompositeJudge:
    """Strong anchor short-circuits to "same"; borderline routed to LLM.

    With ``llm=None`` the composite is effectively the deterministic judge —
    useful when the operator has not configured an LLM key but still wants
    a single judge object to pass to :func:`run_dedup`.
    """

    def __init__(
        self,
        det: DeterministicJudge | None = None,
        llm: LLMJudge | None = None,
    ) -> None:
        self.det = det or DeterministicJudge()
        self.llm = llm

    def judge(self, a: Paper, b: Paper) -> PaperPairVerdict:
        det = self.det.judge(a, b)
        if det.verdict == "same":
            return det
        if self.llm is None:
            return det
        return self.llm.judge(a, b)


# ---------------------------------------------------------------------------
# Test seam
# ---------------------------------------------------------------------------


def build_judge(
    session: Session,
    cfg: LLMConfig,
    *,
    calls_remaining: int | None = None,
) -> PaperPairJudge:
    """Compose the production judge: deterministic + LLM (if available).

    Callers (e.g. ``run_dedup``) use this to get the right judge with the
    right budget for one scan. Tests can substitute their own judge.
    """
    budget = (
        calls_remaining
        if calls_remaining is not None
        else cfg.paper_dedup_judge_max_calls_per_run
    )
    llm = LLMJudge(session, cfg, calls_remaining=budget)
    return CompositeJudge(det=DeterministicJudge(), llm=llm)


__all__ = [
    "DeterministicJudge",
    "LLMJudge",
    "CompositeJudge",
    "build_judge",
]
