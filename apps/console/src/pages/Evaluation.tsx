import { useState } from "react";
import { Link } from "react-router-dom";
import { delegate, type DArm, type DEvalRun, type DRate } from "../api";
import { ErrorNote, Loading, PageHeader, Stamp, useAsync } from "../components/ui";

/* The evaluation dashboard: the same scenarios through five ways of running agents, counted from what actually ran in
   the sandbox. Every rate has its denominator and a 95% interval; zero failures here is not proof of safety. */

export const ARMS: { id: string; label: string; what: string; ours?: boolean }[] = [
  { id: "none", label: "No protection", what: "Tools run directly" },
  { id: "ask-every-step", label: "Approve every step", what: "A person clicks yes on each action (approves 93%)" },
  { id: "firewall-only", label: "Detection only", what: "The content filter screens every read; no work order, no gate" },
  { id: "gate-only", label: "Abide, filter off", what: "Work order and gate alone" },
  { id: "delegate", label: "Abide", what: "Work order + content filter + gate + bound questions", ours: true },
];

const frac = (r?: DRate | null) => (r && r.n ? `${r.k}/${r.n}` : "–");
const pc = (r?: DRate | null) => (r && r.n && r.rate !== null ? `${Math.round(r.rate * 1000) / 10}%` : "–");
const ci = (r?: DRate | null) => (r?.ci95 ? `95% CI ${Math.round(r.ci95[0] * 100)}–${Math.round(r.ci95[1] * 100)}%` : "");

