import { Link, Route, Routes } from "react-router-dom";
import { TooltipProvider } from "@/components/ui/tooltip";
import Home from "@/pages/Home";
import Library from "@/pages/Library";
import PaperDetail from "@/pages/PaperDetail";
import Search from "@/pages/Search";
import Subscriptions from "@/pages/Subscriptions";
import SyncStatus from "@/pages/SyncStatus";

function Header() {
  return (
    <header className="border-b">
      <div className="container flex h-14 items-center gap-6">
        <Link to="/" className="text-lg font-semibold">
          Carrel
        </Link>
        <nav className="flex gap-4 text-sm text-muted-foreground">
          <Link to="/" className="hover:text-foreground">Today</Link>
          <Link to="/library" className="hover:text-foreground">Library</Link>
          <Link to="/search" className="hover:text-foreground">Search</Link>
          <Link to="/subscriptions" className="hover:text-foreground">Subscriptions</Link>
          <Link to="/sync" className="hover:text-foreground">Sync</Link>
        </nav>
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
          <Route path="/" element={<Home />} />
          <Route path="/library" element={<Library />} />
          <Route path="/search" element={<Search />} />
          <Route path="/subscriptions" element={<Subscriptions />} />
          <Route path="/sync" element={<SyncStatus />} />
          <Route path="/papers/:id" element={<PaperDetail />} />
        </Routes>
      </div>
    </TooltipProvider>
  );
}
