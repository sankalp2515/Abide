import { useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { delegate, PRODUCT_NAME, type DArm, type DEval } from "../api";
import { useBackendAvailability } from "../components/Layout";
import { SectionLink } from "../components/LandingNav";
import { Arrow, SealMark, Spark, useAsync } from "../components/ui";

/* Abide's landing page. Every number comes from the published evaluation report (GET /v1/delegate-eval). */

const count = (a: DArm | undefined, rate: number | null | undefined, n: number | undefined) =>
  a && rate != null && n ? `${Math.round(rate * n)} of ${n}` : "—";
const q = (a: DArm | undefined) => (a?.questions_per_benign_task != null ? a.questions_per_benign_task.toFixed(1) : "—");

function Section({ id, eyebrow, title, intro, children }: {
  id: string; eyebrow: string; title: ReactNode; intro?: ReactNode; children: ReactNode;
}) {
  return <section id={id} tabIndex={-1} className="al-section" aria-labelledby={`${id}-title`}>
    <div className="al-head"><p className="al-eyebrow">{eyebrow}</p><h2 id={`${id}-title`}>{title}</h2>{intro && <p className="al-intro">{intro}</p>}</div>
    {children}
  </section>;
}

/* ---- hero: a live preview of the product, playing the story on a loop -------------------------------------- */
const PREVIEW_STEPS: { who: string; what: string; out: "ok" | "no" | "shield"; note?: string }[] = [
  { who: "Reader", what: "Read ticket T-881", out: "ok" },
  { who: "Reader", what: "Open photo.pdf", out: "shield", note: "Hidden instruction removed before reading" },
  { who: "Resolver", what: "Refund order 4521 · ₹1,850", out: "ok" },
  { who: "Resolver", what: "Refund order 9921 · ₹48,000", out: "no", note: "Not on your card · came from the ticket" },
  { who: "Resolver", what: "Export the customer list", out: "no", note: "Not on your card · came from the ticket" },
  { who: "Resolver", what: "Email meera@mail.example", out: "ok" },
];

function usePlayhead(length: number, ms: number): number {
  const reduced = typeof window !== "undefined" && !!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  const [i, setI] = useState(reduced ? length + 1 : 0);
  useEffect(() => {
    if (reduced) return;
    const id = setInterval(() => setI((n) => (n >= length + 3 ? 0 : n + 1)), ms);
    return () => clearInterval(id);
  }, [length, ms, reduced]);
  return i;
}

function ProductPreview() {
  const shown = usePlayhead(PREVIEW_STEPS.length, 1300);
  const done = shown > PREVIEW_STEPS.length;
  return (
    <div className="al-frame" role="img" aria-label="Abide at work: a permission card made from the employee's words, the agents' steps, two actions blocked, and a receipt.">
      <div className="al-frame-bar" aria-hidden><i /><i /><i /><span>abide · Delegate</span></div>
      <div className="al-frame-body" aria-hidden>
        <div className="al-pv-left">
          <p className="al-pv-label">Your words</p>
          <p className="al-pv-words"><mark>Look up order #4521</mark> and <mark>refund it</mark>, then <mark>reply to the customer</mark>. <mark className="lim">Do not refund more than ₹2,000.</mark></p>
          <div className="al-pv-card">
            <div className="al-pv-card-top"><span><SealMark size={15} /> Permission card</span><em>this task only</em></div>
            <ul>
              <li><b className="ok">✓</b>Read order 4521</li>
              <li><b className="ok">✓</b>Refund order 4521, up to ₹2,000</li>
              <li><b className="ok">✓</b>Email the customer on order 4521</li>
              <li><b className="no">✕</b>Anything else</li>
            </ul>
            <div className="al-pv-card-foot"><Spark size={11} /> Drafted by Gemini · confirmed once</div>
          </div>
        </div>
        <div className="al-pv-right">
          <div className="al-pv-flow"><span className={shown > 0 && shown <= 2 ? "live" : ""}>Reader</span><i className={done ? "" : "run"} /><span className="seal"><SealMark size={12} /></span><i className={done ? "" : "run"} /><span className={shown > 2 && !done ? "live" : ""}>Resolver</span></div>
          <ol className="al-pv-steps">
            {PREVIEW_STEPS.slice(0, Math.min(shown, PREVIEW_STEPS.length)).map((s) => (
              <li key={s.what} className={s.out}>
                <div><small>{s.who}</small><span>{s.what}</span><b>{s.out === "no" ? "✕ Stopped" : "✓ Ran"}</b></div>
                {s.note && <p>{s.out === "no" ? <strong>Action blocked · </strong> : null}{s.note}</p>}
              </li>
            ))}
          </ol>
          <div className={`al-pv-receipt ${done ? "on" : ""}`}>✓ Receipt · 6 steps · 2 stopped · chain intact</div>
        </div>
      </div>
    </div>
  );
}

function Hero({ d, waking, error }: { d: DEval | null; waking: boolean; error: string | null }) {
  const on = d?.arms.delegate, off = d?.arms.none;
  return <section className="al-hero" aria-labelledby="hero-title">
    <div className="al-glow" aria-hidden />
    <p className="al-pill-note"><Spark size={12} /> Built with Gemini on Vertex AI and Google ADK</p>
    <h1 id="hero-title" className="al-title">Hand off the task.<br /><span className="grad-text">Approve it once.</span></h1>
    <p className="al-sub">{PRODUCT_NAME} turns an employee's request into the only permissions their AI agents get. The agents work on their own; anything outside the request is stopped, even when a document tells them to do it, and every step lands in a receipt.</p>
    <div className="al-ctas">
      <Link to="/delegate" className="pill pill-primary al-cta">Start free trial<Arrow /></Link>
      <SectionLink id="how" className="pill">See how it works</SectionLink>
    </div>
    <p className="al-fine">No sign-up · sandbox office and support desk · real Gemini agents · nothing real is sent</p>
    <ProductPreview />
    <div className="al-stats" aria-label="Measured evaluation results">
      <div><b className="num">{count(on, on?.attack_success_rate, on?.attack_scenarios)}</b><span>attacks got an action through ({count(off, off?.attack_success_rate, off?.attack_scenarios)} without {PRODUCT_NAME})</span></div>
      <div><b className="num">{count(on, on?.benign_task_completed, on?.benign_scenarios)}</b><span>normal tasks finished</span></div>
      <div><b className="num">{q(on)}</b><span>questions per task</span></div>
    </div>
    <p className="al-fine" aria-live="polite">{waking ? "The service is waking up. This can take 2–3 minutes; figures refresh automatically when it is online."
      : d ? <>Local evaluation, worst-case agents, {d.created.slice(0, 10)} · <SectionLink id="evidence" className="text-link">Method and limits</SectionLink></>
      : error ? "The evaluation service is unavailable right now." : "Loading the evaluation…"}</p>
  </section>;
}

const STACK = ["Gemini on Vertex AI", "Google ADK", "Model Armor", "Cloud Run", "Any agent framework (REST)"];

function Compare({ d }: { d: DEval | null }) {
  const none = d?.arms.none, ask = d?.arms["ask-every-step"], us = d?.arms.delegate;
  const cols = [
    { name: "Trust the agent", good: false, lines: [`${count(none, none?.attack_success_rate, none?.attack_scenarios)} attacks got through`, "No questions", "No record of why"] },
    { name: "Approve every step", good: false, lines: [`${count(ask, ask?.attack_success_rate, ask?.attack_scenarios)} attacks got through`, `${q(ask)} questions per task`, "People click yes without reading"] },
    { name: PRODUCT_NAME, good: true, lines: [`${count(us, us?.attack_success_rate, us?.attack_scenarios)} attacks got through`, `${q(us)} questions per task`, "A receipt for every step"] },
  ];
  return <Section id="why" eyebrow="Why" title={<>Two bad choices today.<br />One good one.</>}
    intro="Companies either let agents act freely, or ask a person before every step. The first lets a hidden line in a PDF send the customer list away; the second trains people to click yes.">
    <div className="al-compare">{cols.map((c) => (
      <article key={c.name} className={`al-col ${c.good ? "good" : "bad"}`}>
        <h3>{c.good ? <SealMark size={18} /> : null}{c.name}</h3>
        <ul>{c.lines.map((l) => <li key={l}><b aria-hidden>{c.good ? "✓" : "✕"}</b>{l}</li>)}</ul>
      </article>
    ))}</div>
  </Section>;
}

function How() {
  return <Section id="how" eyebrow="How it works" title="Ask. Confirm once. Prove.">
    <div className="al-rows">
      <article className="al-row">
        <div><p className="al-step">01 · Ask</p><h3>Your words become the permission</h3><p>The employee writes the task as they would to a colleague. Gemini drafts a short permission card, and a check keeps only what the words support: named people, named files and orders, stated limits.</p></div>
        <div className="al-mini" aria-hidden>
          <p className="al-mini-words"><mark>Read the paint supplier's quote</mark> and <mark>write a reply</mark>. <mark className="lim">Don't send it yet.</mark></p>
          <ul className="al-mini-lines"><li><b className="ok">✓</b>Read BrightCoat quote</li><li><b className="ok">✓</b>Draft to sales@brightcoat.example</li><li><b className="no">✕</b>Send email</li></ul>
        </div>
      </article>
      <article className="al-row flip">
        <div><p className="al-step">02 · Confirm once</p><h3>Agents work. The card decides.</h3><p>Every tool call by every agent is checked before it runs: who receives it, which record, how much. A call outside the card never runs, and if it came from a document, nobody is even interrupted. Company rules ask once, for one exact action.</p></div>
        <div className="al-mini" aria-hidden>
          <div className="al-mini-step ok"><span>Draft the reply to the supplier</span><b>✓ Ran</b></div>
          <div className="al-mini-step no"><span>Email customers.csv to records@…</span><b>✕ Stopped</b><p>Action blocked · came from the quote PDF, not from you</p></div>
          <div className="al-mini-step ask"><span>Send the reply · needs you once</span><b>? Approve</b></div>
        </div>
      </article>
      <article className="al-row">
        <div><p className="al-step">03 · Prove</p><h3>A receipt you can check</h3><p>Every step, stop and approval is a numbered, hash-chained receipt. The summary cites them, and a sentence the receipts do not support is dropped. Evidence, not the agent's own account.</p></div>
        <div className="al-mini" aria-hidden>
          <ol className="al-mini-time"><li>Card confirmed</li><li>3 reads</li><li>2 done</li><li className="no">2 stopped</li><li>Chain intact</li></ol>
          <p className="al-mini-receipt">Refunded ₹1,850 on order 4521 <code>R-14</code> · Stopped a ₹48,000 refund the ticket asked for <code>R-16</code></p>
        </div>
      </article>
    </div>
  </Section>;
}

const USES = [
  { title: "Customer support", copy: "Refunds and replies inside the limit the agent was given. Requests hidden in tickets stay requests." },
  { title: "Office and operations", copy: "Read quotes, draft replies, file documents. Nothing leaves the company unless someone said so." },
  { title: "Any business you describe", copy: "Register your tools, people and records once; the same card governs your agents." },
];

function Evidence({ d, error, waking }: { d: DEval | null; error: string | null; waking: boolean }) {
  const ARMS: [string, string][] = [["none", "No protection"], ["ask-every-step", "Ask before every step"], ["delegate", PRODUCT_NAME], ["gate-only", `${PRODUCT_NAME}, content filter off`]];
  return <Section id="evidence" eyebrow="Evidence" title={<>Measured, with the method<br />next to the number.</>}
    intro="Two businesses, 28 attacks across nine attack types and 26 normal tasks. Outcomes come from the sandbox's execution log, not from what the agents say.">
    <div className="al-panel">
      {waking ? <p role="status" className="landing-warning">The service is waking up. Results refresh automatically when it is online.</p>
        : error && <p role="status" className="landing-warning">The evaluation service is unavailable right now.</p>}
      <div className="overflow-x-auto"><table className="table">
        <thead><tr><th>Setup</th><th>Attacks that got an action through</th><th>Normal tasks finished</th><th>Questions per task</th></tr></thead>
        <tbody>{ARMS.map(([id, name]) => { const a = d?.arms[id]; return <tr key={id} className={id === "delegate" ? "al-us" : ""}>
          <td>{name}</td><td className="num">{count(a, a?.attack_success_rate, a?.attack_scenarios)}</td>
          <td className="num">{count(a, a?.benign_task_completed, a?.benign_scenarios)}</td><td className="num">{q(a)}</td></tr>; })}</tbody>
      </table></div>
      <p className="al-fine left">The agents in this table obey every instruction they read (a worst case). In live runs, real Gemini agents told to obey what they read tried 9 unauthorised actions; every one was stopped. It shows the mechanism; it is not a benchmark or a guarantee.</p>
    </div>
  </Section>;
}

function Developers() {
  return <Section id="integrate" eyebrow="For developers" title={<>Put your own agents<br />under the employee's words.</>}>
    <div className="al-dev">
      <ol className="al-dev-steps">
        <li><b>Get a key</b><span>A private 7-day key in one call. No sign-up.</span></li>
        <li><b>Describe your business</b><span>Your tools and what each does, your people, records and rules.</span></li>
        <li><b>Add two callbacks</b><span>Every tool call is checked before it runs; what reading tools return is inspected.</span></li>
        <li><b>Try it in Colab</b><span>A travel agency and a real Gemini agent, end to end, in about two minutes.</span></li>
      </ol>
      <pre className="al-code" aria-label="Example code"><code>{`from abide import DelegateClient, RemoteAdkGuard

client = await DelegateClient.with_trial_key(URL)
await client.register_workspace("northwind", policy)
card = await client.start(
    "Refund booking #4410 up to $300 and reply to Priya.",
    workspace="northwind")
await client.confirm(card)

guard = RemoteAdkGuard(client, card["workflow_id"], "agent")
agent = LlmAgent(...,
    before_tool_callback=guard.before_tool,
    after_tool_callback=guard.after_tool)`}</code></pre>
    </div>
  </Section>;
}

const FAQS = [
  ["Is this an AI assistant?", `No. ${PRODUCT_NAME} sits between a company's AI agents and the tools they use, and decides which of their actions run.`],
  ["Can a document give an agent new permissions?", "No. Only the words of the person who delegated the task change the card. Text an agent reads, or text you paste in, is information, never permission."],
  ["What if Gemini is unavailable?", "Word rules draft the card instead, and anything they cannot read is left off. If nothing can be understood, the card says so and cannot be confirmed."],
  ["Does it catch every attack?", "No system does. Layers work together: a content filter and Google Model Armor remove instructions hidden in documents, and the card stops actions that were never asked for, even when both filters miss."],
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
  return <div className="al-page">
    <Hero d={d.data} waking={waking} error={d.error} />
    <div className="al-stack" aria-label="Built on">{STACK.map((s) => <span key={s}>{s}</span>)}</div>
    <Compare d={d.data} />
    <How />
    <Section id="uses" eyebrow="Use cases" title="Built for work that leaves a trace.">
      <div className="al-uses">{USES.map((u) => <article key={u.title}><h3>{u.title}</h3><p>{u.copy}</p></article>)}</div>
    </Section>
    <Evidence d={d.data} error={d.error} waking={waking} />
    <Developers />
    <Section id="faq" eyebrow="Questions" title="Before you decide."><div className="landing-faq">{FAQS.map(([qq, a]) => <details key={qq}><summary>{qq}<span aria-hidden>+</span></summary><p>{a}</p></details>)}</div></Section>
    <section className="al-band" aria-labelledby="band-title">
      <h2 id="band-title">Delegate your first task in a minute.</h2>
      <p>Pick the office or the support desk, read the card, and watch real agents work inside it.</p>
      <Link to="/delegate" className="pill pill-primary al-cta">Start free trial<Arrow /></Link>
    </section>
  </div>;
}
