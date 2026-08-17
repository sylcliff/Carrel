/**
 * Rehype plugin that renders $...$ / $$...$$ math *inside raw HTML blocks*.
 *
 * MinerU emits tables as inline `<table>...<td>$K{=}5$</td>...</table>`.
 * By CommonMark rules the contents of an HTML block are never re-parsed as
 * markdown, so remark-math never sees those dollars and rehype-katex leaves
 * them as literal text. This plugin runs *before* rehype-raw, walks every
 * `raw` hast node (the unparsed HTML string that rehype-raw will later turn
 * into elements), and replaces math segments with KaTeX-rendered HTML. The
 * rendered spans stay as raw text so rehype-raw picks them up along with the
 * surrounding <table> markup.
 *
 * We deliberately avoid matching across HTML tags (`[^$<>]+`), which also
 * sidesteps dollar signs in attributes like `width="100%"`.
 */
import katex from "katex";
import { visit } from "unist-util-visit";
import type { Plugin } from "unified";
import type { Root } from "hast";

// Display first ($$...$$), then inline ($...$). Both non-greedy, neither
// crosses another `$` or a tag boundary.
const DISPLAY_RE = /\$\$([^$]+?)\$\$/g;
const INLINE_RE = /\$([^$<>]+?)\$/g;

interface RawNode {
  type: "raw";
  value: string;
}

function renderMath(src: string, displayMode: boolean): string {
  try {
    return katex.renderToString(src.trim(), {
      displayMode,
      throwOnError: false,
      output: "html",
    });
  } catch {
    return src;
  }
}

/** Replace $$...$$ and $...$ inside one raw-HTML string with KaTeX HTML. */
function transform(value: string): string {
  return value
    .replace(DISPLAY_RE, (_m, body: string) => renderMath(body, true))
    .replace(INLINE_RE, (_m, body: string) => renderMath(body, false));
}

const rehypeRawMath: Plugin<[], Root> = () => (tree) => {
  visit(tree, "raw", (node: RawNode) => {
    if (typeof node.value !== "string" || !node.value.includes("$")) return;
    node.value = transform(node.value);
  });
};

export default rehypeRawMath;
