"""Tests for query spelling correction (symspell wrapper)."""
from __future__ import annotations

from carrel import spelling


def test_corrects_obvious_typo():
    corrected, original = spelling.correct_query("transfomer")
    assert corrected == "transformer"
    assert original == "transfomer"


def test_no_correction_when_already_correct():
    corrected, original = spelling.correct_query("transformer")
    assert corrected == "transformer"
    assert original is None


def test_protected_jargon_passes_through():
    for term in ["RAG", "BERT", "LLM", "arxiv"]:
        corrected, original = spelling.correct_query(term)
        assert corrected == term
        assert original is None, f"{term} should not be 'corrected'"


def test_doi_passes_through():
    q = "10.1038/s41586-020-2649-2"
    corrected, original = spelling.correct_query(q)
    assert corrected == q
    assert original is None


def test_arxiv_id_passes_through():
    q = "1901.00001"
    corrected, original = spelling.correct_query(q)
    assert corrected == q
    assert original is None


def test_empty_query():
    corrected, original = spelling.correct_query("")
    assert corrected == ""
    assert original is None


def test_multi_word_phrase():
    corrected, original = spelling.correct_query("retreival augmented genration")
    assert "retrieval" in corrected
    assert "generation" in corrected
    assert original is not None


def test_whitespace_normalized():
    corrected, _ = spelling.correct_query("  transformer   network  ")
    assert corrected == "transformer network"


def test_cjk_passes_through():
    # SymSpell's dictionary is English-only; don't touch non-latin input.
    q = "大型语言模型"
    corrected, original = spelling.correct_query(q)
    assert corrected == q
    assert original is None
