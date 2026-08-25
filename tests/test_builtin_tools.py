"""Tests for :mod:`carrel.mcp.builtin_tools` and the
``append_to_user_section`` helper it leans on.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import carrel.main as main_mod
from carrel.config import CarrelYAML
from carrel.mcp.builtin_tools import (
    BUILTIN_SERVER_NAME,
    _save_scholar_note,
    builtin_dispatch_map,
    collect_builtin_tools,
    dispatch_builtin,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_page(tmp_path: Path, slug: str, body: str = "") -> Path:
    """Drop a minimal scholar page file on disk and return its path."""
    rel = f"wiki/scholars/{slug}.md"
    full = tmp_path / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(
        f"---\nkind: scholar\ntitle: Test Scholar\nslug: {slug}\n---\n{body}",
        encoding="utf-8",
    )
    return full


@pytest.fixture
def storage(tmp_path, monkeypatch):
    """Install a fresh ``app_config`` whose ``storage.root`` is ``tmp_path``,
    so ``_save_scholar_note`` reads/writes under our scratch directory.
    Yields ``tmp_path`` for direct path assertions."""
    cfg = CarrelYAML()
    cfg.storage.root = tmp_path
    # Pre-create the kind dirs the handler may write into.
    (tmp_path / "wiki" / "scholars").mkdir(parents=True, exist_ok=True)
    # The annotation on ``carrel.main`` doesn't materialize an attribute until
    # lifespan runs, so seed it first to give monkeypatch something to replace.
    if not hasattr(main_mod, "app_config"):
        main_mod.app_config = cfg  # noqa: F841 (materialized for monkeypatch)
    monkeypatch.setattr(main_mod, "app_config", cfg)
    return tmp_path


# ---------------------------------------------------------------------------
# Helper-level tests (append_to_user_section)
# ---------------------------------------------------------------------------


def test_append_to_user_section_inserts_inside_existing_section():
    from carrel.pipeline.wiki._merge import (
        EMPTY_USER_SECTION,
        append_to_user_section,
    )

    page = (
        "---\nkind: scholar\ntitle: X\n---\n"
        "# X\n\n"
        + EMPTY_USER_SECTION
        + "\n\n## Summary\nA summary.\n"
    )
    out = append_to_user_section(
        page, section_title="Notes", content="hello world"
    )
    # The block is inside <section data-user="true">.
    assert "## Notes" in out
    assert "hello world" in out
    section_start = out.find('<section data-user="true">')
    section_end = out.find("</section>")
    assert section_start != -1 and section_end != -1
    block = out[section_start:section_end]
    assert "## Notes" in block
    assert "hello world" in block
    # Summary survives untouched.
    assert "## Summary" in out
    assert "A summary." in out


def test_append_to_user_section_creates_section_when_missing():
    from carrel.pipeline.wiki._merge import append_to_user_section

    page = "---\nkind: scholar\n---\n# X\n\n## Summary\nS\n"
    out = append_to_user_section(page, section_title="Notes", content="c")
    assert '<section data-user="true">' in out
    assert "## Notes" in out
    assert "c" in out
    # Summary still there.
    assert "## Summary" in out
    assert "S" in out


def test_append_to_user_section_preserves_frontmatter():
    from carrel.pipeline.wiki._merge import append_to_user_section

    page = (
        "---\nkind: scholar\nopenalex_id: A1234\n"
        "affiliation: ETH Zurich\nfirst_year: 2007\n---\n"
        "# X\n"
        + '<section data-user="true"></section>\n'
    )
    out = append_to_user_section(page, section_title="T", content="x")
    assert "openalex_id: A1234" in out
    assert "affiliation: ETH Zurich" in out
    assert "first_year: 2007" in out


def test_append_to_user_section_appends_repeatedly():
    """The model can call the tool multiple times; each call adds a new
    section heading without overwriting prior ones."""
    from carrel.pipeline.wiki._merge import append_to_user_section

    page = "---\nkind: scholar\n---\n# X\n<section data-user=\"true\"></section>\n"
    page = append_to_user_section(page, section_title="A", content="1")
    page = append_to_user_section(page, section_title="B", content="2")
    assert "## A" in page and "1" in page
    assert "## B" in page and "2" in page
    # A appears before B (append order).
    assert page.find("## A") < page.find("## B")


# ---------------------------------------------------------------------------
# save_scholar_note — happy path
# ---------------------------------------------------------------------------


def test_save_scholar_note_writes_block_to_disk(storage):
    """The handler reads the page, splices the block, and writes atomically."""
    full = _write_page(storage, "A1234", body="# X\n\n## Summary\nS\n")
    result = _save_scholar_note({
        "slug": "A1234",
        "section_title": "Biographical notes",
        "content": "- Born 1945\n- ETH professor",
    })
    assert "Saved note 'Biographical notes' to wiki/scholars/A1234.md" == result
    new = full.read_text(encoding="utf-8")
    assert "## Biographical notes" in new
    assert "- Born 1945" in new
    assert "- ETH professor" in new
    # Compiled body survives.
    assert "## Summary" in new
    assert "S" in new


def test_save_scholar_note_creates_section_when_missing(storage):
    full = _write_page(storage, "A1234", body="# X\n\n## Summary\nS\n")
    # Strip out any user section that _write_page might have left.
    raw = full.read_text(encoding="utf-8")
    raw = raw.replace(
        '<section data-user="true"></section>', ""
    ).replace(
        '<section data-user="true">\n<!-- Your notes on this page. The '
        'compiler preserves everything inside this section. -->\n</section>',
        "",
    )
    full.write_text(raw, encoding="utf-8")

    result = _save_scholar_note({
        "slug": "A1234",
        "section_title": "Note",
        "content": "body",
    })
    assert "Saved" in result
    new = full.read_text(encoding="utf-8")
    assert '<section data-user="true">' in new
    assert "## Note" in new
    assert "body" in new
    assert "## Summary" in new


def test_save_scholar_note_appends_to_existing_user_content(storage):
    """Pre-existing user content (e.g. from a manual edit) is preserved."""
    full = _write_page(
        storage, "A1234",
        body=(
            "# X\n"
            '<section data-user="true">\n'
            "## Prior note\n"
            "Already here.\n"
            "</section>\n"
        ),
    )
    result = _save_scholar_note({
        "slug": "A1234",
        "section_title": "New note",
        "content": "fresh content",
    })
    assert "Saved" in result
    new = full.read_text(encoding="utf-8")
    assert "## Prior note" in new
    assert "Already here." in new
    assert "## New note" in new
    assert "fresh content" in new


def test_save_scholar_note_preserves_frontmatter(storage):
    full = _write_page(storage, "A1234", body="# X\n")
    full.write_text(
        "---\nkind: scholar\ntitle: A\nslug: A1234\n"
        "openalex_id: A1234\naffiliation: ETH Zurich\n"
        "tags:\n- foo\n- bar\n---\n# A\n",
        encoding="utf-8",
    )
    _save_scholar_note({
        "slug": "A1234",
        "section_title": "Note",
        "content": "x",
    })
    new = full.read_text(encoding="utf-8")
    assert "openalex_id: A1234" in new
    assert "affiliation: ETH Zurich" in new
    assert "- foo" in new
    assert "- bar" in new


# ---------------------------------------------------------------------------
# save_scholar_note — validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_slug",
    [
        "../etc/passwd",
        "../../etc/passwd",
        "foo/bar",
        "name--../../etc",
        "B1234",          # wrong prefix (A\d+ only accepts "A<digits>")
        "name--",         # missing slug part after the prefix
        "name--FOO",      # uppercase not allowed
        "name--foo_bar",  # underscore not allowed
        "name--.hidden",  # leading dot not allowed
        "",               # empty
        " ",              # whitespace only
    ],
)
def test_save_scholar_note_rejects_invalid_slug(bad_slug, storage):
    with pytest.raises(ValueError, match="invalid scholar slug"):
        _save_scholar_note({
            "slug": bad_slug,
            "section_title": "x",
            "content": "y",
        })


def test_save_scholar_note_accepts_name_slug(storage):
    _write_page(storage, "name--jane-doe", body="# J\n")
    result = _save_scholar_note({
        "slug": "name--jane-doe",
        "section_title": "Note",
        "content": "ok",
    })
    assert "Saved" in result


def test_save_scholar_note_raises_when_page_missing(storage):
    with pytest.raises(ValueError, match="scholar page not found"):
        _save_scholar_note({
            "slug": "A9999999999",
            "section_title": "x",
            "content": "y",
        })


def test_save_scholar_note_rejects_empty_section_title(storage):
    _write_page(storage, "A1234", body="")
    with pytest.raises(ValueError, match="section_title"):
        _save_scholar_note({
            "slug": "A1234",
            "section_title": "   ",
            "content": "y",
        })


def test_save_scholar_note_rejects_multiline_section_title(storage):
    _write_page(storage, "A1234", body="")
    with pytest.raises(ValueError, match="single line"):
        _save_scholar_note({
            "slug": "A1234",
            "section_title": "line one\nline two",
            "content": "y",
        })


def test_save_scholar_note_rejects_empty_content(storage):
    _write_page(storage, "A1234", body="")
    with pytest.raises(ValueError, match="content"):
        _save_scholar_note({
            "slug": "A1234",
            "section_title": "x",
            "content": "   ",
        })


def test_save_scholar_note_rejects_huge_content(storage):
    _write_page(storage, "A1234", body="")
    with pytest.raises(ValueError, match="too large"):
        _save_scholar_note({
            "slug": "A1234",
            "section_title": "x",
            "content": "a" * 60_000,
        })


# ---------------------------------------------------------------------------
# Litellm shape
# ---------------------------------------------------------------------------


def test_collect_builtin_tools_has_litellm_shape():
    tools = collect_builtin_tools()
    assert len(tools) == 1
    t = tools[0]
    assert t["type"] == "function"
    fn = t["function"]
    assert fn["name"] == f"{BUILTIN_SERVER_NAME}__save_scholar_note"
    assert fn["description"]
    assert fn["parameters"]["type"] == "object"
    assert set(fn["parameters"]["required"]) == {
        "slug", "section_title", "content"
    }
    for prop in ("slug", "section_title", "content"):
        assert prop in fn["parameters"]["properties"]


def test_dispatch_builtin_returns_none_for_unknown():
    assert dispatch_builtin("not_a_real_tool", {}) is None


def test_builtin_dispatch_map_exposes_handlers():
    handlers = builtin_dispatch_map()
    assert "save_scholar_note" in handlers
    assert handlers["save_scholar_note"] is _save_scholar_note
