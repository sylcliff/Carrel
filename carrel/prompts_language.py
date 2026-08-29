"""Language directive suffix appended to per-paper LLM prompts.

The LLM call sites for paper_card and summarize append the directive
returned here to the effective system prompt, so the model produces
all free-form text fields in the user's chosen language.

The two supported values mirror the two language configurations on
the LLM card. New languages only need a new entry here + a Literal
expansion in ``LLMConfig``.

Scope note
----------
This helper only governs paper_card and summarize. Paper_chat /
wiki_chat / paper_extract / topics / wiki compile / dedup_judge are
intentionally untouched: chat already follows the user's question
language, the other features are out of scope for the current
"global LLM output language" setting.
"""
from __future__ import annotations

# Keep in sync with LLMConfig.output_language Literal values in
# carrel.config.  The settings UI's `<select>` options are driven
# from :func:`supported_languages`, not from this dict directly,
# so an entry here MUST have a matching option in
# frontend/src/pages/Settings.tsx (the LLM card).
_DIRECTIVES: dict[str, str] = {
    "zh": (
        "Output language: Simplified Chinese (简体中文). All free-form text "
        "fields you produce must be written in Simplified Chinese."
    ),
    "en": (
        "Output language: English. All free-form text fields you produce "
        "must be written in English."
    ),
}


def language_directive(language: str) -> str:
    """Return the language directive suffix for ``language``.

    Unknown values fall back to the English directive so a typo in
    ``data/config.yaml`` can never silently produce English output the
    user did not ask for (zh is the configured default, so the
    fallback of "en" is the more conservative "stay close to current
    behaviour" choice for a typo like ``langusge: zzh``).
    """
    return _DIRECTIVES.get(language, _DIRECTIVES["en"])


def supported_languages() -> tuple[str, ...]:
    """Languages known to the helper.

    Returned in the canonical order used by the settings UI; the UI
    does not sort this list.
    """
    return tuple(_DIRECTIVES.keys())


__all__ = ["language_directive", "supported_languages"]
