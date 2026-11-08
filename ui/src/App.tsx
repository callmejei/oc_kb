import { Navigate, NavLink, Route, Routes, useLocation } from "react-router-dom";
import { useSession } from "./session";
import Login from "./pages/Login";
import Chat from "./pages/Chat";
import Uploads from "./pages/Uploads";
import Portal from "./pages/Portal";
import Inbox from "./pages/Inbox";
import Flags from "./pages/Flags";
import Inspector from "./pages/Inspector";

export default function App() {
  const { me, loading, logout } = useSession();
  const loc = useLocation();
  if (loading) return <div className="center muted">Loading…</div>;
  if (!me && loc.pathname !== "/login") return <Navigate to="/login" replace />;
  if (!me) return <Login />;

  const maker = me.maker_bus.length > 0;
  const checker = me.checker_bus.length > 0;
  return (
    <div className="shell">
      <header className="top">
        <span className="brand">Knowledge Base</span>
        <nav>
          <NavLink to="/chat">Ask</NavLink>
          <NavLink to="/uploads">My files</NavLink>
          {maker && <NavLink to="/portal">Sources</NavLink>}
          {(maker || checker) && <NavLink to="/flags">Flags</NavLink>}
          {checker && <NavLink to="/inbox">Checker inbox</NavLink>}
        </nav>
        <span className="who" data-testid="who">
          {me.display_name} <span className="muted">({me.user_id})</span>
          <button className="link" onClick={logout}>
            Sign out
          </button>
        </span>
      </header>
      <main>
        <Routes>
          <Route path="/chat" element={<Chat />} />
          <Route path="/uploads" element={<Uploads />} />
          <Route path="/portal" element={<Portal />} />
          <Route path="/inbox" element={<Inbox />} />
          <Route path="/flags" element={<Flags />} />
          <Route path="/documents/:docId" element={<Inspector />} />
          <Route path="*" element={<Navigate to="/chat" replace />} />
        </Routes>
      </main>
    </div>
  );
}
