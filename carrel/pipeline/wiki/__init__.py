"""LLM-compiled wiki pipeline (M8).

The wiki is a layer of *compiled* Markdown pages synthesized from the library's
papers, sitting above the immutable chunk store. Each page is a plain Markdown
file on disk under ``data/wiki/{concepts,scholars,questions}/``; the
``wiki_pages`` / ``wiki_sources`` tables are a rebuildable index and provenance
map. See :mod:`carrel.pipeline.wiki.scholar_compile` for the first concrete
compiler.
"""
