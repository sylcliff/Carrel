# Files

- [Paper dedup](paper-dedup.md) - Deterministic scoring, union-find clustering, optional LLM judge, and PaperAlias indirection for duplicate paper rows, with user-state migration and reversible merges.
- [Scholar dedup](scholar-dedup.md) - Second-pass author disambiguation that clusters same-named OpenAlex A-IDs, scores coauthor/affiliation/topic overlap, and writes ScholarAlias rows (auto/user/reject) so the scholar aggregator and wiki treat duplicates as one person.
