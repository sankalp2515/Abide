import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { api } from "../api";
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
    <div role="status" className="ab-banner-top" style={{ borderColor: `color-mix(in srgb, ${tone} 45%, transparent)`, color: tone }}>
      {waking
        ? "The service is waking up (it sleeps when nobody is using it). This can take 2–3 minutes."
        : "The service is unavailable right now. Live runs are paused; try again in a few minutes."}
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
    <button className="pill pill-sm" style={{ minHeight: 40, width: 40, padding: 0 }} onClick={toggle}
      aria-label={dark ? "Switch to light theme" : "Switch to dark theme"} title={dark ? "Light theme" : "Dark theme"}>
      <svg viewBox="0 0 20 20" aria-hidden>
        {dark ? <><circle cx="10" cy="10" r="3.5" /><path d="M10 1.5v2M10 16.5v2M1.5 10h2M16.5 10h2M4 4l1.4 1.4M14.6 14.6 16 16M4 16l1.4-1.4M14.6 5.4 16 4" /></>
              : <path d="M16 12.5A7 7 0 0 1 7.5 4a7 7 0 1 0 8.5 8.5Z" />}
      </svg>
    </button>
  );
}

export function Layout({ children }: { children: ReactNode }) {
  const theme = useTheme();
  const status = useBackendStatus();
  return (
    <BackendStatusContext.Provider value={status}><div className="min-h-screen">
      <a href="#/?section=try" className="sr-only skip pill" onClick={(event) => {
        event.preventDefault();
        document.getElementById("try")?.focus();
        document.getElementById("try")?.scrollIntoView({ behavior: "instant" });
      }}>Skip to the workspace</a>
      <LandingHeader themeToggle={<ThemeToggle {...theme} />} status={<span className="ab-status" aria-live="polite">
        <span aria-hidden style={{ background: status === "ok" ? "var(--allow)" : status === "checking" ? "var(--subtle)" : "var(--block)" }} />
        {status === "ok" ? "Online" : status === "checking" ? "Connecting" : status === "waking" ? "Waking up" : "Offline"}
      </span>} />
      <StatusBanner status={status} />
      <main id="main" tabIndex={-1} className="landing-main">{children}</main>
      <LandingFooter />
    </div></BackendStatusContext.Provider>
  );
}
