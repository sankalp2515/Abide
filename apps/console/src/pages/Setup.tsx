import { useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { admin, agentName, api, type AgentSummary, type DecisionRow, type NewAgent, type SelfTest } from "../api";
import { useReviewer } from "../reviewer";
import { ReviewerSignIn } from "../components/ReviewerSignIn";
import { ago, Card, CardTitle, DecisionBadge, Empty, ErrorNote, Loading, PageHeader, Tag, useAsync } from "../components/ui";

const PATHS = [
  {
    title: "LLM proxy",
    tag: "No code change",
    text: "Point the agent's model base URL at the gateway. Works with OpenAI-compatible clients (OpenAI, Gemini, Groq, Azure, local models, LangChain…) and the Anthropic SDK, in any language.",
  },
  {
    title: "Python SDK",
    tag: "Deepest protection",
    text: "Wrap your tools and the content your agent reads. Works whatever model provider the agent uses, embedded or against this gateway.",
  },
  {
    title: "REST API",
    tag: "Any language",
    text: "Call /v1/inspect on what the agent reads and /v1/actions/check before each tool call, from any framework with hooks.",
  },
];

function Register({ onCreated }: { onCreated: (agent: string) => void }) {
  const reviewer = useReviewer();
  const templates = useAsync(() => api.templates(), []);
  const providers = useAsync(() => api.providers(), []);
  const [form, setForm] = useState<NewAgent>({
    agent: "", template: "research-assistant", mode: "monitor", upstream: { provider: null, model: null, key: "gateway" },
  });
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const template = templates.data?.find((t) => t.id === form.template);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!reviewer) return;
    setBusy(true);
    setMsg(null);
    try {
      const r = await admin.createAgent(reviewer.key, { ...form, agent: form.agent.trim() });
      setMsg({ ok: true, text: `Created ${r.agent} (version ${r.version}, ${r.mode} mode). Connect it below.` });
      onCreated(r.agent);
    } catch (err) {
      setMsg({ ok: false, text: (err as Error).message });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardTitle meta="Starts in monitor mode: decisions are recorded, nothing is blocked yet.">2. Register the agent</CardTitle>
      {!reviewer && (
        <div className="subtle mb-5 flex flex-wrap items-center gap-3 text-xs"><span>Registering needs a reviewer key. Anyone can read the connection steps below.</span><ReviewerSignIn /></div>
      )}
      <form className="grid gap-5 md:grid-cols-2" onSubmit={submit}>
        <label className="flex flex-col gap-2 text-xs"><span className="section-label">Agent name</span>
          <input className="input mono" aria-label="Agent name" required placeholder="support-bot" pattern="[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}" value={form.agent}
            onChange={(e) => setForm({ ...form, agent: e.target.value })} />
          <span className="subtle">Letters, digits, dot, dash or underscore. Used in the gateway URL.</span>
        </label>
        <label className="flex flex-col gap-2 text-xs"><span className="section-label">Starting policy</span>
          <select className="input" aria-label="Starting policy" value={form.template} onChange={(e) => setForm({ ...form, template: e.target.value })}>
            {(templates.data ?? []).map((t) => <option key={t.id} value={t.id}>{t.title}</option>)}
          </select>
          {template && <span className="subtle">{template.description}</span>}
        </label>
        <label className="flex flex-col gap-2 text-xs"><span className="section-label">Model provider (for the proxy)</span>
          <select className="input" aria-label="Model provider" value={form.upstream.provider ?? ""}
            onChange={(e) => setForm({ ...form, upstream: { ...form.upstream, provider: e.target.value || null } })}>
            <option value="">The gateway's own free models (demo)</option>
            {(providers.data ?? []).map((p) => (
              <option key={p.name} value={p.name}>{p.name}{p.configured ? "" : " (no gateway key: use the agent's own key)"}</option>
            ))}
          </select>
          <span className="subtle">Not needed for the SDK or REST API: they work with any provider.</span>
        </label>
        <label className="flex flex-col gap-2 text-xs"><span className="section-label">Model (optional)</span>
          <input className="input mono" aria-label="Model" placeholder="Use the model the agent asks for" value={form.upstream.model ?? ""}
            onChange={(e) => setForm({ ...form, upstream: { ...form.upstream, model: e.target.value || null } })} />
        </label>
        <fieldset className="flex flex-col gap-2 text-xs md:col-span-2">
          <legend className="section-label mb-2">Provider key</legend>
          <label className="flex items-center gap-2"><input type="radio" name="key" checked={form.upstream.key === "gateway"}
            onChange={() => setForm({ ...form, upstream: { ...form.upstream, key: "gateway" } })} />
            The gateway's key for this provider (set by the operator)</label>
          <label className="flex items-center gap-2"><input type="radio" name="key" checked={form.upstream.key === "client"}
            onChange={() => setForm({ ...form, upstream: { ...form.upstream, key: "client" } })} />
            The agent's own key, sent in the <span className="mono">X-Upstream-Key</span> header (never stored)</label>
        </fieldset>
        {template && Object.keys(template.tools).length > 0 && (
          <div className="text-xs md:col-span-2">
            <span className="section-label mb-2 block">Tools in this template (rename them to your agent's tools on the Policy page)</span>
            <div className="flex flex-wrap gap-2">
              {Object.entries(template.tools).map(([name, risk]) => <Tag key={name}>{name} · {risk}</Tag>)}
            </div>
          </div>
        )}
        <div className="md:col-span-2">
          <button className="pill pill-primary" disabled={!reviewer || busy || !form.agent.trim()}>{busy ? "Creating…" : "Register agent"}</button>
        </div>
      </form>
      {msg && <p role={msg.ok ? "status" : "alert"} className="mt-4 text-xs" style={{ color: msg.ok ? "var(--allow)" : "var(--block)" }}>{msg.text}</p>}
      {(templates.error || providers.error) && <ErrorNote>{templates.error || providers.error}</ErrorNote>}
    </Card>
  );
}

function CodeBlock({ code }: { code: string }) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  }
  return (
    <div className="relative">
      <pre className="mono m-0 overflow-x-auto rounded-2xl border p-4 text-[12px] leading-6 hairline" style={{ background: "var(--surface-2)" }}>{code}</pre>
      <button className="pill pill-sm absolute right-3 top-3" onClick={copy} aria-label="Copy code">{copied ? "Copied" : "Copy"}</button>
    </div>
  );
}

