"""Catalog of every LLM prompt the app issues.

Centralised so the Usage page (and any future "what does this app actually
ask the LLM" view) can list them in one place. The system prompts are read
directly from the modules that own them; user-prompt templates are described
rather than re-implemented so the source of truth stays where the call
site is.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from carrel.api import chat as _chat
from carrel.api import wiki_chat as _wiki_chat
from carrel.pipeline import paper_dedup_judge as _dedup_judge
from carrel.pipeline import paper_extract as _paper_extract
from carrel.pipeline import summarize as _summarize
from carrel.pipeline import topics as _topics
from carrel.pipeline.wiki import concept_compile as _concept_compile
from carrel.pipeline.wiki import question_compile as _question_compile
from carrel.pipeline.wiki import scholar_compile as _scholar_compile


@dataclass(frozen=True)
class PromptEntry:
    feature: str
    label: str
    source: str  # "file:line" of the system prompt constant
    system: str
    user_template: str
    notes: str = ""


def _summarize_user() -> str:
    parts = [
        "Title: {title}",
        "Authors: {authors}",
        "Venue/date: {venue_date}",
        "Abstract: {abstract}",
        "Paper text (parsed from PDF; truncated): {body}",
        "→ Return JSON: {tldr_en, tldr_zh, summary_zh, keywords[]}",
    ]
    return "\n\n".join(parts)


def _extract_user() -> str:
    return (
        "Body of a parsed paper (head + tail sections, image lines dropped):\n"
        "{body}\n\n"
        "→ Return JSON: {concepts[{term, category, aliases[], quote}], "
        "questions[{question, quote}]}"
    )


def _topics_user() -> str:
    parts = [
        "Title: {title}",
        "Authors: {authors}",
        "Venue/date: {venue_date}",
        "Source categories: {categories}",
        "Keywords: {keywords}",
        "Abstract: {abstract}",
        "Existing topic names (reuse verbatim if it fits): {existing_topics}",
        "→ Return JSON: {topics[{name, description}]}",
    ]
    return "\n\n".join(parts)


def _dedup_judge_user() -> str:
    return (
        "PAPER A\n  id: …, doi: …, arxiv_id: …, s2_paper_id: …, journal_doi: …, "
        "title: …, authors: …, venue: …, year: …, abstract: …\n\n"
        "PAPER B\n  id: …, doi: …, arxiv_id: …, s2_paper_id: …, journal_doi: …, "
        "title: …, authors: …, venue: …, year: …, abstract: …\n\n"
        "→ Return JSON: {verdict, confidence, reasons[]}"
    )


def _scholar_user() -> str:
    return (
        "Researcher: {name}\n"
        "Affiliation: {affiliation}\n"
        "OpenAlex profile: {works_count, h_index, cited_by_count}\n"
        "Library papers by this researcher ({N} shown, newest first):\n"
        "  [i] title / venue / year / co-authors / keywords / abstract\n"
        "Previous compiled section (optional): {old_body}\n\n"
        "→ Return JSON: {summary, research_lines[], trajectory, evolving_views, "
        "key_collaborators[{name, aid, reason}], tags[], confidence}"
    )


def _concept_user() -> str:
    return (
        "Concept: {term_display}\n"
        "Library papers mentioning this concept ({N} shown, newest first):\n"
        "  [i] title / venue / year / abstract\n"
        "Previous version of this page (optional): {old_body}\n\n"
        "→ Return JSON: {summary, tags[], confidence}"
    )


def _question_user() -> str:
    return (
        "Question: {question_display}\n"
        "Library papers raising this question ({N} shown, newest first):\n"
        "  [i] title / venue / year / abstract\n"
        "Previous version of this page (optional): {old_body}\n\n"
        "→ Return JSON: {summary, why_it_matters, confidence}"
    )


def _paper_chat_user() -> str:
    return (
        "<paper-context>\n"
        "Title: {title}\n"
        "Authors: {authors}\n\n"
        "{retrieved_chunks | full_text_truncated}\n"
        "</paper-context>\n\n"
        "依据以上论文片段回答用户的问题。\n"
        "+ chat history (most recent N turns)"
    )


def _wiki_chat_user() -> str:
    return (
        "<wiki-context>\n"
        "Page i (kind:slug) — Title: …\n  {body}\n\n----\n\n"
        "(top-k pages by synopsis embedding)\n"
        "</wiki-context>\n\n"
        "依据以上 wiki 页面回答用户的问题。\n"
        "+ chat history (most recent N turns)"
    )


def list_prompts() -> list[dict[str, Any]]:
    """Return the prompt catalog as a list of plain dicts (JSON-friendly)."""
    entries: list[PromptEntry] = [
        PromptEntry(
            feature="summarize",
            label="Paper summarize (M4)",
            source="carrel/pipeline/summarize.py:_SYSTEM_PROMPT",
            system=_summarize._SYSTEM_PROMPT,
            user_template=_summarize_user(),
            notes=(
                "One call per paper; produces tldr_en / tldr_zh / summary_zh / "
                "keywords[] as a single JSON object."
            ),
        ),
        PromptEntry(
            feature="extract",
            label="Paper concept + question extract (M8b)",
            source="carrel/pipeline/paper_extract.py:_SYSTEM_PROMPT",
            system=_paper_extract._SYSTEM_PROMPT,
            user_template=_extract_user(),
            notes=(
                "Per-paper extraction of METHOD/THEORY/DATASET/DOMAIN/PHENOMENON "
                "concepts and open questions, every item grounded by a verbatim "
                "quote from the body."
            ),
        ),
        PromptEntry(
            feature="topics",
            label="Topics classify",
            source="carrel/pipeline/topics.py:_SYSTEM_PROMPT",
            system=_topics._SYSTEM_PROMPT,
            user_template=_topics_user(),
            notes=(
                "Metadata-only classification into 1–4 broad research themes; "
                "reuses existing topic names verbatim when they fit."
            ),
        ),
        PromptEntry(
            feature="dedup_judge",
            label="Paper dedup LLM judge (M10.6)",
            source="carrel/pipeline/paper_dedup_judge.py:_SYSTEM_PROMPT",
            system=_dedup_judge._SYSTEM_PROMPT,
            user_template=_dedup_judge_user(),
            notes=(
                "Resolves borderline paper pairs that strong anchors can't. "
                "Verdicts cached in paper_dedup_verdicts keyed on "
                "(paper_a, paper_b, prompt_hash)."
            ),
        ),
        PromptEntry(
            feature="wiki_scholar",
            label="Wiki · scholar compile (M8a)",
            source="carrel/pipeline/wiki/scholar_compile.py:_SYSTEM_PROMPT",
            system=_scholar_compile._SYSTEM_PROMPT,
            user_template=_scholar_user(),
            notes=(
                "Synthesises a researcher page from in-library paper metadata + "
                "abstracts. Cites each claim with a [^n] footnote."
            ),
        ),
        PromptEntry(
            feature="wiki_concept",
            label="Wiki · concept compile (M8b)",
            source="carrel/pipeline/wiki/concept_compile.py:_SYSTEM_PROMPT",
            system=_concept_compile._SYSTEM_PROMPT,
            user_template=_concept_user(),
            notes=(
                "One page per recurring concept, grounded only in the supplied "
                "paper snippets."
            ),
        ),
        PromptEntry(
            feature="wiki_question",
            label="Wiki · question compile (M8b)",
            source="carrel/pipeline/wiki/question_compile.py:_SYSTEM_PROMPT",
            system=_question_compile._SYSTEM_PROMPT,
            user_template=_question_user(),
            notes=(
                "One page per open question the library's papers keep raising."
            ),
        ),
        PromptEntry(
            feature="paper_chat",
            label="Paper chat (RAG)",
            source="carrel/api/chat.py:_SYSTEM_PROMPT",
            system=_chat._SYSTEM_PROMPT,
            user_template=_paper_chat_user(),
            notes=(
                "Streaming chat over a single paper. Context is the top-k chunks "
                "by embedding similarity, falling back to the truncated full text."
            ),
        ),
        PromptEntry(
            feature="wiki_chat",
            label="Wiki chat (RAG)",
            source="carrel/api/wiki_chat.py:_SYSTEM_PROMPT",
            system=_wiki_chat._SYSTEM_PROMPT,
            user_template=_wiki_chat_user(),
            notes=(
                "Streaming chat over the whole wiki. Context is the top-k pages "
                "by synopsis-embedding similarity."
            ),
        ),
    ]
    return [
        {
            "feature": e.feature,
            "label": e.label,
            "source": e.source,
            "system": e.system,
            "user_template": e.user_template,
            "notes": e.notes,
        }
        for e in entries
    ]


__all__ = ["list_prompts", "PromptEntry"]
