import { useEffect, type ReactNode } from "react";
import { delegate, PRODUCT_NAME, type DArm, type DEval } from "../api";
import { useBackendAvailability } from "../components/Layout";
import { SectionLink } from "../components/LandingNav";
import { Arrow, useAsync } from "../components/ui";

/* The whole product on one page: what it is, the live workspace, the evidence, how to bring your own agent. */

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
      label: `attacks got an action through (${count(off, off?.attack_success_rate, off?.attack_scenarios)} without ${PRODUCT_NAME})` },
    { value: count(on, on?.benign_task_completed, on?.benign_scenarios), label: "normal tasks finished" },
    { value: on?.questions_per_benign_task != null ? on.questions_per_benign_task.toFixed(1) : "—",
      label: `questions per task${ask?.questions_per_benign_task != null ? ` (${ask.questions_per_benign_task.toFixed(1)} when asking before every step)` : ""}` },
  ];
  return <section className="hero canvas dark landing-hero ab-hero" aria-labelledby="hero-title">
    <svg className="circuit" viewBox="0 0 1400 800" preserveAspectRatio="none" aria-hidden><path d="M0 185H235L290 130H730L775 175H1400M0 207H285L340 152H680L723 196H1400M0 600H205L260 545H590M0 628H225L280 573H635M890 630H1090L1160 560H1400" /></svg>
    <div className="landing-hero-copy">
      <span className="eyebrow"><i />Safe delegation for AI agents at work</span>
      <h1 id="hero-title" className="display">Hand off the task. <span>Approve it once.</span></h1>
      <p className="lede">{PRODUCT_NAME} turns your words into the only permissions your AI agents get. Anything else they try is stopped, even when a document tells them to, and you get a receipt of what happened.</p>
      <div className="landing-actions"><SectionLink id="try" className="pill pill-primary ab-glow">Try it now<Arrow /></SectionLink><SectionLink id="how" className="text-link">How it works</SectionLink></div>
      <p className="landing-microcopy">No sign-up. A sandbox office and a sandbox support desk, with real Gemini agents. Nothing real is sent.</p>
      <div className="landing-hero-metrics" aria-label="Measured evaluation results">{metrics.map((m) => <div key={m.label}><b className="num">{m.value}</b><small>{m.label}</small></div>)}</div>
      <div className="landing-provenance" aria-live="polite">{waking ? "The service is waking up. This can take 2–3 minutes; figures refresh automatically when it is online."
        : d ? <>Local evaluation · worst-case agents · {d.created.slice(0, 10)} · <SectionLink id="evidence" className="text-link">Method and limits</SectionLink></>
        : error ? "The evaluation service is unavailable right now." : "Loading the evaluation…"}</div>
    </div>
    <picture><source media="(max-width: 860px)" srcSet={`${import.meta.env.BASE_URL}abide-gate-900.webp`} /><img className="hero-art" src={`${import.meta.env.BASE_URL}abide-gate.webp`} alt="A gate of frosted lavender tiles with light passing through its open centre." fetchPriority="high" /></picture>
    <span className="floating-label landing-hero-label">Your words in. Only those actions out.</span>
  </section>;
}

const STEPS = [
  { label: "1 · Ask", title: "Say what you want, in your words.", copy: "Gemini turns your request into a short permission card. Every line quotes your words; a check refuses anything your words do not support." },
  { label: "2 · Confirm once", title: "Your agents work inside the card.", copy: "Every tool call by every agent is checked: who receives it, which file or order, how much. Anything outside the card stops. If a document asked for it, nobody is even interrupted." },
  { label: "3 · Prove", title: "Get a receipt you can check.", copy: "Each step, stop and approval is a numbered, chained receipt. The summary you read cites them, and a sentence they do not support is dropped." },
];

const ARMS: [string, string][] = [["none", "No protection"], ["ask-every-step", "Ask before every step"], ["delegate", PRODUCT_NAME], ["gate-only", `${PRODUCT_NAME}, content filter off`]];

