import { BookText } from "lucide-react";
import { Link, Route, Routes } from "react-router-dom";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import Home from "@/pages/Home";
import Library from "@/pages/Library";
import PaperDetail from "@/pages/PaperDetail";
import Search from "@/pages/Search";
import Scholars from "@/pages/Scholars";
import ScholarDetail from "@/pages/ScholarDetail";
import Subscriptions from "@/pages/Subscriptions";
import SyncStatus from "@/pages/SyncStatus";
import Settings from "@/pages/Settings";
import Topics from "@/pages/Topics";
import Usage from "@/pages/Usage";
import Agent from "@/pages/Agent";
import AgentPipeline from "@/pages/AgentPipeline";
import WikiIndex from "@/pages/WikiIndex";
import WikiPageList from "@/pages/WikiPageList";
import WikiPageDetail from "@/pages/WikiPageDetail";
import Docs from "@/pages/Docs";

function Header() {
  return (
    <header className="sticky top-0 z-40 border-b bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/80">
      <div className="container max-w-screen-2xl flex h-14 items-center gap-6">
        <Link to="/" className="text-lg font-semibold hover:text-foreground transition-colors">
          Carrel
        </Link>
        <nav className="flex flex-1 gap-4 text-sm text-muted-foreground">
          <Link to="/today" className="hover:text-foreground">Today</Link>
          <Link to="/library" className="hover:text-foreground">Library</Link>
          <Link to="/topics" className="hover:text-foreground">Topics</Link>
          <Link to="/scholars" className="hover:text-foreground">Scholars</Link>
          <Link to="/wiki" className="hover:text-foreground">Wiki</Link>
          <Link to="/subscriptions" className="hover:text-foreground">Subscriptions</Link>
          <Link to="/sync" className="hover:text-foreground">Sync</Link>
          <Link to="/usage" className="hover:text-foreground">Usage</Link>
          <Link to="/agent" className="hover:text-foreground">Agent</Link>
          <Link to="/settings" className="hover:text-foreground">Settings</Link>
        </nav>
        <Tooltip>
          <TooltipTrigger asChild>
            <Link
              to="/docs"
              aria-label="项目文档"
              className={cn(
                "inline-flex h-9 w-9 items-center justify-center rounded-md text-sm font-medium transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              )}
            >
              <BookText className="h-4 w-4" />
            </Link>
          </TooltipTrigger>
          <TooltipContent>项目文档</TooltipContent>
        </Tooltip>
      </div>
    </header>
  );
}

export default function App() {
  return (
    <TooltipProvider delayDuration={150}>
      <div className="min-h-screen bg-background">
        <Header />
        <Routes>
          <Route path="/" element={<Search />} />
          <Route path="/today" element={<Home />} />
          <Route path="/library" element={<Library />} />
          <Route path="/topics" element={<Topics />} />
          <Route path="/scholars" element={<Scholars />} />
          <Route path="/scholars/:key" element={<ScholarDetail />} />
          <Route path="/wiki" element={<WikiIndex />} />
          <Route path="/wiki/:kind" element={<WikiPageList />} />
          <Route path="/wiki/:kind/:slug" element={<WikiPageDetail />} />
          <Route path="/docs" element={<Docs />} />
          <Route path="/subscriptions" element={<Subscriptions />} />
          <Route path="/sync" element={<SyncStatus />} />
          <Route path="/usage" element={<Usage />} />
          <Route path="/agent" element={<Agent />} />
          <Route path="/agent/:pipelineId" element={<AgentPipeline />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="/papers/:id" element={<PaperDetail />} />
        </Routes>
      </div>
    </TooltipProvider>
  );
}
