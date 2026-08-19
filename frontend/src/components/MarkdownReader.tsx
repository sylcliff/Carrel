import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeRaw from "rehype-raw";
import rehypeKatex from "rehype-katex";
import rehypeRawMath from "./rehypeRawMath";
import rehypeCitations from "./rehypeCitations";
import "katex/dist/katex.min.css";

// Resolve MinerU's relative image links (e.g. "images/fig1.png") against the
// backend static mount. `mdPath` is storage-root-relative
// (e.g. "papers/W123/paper.md"); its directory is the base for relative URLs.
function resolveImageSrc(src: string | undefined, mdPath: string | null): string {
  if (!src) return "";
  if (/^https?:\/\//i.test(src) || src.startsWith("/")) return src;
  const dir = mdPath ? mdPath.split("/").slice(0, -1).join("/") : "";
  return `/storage/${dir ? dir + "/" : ""}${src}`;
}

export default function MarkdownReader({
  body,
  mdPath,
}: {
  body: string;
  mdPath: string | null;
}) {
  return (
    <div className="md-body text-justify">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[rehypeRawMath, rehypeRaw, rehypeCitations, rehypeKatex]}
        components={{
          a: ({ href, children, ...rest }) => {
            // In-document anchors (citations, figures, sections) must stay in
            // the same tab so the browser scrolls to the target. Forcing
            // target=_blank on a "#ref-n" link reopened the current page in a
            // new window instead of jumping to the reference.
            const isAnchor = typeof href === "string" && href.startsWith("#");
            return (
              <a
                href={href}
                target={isAnchor ? undefined : "_blank"}
                rel={isAnchor ? undefined : "noopener noreferrer"}
                className="text-primary underline underline-offset-2 hover:opacity-80"
                {...rest}
              >
                {children}
              </a>
            );
          },
          img: ({ src, alt }) => (
            <img
              src={resolveImageSrc(src, mdPath)}
              alt={alt ?? ""}
              loading="lazy"
              className="my-4 h-auto max-w-full rounded-md border"
            />
          ),
          // MinerU emits tables as inline HTML; without these overrides they
          // render with the browser's default borderless styling and overflow.
          table: ({ children }) => (
            <div className="my-4 overflow-x-auto">
              <table className="w-full border-collapse text-sm">
                {children}
              </table>
            </div>
          ),
          thead: ({ children }) => <thead className="bg-muted/50">{children}</thead>,
          th: ({ children }) => (
            <th className="border border-border px-3 py-1.5 text-left font-semibold">
              {children}
            </th>
          ),
          td: ({ children, ...rest }) => (
            <td className="border border-border px-3 py-1.5 align-top" {...rest}>
              {children}
            </td>
          ),
        }}
      >
        {body}
      </ReactMarkdown>
    </div>
  );
}
