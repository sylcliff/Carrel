import ReactMarkdown from "react-markdown";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
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
    <div className="md-body">
      <ReactMarkdown
        remarkPlugins={[remarkMath]}
        rehypePlugins={[rehypeKatex]}
        components={{
          img: ({ src, alt }) => (
            <img
              src={resolveImageSrc(src, mdPath)}
              alt={alt ?? ""}
              loading="lazy"
              className="my-4 h-auto max-w-full rounded-md border"
            />
          ),
        }}
      >
        {body}
      </ReactMarkdown>
    </div>
  );
}
