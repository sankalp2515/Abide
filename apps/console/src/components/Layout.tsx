import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { Link, NavLink, useLocation } from "react-router-dom";
import { api, PRODUCT_NAME } from "../api";
import { useEmployee } from "../employee";
import { useReviewer } from "../reviewer";
import { ReviewerSignIn } from "./ReviewerSignIn";
import { Wordmark } from "./ui";
import { LandingFooter, LandingHeader } from "./LandingNav";

type Status = "checking" | "ok" | "waking" | "down";
const BackendStatusContext = createContext<Status>("checking");
export const useBackendAvailability = () => useContext(BackendStatusContext);
const WAKE_GRACE_MS = 150_000;

function useBackendStatus(): Status {
  const [status, setStatus] = useState<Status>("checking");
  useEffect(() => {
    let alive = true;
    let firstFailureAt: number | null = null;
    let pending = false;
    const tick = async () => {
      if (pending || !alive) return;
      pending = true;
      try {
        await api.health();
        firstFailureAt = null;
        if (alive) setStatus("ok");
      } catch {
        firstFailureAt ??= Date.now();
        if (alive) setStatus(Date.now() - firstFailureAt < WAKE_GRACE_MS ? "waking" : "down");
      } finally {
        pending = false;
      }
    };
    tick();
    const id = setInterval(tick, 15000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);
  return status;
}

function StatusBanner({ status }: { status: Status }) {
  if (status === "ok" || status === "checking") return null;
  const waking = status === "waking";
  const tone = waking ? "var(--neutralise)" : "var(--block)";
  return (
    <div role="status" className="mb-6 rounded-md border px-4 py-3 text-[14px]" style={{ borderColor: tone, color: tone, background: waking ? "var(--wait-wash)" : "var(--block-wash)" }}>
      {waking
        ? "The service is waking up (it sleeps when idle to save cost). This can take up to a minute."
        : "The service is unavailable. Results shown may be out of date; live runs are paused."}
    </div>
  );
}

function useTheme() {
  const [dark, setDark] = useState(() => document.documentElement.classList.contains("dark"));
  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
  }, [dark]);
  const toggle = () => {
    const next = !dark;
    setDark(next);
    try {
      localStorage.setItem("theme", next ? "dark" : "light");
    } catch {
      /* storage unavailable */
    }
  };
  return { dark, toggle };
}

function ThemeToggle({ dark, toggle }: { dark: boolean; toggle: () => void }) {
  return (
    <button className="pill pill-sm" style={{ minHeight: 36, width: 36, padding: 0, borderWidth: 1 }} onClick={toggle}
      aria-label={dark ? "Switch to ledger paper (light)" : "Switch to night shift (dark)"} title={dark ? "Light" : "Night shift"}>
      <svg viewBox="0 0 20 20" aria-hidden>
        {dark ? <><circle cx="10" cy="10" r="3.5" /><path d="M10 1.5v2M10 16.5v2M1.5 10h2M16.5 10h2M4 4l1.4 1.4M14.6 14.6 16 16M4 16l1.4-1.4M14.6 5.4 16 4" /></>
              : <path d="M16 12.5A7 7 0 0 1 7.5 4a7 7 0 1 0 8.5 8.5Z" />}
      </svg>
    </button>
  );
}

const ICON: Record<string, ReactNode> = {
  tasks: <><rect x="4" y="3" width="16" height="18" rx="1.5" /><path d="M8 8h8M8 12h8M8 16h5" /></>,
  activity: <path d="M4 18l5-6 4 3 7-9" />,
  agents: <path d="M9 7H6a4 4 0 000 8h3M15 7h3a4 4 0 010 8h-3M8 11h8" />,
  business: <path d="M4 20V9l8-5 8 5v11M9 20v-6h6v6" />,
};

const NAV: { label: string; icon: string; to: string; children?: { to: string; label: string }[] }[] = [
  { label: "Tasks", icon: "tasks", to: "/delegate", children: [{ to: "/delegate", label: "Run a task" }, { to: "/agents", label: "Your agents" }] },
  { label: "Activity", icon: "activity", to: "/decisions", children: [{ to: "/decisions", label: "Decisions" }, { to: "/replay", label: "Replay" }, { to: "/review", label: "Review" }] },
  { label: "Agents and tools", icon: "agents", to: "/setup" },
  { label: "Your business", icon: "business", to: "/policy" },
];

function KeyChip() {
  const employee = useEmployee();
  const [copied, setCopied] = useState(false);
  if (!employee) return null;
  const masked = `${employee.key.slice(0, 10)}••••${employee.key.slice(-4)}`;
  return (
    <div className="flex flex-col gap-1.5">
      <span className="section-label">Your key</span>
      <span className="mono" style={{ fontSize: 12 }}>{masked}</span>
      <button className="pill pill-sm" style={{ borderWidth: 1 }} onClick={async () => {
        try { await navigator.clipboard.writeText(employee.key); setCopied(true); setTimeout(() => setCopied(false), 1800); } catch { /* no clipboard */ }
      }}>{copied ? "Copied" : "Copy key for your agent"}</button>
    </div>
  );
}

