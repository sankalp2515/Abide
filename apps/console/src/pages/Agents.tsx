import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { serviceOrigin, tasks, type DApproval, type DEvent, type DReceipt, type DReport, type DState, type DTaskRow } from "../api";
import { signOutEmployee, useEmployee } from "../employee";
import { Empty, ErrorNote, PageHeader, Stamp, ago } from "../components/ui";
import { ApprovalCard, KeyGate, StepRow, WorkOrder } from "./Delegate";

/* "Your agents": the tasks your own agents run through the API (the one-file client, ADK, LangChain, MCP …), with
   the same work order, questions and receipt as the sandbox. Sign in with the key your agent uses. */

/** Steps from the receipt chain: each check (and what the runner reported), each screened read. */
export function stepsFromReceipts(receipts: DReceipt[], approvals: DApproval[]): DEvent[] {
  const ran = new Set(receipts.filter((r) => r.kind === "executed" && r.decision === "executed").map((r) => String(r.data.check)));
  const approval = new Map(approvals.map((a) => [a.id, a]));
  const out: DEvent[] = [];
  for (const r of receipts) {
    const d = r.data as Record<string, unknown>;
    if (r.kind === "check") {
      const ap = d.approval ? approval.get(String(d.approval)) : undefined;
      const outcome = r.decision === "block" ? "blocked"
        : r.decision === "ask" ? (ap?.status === "pending" ? "waiting" : ap?.status === "denied" ? "declined" : ap ? "approved" : "waiting")
        : ran.has(r.rid) ? "ran" : "allowed";
      const explain = (d.explanation as { text?: string } | undefined)?.text;
      out.push({ agent: r.agent, tool: r.tool, args: (d.args as Record<string, unknown>) ?? {}, outcome,
        note: outcome === "ran" || outcome === "allowed" ? "" : `${r.reason}${d.traced_to ? ` (traced to ${String(d.traced_to)})` : ""}`, explain });
    } else if (r.kind === "inspect" && d.flagged) {
      out.push({ agent: r.agent, tool: `screened:${String(d.source ?? "content").replace(/^tool:/, "")}`, args: {}, outcome: "screened",
        note: "Hidden or suspicious instructions removed before the agent read it" });
    }
  }
  return out;
}

export function ConnectAgent({ apiKey }: { apiKey: string }) {
  const origin = serviceOrigin();
  const [copied, setCopied] = useState<string | null>(null);
  const copy = async (what: string, text: string) => {
    try { await navigator.clipboard.writeText(text); setCopied(what); setTimeout(() => setCopied(null), 1800); } catch { /* no clipboard */ }
  };
  const code = `pip install httpx\ncurl -O ${origin}/v1/sdk/abide.py\n\n# in your agent\nfrom abide import DelegateClient, ToolGate\nclient = DelegateClient("${origin}", "<your key>")\ncard = await client.start("Read the launch plan and email Sam a summary.", workspace="<your business>")\n# confirm the card here, on this page, then:\ngate = ToolGate(client, card["workflow_id"], "my-agent")\nsend_email = gate.wrap(send_email)`;
  return (
    <section className="card overflow-hidden" aria-labelledby="connect-title">
      <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)]">
        <div className="min-w-0 space-y-4 p-6">
          <span className="section-label">Connect an agent on your machine</span>
          <h2 id="connect-title" className="h3">Your agent runs locally. You approve here.</h2>
          <ol className="m-0 list-none space-y-3 p-0 text-[15px]">
            <li className="flex gap-3"><span className="mono pt-0.5" style={{ color: "var(--accent)" }}>1</span><span>Give your agent this key: <button className="pill pill-sm ml-1" onClick={() => copy("key", apiKey)}>{copied === "key" ? "Copied" : "Copy key"}</button></span></li>
            <li className="flex gap-3"><span className="mono pt-0.5" style={{ color: "var(--accent)" }}>2</span><span>Wrap its tools with the one-file client (ADK, LangChain, OpenAI Agents SDK, MCP or plain functions).</span></li>
            <li className="flex gap-3"><span className="mono pt-0.5" style={{ color: "var(--accent)" }}>3</span><span>Its tasks appear below. Confirm the work order, answer its questions, read the receipt.</span></li>
          </ol>
          <div className="flex flex-wrap gap-3">
            <a className="pill pill-primary pill-sm" href="https://colab.research.google.com/github/sankalp2515/Abide/blob/main/examples/colab/abide_quickstart.ipynb" target="_blank" rel="noreferrer">Open the Colab</a>
            <a className="pill pill-sm" href={`${origin}/v1/sdk/abide.py`} download>Download abide.py</a>
          </div>
        </div>
        <div className="relative min-w-0">
          <pre className="lg-code m-0 h-full rounded-none text-[12.5px]" style={{ borderRadius: 0 }}>{code}</pre>
          <button className="pill pill-sm absolute right-3 top-3" style={{ background: "#18202B", color: "#E6E8E3", borderColor: "#3C4757" }} onClick={() => copy("code", code)}>{copied === "code" ? "Copied" : "Copy"}</button>
        </div>
      </div>
    </section>
  );
}

