import type { ReactNode } from "react";
import { TopicSidebar } from "@/components/TopicSidebar";

// Two-column shell shared by /library and /topics: a sticky topic facet on the
// left and the page content on the right. Mirrors the two-column grid used on
// the Today page (Home.tsx).
export function TopicsLayout({
  children,
  sidebarTop,
}: {
  children: ReactNode;
  sidebarTop?: ReactNode;
}) {
  return (
    <div className="container max-w-screen-2xl py-6">
      <div className="grid grid-cols-1 gap-6 md:grid-cols-[220px_minmax(0,1fr)]">
        <aside className="hidden md:block">
          <div className="sticky top-20 space-y-4">
            {sidebarTop}
            <TopicSidebar />
          </div>
        </aside>
        <div className="min-w-0">{children}</div>
      </div>
    </div>
  );
}
