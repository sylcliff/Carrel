/**
 * Rehype post-process plugin that wires up intra-document links for MinerU's
 * flat markdown output:
 *
 *   1. Targets — assigns ids:
 *      - Each <p> after a "## References" heading         -> #ref-1, #ref-2, ...
 *      - Caption paragraphs ("Figure 1: ...", "Table 2:") -> #figure-1, #table-2
 *      - Numbered headings ("## 3.2 Batch-wise ...")      -> #section-3.2
 *      - Single-letter appendix headings after References -> #appendix-a
 *
 *   2. In-text references — replaces plain text with <a> anchors (outside of
 *      existing links, headings, code, captions and reference entries themselves):
 *      - Parenthetical citations: "(Liu et al., 2025; Frantar & Alistarh, 2023)"
 *        links each cite to its reference entry when a surname+year match exists.
 *      - "Figure N" / "Fig. N" / "Table N" -> caption anchor
 *      - "Section X.Y" / "Appendix X"     -> heading anchor
 *
 * MinerU does not emit BibTeX keys, so citation matching is author-year
 * heuristics. We degrade gracefully: unmatched cites stay as plain text.
 */
import { toText } from "hast-util-to-text";
import { visit } from "unist-util-visit";
import type { Plugin } from "unified";
import type { Element, ElementContent, Root, Text } from "hast";

const SKIP = "skip";
const YEAR_RE = /\b((?:19|20)\d{2}[a-z]?)\b/;

interface RefEntry {
  id: string;
  surname: string; // lowercase
  year: string;
}

const isElement = (n: unknown): n is Element =>
  !!n && typeof n === "object" && (n as { type?: string }).type === "element";

/** Last whitespace-separated token before the first '.' or ',' — the surname. */
function surnameFromReference(text: string): string | null {
  const head = text.split(/[.,]/, 1)[0] ?? "";
  const tokens = head.trim().split(/\s+/).filter(Boolean);
  if (tokens.length === 0) return null;
  return tokens[tokens.length - 1].toLowerCase().replace(/[^a-z0-9\-]/g, "");
}

/** First author's surname from an in-text cite ("Liu et al.", "Frantar & Alistarh"). */
function surnameFromCite(authorPart: string): string | null {
  // Cut at " et al.", "&", " and " — first token left of any of those is surname.
  const head = authorPart
    .split(/\s+et\s+al\.?|\s+&\s+|\s+and\s+/i, 1)[0]
    ?.trim();
  if (!head) return null;
  // For "van der Berg" style, take the last word; for single token use it.
  const tokens = head.split(/\s+/).filter(Boolean);
  const tok = tokens[tokens.length - 1] ?? tokens[0];
  return tok.toLowerCase().replace(/[^a-z0-9\-]/g, "") || null;
}

function yearFrom(text: string): string | null {
  const m = text.match(YEAR_RE);
  return m ? m[1] : null;
}

function setId(node: Element, id: string) {
  node.properties = { ...(node.properties ?? {}), id };
}

function refHref(refs: RefEntry[], surname: string | null, year: string): string | null {
  if (!surname) return null;
  const hit = refs.find((r) => r.surname === surname && r.year === year);
  return hit ? `#${hit.id}` : null;
}

const CITE_SPLIT_RE = /\s*;\s*/g;

/**
 * Given the *contents* of a "(...)" citation group, produce hast nodes that
 * link each resolvable cite to its reference and leave unmatched text intact.
 * Returns null when no cite in the group resolves (caller keeps the original
 * parenthetical as plain text).
 */
function linkCitationGroup(
  content: string,
  refs: RefEntry[],
): ElementContent[] | null {
  const parts = content.split(CITE_SPLIT_RE);
  const out: ElementContent[] = [];
  let matched = false;

  parts.forEach((part, i) => {
    const m = part.match(/^(.*?)(?:,?\s*)(\d{4}[a-z]?)\s*$/);
    let piece: ElementContent;
    if (m) {
      const authorPart = m[1].trim();
      const year = m[2];
      const href = refHref(refs, surnameFromCite(authorPart), year);
      if (href) {
        matched = true;
        piece = {
          type: "element",
          tagName: "a",
          properties: {
            href,
            className: ["cite"],
          },
          children: [{ type: "text", value: part }],
        };
      } else {
        piece = { type: "text", value: part };
      }
    } else {
      piece = { type: "text", value: part };
    }
    out.push(piece);
    if (i < parts.length - 1) {
      out.push({ type: "text", value: "; " });
    }
  });

  return matched ? out : null;
}

