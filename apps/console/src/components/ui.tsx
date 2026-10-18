import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import type { Decision } from "../api";

/* ---- brand ---------------------------------------------------------------------------------- */
/** Abide's mark: a slightly turned stamp frame holding a tick (your words, approved once). */
export function SealMark({ size = 26 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 34 34" aria-hidden fill="none" stroke="currentColor" strokeWidth={2.6}>
      <rect x="3" y="3" width="28" height="28" rx="3" transform="rotate(-4 17 17)" />
      <path d="M10.5 17.5l4.5 4.5 8.5-9" strokeLinecap="square" />
    </svg>
  );
}

export function Wordmark() {
  return <span className="flex items-center gap-2.5" style={{ color: "var(--accent)" }}><SealMark size={24} />
    <span style={{ color: "var(--text)", fontWeight: 700, fontSize: 21, letterSpacing: "-0.02em" }}>abide</span>
    <span className="wm-sub">AI agent governance &amp; security</span></span>;
}

/* ---- stamps: the product's decisions, readable without colour ------------------------------------- */
export type StampKind = "ran" | "stopped" | "wait" | "quiet";
const STAMP_ICON: Record<StampKind, ReactNode> = {
  ran: <path d="M2 7.5l3 3L12 3.5" />,
  stopped: <path d="M3 3l8 8M11 3l-8 8" />,
  wait: <><circle cx="7" cy="7" r="5.2" /><path d="M7 4.2V7.4l2 1.4" /></>,
  quiet: <path d="M3 7h8" />,
};
export function Stamp({ kind, children, live = false }: { kind: StampKind; children: ReactNode; live?: boolean }) {
  return <span className={`stamp stamp-${kind}${live ? " press" : ""}`}><svg viewBox="0 0 14 14" aria-hidden>{STAMP_ICON[kind]}</svg>{children}</span>;
}

/** The Gemini spark, for what Gemini drafted or wrote. */
export function Spark({ size = 14 }: { size?: number }) {
  return <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden><path fill="currentColor"
    d="M12 2c.6 4.9 4.1 8.4 9 9-4.9.6-8.4 4.1-9 9-.6-4.9-4.1-8.4-9-9 4.9-.6 8.4-4.1 9-9z" /></svg>;
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
    <header className="flex flex-col gap-4 border-b pb-6 md:flex-row md:items-end md:justify-between" style={{ borderColor: "var(--rule)" }}>
      <div>
        <p className="section-label mb-3" data-index={index}>{label}</p>
        <h1 className="h2">{title}</h1>
      </div>
      {(lede || actions) && (
        <div className="flex max-w-md flex-col gap-4 md:items-end md:text-right">
          {lede && <p className="lede m-0 text-[15px]">{lede}</p>}
          {actions}
        </div>
      )}
    </header>
  );
}

export function Card({ children, className = "", as: As = "section" }: { children: ReactNode; className?: string; as?: "section" | "article" | "div" }) {
  return <As className={`card p-5 sm:p-6 ${className}`}>{children}</As>;
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
      <b className="num block text-[34px] font-semibold tracking-tight" style={{ color: tone ?? "var(--text)", letterSpacing: "-0.03em" }}>{value}</b>
      <small className="subtle mt-1 block text-[13px] leading-snug">{label}</small>
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="subtle rounded-md border border-dashed p-5 text-sm" style={{ borderColor: "var(--line-strong)" }}>{children}</p>;
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
