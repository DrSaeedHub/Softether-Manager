"use client";

import { useState } from "react";
import { useAuth } from "../lib/auth";
import { useServer } from "../lib/server";
import { Link, back, navigate, seg, useRoute } from "../lib/router";
import { useUpdate } from "../lib/update";
import {
  BrandMark,
  IconBack,
  IconCheck,
  IconChevron,
  IconHub,
  IconLogs,
  IconMoon,
  IconPanels,
  IconPulse,
  IconSettings,
  IconSignOut,
  IconSun,
  IconTerminal,
  IconUsers,
} from "../ui/Icon";
import { useTheme } from "../ui/theme";

/**
 * Two shells, one component.
 *
 * >= 960px: a soft sidebar on the canvas — labelled destinations with the
 * hubs listed beneath, collapsible down to a column of icon keys. Expanded
 * is the default; the choice sticks per browser.
 *
 * < 960px: a native-app shell -- a solid top bar that shows where you are,
 * and a bottom tab bar with three destinations.
 */

function readCollapsed(): boolean {
  try {
    return localStorage.getItem("sem_rail") === "min";
  } catch {
    return false;
  }
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const [collapsed, setCollapsed] = useState(readCollapsed);
  const toggleRail = () =>
    setCollapsed((c) => {
      const next = !c;
      try {
        localStorage.setItem("sem_rail", next ? "min" : "full");
      } catch {
        /* private mode: the choice just does not stick */
      }
      return next;
    });

  return (
    <div className={`shell${collapsed ? " shell--min" : ""}`}>
      <Side collapsed={collapsed} onToggle={toggleRail} />
      <TopBar />
      <main className="shell__scroll">
        <MainBar />
        {children}
      </main>
      <TabBar />
    </div>
  );
}

/* ── the desktop sidebar ────────────────────────────────────────────────── */

const DESTINATIONS = [
  { to: "/users", label: "Users", icon: <IconUsers size={19} /> , match: (p: string) => p === "users" },
  { to: "/connections", label: "Connections", icon: <IconPulse size={19} />, match: (p: string) => p === "connections" },
  { to: "/logs", label: "Logs", icon: <IconLogs size={19} />, match: (p: string) => p === "logs" },
  { to: "/console", label: "API console", icon: <IconTerminal size={19} />, match: (p: string) => p === "console" },
  { to: "/settings", label: "Settings", icon: <IconSettings size={19} />, match: (p: string) => p === "settings" },
];

function Side({ collapsed, onToggle }: { collapsed: boolean; onToggle: () => void }) {
  const { user, logout } = useAuth();
  const { hubs } = useServer();
  const route = useRoute();
  const active = route.parts[0] ?? "";
  const activeHub = active === "hub" ? route.parts[1] : null;

  // The hub leaves carry /hub/* while they are visible; collapsed, the
  // Dashboard key represents them.
  const dashOn =
    active === "" || active === "connect" || active === "server-settings" ||
    (collapsed && active === "hub");

  const item = (
    to: string,
    label: string,
    icon: React.ReactNode,
    on: boolean,
  ) => (
    <Link
      key={to}
      to={to}
      className={`side__item${on ? " on" : ""}`}
      title={collapsed ? label : undefined}
      aria-label={label}
    >
      {icon}
      <span className="side__label">{label}</span>
    </Link>
  );

  return (
    <aside className="side">
      <div className="side__top">
        <Link to="/" className="side__brand" aria-label="Dashboard">
          <BrandMark size={38} />
          <span className="side__label">
            <span className="side__word">SoftEther</span>
            <span className="side__sub">Manager</span>
          </span>
        </Link>
        <button
          className="icon-btn side__fold"
          onClick={onToggle}
          title={collapsed ? "Expand the sidebar" : "Collapse the sidebar"}
          aria-label={collapsed ? "Expand the sidebar" : "Collapse the sidebar"}
          aria-expanded={!collapsed}
        >
          <IconChevron size={15} />
        </button>
      </div>

      <nav className="side__nav" aria-label="Primary">
        {item("/", "Dashboard", <IconPanels size={19} />, dashOn)}

        {!collapsed && hubs && hubs.length > 0 && (
          <div className="side__group">
            <div className="side__gtitle">Hubs</div>
            {hubs.map((h) => {
              const name = String(h.HubName_str);
              return (
                <button
                  key={name}
                  className={`side__leaf${activeHub === name ? " on" : ""}`}
                  data-state={h.Online_bool ? "connected" : "disabled"}
                  onClick={() => navigate(`/hub/${seg(name)}`)}
                  title={`${name} — ${h.Online_bool ? "online" : "offline"}`}
                >
                  <span className="side__dot" />
                  <span className="side__label">{name}</span>
                  <span className="side__count">{Number(h.NumSessions_u32) || ""}</span>
                </button>
              );
            })}
          </div>
        )}
        {collapsed && (
          <Link
            to={activeHub ? `/hub/${seg(activeHub)}` : "/"}
            className={`side__item${active === "hub" ? " on" : ""}`}
            title="Hubs"
            aria-label="Hubs"
          >
            <IconHub size={19} />
            <span className="side__label">Hubs</span>
          </Link>
        )}

        {DESTINATIONS.map((d) => item(d.to, d.label, d.icon, d.match(active)))}
      </nav>

      <div className="side__foot">
        <ThemeToggle />
        <button
          className="side__user"
          onClick={logout}
          title={`${user ?? ""} — sign out`}
          aria-label="Sign out"
        >
          <span className="side__coin">{(user ?? "?").slice(0, 1)}</span>
          <span className="side__label truncate">{user}</span>
          <span className="side__out">
            <IconSignOut size={15} />
          </span>
        </button>
      </div>
    </aside>
  );
}

