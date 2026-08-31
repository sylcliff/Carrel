// Shared helpers for citation/reference rows. Both CitationsCard (cited-by)
// and ReferencesCard (this paper's references) call out to the same external
// landing pages via the same id-priority chain (DOI → arXiv → S2 → OpenAlex),
// so the link picker lives here instead of being duplicated.

export type CitationIds = {
  doi: string | null;
  arxiv_id: string | null;
  s2_paper_id: string | null;
  openalex_id: string | null;
};

/** Return the best external landing-page URL for a citation/reference, or null. */
export function citeUrl(c: CitationIds): string | null {
  if (c.doi) return `https://doi.org/${c.doi}`;
  if (c.arxiv_id) return `https://arxiv.org/abs/${c.arxiv_id}`;
  if (c.s2_paper_id) return `https://www.semanticscholar.org/paper/${c.s2_paper_id}`;
  if (c.openalex_id) {
    const bare = c.openalex_id.includes("/") ? c.openalex_id.split("/").pop() : c.openalex_id;
    return `https://openalex.org/works/${bare}`;
  }
  return null;
}
