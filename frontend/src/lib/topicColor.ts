// Deterministic, stable color for a topic name. The same name always maps to
// the same color so chips agree across the sidebar, cards, and detail page.
// Palette mirrors the {color}-100/{color}-800 tint convention used by source
// badges (Search.tsx), with dark variants.

const PALETTE: string[] = [
  "bg-sky-100 text-sky-800 dark:bg-sky-900/40 dark:text-sky-200",
  "bg-violet-100 text-violet-800 dark:bg-violet-900/40 dark:text-violet-200",
  "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-200",
  "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-200",
  "bg-rose-100 text-rose-800 dark:bg-rose-900/40 dark:text-rose-200",
  "bg-teal-100 text-teal-800 dark:bg-teal-900/40 dark:text-teal-200",
  "bg-indigo-100 text-indigo-800 dark:bg-indigo-900/40 dark:text-indigo-200",
  "bg-fuchsia-100 text-fuchsia-800 dark:bg-fuchsia-900/40 dark:text-fuchsia-200",
  "bg-lime-100 text-lime-800 dark:bg-lime-900/40 dark:text-lime-200",
  "bg-cyan-100 text-cyan-800 dark:bg-cyan-900/40 dark:text-cyan-200",
  "bg-orange-100 text-orange-800 dark:bg-orange-900/40 dark:text-orange-200",
  "bg-pink-100 text-pink-800 dark:bg-pink-900/40 dark:text-pink-200",
];

function hashString(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i++) {
    h = (h << 5) - h + s.charCodeAt(i);
    h |= 0;
  }
  return Math.abs(h);
}

export function topicColorClass(name: string): string {
  return PALETTE[hashString(name) % PALETTE.length];
}

// A small solid color swatch for list rows (sidebar), where the full tinted
// chip would be too heavy. Picks the 800/200 color from each palette entry by
// reusing the same hash so swatch and chip match.
export function topicDotClass(name: string): string {
  // Map each light/dark bg tint to a matching solid dot color.
  const DOT: string[] = [
    "bg-sky-500", "bg-violet-500", "bg-emerald-500", "bg-amber-500",
    "bg-rose-500", "bg-teal-500", "bg-indigo-500", "bg-fuchsia-500",
    "bg-lime-500", "bg-cyan-500", "bg-orange-500", "bg-pink-500",
  ];
  return DOT[hashString(name) % DOT.length];
}
