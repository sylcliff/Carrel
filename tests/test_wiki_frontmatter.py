from carrel.pipeline.wiki import _frontmatter


def test_dump_parse_roundtrip():
    meta = {"kind": "scholar", "title": "Jane Doe", "tags": ["rag", "nlp"]}
    text = _frontmatter.dump(meta, "# Jane Doe\n\nBody")
    parsed, body = _frontmatter.parse(text)
    assert parsed == meta
    assert body == "# Jane Doe\n\nBody"


def test_malformed_frontmatter_returns_original_text():
    text = "---\ntags: [broken\n---\nbody"
    assert _frontmatter.parse(text) == ({}, text)


def test_dump_shape():
    text = _frontmatter.dump({"title": "Example"}, "\n# Example\n")
    assert text.startswith("---\ntitle: Example\n---\n\n# Example")
    assert text.count("---") == 2
