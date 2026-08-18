"""Query spelling correction via SymSpell (M5.5).

Wraps symspellpy with:
  - the bundled 82k English frequency dictionary
  - an optional supplement built from the local library's titles/abstracts,
    so jargon like "BERT", "RAG", "arxiv" isn't "corrected" to common words
  - identifier-aware passthrough: DOIs / arXiv ids / tokens that are mostly
    digits or punctuation are left alone

ponytail: a single in-memory SymDict instance, seeded once and reused. We
don't persist the custom dictionary; rebuilding from a few thousand papers
takes ~100ms. If that ever grows, cache it under storage.root.
"""
from __future__ import annotations

import logging
import re
import threading
from functools import lru_cache

from sqlmodel import Session, select

from carrel.models import Paper

logger = logging.getLogger(__name__)

# CJK / non-latin scripts — symspell's dictionary is English-only and would
# mangle these, so bail out before touching them.
_CJK_RE = re.compile(r"[一-鿿぀-ヿ가-힯]")

# A token that looks like an identifier — DOI, arXiv id, URL-ish, or mostly
# digits/punctuation. We don't try to spell-correct these.
_ID_RE = re.compile(
    r"""^(
        10\.\d{4,}/\S+ |            # DOI
        \d{4}\.\d{4,5} |            # arXiv id (new)
        [a-z-]+/\d{7} |             # arXiv id (old, e.g. cs.CL/0101001)
        \S+://\S+ |                 # URL
        [\d.\-]+                    # pure numeric / version-y
    )$""",
    re.VERBOSE | re.IGNORECASE,
)

# Terms that must never be "corrected" even if the default dictionary doesn't
# know them. Common in CS paper queries.
_PROTECTED = {
    "llm", "llms", "rag", "bert", "gpt", "cnn", "rnn", "lstm", "gru",
    "transformer", "transformers", "attention", "arxiv", "embedding",
    "embeddings", "tokenizer", "tokenizers", "finetune", "finetuning",
    "neurips", "icml", "iclr", "acl", "emnlp", "naacl",
}

_DICT_FREQUENCY = 1  # symspell uses term frequency; absolute value barely matters
_SEED_BATCH = 500


class _SpellCorrector:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sym = None
        self._seeded = False

    def _ensure_loaded(self) -> None:
        if self._sym is not None:
            return
        with self._lock:
            if self._sym is not None:
                return
            from symspellpy import SymSpell
            sym = SymSpell(max_dictionary_edit_distance=2, prefix_length=7)
            # Load the bundled dictionary shipped with symspellpy.
            try:
                import importlib.resources as ir
                dict_ref = ir.files("symspellpy").joinpath(
                    "frequency_dictionary_en_82_765.txt"
                )
                with ir.as_file(dict_ref) as p:
                    sym.load_dictionary(str(p), 0, 1)
            except Exception:
                logger.warning(
                    "could not load symspellpy frequency dictionary; "
                    "spelling correction will use library terms only",
                    exc_info=True,
                )
            for term in _PROTECTED:
                sym.create_dictionary_entry(term, 10_000)
            self._sym = sym

    def seed_from_library(self, session: Session) -> int:
        """Add title/abstract words from local papers so jargon isn't mangled.

        Safe to call repeatedly; subsequent calls are no-ops. Returns the
        number of terms added on the first call.
        """
        self._ensure_loaded()
        if self._seeded:
            return 0
        with self._lock:
            if self._seeded:
                return 0
            assert self._sym is not None
            added = 0
            # Stream papers in chunks; a single-user library rarely has more
            # than a few thousand rows, but avoid materializing every abstract
            # at once regardless.
            offset = 0
            while True:
                rows = session.exec(
                    select(Paper.title, Paper.abstract)
                    .offset(offset)
                    .limit(_SEED_BATCH)
                ).all()
                if not rows:
                    break
                for title, abstract in rows:
                    for txt in (title, abstract):
                        if not txt:
                            continue
                        for word in re.findall(r"[A-Za-z][A-Za-z\-']{2,}", txt):
                            w = word.lower()
                            if w in _PROTECTED:
                                continue
                            if self._sym.create_dictionary_entry(w, _DICT_FREQUENCY):
                                added += 1
                offset += len(rows)
                if len(rows) < _SEED_BATCH:
                    break
            self._seeded = True
            logger.info("spelling: seeded %d library terms into corrector", added)
            return added

    def correct(self, query: str) -> tuple[str, str | None]:
        """Return (corrected_query, original_if_changed).

        If the query looks fine (or is an identifier, or has no real-word
        tokens), returns (query, None).
        """
        self._ensure_loaded()
        assert self._sym is not None
        q = query.strip()
        if not q or _ID_RE.match(q) or _CJK_RE.search(q):
            return q, None

        # Preserve simple queries: if every whitespace-delimited token is
        # identifier-like, don't touch the input.
        tokens = q.split()
        if tokens and all(_ID_RE.match(t) for t in tokens):
            return q, None

        suggestions = self._sym.lookup_compound(q, max_edit_distance=2)
        if not suggestions:
            return q, None
        corrected = suggestions[0].term
        # Normalize whitespace; lookup_compound can insert spaces around punctuation.
        corrected = re.sub(r"\s+", " ", corrected).strip()
        if not corrected or corrected.lower() == q.lower():
            return q, None
        return corrected, q


@lru_cache(maxsize=512)
def _correct_cached(query: str) -> tuple[str, str | None]:
    return _instance.correct(query)


def correct_query(query: str) -> tuple[str, str | None]:
    """Public entrypoint: cached correction. Library must be seeded first."""
    return _correct_cached(query.strip())


def seed_from_library(session: Session) -> int:
    """Seed the corrector from local papers. Idempotent."""
    # Bump the cache after seeding: previously-unknown jargon may now resolve.
    added = _instance.seed_from_library(session)
    if added:
        _correct_cached.cache_clear()
    return added


_instance = _SpellCorrector()
