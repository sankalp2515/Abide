import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { tasks, type DAgent, type DBusiness } from "../api";
import { remembering, setRemember, signOutEmployee, useEmployee } from "../employee";
import { Empty, ErrorNote, PageHeader, Stamp, ago } from "../components/ui";
import { ConnectAgent } from "./Agents";
import { KeyGate } from "./Delegate";

/* Connect: everything about who works under your words. Your key, how to connect your own agent, the agents that
   have acted under your work orders (live), your businesses (tools, people, files), and advanced filter settings. */

const LIVE_S = 300;   // an agent that acted in the last five minutes is shown as live

function YourKey({ apiKey, name }: { apiKey: string; name: string }) {
  const [shown, setShown] = useState(false);
  const [copied, setCopied] = useState(false);
  const [remember, setRem] = useState(remembering());
  return (
    <section className="card settle flex flex-col gap-4 p-6 md:flex-row md:items-center md:justify-between" aria-labelledby="key-title">
      <div className="space-y-2">
        <span className="section-label">Your key · {name}</span>
        <h2 id="key-title" className="h3">Your own key: for you, not for your agents</h2>
        <p className="mono m-0 break-all text-[13px]">{shown ? apiKey : `${apiKey.slice(0, 10)}${"•".repeat(12)}${apiKey.slice(-4)}`}</p>
        <label className="flex cursor-pointer items-center gap-2 text-[14px]">
          <input type="checkbox" className="h-4 w-4" style={{ accentColor: "var(--accent)" }} checked={remember} onChange={(e) => { setRem(e.target.checked); setRemember(e.target.checked); }} />
          Remember me on this device
        </label>
      </div>
      <div className="flex flex-wrap gap-2">
        <button className="pill pill-sm" onClick={() => setShown((v) => !v)}>{shown ? "Hide" : "Show"}</button>
        <button className="pill pill-sm pill-primary" onClick={async () => { try { await navigator.clipboard.writeText(apiKey); setCopied(true); setTimeout(() => setCopied(false), 1800); } catch { /* no clipboard */ } }}>{copied ? "Copied" : "Copy key"}</button>
        <button className="pill pill-sm" onClick={signOutEmployee}>Sign out</button>
      </div>
    </section>
  );
}

