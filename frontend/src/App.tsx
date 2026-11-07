import { Navigate, NavLink, Outlet, Route, Routes, useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./api/client";
import { RequireAuth, useAuth } from "./auth";
import Recordings from "./pages/Recordings";
import MatchLibrary from "./pages/MatchLibrary";
import MatchDetail from "./pages/MatchDetail";
import Settings from "./pages/Settings";
import Login from "./pages/Login";
import Users from "./pages/Users";
import KeyFrames from "./pages/KeyFrames";

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

function AccountMenu() {
  const { user, isLoading } = useAuth();
  const qc = useQueryClient();
  const nav = useNavigate();
  if (isLoading) return null;
  if (!user) return <NavLink to="/login" className="signin">Sign in</NavLink>;
  const signOut = async () => {
    try {
      await api.logout();
    } finally {
      // leave the admin area first so its route guard doesn't bounce us to /login
      nav("/matches");
      qc.setQueryData(["me"], { user: null, setup_required: false });
      // drop cached admin-only responses
      for (const key of ["recordings", "jobs", "settings", "users", "keyframes", "keyframe-summary"]) {
        qc.removeQueries({ queryKey: [key] });
      }
      qc.invalidateQueries({ queryKey: ["me"] });
    }
  };
  return (
    <div className="row" style={{ alignItems: "center", gap: 8 }}>
      <span className="muted">{user.username}{user.is_admin && " · admin"}</span>
      <button className="sm" onClick={signOut}>Sign out</button>
    </div>
  );
}

/** Admin area: everything that creates or changes data. Requires an account. */
function AdminLayout() {
  return (
    <RequireAuth>
      <nav className="subnav">
        <NavLink to="/admin" end>Recordings</NavLink>
        <NavLink to="/admin/keyframes">Key frames</NavLink>
        <NavLink to="/admin/settings">Settings &amp; events</NavLink>
        <NavLink to="/admin/users">Accounts</NavLink>
      </nav>
      <Outlet />
    </RequireAuth>
  );
}

export default function App() {
  const { user } = useAuth();
  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          Scout&nbsp;Hub
        </div>
        <nav>
          <NavLink to="/matches">Match Library</NavLink>
          {user && <NavLink to="/admin">Admin</NavLink>}
        </nav>
        {user && <ToolBadge />}
        <AccountMenu />
      </header>
      <main>
        <Routes>
          <Route path="/" element={<Navigate to="/matches" replace />} />
          <Route path="/matches" element={<MatchLibrary />} />
          <Route path="/matches/:id" element={<MatchDetail />} />
          <Route path="/login" element={<Login />} />
          <Route path="/admin" element={<AdminLayout />}>
            <Route index element={<Recordings />} />
            <Route path="keyframes" element={<KeyFrames />} />
            <Route path="settings" element={<Settings />} />
            <Route path="users" element={<Users />} />
          </Route>
          {/* old locations */}
          <Route path="/recordings" element={<Navigate to="/admin" replace />} />
          <Route path="/settings" element={<Navigate to="/admin/settings" replace />} />
          <Route path="*" element={<Navigate to="/matches" replace />} />
        </Routes>
      </main>
    </div>
  );
}
