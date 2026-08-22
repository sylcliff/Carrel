import type { Element, Root, Text } from "hast";
import type { Plugin } from "unified";
import { visit } from "unist-util-visit";

const KIND_BY_DIRECTORY: Record<string, string> = {
  concepts: "concept",
  scholars: "scholar",
  questions: "question",
};

const rehypeWikiLinks: Plugin<[], Root> = () => (tree) => {
  visit(tree, "element", (node: Element) => {
    if (node.tagName !== "a") return;
    const href = node.properties?.href;
    if (typeof href !== "string" || /^https?:\/\//i.test(href) || href.startsWith("#")) return;

    const match = href.match(/\/(concepts|scholars|questions)\/([^/#]+)\.md(?:#.*)?$/);
    if (!match) return;

    node.properties = {
      ...node.properties,
      href: `/wiki/${KIND_BY_DIRECTORY[match[1]]}/${match[2]}`,
      dataWikilink: "true",
      className: ["wikilink"],
    };

    if (node.children.length === 1 && node.children[0].type === "text") {
      const text = node.children[0] as Text;
      text.value = text.value.replace(/^\[/, "").replace(/\]$/, "");
    }
  });
};

export default rehypeWikiLinks;
