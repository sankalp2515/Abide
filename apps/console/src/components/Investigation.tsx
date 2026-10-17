import { useState } from "react";
import { admin, api, type Investigation, type InvestigationStep, type Suggestion } from "../api";
import { useReviewer } from "../reviewer";
import { ago, Card, CardTitle, Empty, ErrorNote, Loading, Stat, Tag, useAsync } from "./ui";

const SEVERITY_TONE: Record<string, string> = {
  none: "var(--allow)", low: "var(--allow)", medium: "var(--neutralise)", high: "var(--block)", critical: "var(--block)",
};
const VERDICT: Record<string, string> = {
  attack: "Attack", likely_attack: "Likely attack", false_alarm: "False alarm", inconclusive: "Inconclusive",
};

function stepLine(s: InvestigationStep): string {
  if (s.step === "tool") {
    const args = Object.entries(s.args ?? {}).map(([k, v]) => `${k}=${v}`).join(", ");
    return `${s.tool}(${args})${s.ok === false ? " failed, the agent continued" : ""}`;
  }
  if (s.step === "failover") return `Model failover: ${s.detail}`;
  if (s.step === "error") return s.detail ?? "error";
  return s.detail ?? "";
}

function Proposal({ s, onDone }: { s: Suggestion; onDone: () => void }) {
  const reviewer = useReviewer();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const what = s.kind === "memory"
    ? `Remember event ${s.payload.event_id} as ${s.payload.verdict === "attack" ? "a confirmed attack" : "a false alarm"}`
    : s.payload.change;
  async function resolve(apply: boolean) {
    setBusy(true);
    setErr(null);
    try {
      await admin.resolveSuggestion(reviewer!.key, s.id, apply);
      onDone();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <li className="border-b py-3 hairline">
      <p className="m-0 text-[13px]"><Tag>{s.kind === "memory" ? "Memory" : "Policy"}</Tag> <span className="ml-1">{what}</span></p>
      {s.payload.why && <p className="subtle m-0 mt-1 text-xs">Why: {s.payload.why}</p>}
      {s.status !== "pending" ? <p className="subtle m-0 mt-1 text-xs">{s.status}{s.resolved_by ? ` by ${s.resolved_by}` : ""}</p>
        : reviewer ? (
          <div className="mt-2 flex flex-wrap gap-2">
            <button className="pill pill-primary pill-sm" disabled={busy} onClick={() => resolve(true)}>{s.kind === "memory" ? "Apply" : "Accept"}</button>
            <button className="pill pill-sm" disabled={busy} onClick={() => resolve(false)}>Dismiss</button>
          </div>
        ) : <p className="subtle m-0 mt-1 text-xs">Waiting for a reviewer.</p>}
      {err && <ErrorNote>{err}</ErrorNote>}
    </li>
  );
}

export function InvestigationReportView({ inv, onChange }: { inv: Investigation; onChange: () => void }) {
  const r = inv.report;
  if (!r) return <Loading>Investigating…</Loading>;
  const tone = SEVERITY_TONE[r.severity] ?? "var(--muted)";
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-2">
        <Tag tone={tone}>{VERDICT[r.verdict] ?? r.verdict}</Tag>
        <Tag tone={tone}>Severity: {r.severity}</Tag>
        {typeof r.confidence === "number" && <Tag>Confidence {Math.round(r.confidence * 100)}%</Tag>}
        {inv.status === "fallback" && <Tag tone="var(--neutralise)">Built from the log (AI unavailable)</Tag>}
        <span className="subtle ml-auto text-xs">{inv.trigger} · {ago(inv.created_at)}{inv.model ? ` · ${inv.model}` : ""}</span>
      </div>
      <p className="m-0 text-[15px] leading-7">{r.summary}</p>
      <div className="grid gap-5 md:grid-cols-2">
        <div className="space-y-3 text-[13px]">
          {r.entry_point && <p className="m-0"><span className="section-label block pb-1">Entry point</span>{r.entry_point}</p>}
          <p className="m-0"><span className="section-label block pb-1">What the attacker wanted</span>{r.objective}</p>
          <p className="m-0"><span className="section-label block pb-1">Outcome</span>{r.outcome}</p>
          {r.approval_recommendation && <p className="m-0"><span className="section-label block pb-1">Recommendation for the pending approval</span>{r.approval_recommendation}</p>}
          {(r.attack_types?.length ?? 0) > 0 && <div className="flex flex-wrap gap-1.5">{r.attack_types!.map((l) => <Tag key={l}>{l}</Tag>)}</div>}
        </div>
        <div className="grid grid-cols-2 gap-4 self-start">
          <Stat value={r.facts.flagged} label="Events flagged" tone={r.facts.flagged ? "var(--block)" : undefined} />
          <Stat value={r.facts.actions_stopped} label="Actions stopped" />
          <Stat value={r.facts.actions_run} label="Actions that ran" />
          <Stat value={r.facts.max_session_risk.toFixed(2)} label="Peak session risk" />
        </div>
      </div>
      {(r.next_steps?.length ?? 0) > 0 && (
        <div><span className="section-label block pb-2">Next steps</span>
          <ul className="m-0 pl-5 text-[13px] leading-6">{r.next_steps!.map((s, i) => <li key={i}>{s}</li>)}</ul></div>
      )}
      {inv.suggestions.length > 0 && (
        <div><span className="section-label block pb-2">Proposals (a reviewer decides)</span>
          <ul className="m-0 list-none border-t p-0 hairline">{inv.suggestions.map((s) => <Proposal key={s.id} s={s} onDone={onChange} />)}</ul></div>
      )}
      <details>
        <summary className="text-link cursor-pointer text-xs">How it investigated ({inv.steps.length} steps)</summary>
        <ol className="mono mt-3 space-y-1.5 pl-5 text-[12px] leading-5">
          {inv.steps.map((s, i) => <li key={i} style={s.step === "tool" && s.ok === false ? { color: "var(--neutralise)" } : undefined}>{stepLine(s)}</li>)}
        </ol>
      </details>
    </div>
  );
}

/** The Investigator for one session: its latest report, and a button to run it (read-only; proposals wait for a reviewer). */
export function InvestigationPanel({ sessionId }: { sessionId: string }) {
  const { data, error, loading, reload } = useAsync(() => api.investigations(sessionId), [sessionId]);
  const [running, setRunning] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  async function run() {
    setRunning(true);
    setErr(null);
    try {
      await api.investigate(sessionId);
      reload();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setRunning(false);
    }
  }
  const latest = data?.[0];
  return (
    <Card className="investigator-panel">
      <CardTitle meta="Read-only analysis. Recommendations remain subject to human review.">
        Investigator
      </CardTitle>
      <div className="mb-5 flex flex-wrap items-center gap-3">
        <button className="pill pill-primary pill-sm" disabled={running} onClick={run}>{running ? "Investigating… (up to a minute)" : latest ? "Investigate again" : "Investigate this session"}</button>
        <span className="subtle text-xs">Reads the log, evidence and policy with read-only tools. Any proposal it makes waits for a reviewer.</span>
      </div>
      {loading && <Loading />}
      {(error || err) && <ErrorNote>{error ?? err}</ErrorNote>}
      {running && <Loading>Reading session evidence and checking policy…</Loading>}
      {data && !latest && !running && <Empty>No report for this session yet. Select Investigate this session to analyse its evidence.</Empty>}
      {latest && <InvestigationReportView inv={latest} onChange={reload} />}
    </Card>
  );
}
