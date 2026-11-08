import { useState } from "react";
import { Navigate, NavLink, Route, Routes, useLocation } from "react-router-dom";
import { useSession } from "./session";
import { GROUPS, TOP, allowed, crumbFor, slug, type NavItem } from "./nav";
import { Icon } from "./icons";
import Login from "./pages/Login";
import Chat from "./pages/Chat";
import Uploads from "./pages/Uploads";
import Portal from "./pages/Portal";
import Inbox from "./pages/Inbox";
import Flags from "./pages/Flags";
import Inspector from "./pages/Inspector";
import Ops from "./pages/Ops";
import StarMap from "./pages/StarMap";
import Search from "./pages/Search";
import Spaces from "./pages/Spaces";
import Marts from "./pages/Marts";
import Mapping from "./pages/Mapping";
import Graphs from "./pages/Graphs";
import OntologyBrowse from "./pages/OntologyBrowse";
import Access from "./pages/Access";
import Activity from "./pages/Activity";
import Approvals from "./pages/Approvals";
import NotBuilt from "./pages/NotBuilt";

const TOP_ICONS: Record<string, string> = { "New chat": "chat", Search: "search", "Explore graph": "graph" };

function itemLink(i: NavItem) {
  if (i.notBuilt)
    return (
      <NavLink key={i.label} to={`/not-built/${slug(i.label)}`} className="nb-link">
        {i.label} <span className="nb-tag">Not built</span>
      </NavLink>
    );
  return (
    <NavLink key={i.label} to={i.to!} end>
      {i.label}
    </NavLink>
  );
}

export default function App() {
  const { me, loading, logout } = useSession();
  const loc = useLocation();
  const [navOpen, setNavOpen] = useState(() => !window.matchMedia("(max-width: 760px)").matches);
  const [closed, setClosed] = useState<Record<string, boolean>>({});
  if (loading) return <div className="center muted">Loading…</div>;
  if (!me && loc.pathname !== "/login") return <Navigate to="/login" replace />;
  if (!me) return <Login />;

  const crumb = crumbFor(loc.pathname);
  const initials = me.display_name
    .split(/\s+/)
    .map((w) => w[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();
  return (
    <div className={`xshell ${navOpen ? "" : "collapsed"}`}>
      <nav className="xnav" aria-label="Main navigation">
        <div className="xbrand">
          <span className="mark" aria-hidden="true" />
          <span>Knowledge Explorer</span>
          <span className="grow" />
          <button className="iconbtn" aria-label="Hide navigation" onClick={() => setNavOpen(false)}>
            <Icon name="panel" />
          </button>
        </div>
        <div className="xscroll">
          <div className="xtop">
            {TOP.map((t) => (
              <NavLink key={t.label} to={t.to!} end>
                <Icon name={TOP_ICONS[t.label]} />
                {t.label}
              </NavLink>
            ))}
          </div>
          <div className="xsep" />
          {GROUPS.map((g) => {
            const items = g.items.filter((i) => allowed(i, me));
            if (!items.length) return null;
            const open = !closed[g.title];
            return (
              <div key={g.title} className={`xgroup ${open ? "open" : ""}`} data-testid={`group-${slug(g.title)}`}>
                <button
                  className="xghead"
                  aria-expanded={open}
                  onClick={() => setClosed({ ...closed, [g.title]: open })}
                >
                  <Icon name={g.icon} />
                  <span className="grow">{g.title}</span>
                  <Icon name="chev" className="chev" />
                </button>
                {open && <div className="xsub">{items.map(itemLink)}</div>}
              </div>
            );
          })}
        </div>
        <div className="xuser" data-testid="who">
          <span className="av">{initials}</span>
          <span className="grow">
            {me.display_name}
            <br />
            <span className="muted">{me.user_id}</span>
          </span>
          <button className="link" onClick={logout}>
            Sign out
          </button>
        </div>
      </nav>
      <div className="xmain">
        <div className="xbar">
          {!navOpen && (
            <button className="iconbtn" aria-label="Show navigation" onClick={() => setNavOpen(true)}>
              <Icon name="panel" />
            </button>
          )}
          <span className="crumb">
            {crumb ? (
              <>
                {crumb.group} / <b>{crumb.label}</b>
              </>
            ) : null}
          </span>
          <span className="grow" />
          <span className="pill-note">Synthetic data · local prototype</span>
        </div>
        <main>
          <Routes>
            <Route path="/chat" element={<Chat />} />
            <Route path="/search" element={<Search />} />
            <Route path="/uploads" element={<Uploads />} />
            <Route path="/portal" element={<Portal />} />
            <Route path="/inbox" element={<Inbox />} />
            <Route path="/flags" element={<Flags />} />
            <Route path="/documents/:docId" element={<Inspector />} />
            <Route path="/ops" element={<Ops />} />
            <Route path="/ontology" element={<StarMap />} />
            <Route path="/ontology/browse" element={<OntologyBrowse />} />
            <Route path="/ontology/mapping" element={<Mapping />} />
            <Route path="/ontology/graphs" element={<Graphs />} />
            <Route path="/spaces" element={<Spaces />} />
            <Route path="/catalog/marts" element={<Marts />} />
            <Route path="/access" element={<Access />} />
            <Route path="/activity" element={<Activity />} />
            <Route path="/governance/approvals" element={<Approvals />} />
            <Route path="/not-built/:slug" element={<NotBuilt />} />
            <Route path="*" element={<Navigate to="/chat" replace />} />
          </Routes>
        </main>
      </div>
    </div>
  );
}
