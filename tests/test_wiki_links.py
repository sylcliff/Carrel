from carrel.pipeline.wiki._links import extract_wikilinks, is_internal, resolve_link


def test_extract_wikilinks():
    md = "See [[RAG]](../concepts/rag.md) and [[Jane]](../scholars/A1.md)."
    assert extract_wikilinks(md) == [
        ("RAG", "../concepts/rag.md"),
        ("Jane", "../scholars/A1.md"),
    ]


def test_is_internal_rejects_external_forms():
    for href in ("http://x/a.md", "https://x/a.md", "mailto:a@b.md", "#part.md", "/papers/W1"):
        assert not is_internal(href)
    assert is_internal("../concepts/rag.md")


def test_resolve_link():
    assert resolve_link("wiki/scholars/A.md", "../concepts/rag.md") == ("concept", "rag")
    assert resolve_link("wiki/scholars/A.md", "https://example.com/x.md") is None
