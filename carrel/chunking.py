"""Heading-aware Markdown chunker (M5).

Splits a parsed paper Markdown into chunks of roughly ``target_tokens`` words
(plus ``overlap_tokens``) while preserving the nearest preceding heading as
context. The goal is to feed embedding-friendly slices to Ark — not perfect
tokenization, not table/figure-aware, just "good enough" coverage.

Token proxy: words / 0.75 ≈ tokens (English heuristic). The actual embedding
model re-tokenizes; this is a chunk-size knob, not a token counter.

ponytail: this is deliberately a regex splitter. Switching to a real tokenizer
(tiktoken, sentencepiece) costs us 1 dep and a tokenizer-cache file; the gain
is a tighter length target, which the embedding model doesn't care about.
Upgrade if chunks start ballooning on a paper you actually read.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

# ATX headings only (#, ##, …). Setext (===, ---) is rare in MinerU output.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.MULTILINE)
# Words (latin runs + CJK chars). CJK counts each character as one "word"
# for chunking purposes; close enough to embedding-side token counts.
_WORD_RE = re.compile(r"[A-Za-z0-9_]+|[一-鿿]")


def estimate_tokens(text: str) -> int:
    """Approximate token count: 1 token per 0.75 words, rounded up."""
    words = len(_WORD_RE.findall(text))
    # round up so an empty chunk reports 0, a 1-word chunk reports 1
    return (words * 4 + 2) // 3


def split_by_heading(md: str) -> list[tuple[str, str]]:
    """Yield (heading, body) for each heading section in ``md``.

    The first chunk may have ``heading == ""`` (preamble before any heading).
    Sections are returned in document order with the heading path joined by
    " / " so a chunk under "## Methods" in "## Experiments" gets
    ``"Experiments / Methods"``.
    """
    matches = list(_HEADING_RE.finditer(md))
    if not matches:
        return [("", md.strip())]

    out: list[tuple[str, list[str]]] = []
    # Preamble before the first heading
    preamble = md[: matches[0].start()].strip()
    if preamble:
        out.append(("", [preamble]))

    heading_stack: list[str] = []
    for i, m in enumerate(matches):
        level = len(m.group(1))
        title = m.group(2).strip()
        # Trim stack to this level
        heading_stack = heading_stack[: level - 1]
        heading_stack.append(title)
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(md)
        body = md[body_start:body_end].strip()
        if body:
            out.append((" / ".join(heading_stack), [body]))

    return [(h, "\n\n".join(parts)) for h, parts in out]


@dataclass(slots=True)
class Chunk:
    index: int
    heading: str
    content_md: str
    token_count: int


def chunk_markdown(
    md: str,
    *,
    target_tokens: int = 900,
    overlap_tokens: int = 150,
    min_tokens: int = 200,
) -> list[Chunk]:
    """Split ``md`` into chunks of ~``target_tokens`` words each.

    A heading's body is kept whole if it fits in ``target_tokens``; otherwise
    it is split into windows with ``overlap_tokens`` carried over. Chunks
    smaller than ``min_tokens`` are merged into the next neighbour so we don't
    store near-empty rows.
    """
    if not md.strip():
        return []

    pieces = split_by_heading(md)
    chunks: list[Chunk] = []
    idx = 0
    pending: list[str] = []
    pending_heading = ""
    pending_tokens = 0

    def flush() -> None:
        nonlocal idx, pending, pending_heading, pending_tokens
        if not pending:
            return
        text = "\n\n".join(pending).strip()
        if not text:
            pending, pending_heading, pending_tokens = [], "", 0
            return
        # If a single piece exceeds the target, split it by approximate
        # word-windows with overlap.
        if pending_tokens > target_tokens and len(pending) == 1:
            for sub in _window_split(pending[0], target_tokens, overlap_tokens):
                chunks.append(Chunk(
                    index=idx,
                    heading=pending_heading,
                    content_md=sub,
                    token_count=estimate_tokens(sub),
                ))
                idx += 1
        else:
            chunks.append(Chunk(
                index=idx,
                heading=pending_heading,
                content_md=text,
                token_count=pending_tokens,
            ))
            idx += 1
        pending, pending_heading, pending_tokens = [], "", 0

    for heading, body in pieces:
        body_tokens = estimate_tokens(body)
        # Heading fits comfortably: keep as one chunk
        if body_tokens <= target_tokens and not pending:
            chunks.append(Chunk(
                index=idx,
                heading=heading,
                content_md=body,
                token_count=body_tokens,
            ))
            idx += 1
            continue
        # Heading too big: split it directly
        if body_tokens > target_tokens and not pending:
            for sub in _window_split(body, target_tokens, overlap_tokens):
                chunks.append(Chunk(
                    index=idx,
                    heading=heading,
                    content_md=sub,
                    token_count=estimate_tokens(sub),
                ))
                idx += 1
            continue
        # Otherwise accumulate
        new_total = pending_tokens + body_tokens
        if new_total > target_tokens and pending_tokens >= min_tokens:
            flush()
        pending.append(body)
        pending_heading = heading
        pending_tokens += body_tokens

    flush()

    # Drop trailing tinies
    while len(chunks) >= 2 and chunks[-1].token_count < min_tokens:
        last = chunks.pop()
        prev = chunks[-1]
        merged_heading = prev.heading or last.heading
        merged = prev.content_md + "\n\n" + last.content_md
        chunks[-1] = Chunk(
            index=prev.index,
            heading=merged_heading,
            content_md=merged,
            token_count=prev.token_count + last.token_count,
        )
    return chunks


def _window_split(text: str, target_tokens: int, overlap_tokens: int) -> Iterable[str]:
    """Split an over-long string into token-windows with overlap.

    ponytail: word boundaries, not token boundaries — same as the rest of
    this module. The overlap is approximate.
    """
    words = _WORD_RE.findall(text)
    if not words:
        return []
    # Convert token target back to a word count
    target_words = max(1, int(target_tokens * 0.75))
    overlap_words = max(0, int(overlap_tokens * 0.75))
    step = max(1, target_words - overlap_words)

    # Re-stitch using the original text so we don't lose punctuation/joins.
    spans: list[str] = []
    pos = 0
    n = len(words)
    i = 0
    while i < n:
        end = min(n, i + target_words)
        slice_ = words[i:end]
        snippet = " ".join(slice_)
        # Find the snippet in the original text starting near `pos` so we
        # keep Markdown punctuation. This is O(n) per window; fine at our
        # scale (hundreds of chunks per paper at most).
        idx = text.find(snippet[:40], pos) if len(snippet) >= 40 else text.find(snippet, pos)
        if idx < 0:
            idx = pos
        end_pos = idx + len(snippet)
        spans.append(text[idx:end_pos].strip())
        pos = max(end_pos - overlap_words, pos + 1)  # avoid infinite loop
        if end >= n:
            break
        i += step
    return [s for s in spans if s]
