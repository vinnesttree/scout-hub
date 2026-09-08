import { NavLink, Route, Routes } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "./api/client";
import Recordings from "./pages/Recordings";
import MatchLibrary from "./pages/MatchLibrary";
import MatchDetail from "./pages/MatchDetail";
import Settings from "./pages/Settings";

function ToolBadge() {
  const { data } = useQuery({ queryKey: ["health"], queryFn: api.health });
  if (!data) return null;
  const bad = Object.entries(data.tools).filter(([, ok]) => !ok).map(([k]) => k);
  if (bad.length === 0) return <span className="badge ok">tools ready</span>;
  return (
    <span className="badge warn" title={data.ocr_detail}>
      missing: {bad.join(", ")}
    </span>
  );
}

export default function App() {
  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          Scout&nbsp;Hub
        </div>
        <nav>
          <NavLink to="/recordings">Recordings</NavLink>
          <NavLink to="/matches">Match Library</NavLink>
          <NavLink to="/settings">Settings</NavLink>
        </nav>
        <ToolBadge />
      </header>
      <main>
        <Routes>
          <Route path="/" element={<Recordings />} />
          <Route path="/recordings" element={<Recordings />} />
          <Route path="/matches" element={<MatchLibrary />} />
          <Route path="/matches/:id" element={<MatchDetail />} />
          <Route path="/settings" element={<Settings />} />
        </Routes>
      </main>
    </div>
  );
}