/* ── the utility cluster at the top of the content ──────────────────────── */

function MainBar() {
  const route = useRoute();
  const live =
    route.path === "/" || route.parts[0] === "hub" || route.parts[0] === "connections";
  return (
    <div className="mainbar">
      <HealthChip />
      {live && (
        <span className="mchip mchip--live">
          <span className="mchip__dot" style={{ background: "var(--ok)" }} />
          Live
        </span>
      )}
      <VersionPill />
    </div>
  );
}

/** One server, one verdict. */
function HealthChip() {
  const { health, probe } = useServer();
  if (health === "probing") return null;
  if (health === "online") {
    return (
      <span className="mchip mchip--ok">
        <IconCheck size={13} />
        VPN server up
      </span>
    );
  }
  if (health === "unconfigured") {
    return (
      <Link to="/connect" className="mchip">
        not connected
      </Link>
    );
  }
  return (
    <span className="mchip mchip--err" title={probe?.error ?? ""}>
      VPN server down
    </span>
  );
}

/* ── mobile chrome ──────────────────────────────────────────────────────── */

function TopBar() {
  const route = useRoute();
  const p = route.parts;

  const isPushed = p.length > 0 && !(p[0] === "settings" && p.length === 1) && p[0] !== "users";
  let title = "Dashboard";
  if (p[0] === "users") title = "Users";
  else if (p[0] === "settings") title = "Settings";
  else if (p[0] === "connect") title = "Connect";
  else if (p[0] === "server-settings") title = "Server settings";
  else if (p[0] === "connections") title = "Connections";
  else if (p[0] === "logs") title = "Logs";
  else if (p[0] === "console") title = "Console";
  else if (p[0] === "hub" && p[1]) {
    title = p[2] === "user" && p[3] ? p[3] : p[1];
  }

  return (
    <header className="topbar">
      {isPushed && p.length > 0 ? (
        <button className="topbar__back" onClick={back} aria-label="Back">
          <IconBack size={20} />
        </button>
      ) : (
        <BrandMark size={28} />
      )}
      <span className="topbar__title">{title}</span>
      <ThemeToggle />
      <VersionPill compact />
    </header>
  );
}

function TabBar() {
  const route = useRoute();
  const active = route.parts[0] ?? "";
  const tab = (match: (p: string) => boolean, to: string, label: string, icon: React.ReactNode) => (
    <Link to={to} className="tab" aria-current={match(active) ? "page" : undefined}>
      {icon}
      <span>{label}</span>
    </Link>
  );
  return (
    <nav className="tabbar" aria-label="Primary">
      {tab((p) => p === "" || p === "hub" || p === "connect", "/", "Dashboard", <IconPanels />)}
      {tab((p) => p === "users", "/users", "Users", <IconUsers />)}
      {tab((p) => p === "settings", "/settings", "Settings", <IconSettings />)}
    </nav>
  );
}

/* ── shared chrome ──────────────────────────────────────────────────────── */

export function ThemeToggle() {
  const { resolved, toggle } = useTheme();
  const dark = resolved === "dark";
  return (
    <button
      className="icon-btn"
      onClick={toggle}
      title={dark ? "Switch to light" : "Switch to dark"}
      aria-label={dark ? "Switch to light theme" : "Switch to dark theme"}
    >
      {dark ? <IconSun size={17} /> : <IconMoon size={17} />}
    </button>
  );
}

/** The version is a button, because the only thing anyone wants to do with a
 *  version number is find out whether it is the current one. */
export function VersionPill({ compact }: { compact?: boolean }) {
  const { status, open, applying } = useUpdate();
  const available = Boolean(status?.check?.update_available);
  const v = String(status?.check?.current_version ?? "");

  return (
    <button
      className={`verpill${available ? " verpill--new" : ""}`}
      onClick={open}
      title={
        applying
          ? "An update is running"
          : available
            ? `Version ${status?.check?.latest?.version} is available`
            : "Check for updates"
      }
    >
      {applying && <span className="spin" style={{ width: 11, height: 11 }} />}
      <span className="truncate">{compact ? v.replace(/^v/, "") : v || "—"}</span>
      {available && <span className="verpill__dot" />}
    </button>
  );
}

export { seg };