type Replacement = ElementContent[];

/**
 * Walk a text string, producing a sequence of text/element nodes where each
 * resolved reference becomes an <a>. Figures/tables/sections are matched
 * directly; parenthetical citations are handled as one group (so the outer
 * parens remain around multiple linked cites).
 */
function linkifyText(
  raw: string,
  ctx: { refs: RefEntry[]; figures: Set<string>; tables: Set<string>; sections: Set<string>; appendices: Set<string> },
): Replacement {
  const { refs, figures, tables, sections, appendices } = ctx;
  const out: ElementContent[] = [];
  let buffer = "";
  const flush = () => {
    if (buffer) {
      out.push({ type: "text", value: buffer });
      buffer = "";
    }
  };

  // Combined scanner. Citations (parens containing a year) take priority over
  // fig/table matches so "(see Figure 1)" doesn't get its parens eaten.
  const tokenRe =
    /\(([^()]*\b(?:19|20)\d{2}[a-z]?[^()]*)\)|(\b(?:Figure|Fig)\.?\s+)([A-Z0-9][\w.]*)|(\bTable\s+)([A-Z0-9][\w.]*)|(\bSection\s+)(\d+(?:\.\d+)*)|(\bAppendix\s+)([A-Z])\b|(doi:\s*10\.\d{4,9}\/[-._;()/:A-Z0-9]+)/gi;

  let lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = tokenRe.exec(raw)) !== null) {
    buffer += raw.slice(lastIndex, m.index);
    lastIndex = m.index + m[0].length;

    if (m[1] !== undefined) {
      // Citation group
      const linked = linkCitationGroup(m[1].trim(), refs);
      if (linked) {
        flush();
        out.push({ type: "text", value: "(" });
        out.push(...linked);
        out.push({ type: "text", value: ")" });
      } else {
        buffer += m[0];
      }
    } else if (m[3] !== undefined) {
      const label = m[2];
      const num = m[3];
      if (figures.has(num)) {
        flush();
        out.push(anchor(`#figure-${num}`, label + num));
      } else {
        buffer += label + num;
      }
    } else if (m[5] !== undefined) {
      const label = m[4];
      const num = m[5];
      if (tables.has(num)) {
        flush();
        out.push(anchor(`#table-${num}`, label + num));
      } else {
        buffer += label + num;
      }
    } else if (m[7] !== undefined) {
      const label = m[6];
      const num = m[7];
      if (sections.has(num)) {
        flush();
        out.push(anchor(`#section-${num}`, label + num));
      } else {
        buffer += label + num;
      }
    } else if (m[9] !== undefined) {
      const label = m[8];
      const letter = m[9];
      if (appendices.has(letter)) {
        flush();
        out.push(anchor(`#appendix-${letter}`, label + letter));
      } else {
        buffer += label + letter;
      }
    } else if (m[10] !== undefined) {
      // "doi:10.xxxx/..." (bare, not already a URL). Strip trailing sentence
      // punctuation the greedy character class may have captured.
      const raw = m[10];
      const trimmed = raw.replace(/[.,;)\]'"]+$/, "");
      const tail = raw.slice(trimmed.length);
      const doi = trimmed.replace(/^\s*doi:\s*/i, "");
      flush();
      out.push({
        type: "element",
        tagName: "a",
        properties: {
          href: `https://doi.org/${doi}`,
          target: "_blank",
          rel: ["noopener", "noreferrer"],
          className: ["doi-link"],
        },
        children: [{ type: "text", value: trimmed }],
      });
      if (tail) buffer += tail;
    }
  }
  buffer += raw.slice(lastIndex);
  flush();
  return out;
}

function anchor(href: string, text: string): Element {
  return {
    type: "element",
    tagName: "a",
    properties: { href, className: ["xref"] },
    children: [{ type: "text", value: text }],
  };
}

const NO_LINK_TAGS = new Set([
  "a",
  "code",
  "pre",
  "script",
  "style",
  "svg",
  "math",
]);

