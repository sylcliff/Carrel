"""Tests for the heading-aware Markdown chunker."""
from __future__ import annotations

from carrel.chunking import (
    chunk_markdown,
    estimate_tokens,
    split_by_heading,
)


def test_estimate_tokens_basic():
    # 12 words -> (12*4+2)//3 = 16
    assert estimate_tokens("one two three four five six seven eight nine ten eleven twelve") == 16


def test_estimate_tokens_empty():
    assert estimate_tokens("") == 0


def test_split_by_heading_groups_sections():
    md = """# Title

intro

## Methods

m body

## Results

r body
"""
    sections = split_by_heading(md)
    assert len(sections) == 3
    # All headings are `# Title` / `## Methods` / `## Results`, so the
    # nested path is "Title / Methods" etc.
    assert sections[0][0] == "Title"
    assert "intro" in sections[0][1]
    assert sections[1][0] == "Title / Methods"
    assert "m body" in sections[1][1]
    assert sections[2][0] == "Title / Results"


def test_split_by_heading_no_headings_returns_one_section():
    md = "just a paragraph\nwith no heading"
    sections = split_by_heading(md)
    assert len(sections) == 1
    assert sections[0][0] == ""


def test_split_by_heading_nested_path():
    md = """# Outer

outer body

## A

a body

## B

b body

### B1

b1 body
"""
    sections = split_by_heading(md)
    headings = [s[0] for s in sections]
    assert "Outer / A" in headings
    assert "Outer / B" in headings
    assert "Outer / B / B1" in headings


def test_chunk_markdown_keeps_small_section_as_one():
    md = """# Title

small body.
"""
    chunks = chunk_markdown(md, target_tokens=900, overlap_tokens=150, min_tokens=200)
    assert len(chunks) == 1
    assert chunks[0].heading == "Title"
    assert "small body" in chunks[0].content_md
    assert chunks[0].index == 0


def test_chunk_markdown_splits_oversized_section():
    """A single long section should be windowed with overlap."""
    body = "lorem ipsum dolor sit amet " * 200  # ~1000 words
    md = f"# Title\n\n{body}\n"
    chunks = chunk_markdown(
        md, target_tokens=200, overlap_tokens=40, min_tokens=50
    )
    assert len(chunks) >= 2
    # First chunk has index 0
    assert chunks[0].index == 0
    assert chunks[1].index == 1


def test_chunk_markdown_returns_empty_for_empty_input():
    assert chunk_markdown("") == []
    assert chunk_markdown("   \n\n  ") == []


def test_chunk_indices_are_sequential():
    md = "# A\n\naaa\n\n# B\n\nbbb\n\n# C\n\nccc\n"
    chunks = chunk_markdown(md, target_tokens=900, min_tokens=5)
    assert [c.index for c in chunks] == list(range(len(chunks)))


def test_chunk_drops_tiny_trailing():
    """A trailing section smaller than min_tokens should be merged into the previous chunk."""
    # Make Big body small enough to fit in one chunk, so the merge is direct.
    md = "# Big\n\n" + ("x " * 100) + "\n\n# Tiny\n\nshort"
    chunks = chunk_markdown(md, target_tokens=900, overlap_tokens=20, min_tokens=50)
    # The tiny section should be merged into the previous one (one chunk total).
    assert len(chunks) == 1
    assert chunks[-1].token_count >= 50
    assert "short" in chunks[-1].content_md