/** Keys for agents: they act inside your work orders and can never confirm, widen or approve them. */
function AgentKeys({ apiKey }: { apiKey: string }) {
  const [list, setList] = useState<{ id: string; label: string; created: number; expires: number; active: boolean }[] | null>(null);
  const [label, setLabel] = useState("my agent");
  const [fresh, setFresh] = useState<{ key: string; label: string } | null>(null);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const load = () => tasks.agentKeys(apiKey).then(setList).catch((e) => setError((e as Error).message));
  useEffect(() => { load(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [apiKey]);
  return (
    <section id="agent-keys" className="card settle space-y-4 p-6" aria-labelledby="ak-title">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div><span className="section-label">Agent keys</span><h2 id="ak-title" className="h3 mt-1">Give each agent its own key</h2>
          <p className="muted m-0 mt-1 max-w-[62ch] text-[14px]">An agent key can start tasks for you, have its tool calls checked and read the receipt. It can never confirm or change a work order, approve its own questions, change your business or make keys. Only you can.</p></div>
      </div>
      <form className="flex flex-col gap-2 sm:flex-row" onSubmit={async (e) => {
        e.preventDefault(); setError(null);
        try { const k = await tasks.newAgentKey(apiKey, label.trim() || "my agent"); setFresh({ key: k.key, label: k.label }); setCopied(false); load(); }
        catch (err) { setError((err as Error).message); }
      }}>
        <label className="sr-only" htmlFor="ak-label">Agent name</label>
        <input id="ak-label" className="input flex-1" maxLength={60} value={label} onChange={(e) => setLabel(e.target.value)} placeholder="Which agent is it for?" />
        <button className="pill pill-primary">Make an agent key</button>
      </form>
      {fresh && (
        <div className="settle space-y-2 rounded-md p-4" style={{ background: "var(--accent-wash)", border: "1.5px solid var(--accent)" }} role="status">
          <p className="m-0 text-[14px]"><b>{fresh.label}</b>: copy it now; it is shown once.</p>
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
            <code className="mono flex-1 break-all rounded px-2 py-1.5 text-[13px]" style={{ background: "var(--surface)" }}>{fresh.key}</code>
            <button className="pill pill-sm" onClick={async () => { try { await navigator.clipboard.writeText(fresh.key); setCopied(true); } catch { /* select by hand */ } }}>{copied ? "Copied" : "Copy"}</button>
          </div>
        </div>
      )}
      {error && <ErrorNote>{error}</ErrorNote>}
      {list && list.length > 0 && (
        <ul className="m-0 list-none divide-y p-0" style={{ borderColor: "var(--line)" }}>
          {list.map((k) => (
            <li key={k.id} className="flex flex-wrap items-center justify-between gap-2 py-2.5 text-[14px]" style={{ borderTop: "1px solid var(--line)" }}>
              <span><b className="font-semibold">{k.label}</b> <span className="mono subtle text-[12px]">{k.id} · made {ago(k.created)}</span></span>
              {k.active ? <button className="pill pill-sm pill-danger" onClick={async () => { await tasks.revokeAgentKey(apiKey, k.id); load(); }}>Revoke</button> : <Stamp kind="quiet">Revoked</Stamp>}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function AddBusiness({ apiKey, onAdded }: { apiKey: string; onAdded: (id: string) => void }) {
  const [id, setId] = useState("");
  const [json, setJson] = useState("");
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  return (
    <details className="card p-5">
      <summary className="cursor-pointer text-[15px] font-semibold">Add your business <span className="subtle font-normal">· paste its definition (tools, people, files, records); a person does this once</span></summary>
      <form className="mt-4 space-y-3" onSubmit={async (e) => {
        e.preventDefault(); setMsg(null);
        try {
          const policy = JSON.parse(json);
          await tasks.registerBusiness(apiKey, id.trim(), policy);
          setMsg({ ok: true, text: `Added ${id.trim()}. Your agents can now start tasks for it.` }); onAdded(id.trim());
        } catch (err) { setMsg({ ok: false, text: err instanceof SyntaxError ? "That is not valid JSON." : (err as Error).message }); }
      }}>
        <label className="flex flex-col gap-1"><span className="section-label">Business id</span>
          <input id="biz-id" className="input mono" required pattern="[a-z0-9][a-z0-9-]{2,40}" placeholder="northwind-books" value={id} onChange={(e) => setId(e.target.value)} /></label>
        <label className="flex flex-col gap-1"><span className="section-label">Definition (JSON)</span>
          <textarea id="biz-json" className="input mono min-h-[160px] text-[12.5px]" required placeholder='{"tools": {"send_email": {"action": "send", "recipient_args": ["to"]}}, "contacts": [...]}' value={json} onChange={(e) => setJson(e.target.value)} /></label>
        <button className="pill pill-primary">Add business</button>
        {msg && (msg.ok ? <p className="m-0 text-[14px]" style={{ color: "var(--allow)" }}>{msg.text}</p> : <ErrorNote>{msg.text}</ErrorNote>)}
      </form>
    </details>
  );
}

function Bar({ n, total, tone }: { n: number; total: number; tone: string }) {
  return <span className="block h-1.5 rounded-sm" style={{ width: `${total ? Math.max(4, (n / total) * 100) : 0}%`, background: tone, transition: "width .6s cubic-bezier(.2,.8,.2,1)" }} />;
}

/** One agent that acted under your work orders: who, what kind, live or not, and what happened to its actions. */
function AgentCard({ a, now, i }: { a: DAgent; now: number; i: number }) {
  const live = now - a.last < LIVE_S;
  return (
    <article className="card settle flex flex-col gap-4 p-5 transition-shadow hover:shadow-[var(--shadow)]" style={{ animationDelay: `${i * 50}ms` }} aria-label={`Agent ${a.agent}`}>
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-3">
          <span aria-hidden className="grid h-10 w-10 place-items-center rounded-md text-[15px] font-bold" style={{ background: a.sandbox ? "var(--accent-wash)" : "var(--surface-2)", color: "var(--accent)", border: "1px solid var(--line)" }}>{a.agent.slice(0, 2).toUpperCase()}</span>
          <div><h3 className="m-0 text-[17px] font-semibold">{a.agent}</h3><p className="subtle m-0 text-[12px]">{a.kind}</p></div>
        </div>
        {live ? <span className="lg-live"><i />Live</span> : <span className="subtle text-[12px]">{ago(a.last)}</span>}
      </div>
      <div className="grid grid-cols-3 gap-3 text-center">
        <div><b className="block text-[22px] font-semibold">{a.tasks}</b><span className="subtle text-[12px]">tasks</span></div>
        <div><b className="block text-[22px] font-semibold">{a.checks}</b><span className="subtle text-[12px]">actions checked</span></div>
        <div><b className="block text-[22px] font-semibold" style={{ color: a.stopped ? "var(--block)" : undefined }}>{a.stopped}</b><span className="subtle text-[12px]">stopped</span></div>
      </div>
      <div className="space-y-1.5" aria-label={`${a.allowed} allowed, ${a.asked} asked, ${a.stopped} stopped`}>
        <div className="flex gap-1">{a.allowed > 0 && <Bar n={a.allowed} total={a.checks} tone="var(--allow)" />}{a.asked > 0 && <Bar n={a.asked} total={a.checks} tone="var(--neutralise)" />}{a.stopped > 0 && <Bar n={a.stopped} total={a.checks} tone="var(--block)" />}</div>
        <p className="subtle m-0 flex flex-wrap gap-3 text-[12px]"><span>● {a.allowed} ran</span><span style={{ color: "var(--neutralise)" }}>● {a.asked} asked you</span><span style={{ color: "var(--block)" }}>● {a.stopped} stopped</span></p>
      </div>
      <p className="subtle m-0 border-t pt-3 text-[12px]" style={{ borderColor: "var(--line)" }}>Works for {a.business} · every action checked against your words</p>
    </article>
  );
}

function ConnectedAgents({ apiKey }: { apiKey: string }) {
  const [agents, setAgents] = useState<DAgent[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    let alive = true;
    const load = () => tasks.agents(apiKey).then((r) => { if (alive) { setAgents(r); setNow(Date.now() / 1000); } }).catch((e) => alive && setError((e as Error).message));
    load();
    const timer = setInterval(() => { if (!document.hidden) load(); }, 5000);
    return () => { alive = false; clearInterval(timer); };
  }, [apiKey]);
  const live = (agents ?? []).filter((a) => now - a.last < LIVE_S).length;
  return (
    <section className="space-y-4" aria-labelledby="agents-title">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div><span className="section-label">Connected agents</span><h2 id="agents-title" className="h3 mt-1">Who works under your words</h2></div>
        {agents && <span className="subtle flex items-center gap-3 text-[13px]">{live > 0 && <span className="lg-live"><i />{live} live now</span>}{agents.length} agents · from your receipts</span>}
      </div>
      {error && <ErrorNote>{error}</ErrorNote>}
      {agents === null ? <p className="subtle text-sm">Loading…</p>
        : agents.length === 0 ? <Empty>No agent has acted under your work orders yet. <Link className="text-link" to="/delegate">Run a sandbox task</Link> or connect your own agent above.</Empty>
        : <div className="grid gap-4 md:grid-cols-2 2xl:grid-cols-3">{agents.map((a, i) => <AgentCard key={a.agent} a={a} now={now} i={i} />)}</div>}
    </section>
  );
}

function Business({ apiKey }: { apiKey: string }) {
  const [list, setList] = useState<{ id: string; builtin: boolean; business?: string; description?: string }[] | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [detail, setDetail] = useState<DBusiness | null>(null);
  const [added, setAdded] = useState(0);
  useEffect(() => { tasks.businesses(apiKey).then((l) => { setList(l); setOpen((o) => o ?? l[0]?.id ?? null); }).catch(() => setList([])); }, [apiKey, added]);
  useEffect(() => { if (open) tasks.business(apiKey, open).then((b) => setDetail(b.policy)).catch(() => setDetail(null)); }, [open, apiKey]);
  return (
    <section className="space-y-4" aria-labelledby="biz-title">
      <div><span className="section-label">Your business</span><h2 id="biz-title" className="h3 mt-1">What your agents can touch</h2>
        <p className="muted m-0 mt-1 text-[14px]">Tools, the people they may contact, files and records. Anything not here stays off, whatever a document says. Add your own below.</p></div>
      <div className="flex flex-wrap gap-2" role="radiogroup" aria-label="Business">
        {(list ?? []).map((b) => <button key={b.id} role="radio" aria-checked={open === b.id} className={`pill pill-sm ${open === b.id ? "pill-primary" : ""}`} onClick={() => setOpen(b.id)}>{b.business ?? b.id}{b.builtin ? " · sandbox" : ""}</button>)}
      </div>
      {detail && (
        <div className="grid gap-4 md:grid-cols-3">
          <div className="card settle p-5"><span className="section-label">Tools</span><ul className="m-0 mt-3 list-none space-y-2 p-0 text-[14px]">
            {Object.entries(detail.tools).map(([n, t]) => <li key={n} className="flex items-center justify-between gap-2"><span className="mono">{n}</span><span className="tag">{t.action}</span></li>)}</ul></div>
          <div className="card settle p-5"><span className="section-label">People</span><ul className="m-0 mt-3 list-none space-y-2 p-0 text-[14px]">
            {detail.contacts.map((c) => <li key={c.id}><b className="font-semibold">{c.name}</b><span className="subtle block text-[12px]">{c.address.startsWith("@") ? "whoever owns the record" : c.address}</span></li>)}
            <li className="subtle text-[12px]">Inside the company: {detail.org_domains.join(", ")}</li></ul></div>
          <div className="card settle p-5"><span className="section-label">Files and records</span><ul className="m-0 mt-3 list-none space-y-2 p-0 text-[14px]">
            {detail.resources.map((r) => <li key={r.id} className="flex items-center justify-between gap-2"><span>{r.name}</span>{r.data_class === "confidential" ? <Stamp kind="wait">Confidential</Stamp> : <span className="tag">{r.data_class ?? "internal"}</span>}</li>)}
            {detail.records.map((r) => <li key={r.type} className="subtle text-[13px]">{r.type} records · {r.data_class ?? "internal"}</li>)}</ul></div>
        </div>
      )}
      <AddBusiness apiKey={apiKey} onAdded={(bid) => { setAdded((n) => n + 1); setOpen(bid); }} />
    </section>
  );
}

export function Connect() {
  const employee = useEmployee();
  const header = <PageHeader label="Connect" title="Your agents, your business, your key."
    lede="Connect an agent on your machine or in your cloud. It works under the same work orders, checks and receipts as the sandbox." />;
  if (!employee) return <div className="space-y-8">{header}<KeyGate /></div>;
  return (
    <div className="space-y-12">
      {header}
      <YourKey apiKey={employee.key} name={employee.name} />
      <ConnectAgent apiKey={employee.key} />
      <AgentKeys apiKey={employee.key} />
      <ConnectedAgents apiKey={employee.key} />
      <Business apiKey={employee.key} />
      <details className="card p-5">
        <summary className="cursor-pointer text-[15px] font-semibold">Advanced <span className="subtle font-normal">· content filter and gateway agents (for security teams)</span></summary>
        <div className="mt-4 grid gap-3 sm:grid-cols-2">
          <Link to="/policy" className="card block p-4 transition-shadow hover:shadow-[var(--shadow)]"><b>Content-filter settings</b><p className="subtle m-0 mt-1 text-[13px]">How strictly what agents read is screened, per agent: thresholds, labels, actions.</p></Link>
          <Link to="/setup" className="card block p-4 transition-shadow hover:shadow-[var(--shadow)]"><b>Gateway agents</b><p className="subtle m-0 mt-1 text-[13px]">Agents that send every message through the filter as a proxy (OpenAI-compatible), with self-tests.</p></Link>
        </div>
      </details>
    </div>
  );
}
