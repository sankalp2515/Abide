import { useEffect, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { delegate, PRODUCT_NAME, type DArm, type DEval } from "../api";
import { useBackendAvailability } from "../components/Layout";
import { SectionLink } from "../components/LandingNav";
import { Arrow, useAsync } from "../components/ui";

function DemoCTA({ children = "Delegate a task" }: { children?: ReactNode }) {
  return <Link to="/delegate" className="pill pill-primary">{children}<Arrow /></Link>;
}

function Section({ id, title, label, children }: { id: string; title: ReactNode; label: string; children: ReactNode }) {
  return <section id={id} tabIndex={-1} className="landing-section" aria-labelledby={`${id}-title`}>
    <div className="landing-section-heading"><p className="landing-label">{label}</p><h2 id={`${id}-title`}>{title}</h2></div>{children}
  </section>;
}

const count = (a: DArm | undefined, rate: number | null | undefined, n: number | undefined) =>
  a && rate != null && n ? `${Math.round(rate * n)} of ${n}` : "—";

/** Every number on this page comes from the published evaluation report (GET /v1/delegate-eval). */
function Hero({ d, waking, error }: { d: DEval | null; waking: boolean; error: string | null }) {
  const on = d?.arms.delegate, off = d?.arms.none, ask = d?.arms["ask-every-step"];
  const metrics = [
    { value: count(on, on?.attack_success_rate, on?.attack_scenarios),
      label: `Attacks that got an action through (${count(off, off?.attack_success_rate, off?.attack_scenarios)} without ${PRODUCT_NAME})` },
    { value: count(on, on?.benign_task_completed, on?.benign_scenarios), label: "Normal tasks finished" },
    { value: on?.questions_per_benign_task != null ? on.questions_per_benign_task.toFixed(1) : "—",
      label: `Questions per task${ask?.questions_per_benign_task != null ? ` (${ask.questions_per_benign_task.toFixed(1)} when asking before every step)` : ""}` },
  ];
  return <section className="hero canvas dark landing-hero" aria-labelledby="hero-title">
    <svg className="circuit" viewBox="0 0 1400 800" preserveAspectRatio="none" aria-hidden><path d="M0 185H235L290 130H730L775 175H1400M0 207H285L340 152H680L723 196H1400M0 600H205L260 545H590M0 628H225L280 573H635M890 630H1090L1160 560H1400" /></svg>
    <div className="landing-hero-copy">
      <span className="eyebrow"><i />Safe delegation for AI assistants at work</span>
      <h1 id="hero-title" className="display">Hand off the task. <span>Approve it once.</span></h1>
      <p className="lede">Your request becomes the permission. {PRODUCT_NAME} turns what you ask into a short list your AI assistants may do, stops anything outside it, even when a document tells them to, and gives you a receipt of what happened.</p>
      <div className="landing-actions"><DemoCTA /><SectionLink id="how" className="text-link">How it works</SectionLink></div>
      <p className="landing-microcopy">Try it with a sandbox office and a sandbox support desk. Nothing real is sent.</p>
      <div className="landing-hero-metrics" aria-label="Measured evaluation results">{metrics.map((m) => <div key={m.label}><b className="num">{m.value}</b><small>{m.label}</small></div>)}</div>
      <div className="landing-provenance" aria-live="polite">{waking ? "The service is waking up. This can take 2–3 minutes; figures refresh automatically when it is online."
        : d ? <>Local evaluation · worst-case scripted agents · {d.created.slice(0, 10)} · <SectionLink id="evidence" className="text-link">Method and limits</SectionLink></>
        : error ? "The evaluation service is unavailable right now." : "Loading the evaluation…"}</div>
    </div>
    <picture><source media="(max-width: 860px)" srcSet={`${import.meta.env.BASE_URL}intentseal-gate-900.webp`} /><img className="hero-art" src={`${import.meta.env.BASE_URL}intentseal-gate.webp`} alt="A dimensional security gate of frosted lavender tiles, with light passing through its protected centre." fetchPriority="high" /></picture>
    <span className="floating-label landing-hero-label">Your words in. Only those actions out.</span>
    <div className="landing-hero-bottom"><span>Gemini drafts. Your words decide.</span><span>Ask → Confirm once → Prove</span></div>
  </section>;
}

const STEPS = [
  { label: "Ask", title: "Say what you want, in your words.", copy: "“Read the paint supplier’s quote and write a reply. Don’t send it yet.” Gemini drafts a short card of what your assistants may and may not do; every line quotes your words, and a check refuses anything your words do not support.", benefit: "One readable card instead of a permissions screen." },
  { label: "Confirm once", title: "Your assistants work inside it.", copy: "Every tool call by every assistant is checked against the card: who receives it, which file or order, how much. Anything outside it stops, and if it came from a document, it stops without asking you.", benefit: "No approval pop-ups for every step." },
  { label: "Prove", title: "Get a receipt you can check.", copy: "Each step, stop and approval is a numbered, chained receipt. The report you read cites them; a sentence the receipts do not support is dropped.", benefit: "Evidence, not the assistant’s own account." },
];

function How() {
  return <Section id="how" label="How it works" title={<>Your request is the permission.<br />Nothing a document says can widen it.</>}>
    <div className="landing-grid-three">{STEPS.map((s) => <article key={s.label} className="landing-feature"><p className="landing-label">{s.label}</p><h3>{s.title}</h3><p>{s.copy}</p><div className="landing-benefit">{s.benefit}</div></article>)}</div>
  </Section>;
}

const ARMS: [string, string][] = [["none", "No protection"], ["ask-every-step", "Ask before every step"], ["delegate", `${PRODUCT_NAME} Delegate`], ["gate-only", "Delegate, content filter off"]];

function Evidence({ d, error, waking }: { d: DEval | null; error: string | null; waking: boolean }) {
  return <Section id="evidence" label="Evidence and limits" title={<>Measured, with the method<br />next to the number.</>}>
    <p className="landing-intro">Two businesses (an office and a support desk), 28 attacks across all nine attack types and 26 normal tasks. Outcomes come from the sandbox’s execution log, not from what the assistants say.</p>
    <div className="landing-evidence-panel">
      {waking ? <p role="status" className="landing-warning">The service is waking up. Results refresh automatically when it is online.</p>
        : error && <p role="status" className="landing-warning">The evaluation service is unavailable right now.</p>}
      <div className="overflow-x-auto"><table className="w-full text-left text-sm">
        <thead><tr className="subtle text-xs"><th className="py-2 pr-4">Setup</th><th className="py-2 pr-4">Attacks that got an action through</th><th className="py-2 pr-4">Normal tasks finished</th><th className="py-2">Questions per task</th></tr></thead>
        <tbody>{ARMS.map(([id, name]) => { const a = d?.arms[id]; return <tr key={id} className="border-t hairline">
          <td className="py-2 pr-4">{name}</td>
          <td className="num py-2 pr-4">{count(a, a?.attack_success_rate, a?.attack_scenarios)}</td>
          <td className="num py-2 pr-4">{count(a, a?.benign_task_completed, a?.benign_scenarios)}</td>
          <td className="num py-2">{a?.questions_per_benign_task != null ? a.questions_per_benign_task.toFixed(1) : "—"}</td></tr>; })}</tbody>
      </table></div>
      <p className="landing-warning">Limits: the agents in this evaluation are scripted to obey every instruction they read (a worst case); the scenarios are ours, in two sandboxed businesses; the confidence intervals are wide. It demonstrates the mechanism; it is not a benchmark or a guarantee.</p>
    </div>
  </Section>;
}

function Integrate() {
  return <Section id="integrate" label="Integration" title={<>Put it in front of<br />the agents you already run.</>}>
    <div className="landing-grid-three">
      <article className="landing-card"><p className="landing-label">Google ADK</p><h3>Two callbacks</h3><p>Check every tool call before it runs; filter what reading tools return.</p><code className="landing-code">before_tool_callback=guard.before_tool<br />after_tool_callback=guard.after_tool</code></article>
      <article className="landing-card"><p className="landing-label">Any framework</p><h3>REST API</h3><p>Start a delegation from the employee’s words, then check each call and report what ran.</p><code className="landing-code">POST /v1/delegations<br />POST /v1/delegations/&#123;id&#125;/check</code></article>
      <article className="landing-card"><p className="landing-label">Per business</p><h3>One policy file</h3><p>Tools, contacts, records, business words and company rules. The office and the support desk differ only here.</p><code className="landing-code">policies/delegate/desk.yaml</code></article>
    </div>
  </Section>;
}

const FAQS = [
  ["Is this an AI assistant?", `No. ${PRODUCT_NAME} sits between a company's AI assistants and the tools they use. The assistants in the demo are stand-ins.`],
  ["Can a document give an assistant new permissions?", "No. Only the words of the person who delegated the task change the card. Text an assistant reads, or text you paste into your request, is information, never permission."],
  ["What if Gemini is unavailable?", "Word rules draft the card instead, and anything they cannot read is left off. If nothing can be understood, the card says so and cannot be confirmed."],
  ["Does it catch every attack?", "No system does. Two layers work together: a content filter removes instructions hidden in documents, and the card stops actions that were never asked for, even when the filter misses."],
  ["Who can see a delegation?", "The person who started it, and administrators. Agents can check actions but cannot confirm, change or approve the task."],
];

export function Overview() {
  const status = useBackendAvailability();
  const waking = status === "checking" || status === "waking";
  const d = useAsync(() => delegate.evaluation(), []);
  const { reload, error } = d;
  // Cloud Run scales to zero: refetch once each time the backend comes back, and retry while it is failing.
  useEffect(() => { if (status === "ok") reload(); }, [status, reload]);
  useEffect(() => {
    if (!error) return;
    const timer = setInterval(() => { if (!document.hidden) reload(); }, 15000);
    return () => clearInterval(timer);
  }, [error, reload]);
  return <div className="landing-page">
    <Hero d={d.data} waking={waking} error={d.error} />
    <div className="landing-content">
      <How />
      <Evidence d={d.data} error={d.error} waking={waking} />
      <Integrate />
      <Section id="faq" label="Before you decide" title="The questions worth asking."><div className="landing-faq">{FAQS.map(([q, a]) => <details key={q}><summary>{q}<span aria-hidden>+</span></summary><p>{a}</p></details>)}</div></Section>
      <section className="landing-closing" aria-labelledby="closing-title"><p className="landing-label">Ask. Confirm once. Prove.</p><h2 id="closing-title">Delegate your first task.</h2><p>Pick the office or the support desk, read the card, and watch what your assistants do.</p><div className="landing-actions"><DemoCTA /></div><p className="landing-microcopy">Sandbox data. Nothing real is sent.</p></section>
    </div>
  </div>;
}
