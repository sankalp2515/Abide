import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import type { Decision } from "../api";

/* ---- brand ---------------------------------------------------------------------------------- */
export function SealMark({ size = 26 }: { size?: number }) {
  return (
    <svg width={size} height={size * 1.07} viewBox="0 0 30 32" aria-hidden fill="none" stroke="currentColor" strokeWidth={1.7}>
      <path d="m15 2 11 4v10c0 6-5 11-11 14C9 27 4 22 4 16V6z" />
      <path d="m10 15 4 4 7-8" />
    </svg>
  );
}

export function Arrow() {
  return <svg viewBox="0 0 20 20" aria-hidden><path d="M3 10h13M11 5l5 5-5 5" /></svg>;
}

/* ---- decisions ------------------------------------------------------------------------------ */
const DECISION_COLOR: Record<Decision, string> = {
  ALLOW: "var(--allow)", NEUTRALISE: "var(--neutralise)", BLOCK: "var(--block)", ESCALATE: "var(--escalate)",
};
export const DECISION_TEXT: Record<Decision, string> = {
  ALLOW: "Allowed", NEUTRALISE: "Neutralised", BLOCK: "Blocked", ESCALATE: "Needs approval",
};

export function DecisionBadge({ decision }: { decision: Decision | null | undefined }) {
  if (!decision) return null;
  const c = DECISION_COLOR[decision];
  return (
    <span className="tag" style={{ color: c, borderColor: `color-mix(in srgb, ${c} 45%, transparent)` }}>
      <span aria-hidden className="inline-block h-1.5 w-1.5 rounded-full" style={{ background: c }} />
      {DECISION_TEXT[decision]}
    </span>
  );
}

export function Dot({ tone }: { tone: "allow" | "block" | "neutralise" | "escalate" | "accent" }) {
  const c = tone === "accent" ? "var(--accent-soft)" : `var(--${tone})`;
  return <span aria-hidden className="inline-block h-1.5 w-1.5 shrink-0 rounded-full" style={{ background: c }} />;
}

export function Tag({ children, tone }: { children: ReactNode; tone?: string }) {
  return <span className="tag" style={tone ? { color: tone, borderColor: `color-mix(in srgb, ${tone} 45%, transparent)` } : undefined}>{children}</span>;
}

/* ---- layout --------------------------------------------------------------------------------- */
export function PageHeader({ index, label, title, lede, actions }: {
  index?: string; label: string; title: ReactNode; lede?: ReactNode; actions?: ReactNode;
}) {
  return (
    <header className="flex flex-col gap-6 md:flex-row md:items-end md:justify-between">
      <div>
        <p className="section-label mb-4">{index ? `${index} / ` : ""}{label}</p>
        <h1 className="h2">{title}</h1>
      </div>
      {(lede || actions) && (
        <div className="flex max-w-md flex-col gap-4 md:items-end md:text-right">
          {lede && <p className="lede m-0 text-[13px]">{lede}</p>}
          {actions}
        </div>
      )}
    </header>
  );
}

export function Card({ children, className = "", as: As = "section" }: { children: ReactNode; className?: string; as?: "section" | "article" | "div" }) {
  return <As className={`card p-5 sm:p-7 ${className}`}>{children}</As>;
}

export function CardTitle({ children, meta }: { children: ReactNode; meta?: ReactNode }) {
  return (
    <div className="mb-5 flex flex-wrap items-baseline justify-between gap-3">
      <h2 className="h3">{children}</h2>
      {meta && <div className="subtle text-xs">{meta}</div>}
    </div>
  );
}

export function Stat({ value, label, tone }: { value: ReactNode; label: ReactNode; tone?: string }) {
  return (
    <div>
      <b className="num block text-[30px] font-normal tracking-tight" style={{ color: tone ?? "var(--accent-soft)" }}>{value}</b>
      <small className="subtle mt-1 block text-[11px] leading-snug">{label}</small>
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="subtle rounded-2xl border border-dashed p-5 text-sm hairline">{children}</p>;
}

export function ErrorNote({ children }: { children: ReactNode }) {
  return <p role="alert" className="text-sm" style={{ color: "var(--block)" }}>{children}</p>;
}

export function Loading({ children = "Loading…" }: { children?: ReactNode }) {
  return <p className="subtle text-sm" aria-live="polite">{children}</p>;
}

export function Bar({ value, tone = "var(--accent-soft)" }: { value: number | null | undefined; tone?: string }) {
  const w = value ? Math.max(2, Math.round(value * 100)) : 0;
  return <div className="bar w-full" aria-hidden><span style={{ width: `${w}%`, background: tone }} /></div>;
}

/* ---- format --------------------------------------------------------------------------------- */
export function pct(v: number | null | undefined, digits = 1): string {
  return v === null || v === undefined ? "—" : `${(v * 100).toFixed(digits)}%`;
}

export function ago(ts: number): string {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(ts * 1000).toLocaleDateString();
}

/* ---- data ----------------------------------------------------------------------------------- */
export function useAsync<T>(fn: () => Promise<T>, deps: unknown[] = []) {
  const request = useRef(0);
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const run = useCallback(fn, deps);
  const reload = useCallback(async () => {
    const id = ++request.current;
    setLoading(true);
    setError(null);
    try {
      const result = await run();
      if (id === request.current) { setData(result); setError(null); }
    } catch (e) {
      if (id === request.current) setError((e as Error).message);
    } finally {
      if (id === request.current) setLoading(false);
    }
  }, [run]);
  useEffect(() => {
    reload();
    return () => { request.current += 1; };
  }, [reload]);
  return { data, error, loading, reload };
}
