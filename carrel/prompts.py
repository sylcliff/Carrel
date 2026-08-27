"""Catalog of every LLM prompt the app issues.

Centralised so the Usage page (and the Agent / AgentPipeline pages) can
list the prompts in one place, and so the prompt editor on those pages
has a single source of truth for which ``feature`` names exist and what
shape their templates have.

**system prompt** — read directly from the module that owns the
constant. A user override (see :mod:`carrel.prompts_runtime`) replaces
it at call time; this catalog only knows the default.

**user template** — the default template string the call site feeds to
``str.format(**kwargs)`` to build the user message. Because the call
site owns the actual ``format(**kwargs)`` step, edits to the template
only make sense if the user keeps the same placeholder names. The
``placeholders`` field below is the validator's source of truth for
what those names are.

The catalog now also reports ``overridden`` / ``override_updated_at`` /
``placeholders`` / ``danger`` so the UI can render a "modified" badge
and warn on edits to prompts whose blast radius is wide (chat flows).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlmodel import Session, select

from carrel.api import chat as _chat
from carrel.api import wiki_chat as _wiki_chat
from carrel.models import PromptOverride
from carrel.pipeline import paper_dedup_judge as _dedup_judge
from carrel.pipeline import paper_extract as _paper_extract
from carrel.pipeline import summarize as _summarize
from carrel.pipeline import topics as _topics
from carrel.pipeline.wiki import concept_compile as _concept_compile
from carrel.pipeline.wiki import question_compile as _question_compile
from carrel.pipeline.wiki import scholar_compile as _scholar_compile
from carrel.pipeline.wiki import scholar_enrich as _scholar_enrich


@dataclass(frozen=True)
class PromptEntry:
    feature: str
    label: str
    source: str
    notes: str
    placeholders: tuple[str, ...]
    danger: bool
    system_default: str
    user_template_default: str
    system: str
    user_template: str
    overridden: bool
    override_updated_at: datetime | None = field(default=None)


_SUMMARIZE_USER_TEMPLATE = (
    "Title: {title}\n"
    "Authors: {authors}\n"
    "Venue/date: {venue_date}\n"
    "Abstract:\n{abstract}\n\n"
    "Paper text (parsed from PDF; may contain OCR noise; truncated):\n\n"
    "{body}\n\n"
    "Return the JSON object now, with no commentary."
)

_EXTRACT_USER_TEMPLATE = "{body}"

_TOPICS_USER_TEMPLATE = (
    "Title: {title}\n"
    "Authors: {authors}\n"
    "Venue/date: {venue_date}\n"
    "Source categories: {categories}\n"
    "Keywords: {keywords}\n\n"
    "Abstract:\n{abstract}\n\n"
    "{existing_topics_block}\n\n"
    "Return the JSON object now, with no commentary."
)

_DEDUP_JUDGE_USER_TEMPLATE = (
    "PAPER A\n{paper_a_block}\n\n"
    "PAPER B\n{paper_b_block}\n\n"
    "{trailer}"
)

_WIKI_SCHOLAR_USER_TEMPLATE = "{parts}\n\nReturn the JSON object now, with no commentary."
_WIKI_CONCEPT_USER_TEMPLATE = "{parts}\n\nReturn the JSON object now, with no commentary."
_WIKI_QUESTION_USER_TEMPLATE = "{parts}\n\nReturn the JSON object now, with no commentary."

_PAPER_CHAT_USER_TEMPLATE = (
    "<paper-context>\n"
    "Title: {title}\n"
    "Authors: {authors}\n\n"
    "{context_block}\n"
    "</paper-context>\n\n"
    "依据以上论文片段回答用户的问题。"
)

_WIKI_CHAT_USER_TEMPLATE = (
    "<wiki-context>\n{context_block}\n</wiki-context>\n\n"
    "依据以上 wiki 页面回答用户的问题。"
)

_WIKI_ENRICH_SYSTEM_TEMPLATE = """You are a research assistant enriching a Carrel scholar profile.

The scholar is **{name}** (slug: `{slug}`). Your job is to write a short
Web research note that the user will see on their wiki page. The page
already has compiled sections (Summary, Research lines, etc.) from in-library
papers; do NOT try to rewrite them. You only have access to two tools:

1. brave_search__brave_web_search(query, count?, country?) - search the
   live web for up-to-date information about the scholar.
