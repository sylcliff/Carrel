"""Tests for the prompts_language helper.

Pure-function tests — no DB, no LLM stub. The helper is intentionally
small (two values + a safety net) so this file should stay small too.
"""
from __future__ import annotations

from carrel.prompts_language import language_directive, supported_languages


def test_supported_languages_returns_canonical_order():
    assert supported_languages() == ("zh", "en")


def test_zh_directive_mentions_simplified_chinese():
    out = language_directive("zh")
    assert "Simplified Chinese" in out
    assert "简体中文" in out
    # The directive should be self-contained: the model has to know
    # *which* free-form text fields it controls. If this assertion
    # ever breaks, the prompt no longer tells the LLM "all free-form
    # text fields" and we may start silently producing English text.
    assert "free-form text" in out


def test_en_directive_mentions_english():
    out = language_directive("en")
    assert "English" in out
    assert "free-form text" in out


def test_unknown_language_falls_back_to_english():
    """A typo in data/config.yaml must not silently produce English
    output that the user did not ask for, so we default to the
    current "everything in English" behaviour. (zh is the configured
    default; falling back to en on a typo is the conservative choice
    since the alternative is the same silent drift the user would
    notice first on Chinese-source papers.)"""
    assert language_directive("xx") == language_directive("en")
    assert language_directive("") == language_directive("en")
    assert language_directive("fr") == language_directive("en")
