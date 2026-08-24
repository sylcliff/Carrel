# Files

- [Wiki compilers](compilers.md) - The three LLM compilers (scholar, concept, question), their shared aggregate→prompt→atomic-write→reindex pattern, the scheduled scholar-only job, and the four-stage /wiki/compile batch with its no-op skip gate.
- [LLM wiki layer](overview.md) - The compiled Markdown wiki (scholar/concept/question pages) on disk, its wiki_pages/wiki_sources index, frontmatter/link/slug conventions, and the reindex/backlink rebuild that makes disk the source of truth.
- [Per-paper concept and question extraction](paper-extract.md) - LLM extraction of grounded technical concepts and open questions from parsed paper bodies into paper_concepts/paper_questions tables, the input feed for the concept and question wiki compilers.
- [Wiki identity reconciliation](reconciliation.md) - Decouples wiki entity identity (entity_key) from on-disk address (kind, slug), so A-ID assignment, alias merges, and name-spelling changes rewrite pages into redirect shells instead of leaving orphans.
