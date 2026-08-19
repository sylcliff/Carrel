"""Tests for carrel.sources.merge: field authority, dedup keys, RRF."""
from __future__ import annotations

from carrel.sources import merge as m
from carrel.sources.merge import MutableSearchHit as Hit


def test_doi_merge_union_ids_and_sources():
    a = Hit(title="Paper A", doi="10.1/x", openalex_id="W1",
            citation_count=5, sources={m.SOURCE_OPENALEX})
    b = Hit(title="Paper A", doi="10.1/X", s2_id="abc",
            citation_count=12, venue="NeurIPS", sources={m.SOURCE_SEMANTIC_SCHOLAR})
    out = m.merge_search_hits([a, b])
    assert len(out) == 1
    h = out[0]
    assert h.sources == {m.SOURCE_OPENALEX, m.SOURCE_SEMANTIC_SCHOLAR}
    assert h.openalex_id == "W1"
    assert h.s2_id == "abc"
    assert h.doi == "10.1/x"  # lowercased, first wins
    # max citations
    assert h.citation_count == 12
    # S2 venue wins even though OA arrived first
    assert h.venue == "NeurIPS"


def test_arxiv_id_merge_pdf_prefers_arxiv():
    a = Hit(title="Paper", arxiv_id="2401.00001",
            pdf_url="https://arxiv.org/pdf/2401.00001", sources={m.SOURCE_ARXIV})
    b = Hit(title="Paper", arxiv_id="2401.00001",
            pdf_url="https://www.semanticscholar.org/paper/123.pdf",
            sources={m.SOURCE_SEMANTIC_SCHOLAR})
    out = m.merge_search_hits([a, b])
    assert len(out) == 1
    assert out[0].pdf_url == "https://arxiv.org/pdf/2401.00001"


def test_arxiv_id_strips_version_and_prefix():
    a = Hit(title="T", arxiv_id="2401.00001v3", sources={m.SOURCE_ARXIV})
    b = Hit(title="T", arxiv_id="arxiv:2401.00001", sources={m.SOURCE_OPENALEX})
    out = m.merge_search_hits([a, b])
    assert len(out) == 1


def test_s2_and_oa_different_dois_same_s2_collide():
    # S2 row has s2 id but no DOI; OA row arrives with same s2 id later (rare
    # but possible when S2 enriches after initial OA-only hit).
    a = Hit(title="T", s2_id="s2id", sources={m.SOURCE_SEMANTIC_SCHOLAR})
    b = Hit(title="T", s2_id="s2id", doi="10.1/x",
            openalex_id="W1", sources={m.SOURCE_OPENALEX})
    out = m.merge_search_hits([a, b])
    assert len(out) == 1
    assert out[0].doi == "10.1/x"
    assert out[0].openalex_id == "W1"


def test_title_normalization_merges_when_no_ids():
    a = Hit(title="Retrieval-Augmented Generation!", sources={m.SOURCE_OPENALEX})
    b = Hit(title="retrieval augmented generation", sources={m.SOURCE_ARXIV})
    out = m.merge_search_hits([a, b])
    assert len(out) == 1


def test_title_collision_skipped_when_ids_disagree():
    # Different DOIs should not merge even if titles match.
    a = Hit(title="Same Title", doi="10.1/a", sources={m.SOURCE_OPENALEX})
    b = Hit(title="Same Title", doi="10.1/b", sources={m.SOURCE_OPENALEX})
    out = m.merge_search_hits([a, b])
    assert len(out) == 2


def test_authors_prefer_first_nonempty_oa_over_s2():
    a = Hit(title="T", authors=["Alice", "Bob"], sources={m.SOURCE_OPENALEX})
    b = Hit(title="T", authors=["A. Smith"], sources={m.SOURCE_SEMANTIC_SCHOLAR})
    out = m.merge_search_hits([b, a])
    assert out[0].authors == ["A. Smith"]
    # Reverse order: OA first keeps OA authors.
    out2 = m.merge_search_hits([a, b])
    assert out2[0].authors == ["Alice", "Bob"]


def test_tldr_s2_only():
    a = Hit(title="T", sources={m.SOURCE_OPENALEX})
    b = Hit(title="T", tldr="Short and sweet.", sources={m.SOURCE_SEMANTIC_SCHOLAR})
    out = m.merge_search_hits([a, b])
    assert out[0].tldr == "Short and sweet."


def test_abstract_first_nonempty_wins():
    a = Hit(title="T", abstract="From OA", sources={m.SOURCE_OPENALEX})
    b = Hit(title="T", abstract="From S2", sources={m.SOURCE_SEMANTIC_SCHOLAR})
    out = m.merge_search_hits([b, a])
    assert out[0].abstract == "From S2"


def test_rrf_orders_by_reciprocal_rank_sum():
    # Hit A appears at rank 1 in both sources; B rank 1 in one, absent in other.
    a = Hit(title="A", doi="10.1/a", ranks={m.SOURCE_OPENALEX: 1, m.SOURCE_SEMANTIC_SCHOLAR: 1},
            sources={m.SOURCE_OPENALEX, m.SOURCE_SEMANTIC_SCHOLAR})
    b = Hit(title="B", doi="10.1/b", ranks={m.SOURCE_OPENALEX: 2},
            sources={m.SOURCE_OPENALEX})
    out = m.reciprocal_rank_fusion([b, a])
    assert out[0].title == "A"
    assert out[1].title == "B"