function Evidence({ d, error, waking }: { d: DEval | null; error: string | null; waking: boolean }) {
  return <Section id="evidence" label="Evidence and limits" title={<>Measured, with the method<br />next to the number.</>}>
    <p className="landing-intro">Two businesses, 28 attacks across nine attack types and 26 normal tasks. Outcomes come from the sandbox's execution log, not from what the agents say.</p>
    <div className="landing-evidence-panel">
      {waking ? <p role="status" className="landing-warning">The service is waking up. Results refresh automatically when it is online.</p>
        : error && <p role="status" className="landing-warning">The evaluation service is unavailable right now.</p>}
      <div className="overflow-x-auto"><table className="table">
        <thead><tr><th>Setup</th><th>Attacks that got an action through</th><th>Normal tasks finished</th><th>Questions per task</th></tr></thead>
        <tbody>{ARMS.map(([id, name]) => { const a = d?.arms[id]; return <tr key={id} className={id === "delegate" ? "ab-row-us" : ""}>
          <td>{name}</td>
          <td className="num">{count(a, a?.attack_success_rate, a?.attack_scenarios)}</td>
          <td className="num">{count(a, a?.benign_task_completed, a?.benign_scenarios)}</td>
          <td className="num">{a?.questions_per_benign_task != null ? a.questions_per_benign_task.toFixed(1) : "—"}</td></tr>; })}</tbody>
      </table></div>
      <p className="landing-microcopy">The agents in this table obey every instruction they read (a worst case); the scenarios are ours and the confidence intervals are wide. In live runs, real Gemini agents told to obey what they read tried 9 unauthorised actions and every one was stopped. It shows the mechanism; it is not a benchmark or a guarantee.</p>
    </div>
  </Section>;
}

function Integrate() {
  return <Section id="integrate" label="Bring your own" title={<>Your agent, your business,<br />in about two minutes.</>}>
    <div className="landing-grid-three">
      <article className="landing-card"><p className="landing-label">1 · Get a key</p><h3>No sign-up</h3><p>A private 7-day key in one call. Your delegations are visible only to you.</p><code className="landing-code">POST /v1/trial-keys</code></article>
      <article className="landing-card"><p className="landing-label">2 · Describe your business</p><h3>One policy</h3><p>Your tools and what each does, your people and records, your company rules.</p><code className="landing-code">POST /v1/delegate-workspaces</code></article>
      <article className="landing-card"><p className="landing-label">3 · Put your agent under the card</p><h3>Two ADK callbacks</h3><p>Every tool call is checked before it runs, and what reading tools return is inspected.</p><code className="landing-code">before_tool_callback=guard.before_tool<br />after_tool_callback=guard.after_tool</code></article>
    </div>
    <p className="landing-microcopy">The Colab quickstart does all three with a travel agency and a real Gemini agent. Any framework can use the REST API instead.</p>
  </Section>;
}

const FAQS = [
  ["Is this an AI assistant?", `No. ${PRODUCT_NAME} sits between a company's AI agents and the tools they use. It decides which of their actions run.`],
  ["Can a document give an agent new permissions?", "No. Only the words of the person who delegated the task change the card. Text an agent reads, or text you paste in, is information, never permission."],
  ["What if Gemini is unavailable?", "Word rules draft the card instead, and anything they cannot read is left off. If nothing can be understood, the card says so and cannot be confirmed."],
  ["Does it catch every attack?", "No system does. Layers work together: a content filter and Google Model Armor remove instructions hidden in documents, and the card stops actions that were never asked for, even when both filters miss."],
  ["Who can see a delegation?", "The person who started it, and administrators. Agents can check actions but cannot confirm, change or approve the task."],
];

export function Overview({ workspace }: { workspace?: ReactNode } = {}) {
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
      <section id="try" tabIndex={-1} className="ab-try" aria-labelledby="try-title">
        <div className="landing-section-heading"><p className="landing-label">Try it</p><h2 id="try-title">Hand a task to two AI agents.</h2></div>
        {workspace}
      </section>
      <Section id="how" label="How it works" title={<>Your request is the permission.<br />Nothing a document says can widen it.</>}>
        <div className="landing-grid-three">{STEPS.map((s) => <article key={s.label} className="landing-feature"><p className="landing-label">{s.label}</p><h3>{s.title}</h3><p>{s.copy}</p></article>)}</div>
      </Section>
      <Evidence d={d.data} error={d.error} waking={waking} />
      <Integrate />
      <Section id="faq" label="Questions" title="Before you decide."><div className="landing-faq">{FAQS.map(([q, a]) => <details key={q}><summary>{q}<span aria-hidden>+</span></summary><p>{a}</p></details>)}</div></Section>
    </div>
  </div>;
}