function Connect({ agent }: { agent: string }) {
  const { data, error, loading } = useAsync(() => api.connect(agent), [agent]);
  const [tab, setTab] = useState("openai-env");
  if (loading) return <Loading />;
  if (error) return <ErrorNote>{error}</ErrorNote>;
  if (!data) return null;
  const current = data.integrations.find((i) => i.id === tab) ?? data.integrations[0];
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap gap-2" role="tablist" aria-label="Integration">
        {data.integrations.map((i) => (
          <button key={i.id} role="tab" aria-selected={i.id === current.id}
            className={`pill pill-sm ${i.id === current.id ? "pill-primary" : ""}`} onClick={() => setTab(i.id)}>{i.title}</button>
        ))}
      </div>
      <p className="subtle m-0 text-xs">{current.when}</p>
      <CodeBlock code={current.code} />
      <p className="subtle m-0 text-xs">
        Gateway <span className="mono">{data.gateway_url}</span> · upstream{" "}
        <span className="mono">{data.upstream.provider ?? "gateway free models"}{data.upstream.model ? ` / ${data.upstream.model}` : ""}</span>{" "}
        · {data.upstream.key === "client" ? "agent's own provider key" : "gateway's provider key"} · project keys come from the gateway operator.
      </p>
    </div>
  );
}