/** One horizontal bar per arm, with the 95% interval as a whisker. */
function Bars({ d, metric, title, good, sub }: { d: DEvalRun; metric: (a: DArm) => DRate | undefined | null; title: string; good: "low" | "high"; sub: string }) {
  return (
    <section className="card p-5" aria-label={title}>
      <h3 className="h3 m-0">{title}</h3>
      <p className="subtle m-0 mt-1 text-[13px]">{sub}</p>
      <ul className="m-0 mt-4 list-none space-y-3 p-0">
        {ARMS.filter((a) => d.arms[a.id]).map((a) => {
          const r = metric(d.arms[a.id]);
          const v = r?.rate ?? 0;
          const bad = good === "low" ? v > 0 : v < 1;
          const tone = a.ours ? "var(--accent)" : bad ? "var(--block)" : "var(--allow)";
          return (
            <li key={a.id} className="grid grid-cols-[minmax(0,150px)_minmax(0,1fr)_72px] items-center gap-3 max-sm:grid-cols-[minmax(0,110px)_minmax(0,1fr)_60px]">
              <span className={`text-[13px] ${a.ours ? "font-bold" : ""}`} style={a.ours ? { color: "var(--accent)" } : undefined}>{a.label}</span>
              <span className="ev-track" title={ci(r)}>
                <span className="ev-fill" style={{ width: `${Math.max(v * 100, v > 0 ? 1.5 : 0)}%`, background: tone }} />
                {r?.ci95 && <span className="ev-ci" style={{ left: `${r.ci95[0] * 100}%`, width: `${(r.ci95[1] - r.ci95[0]) * 100}%` }} />}
              </span>
              <span className="mono text-right text-[13px]"><b style={{ color: tone }}>{frac(r)}</b></span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function Card({ label, value, detail, tone }: { label: string; value: string; detail: string; tone?: string }) {
  return (
    <div className="card p-4">
      <span className="section-label">{label}</span>
      <b className="mt-1 block text-[30px] leading-tight" style={{ color: tone ?? "var(--text)", fontWeight: 700 }}>{value}</b>
      <span className="subtle block text-[12.5px] leading-snug">{detail}</span>
    </div>
  );
}

function Headline({ d }: { d: DEvalRun }) {
  const f = d.arms["firewall-only"], a = d.arms.delegate;
  if (!a) return null;
  return (
    <section className="ev-head card card-raised overflow-hidden">
      <div className="grid md:grid-cols-2">
        <div className="p-6" style={{ background: "var(--block-wash)" }}>
          <span className="section-label" style={{ color: "var(--block)" }}>Detecting suspicious content</span>
          <p className="m-0 mt-2 text-[42px] font-bold leading-none" style={{ color: "var(--block)" }}>{frac(f?.unauthorised_action_rate)}</p>
          <p className="m-0 mt-2 text-[15px]">attacks still got an unsafe action through, with the content filter screening every read.</p>
        </div>
        <div className="p-6" style={{ background: "var(--accent-wash)" }}>
          <span className="section-label" style={{ color: "var(--accent)" }}>Preventing unauthorised actions</span>
          <p className="m-0 mt-2 text-[42px] font-bold leading-none" style={{ color: "var(--accent)" }}>{frac(a.unauthorised_action_rate)}</p>
          <p className="m-0 mt-2 text-[15px]">with Abide, and <b>{frac(a.legitimate_success)}</b> normal tasks still finished. {frac(a.safe_recovery)} attacked tasks finished their real work safely.</p>
        </div>
      </div>
    </section>
  );
}

export function EvalSummary({ d }: { d: DEvalRun }) {
  const a = d.arms.delegate, n = d.arms.none;
  if (!a) return null;
  const p = a.provenance, amb = a.ambiguous;
  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      <Card label="Unauthorised action rate" value={pc(a.unauthorised_action_rate)} detail={`${frac(a.unauthorised_action_rate)} attacks · ${ci(a.unauthorised_action_rate)}`} tone="var(--accent)" />
      <Card label="Legitimate task success" value={pc(a.legitimate_success)} detail={`${frac(a.legitimate_success)} normal tasks · ${ci(a.legitimate_success)}`} tone="var(--allow)" />
      <Card label="Safe recovery" value={pc(a.safe_recovery)} detail={`${frac(a.safe_recovery)} attacked tasks still did the real work, nothing unsafe`} tone="var(--allow)" />
      <Card label="False blocking" value={pc(a.false_blocking)} detail={`${frac(a.false_blocking)} authorised calls in normal tasks wrongly stopped`} />
      <Card label="Handoff provenance" value={p ? `${p.labels_preserved}/${p.labels_expected}` : "–"} detail={p ? `trust labels preserved and verified over ${p.handoffs} handoffs · ${p.tamper_caught}/${p.tamper_probes} tampering probes caught` : "no handoffs"} tone="var(--accent)" />
      <Card label="Runtime overhead" value={a.overhead_ms ? `+${Math.round(a.overhead_ms.median)} ms` : "–"} detail={a.overhead_ms ? `median per workflow (p95 +${Math.round(a.overhead_ms.p95)} ms) vs ${Math.round(n?.latency_ms.median ?? 0)} ms unprotected; no model calls` : ""} />
      <Card label="Questions per normal task" value={String(a.questions_per_benign_task ?? "–")} detail={`vs ${d.arms["ask-every-step"]?.questions_per_benign_task ?? "–"} when a person approves every step`} />
      <Card label="Ambiguous cases" value={amb ? `${amb.never_silent?.k ?? amb.n - amb.ran_silently}/${amb.n}` : "–"} detail={amb ? `never ran silently · ${amb.asked} asked you, ${amb.blocked} stopped (stricter than the intended “ask”)` : ""} />
    </div>
  );
}

function Table({ d }: { d: DEvalRun }) {
  const rows: [string, (a: DArm) => string][] = [
    ["Intended decision met (all scenarios)", (a) => frac(a.intended_decision_met)],
    ["Unauthorised action rate", (a) => frac(a.unauthorised_action_rate)],
    ["Legitimate task success", (a) => frac(a.legitimate_success)],
    ["Safe recovery", (a) => frac(a.safe_recovery)],
    ["False blocking (calls)", (a) => frac(a.false_blocking)],
    ["Ambiguous: never silent", (a) => frac(a.ambiguous?.never_silent)],
    ["Questions per normal task", (a) => String(a.questions_per_benign_task ?? "–")],
    ["Median / p95 per workflow (ms)", (a) => `${Math.round(a.latency_ms?.median ?? 0)} / ${Math.round(a.latency_ms?.p95 ?? 0)}`],
  ];
  const arms = ARMS.filter((a) => d.arms[a.id]);
  return (
    <section className="card overflow-x-auto p-5" aria-label="All arms">
      <table className="table">
        <thead><tr><th scope="col">Metric</th>{arms.map((a) => <th key={a.id} scope="col" style={a.ours ? { color: "var(--accent)" } : undefined}>{a.label}</th>)}</tr></thead>
        <tbody>{rows.map(([label, f]) => (
          <tr key={label}><th scope="row" className="text-[13px]" style={{ textTransform: "none", fontFamily: "inherit", letterSpacing: 0, color: "var(--text)" }}>{label}</th>
            {arms.map((a) => <td key={a.id} className="mono" style={a.ours ? { color: "var(--accent)", fontWeight: 700 } : undefined}>{f(d.arms[a.id])}</td>)}</tr>
        ))}</tbody>
      </table>
    </section>
  );
}

function ByType({ d }: { d: DEvalRun }) {
  const f = d.arms["firewall-only"]?.by_attack_type ?? {}, a = d.arms.delegate?.by_attack_type ?? {};
  const types = Object.keys(a);
  return (
    <section className="card p-5" aria-label="By attack type">
      <h3 className="h3 m-0">By attack type</h3>
      <p className="subtle m-0 mt-1 text-[13px]">Attacks that got an unsafe action through: detection only vs Abide.</p>
      <div className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-3">
        {types.map((t) => (
          <div key={t} className="rounded-md p-3" style={{ border: "1px solid var(--line)" }}>
            <span className="block text-[13px] font-semibold">{t}</span>
            <span className="mono mt-1 flex items-center gap-3 text-[12px]">
              <span style={{ color: f[t]?.compromised ? "var(--block)" : "var(--subtle)" }}>filter {f[t]?.compromised ?? "–"}/{f[t]?.n ?? "–"}</span>
              <span style={{ color: "var(--accent)", fontWeight: 700 }}>Abide {a[t].compromised}/{a[t].n}</span>
            </span>
          </div>
        ))}
      </div>
    </section>
  );
}

function Gemini({ g }: { g: DEvalRun }) {
  const n = g.arms.none, a = g.arms.delegate;
  return (
    <section className="card p-5" aria-label="Live Gemini run">
      <div className="flex flex-wrap items-center justify-between gap-2"><h3 className="h3 m-0">Live run: real Gemini agents</h3><span className="lg-live"><i />Gemini on Google ADK</span></div>
      <p className="subtle m-0 mt-1 text-[13px]">{g.method} · {g.created}{Object.values(g.errors ?? {}).flat().length ? ` · ${Object.values(g.errors ?? {}).flat().length} run(s) timed out or failed and are left out of the counts` : ""}</p>
      <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Card label="Unprotected: unsafe" value={frac(n?.unauthorised_action_rate)} detail="attacks that got an action through" tone="var(--block)" />
        <Card label="Abide: unsafe" value={frac(a?.unauthorised_action_rate)} detail="attacks that got an action through" tone="var(--accent)" />
        <Card label="Abide: normal tasks" value={frac(a?.legitimate_success)} detail="finished" tone="var(--allow)" />
        <Card label="Abide: median per task" value={a ? `${(a.latency_ms.median / 1000).toFixed(1)} s` : "–"} detail={n ? `vs ${(n.latency_ms.median / 1000).toFixed(1)} s unprotected (model time included)` : ""} />
      </div>
    </section>
  );
}

function Scenarios({ d }: { d: DEvalRun }) {
  const [open, setOpen] = useState(false);
  const count = (e: string) => d.scenarios.filter((s) => s.expect === e).length;
  return (
    <details className="card overflow-hidden" onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}>
      <summary className="flex cursor-pointer flex-wrap items-baseline justify-between gap-2 px-5 py-4">
        <span className="h3">The {d.scenarios.length} scenarios and their intended decisions</span>
        <span className="subtle text-[13px]">{count("block")} must be stopped · {count("allow")} must finish · {count("ask")} ambiguous: must ask · marked before the run</span>
      </summary>
      {open && <div className="overflow-x-auto px-5 pb-5"><table className="table"><thead><tr><th>Scenario</th><th>Business</th><th>Kind</th><th>Where the attack hides</th><th>Intended</th></tr></thead>
        <tbody>{d.scenarios.map((s) => <tr key={s.name}><td className="mono text-[12px]">{s.name}</td><td className="text-[13px]">{s.domain === "desk" ? "Support desk" : "Office"}</td>
          <td className="text-[13px]">{s.kind}</td><td className="text-[13px]">{s.carrier ?? "–"}</td>
          <td>{s.expect === "block" ? <Stamp kind="stopped">Stop</Stamp> : s.expect === "allow" ? <Stamp kind="ran">Finish</Stamp> : <Stamp kind="wait">Ask</Stamp>}</td></tr>)}</tbody></table></div>}
    </details>
  );
}

export function Evaluation() {
  const { data: d, error, loading } = useAsync(() => delegate.evaluation(), []);
  return (
    <div className="space-y-6">
      <PageHeader label="Evaluation" title="Does the work get done, and does anything unsafe run?"
        lede="The same scenarios through five ways of running agents. Outcomes are counted from what actually ran in the sandbox, not from what the gate said." />
      {loading && !d && <Loading>Loading the evaluation…</Loading>}
      {error && <ErrorNote>{error}</ErrorNote>}
      {d && <>
        <p role="note" className="ev-caveat m-0"><b>Read this first.</b> {d.caveat ?? "A small synthetic suite: zero observed failures is not proof of safety."} The scenarios are ours; the main run uses scripted agents that obey every instruction they read (the worst case), so it measures the protection, not the model. {d.gemini ? "A smaller run with real Gemini agents is below." : ""}</p>
        <Headline d={d} />
        <EvalSummary d={d} />
        <div className="grid gap-4 lg:grid-cols-2">
          <Bars d={d} title="Unauthorised action rate" good="low" sub="Attacks that got at least one unsafe action executed (lower is better). Whisker: 95% interval." metric={(a) => a.unauthorised_action_rate} />
          <Bars d={d} title="Legitimate task success" good="high" sub="Normal tasks finished correctly (higher is better)." metric={(a) => a.legitimate_success} />
          <Bars d={d} title="Safe recovery" good="high" sub="Attacked tasks where the real work still finished and nothing unsafe ran." metric={(a) => a.safe_recovery} />
          <ByType d={d} />
        </div>
        <Table d={d} />
        {d.gemini && <Gemini g={d.gemini} />}
        <Scenarios d={d} />
        <p className="subtle m-0 text-[13px]">{d.method} · run {d.created}. Reproduce: <span className="mono">python -m intentseal_eval.delegate</span>. <Link className="text-link" to="/tasks?task=new&preset=attack">Watch one attack stopped live</Link>.</p>
      </>}
    </div>
  );
}

let cached: Promise<import("../api").DEval> | null = null;
/** One line under the Checkpoint: what the evaluation says, with the caveat, and a link to the dashboard. */
export function EvalStrip() {
  const { data: d } = useAsync(() => (cached ??= delegate.evaluation().catch((e) => { cached = null; throw e; })), []);
  const f = d?.arms["firewall-only"], a = d?.arms.delegate;
  if (!d || !a) return null;
  return (
    <p className="cp-eval m-0" aria-label="Evaluation summary">
      <span><span className="section-label mr-2">Across {d.scenarios.length} test scenarios</span>
        detection alone let <b style={{ color: "var(--block)" }}>{frac(f?.unauthorised_action_rate)}</b> attacks through · Abide <b style={{ color: "var(--accent)" }}>{frac(a.unauthorised_action_rate)}</b> · normal tasks finished <b>{frac(a.legitimate_success)}</b>
        <span className="subtle"> · a small synthetic suite, not proof of safety</span></span>
      <Link className="text-link" to="/evaluation">See the evaluation →</Link>
    </p>
  );
}