const rehypeCitations: Plugin<[], Root> = () => (tree) => {
  // ----- Pass 1: collect targets (refs / captions / headings) -----
  const refs: RefEntry[] = [];
  const figures = new Set<string>();
  const tables = new Set<string>();
  const sections = new Set<string>();
  const appendices = new Set<string>();

  // Walk at root level to find the References heading and its following <p>s.
  const rootChildren = tree.children;
  let inRefs = false;
  let refCounter = 0;
  for (const child of rootChildren) {
    if (!isElement(child)) {
      if (inRefs && child.type !== "text") inRefs = false;
      continue;
    }
    if (/^h[1-6]$/.test(child.tagName)) {
      if (/^\s*references\s*$/i.test(toText(child))) {
        inRefs = true;
      } else {
        // A new heading after References ends the reference list.
        inRefs = false;
      }
      // Also register numbered headings / appendix letters as targets.
      const htext = toText(child).trim();
      let hm = htext.match(/^(\d+(?:\.\d+)*)\b/);
      if (hm) {
        setId(child, `section-${hm[1]}`);
        sections.add(hm[1]);
      } else if (inRefs === false && afterRefsStart(tree, child)) {
        hm = htext.match(/^([A-Z])(?:\s|$)/);
        if (hm) {
          setId(child, `appendix-${hm[1].toLowerCase()}`);
          appendices.add(hm[1]);
        }
      }
      continue;
    }
    if (inRefs && child.tagName === "p") {
      refCounter += 1;
      const id = `ref-${refCounter}`;
      setId(child, id);
      child.properties = { ...(child.properties ?? {}), "data-ref": "true" };
      const text = toText(child);
      const surname = surnameFromReference(text);
      const year = yearFrom(text);
      if (surname && year) refs.push({ id, surname, year });
    }
  }

  // Figure/table captions anywhere in the tree.
  visit(tree, "element", (node) => {
    if (node.tagName !== "p") return;
    const text = toText(node);
    let m = text.match(/^\s*(?:Figure|Fig)\.?\s*([A-Z0-9][\w.]*)/i);
    if (m) {
      setId(node, `figure-${m[1]}`);
      node.properties = { ...(node.properties ?? {}), "data-caption": "figure" };
      figures.add(m[1]);
      return;
    }
    m = text.match(/^\s*Table\s+([A-Z0-9][\w.]*)/i);
    if (m) {
      setId(node, `table-${m[1]}`);
      node.properties = { ...(node.properties ?? {}), "data-caption": "table" };
      tables.add(m[1]);
    }
  });

  // ----- Pass 2: linkify text nodes -----
  visit(tree, "text", (node: Text, index, parent) => {
    if (!parent || !isElement(parent) || parent.tagName === undefined) return;
    if (NO_LINK_TAGS.has(parent.tagName)) return;
    if (/^h[1-6]$/.test(parent.tagName)) return;
    // Don't link inside a caption's own "Figure N:" or a reference entry.
    if (parent.properties && (parent.properties["data-ref"] || parent.properties["data-caption"])) {
      return;
    }
    const text = node.value;
    if (!text || text.length < 4) return;
    // Cheap pre-filter
    if (!/[Ff]ig|[Tt]able|[Ss]ection|[Aa]ppendix|\(\s*[A-Z][^)]*\d{4}/.test(text)) {
      return;
    }
    const replacements = linkifyText(text, { refs, figures, tables, sections, appendices });
    // If nothing was turned into a link, leave the node untouched.
    if (
      replacements.length === 1 &&
      replacements[0].type === "text" &&
      (replacements[0] as Text).value === text
    ) {
      return;
    }
    if (typeof index === "number") {
      parent.children.splice(index, 1, ...replacements);
      return [SKIP, index + replacements.length];
    }
  });
};

/** Has `target` been seen after the "## References" heading in root order? */
function afterRefsStart(tree: Root, target: Element): boolean {
  let seenRefs = false;
  for (const c of tree.children) {
    if (c === target) return seenRefs;
    if (isElement(c) && /^h[1-6]$/.test(c.tagName) && /^\s*references\s*$/i.test(toText(c))) {
      seenRefs = true;
    }
  }
  return false;
}

export default rehypeCitations;
