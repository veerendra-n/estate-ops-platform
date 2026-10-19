import type { ReactNode } from "react";
import { NavLink, useLocation } from "react-router-dom";
import {
  Boxes,
  Building2,
  Coins,
  FlaskConical,
  Inbox,
  LogOut,
  Monitor,
  Moon,
  Network,
  Plug,
  Sun,
  Wrench,
} from "lucide-react";
import { post, setToken } from "../api";
import { useApp } from "../context";
import { usePoll } from "../hooks";
import { useTheme, type ThemePref } from "../theme";
import type { Overview } from "../types";
import { ErrorNote } from "./ui";

export function Logo({ size = 28 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" className="logo" aria-hidden>
      <defs>
        <linearGradient id="lg" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#818cf8" />
          <stop offset="1" stopColor="#6366f1" />
        </linearGradient>
      </defs>
      <rect width="32" height="32" rx="8" fill="url(#lg)" />
      <path d="M8 21.5l4.6-11 4 8 2.2-4.2 3.2 7.2" stroke="white" strokeWidth="2.4" fill="none" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

const THEME_NEXT: Record<ThemePref, ThemePref> = { system: "light", light: "dark", dark: "system" };

export function Layout({ children, onSignedOut, bootError }: { children: ReactNode; onSignedOut: () => void; bootError: string | null }) {
  const { health, me, labEnabled, tenants } = useApp();
  const [theme, setTheme] = useTheme();
  const loc = useLocation();
  const ov = usePoll<Overview>("/api/v1/overview", 10000);
  const awaiting = ov.data?.totals.awaiting_approval ?? 0;
  const estateHref = tenants[0] ? `/estate/${tenants[0].id}` : "/estate";

  const nav: { to: string; label: string; icon: ReactNode; badge?: number; match?: (p: string) => boolean }[] = [
    { to: "/", label: "Queue", icon: <Inbox size={16} />, badge: awaiting, match: (p) => p === "/" || p.startsWith("/cases") },
    { to: estateHref, label: "Estate", icon: <Network size={16} />, match: (p) => p.startsWith("/estate") },
    { to: "/skills", label: "Skills", icon: <Wrench size={16} /> },
    { to: "/customers", label: "Customers", icon: <Building2 size={16} /> },
    { to: "/integrations", label: "Integrations", icon: <Plug size={16} /> },
    { to: "/cost", label: "AI cost", icon: <Coins size={16} /> },
  ];
  if (labEnabled) nav.push({ to: "/lab", label: "Lab", icon: <FlaskConical size={16} /> });

  const signOut = async () => {
    try {
      await post("/auth/logout");
    } catch {
      /* already signed out */
    }
    setToken(null);
    onSignedOut();
  };

  const ThemeIcon = theme === "light" ? Sun : theme === "dark" ? Moon : Monitor;

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <Logo />
          <div className="brand-text">
            <div className="brand-name">Estate Ops</div>
            <div className="brand-sub">Operations cockpit</div>
          </div>
        </div>
        <nav className="nav">
          {nav.map((n) => {
            const active = n.match ? n.match(loc.pathname) : loc.pathname.startsWith(n.to);
            return (
              <NavLink key={n.label} to={n.to} className={() => `nav-item ${active ? "active" : ""}`} title={n.label}>
                {n.icon}
                <span className="nav-label">{n.label}</span>
                {!!n.badge && (
                  <span className="nav-badge" title={`${n.badge} awaiting approval`}>
                    {n.badge}
                  </span>
                )}
              </NavLink>
            );
          })}
        </nav>
        <div className="sidebar-foot">
          {me?.user && (
            <div className="user" title={me.user.email}>
              <div className="avatar">{me.user.name.slice(0, 1).toUpperCase()}</div>
              <div className="user-text">
                <div className="user-name">{me.user.name}</div>
                <div className="user-mail">{me.user.email}</div>
              </div>
            </div>
          )}
          <div className="foot-actions">
            <button className="icon-btn" onClick={() => setTheme(THEME_NEXT[theme])} title={`Theme: ${theme} (click to change)`}>
              <ThemeIcon size={15} />
            </button>
            <button className="icon-btn" onClick={() => void signOut()} title="Sign out">
              <LogOut size={15} />
            </button>
          </div>
          <div className="build" title={health ? `${health.skills} skills loaded` : "API health unknown"}>
            <span className={`dot ${health?.ok ? "ok" : "bad"}`} />
            <span className="build-text">
              {health ? `v${health.version}` : "offline"}
              {health?.model && <> · {health.model}</>}
            </span>
            {health && (
              <span className="build-skills">
                <Boxes size={11} /> {health.skills}
              </span>
            )}
          </div>
        </div>
      </aside>
      <main className="main">
        {bootError && <ErrorNote error={new Error(bootError)} />}
        {children}
      </main>
    </div>
  );
}