export function TaskRow({ t, active, onOpen }: { t: DTaskRow; active: boolean; onOpen: () => void }) {
  return (
    <li>
      <button onClick={onOpen} aria-current={active ? "true" : undefined} className="w-full cursor-pointer px-4 py-3.5 text-left"
        style={{ background: active ? "var(--surface-2)" : "transparent", boxShadow: active ? "inset 3px 0 0 var(--accent)" : undefined, borderTop: "1px solid var(--line)" }}>
        <span className="flex items-center justify-between gap-2">
          <span className="mono subtle text-[11px]">{t.id} · {t.source === "demo" ? "sandbox" : "your agent"} · {ago(t.created)}</span>
          {t.waiting > 0 ? <Stamp kind="wait">{t.waiting} waits</Stamp> : t.status === "draft" ? <Stamp kind="quiet">Draft</Stamp> : null}
        </span>
        <span className="words mt-1 line-clamp-2 block text-[16px] leading-snug">“{t.request}”</span>
        <span className="subtle mt-1 block text-[12px]">{t.business} · {t.allowed} allowed · <span style={{ color: t.stopped ? "var(--block)" : undefined }}>{t.stopped} stopped</span> · {t.asked} asked</span>
      </button>
    </li>
  );
}

export function TaskDetail({ apiKey, wf }: { apiKey: string; wf: string }) {
  const [state, setState] = useState<DState | null>(null);
  const [report, setReport] = useState<DReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [words, setWords] = useState("");
  const load = () => tasks.state(apiKey, wf).then((s) => { setState(s); setError(null); }).catch((e) => setError((e as Error).message));
  useEffect(() => { setState(null); setReport(null); load(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [wf, apiKey]);
  useEffect(() => {   // follow a running agent: new steps and questions appear by themselves
    const timer = setInterval(() => { if (!document.hidden) load(); }, 3000);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wf, apiKey]);
  async function act(fn: () => Promise<unknown>) {
    setBusy(true); setError(null);
    try { await fn(); await load(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  if (!state) return error ? <ErrorNote>{error}</ErrorNote> : <p className="subtle text-sm" aria-live="polite">Opening the task…</p>;
  const steps = stepsFromReceipts(state.receipts, state.approvals);
  const pending = state.approvals.filter((a) => a.status === "pending");
  const confirmed = state.contract.status === "confirmed";
  return (
    <div className="space-y-6">
      {error && <ErrorNote>{error}</ErrorNote>}
      <WorkOrder state={state} busy={busy} onConfirm={(accept) => act(() => tasks.confirm(apiKey, wf, state.contract.hash, accept))}>
        {confirmed && (
          <form className="flex flex-col gap-2 border-t pt-4 sm:flex-row" style={{ borderColor: "var(--line)" }} onSubmit={(e) => { e.preventDefault(); if (words.trim()) act(() => tasks.amend(apiKey, wf, words)).then(() => setWords("")); }}>
            <label className="sr-only" htmlFor="agents-say">Tell your agent more</label>
            <input id="agents-say" className="input words flex-1 text-[16px]" placeholder="Add to or narrow the work order, in your words" value={words} onChange={(e) => setWords(e.target.value)} />
            <button className="pill" disabled={busy || !words.trim()}>Update the order</button>
          </form>
        )}
      </WorkOrder>
      {pending.map((a) => <ApprovalCard key={a.id} a={a} busy={busy} explain={steps.find((s) => s.outcome === "waiting" && s.tool === a.tool)?.explain}
        onDecide={(ok) => act(() => tasks.decide(apiKey, wf, a.id, ok))} />)}
      <section className="card overflow-hidden" aria-labelledby="agent-steps">
        <div className="flex flex-wrap items-baseline justify-between gap-2 px-5 pb-3 pt-5">
          <h2 id="agent-steps" className="h3">What your agent did</h2>
          <span className="subtle flex items-center gap-2 text-[13px]"><span className="lg-live"><i />Live</span>{steps.length} steps</span>
        </div>
        {steps.length === 0 ? <div className="px-5 pb-5"><Empty>{confirmed ? "No tool calls yet. They appear here as your agent makes them." : "Confirm the work order; your agent's tool calls are refused until you do."}</Empty></div>
          : <ol className="m-0 list-none p-0">{steps.map((e, i) => <StepRow key={i} e={e} n={i + 1} />)}</ol>}
      </section>
      <section className="card overflow-hidden" aria-labelledby="agent-receipt">
        <div className="flex flex-wrap items-center justify-between gap-2 px-5 py-4">
          <h2 id="agent-receipt" className="h3">Work receipt</h2>
          <button className="pill pill-sm" disabled={busy} onClick={() => act(async () => setReport(await tasks.report(apiKey, wf)))}>{report ? "Write it again" : "Write the receipt"}</button>
        </div>
        {report && <>
          <ul className="m-0 list-none space-y-2.5 border-t px-5 py-4" style={{ borderColor: "var(--line)" }}>
            {report.sentences.map((s, i) => <li key={i} className="text-[15px] leading-relaxed">{s.text}{" "}{s.cites.filter(Boolean).map((c) => <span key={c} className="mono subtle text-[11px]"> [{c}]</span>)}</li>)}
          </ul>
          <p className="subtle m-0 flex items-center gap-3 border-t px-5 py-3 text-[12px]" style={{ borderColor: "var(--line)" }}>
            {report.chain_ok ? <Stamp kind="ran" live>Chain intact</Stamp> : <Stamp kind="stopped">Chain broken</Stamp>}
            {report.writer.startsWith("model") ? "Written by Gemini from the evidence; every sentence cites receipts." : "Written from the evidence; every sentence cites receipts."}</p>
        </>}
      </section>
    </div>
  );
}

export function Agents() {
  const employee = useEmployee();
  const [rows, setRows] = useState<DTaskRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [show, setShow] = useState<"api" | "all">("api");
  useEffect(() => {
    if (!employee) return;
    let alive = true;
    const load = () => tasks.list(employee.key).then((r) => { if (alive) { setRows(r); setError(null); } }).catch((e) => alive && setError((e as Error).message));
    load();
    const timer = setInterval(() => { if (!document.hidden) load(); }, 4000);
    return () => { alive = false; clearInterval(timer); };
  }, [employee?.key]);

  const header = <PageHeader label="Tasks · Your agents" title="Your agents’ tasks."
    lede="Tasks your own agents run through the Abide API, on your machine or in your cloud. Same work order, same checks, same receipt."
    actions={employee ? <span className="flex flex-wrap items-center gap-2 text-xs md:justify-end"><span className="tag">Signed in · {employee.name}</span><button className="pill pill-sm" onClick={signOutEmployee}>Sign out</button></span> : undefined} />;
  if (!employee) return <div className="space-y-8">{header}<KeyGate /></div>;

  const shown = (rows ?? []).filter((r) => show === "all" || r.source === "api");
  const selected = open && shown.some((r) => r.id === open) ? open : shown[0]?.id ?? null;
  return (
    <div className="space-y-8">
      {header}
      <ConnectAgent apiKey={employee.key} />
      {error && <ErrorNote>{error}</ErrorNote>}
      <div className="grid items-start gap-6 xl:grid-cols-[minmax(0,0.75fr)_minmax(0,1.25fr)]">
        <section className="card overflow-hidden xl:sticky xl:top-6" aria-labelledby="list-title">
          <div className="flex flex-wrap items-center justify-between gap-2 px-4 py-3">
            <h2 id="list-title" className="h3">Tasks</h2>
            <div className="flex gap-1" role="radiogroup" aria-label="Which tasks">
              <button role="radio" aria-checked={show === "api"} className={`pill pill-sm ${show === "api" ? "pill-primary" : ""}`} onClick={() => setShow("api")}>Your agents</button>
              <button role="radio" aria-checked={show === "all"} className={`pill pill-sm ${show === "all" ? "pill-primary" : ""}`} onClick={() => setShow("all")}>All, with sandbox</button>
            </div>
          </div>
          {rows === null ? <p className="subtle px-4 pb-4 text-sm">Loading…</p>
            : shown.length === 0 ? <div className="px-4 pb-4"><Empty>No tasks from your agents yet. Connect one above, or <Link className="text-link" to="/delegate">run a sandbox task</Link>.</Empty></div>
            : <ul className="m-0 max-h-[70vh] list-none overflow-y-auto p-0">{shown.map((t) => <TaskRow key={t.id} t={t} active={t.id === selected} onOpen={() => setOpen(t.id)} />)}</ul>}
        </section>
        <div className="min-w-0">{selected ? <TaskDetail apiKey={employee.key} wf={selected} /> : null}</div>
      </div>
    </div>
  );
}