function Sidebar({ status, theme }: { status: Status; theme: ReturnType<typeof useTheme> }) {
  const reviewer = useReviewer();
  const { pathname } = useLocation();
  return (
    <aside className="lg:sticky lg:top-0 lg:h-screen lg:w-[256px] lg:shrink-0 lg:overflow-y-auto" style={{ background: "var(--bg-2)", borderRight: "1px solid var(--line)" }}>
      <div className="flex items-center justify-between gap-3 px-4 py-4 lg:px-5 lg:py-5">
        <Link to="/" aria-label={`${PRODUCT_NAME} home`}><Wordmark /></Link>
        <div className="flex items-center gap-2 lg:hidden"><ThemeToggle {...theme} /><Link to="/delegate" className="pill pill-primary pill-sm">New task</Link></div>
      </div>
      <div className="hidden px-4 pb-3 lg:block"><Link to="/delegate" className="pill pill-primary w-full">New task</Link></div>
      <nav aria-label="App" className="no-scrollbar flex gap-1 overflow-x-auto px-3 pb-3 lg:flex-col lg:overflow-visible lg:pb-0">
        {NAV.map((n) => {
          const inGroup = n.children ? n.children.some((c) => pathname === c.to) : pathname === n.to;
          return (
            <div key={n.label} className="flex shrink-0 gap-1 lg:flex-col">
              <NavLink to={n.to} end className={() => `nav-link ${inGroup && !n.children ? "active" : ""}`} aria-current={inGroup && !n.children ? "page" : undefined}>
                <svg viewBox="0 0 24 24" aria-hidden>{ICON[n.icon]}</svg>{n.label}
              </NavLink>
              {n.children && <div className="flex gap-1 lg:flex-col">
                {n.children.map((c) => <NavLink key={c.to} to={c.to} end className={({ isActive }) => `nav-link nav-sub ${isActive ? "active" : ""}`}>{c.label}</NavLink>)}
              </div>}
            </div>
          );
        })}
      </nav>
      <div className="hidden flex-col gap-5 border-t px-5 py-5 lg:mt-6 lg:flex" style={{ borderColor: "var(--line)" }}>
        <KeyChip />
        <div className="flex items-center justify-between gap-2">
          <span className="subtle flex items-center gap-2 text-[13px]" aria-live="polite">
            <span aria-hidden className="inline-block h-2 w-2 rounded-full" style={{ background: status === "ok" ? "var(--allow)" : status === "checking" ? "var(--subtle)" : "var(--block)" }} />
            {status === "ok" ? "Service online" : status === "checking" ? "Checking" : status === "waking" ? "Waking" : "Offline"}
          </span>
          <ThemeToggle {...theme} />
        </div>
        {reviewer && <ReviewerSignIn />}
      </div>
    </aside>
  );
}

export function Layout({ children }: { children: ReactNode }) {
  const theme = useTheme();
  const status = useBackendStatus();
  const { pathname, search } = useLocation();
  const landing = pathname === "/";
  useEffect(() => {
    const section = pathname === "/" ? new URLSearchParams(search).get("section") : null;
    const target = section ? document.getElementById(section) : null;
    if (target) { target.scrollIntoView({ behavior: "instant" }); target.focus({ preventScroll: true }); }
    else window.scrollTo({ top: 0, behavior: "instant" });
  }, [pathname, search]);
  const skip = <a href={landing ? "#/?section=main" : `#${pathname}`} className="sr-only skip pill" onClick={(event) => {
    event.preventDefault();
    document.getElementById("main")?.focus();
    document.getElementById("main")?.scrollIntoView({ behavior: "instant" });
  }}>Skip to content</a>;
  if (landing) {
    return (
      <BackendStatusContext.Provider value={status}><div className="min-h-screen">
        {skip}
        <LandingHeader themeToggle={<ThemeToggle {...theme} />} />
        <main id="main" tabIndex={-1}>{children}</main>
        <LandingFooter />
      </div></BackendStatusContext.Provider>
    );
  }
  return (
    <BackendStatusContext.Provider value={status}>
      {skip}
      <div className="min-h-screen lg:flex">
        <Sidebar status={status} theme={theme} />
        <main id="main" tabIndex={-1} className="min-w-0 flex-1 px-4 py-8 sm:px-8 lg:px-12 lg:py-10">
          <div className="mx-auto max-w-[1240px]">
            <StatusBanner status={status} />
            {children}
            <footer className="subtle mt-16 flex flex-col gap-2 border-t pt-6 text-[12px] sm:flex-row sm:justify-between" style={{ borderColor: "var(--line)" }}>
              <span>Rules decide, Gemini explains · demo actions run in a sandbox</span>
              <span className="mono">abide · on Google Cloud</span>
            </footer>
          </div>
        </main>
      </div>
    </BackendStatusContext.Provider>
  );
}
