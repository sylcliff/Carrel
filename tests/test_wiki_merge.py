from carrel.pipeline.wiki._merge import (
    EMPTY_USER_SECTION,
    ensure_user_section,
    extract_user_section,
    protect_user_section,
)


def test_extract_and_ensure_user_section():
    custom = '<section data-user="true">\nMY NOTES\n</section>'
    assert extract_user_section(f"before\n{custom}\nafter") == custom
    ensured = ensure_user_section("# Title\n\nBody")
    assert ensured.startswith("# Title")
    assert EMPTY_USER_SECTION in ensured
    assert ensured.index(EMPTY_USER_SECTION) < ensured.index("Body")


def test_ensure_without_h1_uses_top_fallback():
    assert ensure_user_section("Body").startswith(EMPTY_USER_SECTION)


def test_protect_preserves_old_section_and_falls_back_to_empty():
    old = '# Old\n<section data-user="true">\nMY NOTES\n</section>'
    new = f"# New\n{EMPTY_USER_SECTION}\nGenerated"
    merged = protect_user_section(old, new)
    assert "MY NOTES" in merged
    assert EMPTY_USER_SECTION not in merged
    assert EMPTY_USER_SECTION in protect_user_section(None, "# New\nGenerated")