function Verify({ agent }: { agent: string }) {
  const reviewer = useReviewer();
  const [res, setRes] = useState<SelfTest | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { setRes(null); setErr(null); }, [agent]);
  async function run() {
    if (!reviewer) return;
    setBusy(true);
    setErr(null);
    try {
      setRes(await admin.selfTest(reviewer.key, agent));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="space-y-4">
      <p className="subtle m-0 text-xs">Three harmless probes go through <span className="mono">{agent}</span>'s real policy: text a human cannot see, a forged role marker, and a tool the policy does not list.</p>
      <button className="pill pill-primary pill-sm" disabled={!reviewer || busy} onClick={run}>{busy ? "Running…" : "Run self-test"}</button>
      {!reviewer && <p className="subtle m-0 text-xs">Needs a reviewer key (step 2).</p>}
      {err && <ErrorNote>{err}</ErrorNote>}
      {res && (
        <div className="space-y-3">
          <p role="status" className="m-0 text-sm" style={{ color: res.passed ? "var(--allow)" : "var(--block)" }}>
            {res.passed ? "All checkpoints are active." : "A checkpoint did not fire: check the policy."} <span className="subtle text-xs">{res.note} ({res.ms} ms)</span>
          </p>
          <ul className="m-0 list-none border-t p-0 hairline">
            {res.checks.map((c) => (
              <li key={c.check} className="flex flex-wrap items-center justify-between gap-3 border-b py-3 text-xs hairline">
                <span>{c.passed ? "✓" : "✗"} {c.check}</span>
                <span className="flex items-center gap-3"><DecisionBadge decision={c.decision} /><span className="mono">{c.rule}</span>
                  <Link className="subtle underline" to={`/replay?session=${encodeURIComponent(c.session_id)}`}>Replay</Link></span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

const DAY = 86400;

interface Activity { last: number | null; day: number; stopped: number }

function activity(rows: DecisionRow[], agent: string): Activity {
  const mine = rows.filter((r) => r.agent_id === agent);
  const since = Date.now() / 1000 - DAY;
  const today = mine.filter((r) => r.created_at >= since);
  return { last: mine.length ? mine[0].created_at : null, day: today.length, stopped: today.filter((r) => r.decision !== "ALLOW").length };
}

/** Every registered agent, whether it is sending traffic, and its last 24 hours. */
function ConnectedAgents({ agents, rows }: { agents: AgentSummary[]; rows: DecisionRow[] }) {
  const now = Date.now() / 1000;
  const list = agents.map((a) => ({ a, act: activity(rows, a.agent) }))
    .sort((x, y) => (y.act.last ?? 0) - (x.act.last ?? 0));
  return (
    <Card>
      <CardTitle meta="An agent counts as connected once its traffic reaches the gateway. Activity from the last 7 days.">Your agents</CardTitle>
      <div className="overflow-x-auto">
        <table className="table min-w-[860px]">
          <thead><tr><th>Agent</th><th>Status</th><th>Mode</th><th>Last decision</th><th>Decisions · 24 h</th><th>Stopped · 24 h</th><th></th></tr></thead>
          <tbody>
            {list.map(({ a, act }) => {
              const live = act.last !== null && now - act.last < DAY;
              const status = live ? "Active" : act.last !== null ? "Connected · idle" : "Registered · no traffic yet";
              const tone = live ? "var(--allow)" : act.last !== null ? "var(--neutralise)" : "var(--subtle)";
              return (
                <tr key={a.agent}>
                  <td><span className="block text-[14px]">{agentName(a.agent)}</span><span className="mono subtle text-[11px]">{a.agent}</span></td>
                  <td><span className="flex items-center gap-2 text-xs" style={{ color: tone }}><span aria-hidden className="inline-block h-2 w-2 rounded-full" style={{ background: tone }} />{status}</span></td>
                  <td><Tag tone={a.mode === "enforce" ? "var(--allow)" : "var(--neutralise)"}>{a.mode}</Tag></td>
                  <td className="subtle text-xs">{act.last ? ago(act.last) : "—"}</td>
                  <td className="num">{act.day}</td>
                  <td className="num" style={{ color: act.stopped ? "var(--block)" : undefined }}>{act.stopped}</td>
                  <td className="whitespace-nowrap text-xs"><Link className="text-link" to={`/decisions?agent=${encodeURIComponent(a.agent)}`}>Decisions</Link> · <Link className="text-link" to={`/policy?agent=${encodeURIComponent(a.agent)}`}>Policy</Link></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

export function Setup() {
  const agents = useAsync(() => api.agents(), []);
  const recent = useAsync(() => api.decisions({ limit: "200", since: String(Date.now() / 1000 - 7 * DAY) }), []);
  const [agent, setAgent] = useState<string>("");
  const [adding, setAdding] = useState<boolean | null>(null);
  useEffect(() => {
    if (!agent && agents.data && agents.data.length) setAgent(agents.data[0].agent);
  }, [agents.data, agent]);
  const anyTraffic = (recent.data ?? []).length > 0;
  const open = adding ?? !anyTraffic;
  return (
    <div className="space-y-8">
      <PageHeader index="00" label="Agents" title={<>Every agent,<br />one boundary.</>}
        lede="The agents protected by this gateway and what each did today. Adding another takes four steps: register, connect with one setting or a few lines, verify, enforce." />
      {agents.data && recent.data && agents.data.length > 0 && <ConnectedAgents agents={agents.data} rows={recent.data} />}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="h3 m-0">Connect another agent</h2>
        <button className="pill pill-sm" aria-expanded={open} onClick={() => setAdding(!open)}>{open ? "Hide the steps" : "Show the four steps"}</button>
      </div>
      {open && <>
      <Card>
        <CardTitle meta="All three use the same engine, policies and evidence.">1. Choose how your agent connects</CardTitle>
        <div className="grid gap-5 md:grid-cols-3">
          {PATHS.map((p) => (
            <article key={p.title} className="rounded-2xl border p-5 hairline">
              <Tag>{p.tag}</Tag>
              <h3 className="h3 mt-4">{p.title}</h3>
              <p className="lede m-0 mt-2 text-[13px]">{p.text}</p>
            </article>
          ))}
        </div>
      </Card>
      <Register onCreated={(a) => { agents.reload(); setAgent(a); }} />
      <Card>
        <CardTitle meta={
          <label className="flex items-center gap-2"><span className="sr-only">Agent</span>
            <select className="input mono" aria-label="Agent to connect" value={agent} onChange={(e) => setAgent(e.target.value)}>
              {(agents.data ?? []).map((a) => <option key={a.agent} value={a.agent}>{a.agent} · {a.mode}</option>)}
            </select>
          </label>
        }>3. Connect it</CardTitle>
        {agents.loading && <Loading />}
        {agents.error && <ErrorNote>{agents.error}</ErrorNote>}
        {agents.data && agents.data.length === 0 && <Empty>No agents yet. Register one above.</Empty>}
        {agent && <Connect agent={agent} />}
      </Card>
      {agent && (
        <Card>
          <CardTitle>4. Verify, then go live</CardTitle>
          <Verify agent={agent} />
          <ol className="lede mt-6 space-y-2 pl-5 text-[13px]">
            <li>Use the agent normally for a while in monitor mode. Watch <Link className="underline" to="/decisions">Decisions</Link> for <span className="mono">tool-not-allowed</span> and false alarms.</li>
            <li>Rename and add tools, recipients and domains on the <Link className="underline" to="/policy">Policy</Link> page.</li>
            <li>Switch the policy to enforce. Escalations wait in <Link className="underline" to="/review">Review</Link>.</li>
          </ol>
        </Card>
      )}
      </>}
    </div>
  );
}
