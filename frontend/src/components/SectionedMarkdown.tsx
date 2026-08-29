import MarkdownReader from "./MarkdownReader";
import type { PaperSection } from "@/api/client";

/**
 * Render a parsed paper split into heading-delimited sections in
 * document order. Each section is its own MarkdownReader so math,
 * citation refs, and wiki-links keep working the way they do in the
 * flat view; the wrapper adds a small `§ N` eyebrow and a leaf
 * heading so the section boundary is obvious even when the
 * document's own heading hierarchy is shallow.
 *
 * The TOC is a tiny collapsible `<details>` at the top — one anchor
 * link per section. Anchor ids are `paper-section-N` (integer) so
 * they don't collide with the rehypeCitations anchors (`ref-N`,
 * `figure-N`, `section-X.Y`, `appendix-x`).
 */
export default function SectionedMarkdown({
  sections,
  mdPath,
}: {
  sections: PaperSection[];
  mdPath: string | null;
}) {
  if (!sections.length) {
    // Fall back to a flat render if the server returned an empty list
    // (e.g. md file missing on disk). Better than rendering nothing.
    return null;
  }

  return (
    <div className="paper-sectioned">
      {sections.length > 1 && (
        <details className="paper-section-toc" open>
          <summary className="paper-section-toc__summary">
            Contents · {sections.length} sections
          </summary>
          <ol className="paper-section-toc__list">
            {sections.map((s) => {
              const label = s.heading_path || "Preamble";
              return (
                <li
                  key={s.index}
                  className="paper-section-toc__item"
                  data-depth={s.heading_path ? s.heading_path.split(" / ").length : 0}
                >
                  <a href={`#paper-section-${s.index}`} className="paper-section-toc__link">
                    <span className="paper-section-toc__num">§ {s.index}</span>
                    <span className="paper-section-toc__label">{label}</span>
                  </a>
                </li>
              );
            })}
          </ol>
        </details>
      )}

      {sections.map((s) => (
        <section
          key={s.index}
          id={`paper-section-${s.index}`}
          className="paper-section"
          aria-label={s.heading_path || "Preamble"}
        >
          <header className="paper-section__header">
            <span className="paper-section__eyebrow">§ {s.index}</span>
            <h2 className="paper-section__title">
              {s.heading_path ? leafOf(s.heading_path) : "Preamble"}
            </h2>
            {s.heading_path && s.heading_path.includes(" / ") && (
              <p className="paper-section__crumb">{s.heading_path}</p>
            )}
          </header>
          <MarkdownReader body={s.body} mdPath={mdPath} />
        </section>
      ))}
    </div>
  );
}

function leafOf(path: string): string {
  const parts = path.split(" / ");
  return parts[parts.length - 1] || path;
}
