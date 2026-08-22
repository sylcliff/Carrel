from carrel.pipeline.wiki._slug import page_path, scholar_slug, slugify


def test_slugify_ascii_lowercase_and_collapse():
    assert slugify("  RAG is Gréat!!!  ") == "rag-is-great"
    assert slugify("---") == "untitled"


def test_scholar_slug_aid_and_name_only():
    assert scholar_slug(" A5013214678 ", "Jane Doe") == "A5013214678"
    assert scholar_slug(None, "Jane Doe") == "name--jane-doe"
    assert scholar_slug("not-an-aid", "Jane Doe") == "name--jane-doe"


def test_page_path():
    assert page_path("scholar", "A5013214678") == "wiki/scholars/A5013214678.md"