2. builtin__save_scholar_note(slug, section_title, content) - append a
   note inside the scholar page's preserved user-section. Pass
   slug="{slug}", section_title="Web research", and a synthesized body.

Hard rules:
- Use ONLY facts that came back from brave_search__brave_web_search.
- Call save_scholar_note exactly ONCE with the synthesized body, then stop.
- If the first search call returns an error string, still call
  save_scholar_note with a one-line body so the user sees the button worked."""

_WIKI_ENRICH_USER_TEMPLATE = "Research the scholar {name}{affiliation_hint} and save the result."


def list_prompts(session: Session) -> list[dict[str, Any]]:
    """Return the prompt catalog as a list of plain dicts (JSON-friendly)."""
    entries: list[PromptEntry] = [
        PromptEntry(
            feature="summarize",
            label="Paper summarize (M4)",
            source="carrel/pipeline/summarize.py:_SYSTEM_PROMPT",
            system_default=_summarize._SYSTEM_PROMPT,
            user_template_default=_SUMMARIZE_USER_TEMPLATE,
            placeholders=("title", "authors", "venue_date", "abstract", "body"),
            danger=False,
            notes="One call per paper; produces tldr_en / tldr_zh / summary_zh / keywords[] as a single JSON object.",
            system=_summarize._SYSTEM_PROMPT,
            user_template=_SUMMARIZE_USER_TEMPLATE,
            overridden=False,
        ),
        PromptEntry(
            feature="extract",
            label="Paper concept + question extract (M8b)",
            source="carrel/pipeline/paper_extract.py:_SYSTEM_PROMPT",
            system_default=_paper_extract._SYSTEM_PROMPT,
            user_template_default=_EXTRACT_USER_TEMPLATE,
            placeholders=("body",),
            danger=False,
            notes="Per-paper extraction of METHOD/THEORY/DATASET/DOMAIN/PHENOMENON concepts and open questions, every item grounded by a verbatim quote from the body.",
            system=_paper_extract._SYSTEM_PROMPT,
            user_template=_EXTRACT_USER_TEMPLATE,
            overridden=False,
        ),
        PromptEntry(
            feature="topics",
            label="Topics classify",
            source="carrel/pipeline/topics.py:_SYSTEM_PROMPT",
            system_default=_topics._SYSTEM_PROMPT,
            user_template_default=_TOPICS_USER_TEMPLATE,
            placeholders=("title", "authors", "venue_date", "categories", "keywords", "abstract", "existing_topics_block"),
            danger=False,
            notes="Metadata-only classification into 1-4 broad research themes; reuses existing topic names verbatim when they fit.",
            system=_topics._SYSTEM_PROMPT,
            user_template=_TOPICS_USER_TEMPLATE,
            overridden=False,
        ),
        PromptEntry(
            feature="dedup_judge",
            label="Paper dedup LLM judge (M10.6)",
            source="carrel/pipeline/paper_dedup_judge.py:_SYSTEM_PROMPT",
            system_default=_dedup_judge._SYSTEM_PROMPT,
            user_template_default=_DEDUP_JUDGE_USER_TEMPLATE,
            placeholders=("paper_a_block", "paper_b_block", "trailer"),
            danger=False,
            notes="Resolves borderline paper pairs that strong anchors can't. Verdicts cached in paper_dedup_verdicts keyed on (paper_a, paper_b, prompt_hash).",
            system=_dedup_judge._SYSTEM_PROMPT,
            user_template=_DEDUP_JUDGE_USER_TEMPLATE,
            overridden=False,
        ),
        PromptEntry(
            feature="wiki_scholar",
            label="Wiki scholar compile (M8a)",
            source="carrel/pipeline/wiki/scholar_compile.py:_SYSTEM_PROMPT",
            system_default=_scholar_compile._SYSTEM_PROMPT,
            user_template_default=_WIKI_SCHOLAR_USER_TEMPLATE,
            placeholders=("parts",),
            danger=False,
            notes="Synthesises a researcher page from in-library paper metadata + abstracts.",
            system=_scholar_compile._SYSTEM_PROMPT,
            user_template=_WIKI_SCHOLAR_USER_TEMPLATE,
            overridden=False,
        ),
        PromptEntry(
            feature="wiki_concept",
            label="Wiki concept compile (M8b)",
            source="carrel/pipeline/wiki/concept_compile.py:_SYSTEM_PROMPT",
            system_default=_concept_compile._SYSTEM_PROMPT,
            user_template_default=_WIKI_CONCEPT_USER_TEMPLATE,
            placeholders=("parts",),
            danger=False,
            notes="One page per recurring concept, grounded only in the supplied paper snippets.",
            system=_concept_compile._SYSTEM_PROMPT,
            user_template=_WIKI_CONCEPT_USER_TEMPLATE,
            overridden=False,
        ),
        PromptEntry(
            feature="wiki_question",
            label="Wiki question compile (M8b)",
            source="carrel/pipeline/wiki/question_compile.py:_SYSTEM_PROMPT",
            system_default=_question_compile._SYSTEM_PROMPT,
            user_template_default=_WIKI_QUESTION_USER_TEMPLATE,
            placeholders=("parts",),
            danger=False,
            notes="One page per open question the library's papers keep raising.",
            system=_question_compile._SYSTEM_PROMPT,
            user_template=_WIKI_QUESTION_USER_TEMPLATE,
            overridden=False,
        ),
        PromptEntry(
            feature="paper_chat",
            label="Paper chat (RAG)",
            source="carrel/api/chat.py:_SYSTEM_PROMPT",
            system_default=_chat._SYSTEM_PROMPT,
            user_template_default=_PAPER_CHAT_USER_TEMPLATE,
            placeholders=("title", "authors", "context_block"),
            danger=True,
            notes="Streaming chat over a single paper. Context is the top-k chunks by embedding similarity.",
            system=_chat._SYSTEM_PROMPT,
            user_template=_PAPER_CHAT_USER_TEMPLATE,
            overridden=False,
        ),
        PromptEntry(
            feature="wiki_chat",
            label="Wiki chat (RAG)",
            source="carrel/api/wiki_chat.py:_SYSTEM_PROMPT",
            system_default=_wiki_chat._SYSTEM_PROMPT,
            user_template_default=_WIKI_CHAT_USER_TEMPLATE,
            placeholders=("context_block",),
            danger=True,
            notes="Streaming chat over the whole wiki.",
            system=_wiki_chat._SYSTEM_PROMPT,
            user_template=_WIKI_CHAT_USER_TEMPLATE,
            overridden=False,
        ),
        PromptEntry(
            feature="wiki_enrich",
            label="Wiki scholar enrich (agent)",
            source="carrel/pipeline/wiki/scholar_enrich.py:_SYSTEM_TEMPLATE",
            system_default=_WIKI_ENRICH_SYSTEM_TEMPLATE,
            user_template_default=_WIKI_ENRICH_USER_TEMPLATE,
            placeholders=("slug", "name", "affiliation_hint"),
            danger=True,
            notes="Tool-using agent: searches the web (Brave) then writes a Web research note. System uses {slug, name}; user template uses {name, affiliation_hint}.",
            system=_WIKI_ENRICH_SYSTEM_TEMPLATE,
            user_template=_WIKI_ENRICH_USER_TEMPLATE,
            overridden=False,
        ),
    ]

    overrides = {row.feature: row for row in session.exec(select(PromptOverride)).all()}

    out: list[dict[str, Any]] = []
    for e in entries:
        override = overrides.get(e.feature)
        if override is None:
            out.append({
                "feature": e.feature,
                "label": e.label,
                "source": e.source,
                "notes": e.notes,
                "placeholders": list(e.placeholders),
                "danger": e.danger,
                "system": e.system_default,
                "user_template": e.user_template_default,
                "system_default": e.system_default,
                "user_template_default": e.user_template_default,
                "overridden": False,
                "override_updated_at": None,
            })
            continue
        out.append({
            "feature": e.feature,
            "label": e.label,
            "source": e.source,
            "notes": e.notes,
            "placeholders": list(e.placeholders),
            "danger": e.danger,
            "system": override.system if override.system is not None else e.system_default,
            "user_template": (
                override.user_template if override.user_template is not None
                else e.user_template_default
            ),
            "system_default": e.system_default,
            "user_template_default": e.user_template_default,
            "overridden": True,
            "override_updated_at": override.updated_at.isoformat()
            if override.updated_at else None,
        })
    return out


__all__ = ["list_prompts", "PromptEntry"]
