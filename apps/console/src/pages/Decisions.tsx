import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { admin, agentName, api, type DecisionRow } from "../api";
import { useReviewer } from "../reviewer";
import { Card, DecisionBadge, Empty, ErrorNote, Loading, PageHeader, Tag, useAsync } from "../components/ui";

const LABELS = [
  "", "Instruction Override", "Role Change", "Secret Extraction", "Tool Abuse", "Credential Theft",
  "Context Poisoning", "Multi-Step Jailbreaks", "Encoded Instructions", "Indirect Prompt Injection",
];

function Label({ row }: { row: DecisionRow }) {
  const reviewer = useReviewer();
  const [state, setState] = useState<{ busy: boolean; msg: string | null; ok: boolean }>({ busy: false, msg: null, ok: false });
  if (!reviewer || row.kind !== "content_in") return null;
  async function mark(verdict: "attack" | "false_positive") {
    setState({ busy: true, msg: null, ok: false });
    try {
      await admin.feedback(reviewer!.key, verdict, row.event_id, verdict === "attack" ? row.labels : []);
      setState({ busy: false, ok: true, msg: verdict === "attack" ? "Remembered as an attack" : "Remembered as a false alarm" });
    } catch (e) {
      const m = (e as Error).message;
      setState({ busy: false, ok: false, msg: m.includes("recent memory") ? "Too old to label: the gateway keeps only recent text" : m });
    }
  }
  if (state.ok) return <span className="text-[11px]" style={{ color: "var(--allow)" }}>{state.msg}</span>;
  return (
    <span className="flex flex-col gap-1.5">
      <span className="flex gap-1.5">
        <button className="pill pill-sm" style={{ minHeight: 30 }} disabled={state.busy} onClick={() => mark("attack")}>Confirm attack</button>
        <button className="pill pill-sm" style={{ minHeight: 30 }} disabled={state.busy} onClick={() => mark("false_positive")}>False alarm</button>
      </span>
      {state.msg && <span className="text-[11px]" style={{ color: "var(--block)" }}>{state.msg}</span>}
    </span>
  );
}

const PAGE = 50;
const WINDOWS = [{ v: "", l: "Any time" }, { v: "3600", l: "Last hour" }, { v: "86400", l: "Last 24 hours" }, { v: "604800", l: "Last 7 days" }];

export function Decisions() {
  const [params] = useSearchParams();
  const [filters, setFilters] = useState({ decision: "", label: "", agent: params.get("agent") ?? "", window: "" });
  const [rows, setRows] = useState<DecisionRow[] | null>(null);
  const [more, setMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const reviewer = useReviewer();
  const agents = useAsync(() => api.agents(), []);

  async function load(older = false) {
    setLoading(true);
    setError(null);
    try {
      const { window, ...rest } = filters;
      const params: Record<string, string> = { ...rest, limit: String(PAGE) };
      if (window) params.since = String(Date.now() / 1000 - Number(window));
      if (older && rows?.length) params.before = String(rows[rows.length - 1].created_at);
      const page = await api.decisions(params);
      setRows(older && rows ? [...rows, ...page] : page);
      setMore(page.length === PAGE);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filters]);

  return (
    <div className="space-y-8">
      <PageHeader index="04" label="Decisions" title={<>Every boundary<br />has a reason.</>}
        lede="Every inspection and tool-call decision from every connected agent, newest first. Content appears only as a redacted preview." />
      <Card>
        <div className="flex flex-col gap-4 md:flex-row md:items-end">
          <label className="flex flex-col gap-2"><span className="section-label">Decision</span>
            <select className="input" value={filters.decision} onChange={(e) => setFilters((f) => ({ ...f, decision: e.target.value }))}>
              {["", "ALLOW", "NEUTRALISE", "BLOCK", "ESCALATE"].map((d) => <option key={d} value={d}>{d || "Any decision"}</option>)}
            </select></label>
          <label className="flex flex-col gap-2"><span className="section-label">Attack type</span>
            <select className="input" value={filters.label} onChange={(e) => setFilters((f) => ({ ...f, label: e.target.value }))}>
              {LABELS.map((l) => <option key={l} value={l}>{l || "Any type"}</option>)}
            </select></label>
          <label className="flex flex-col gap-2"><span className="section-label">When</span>
            <select className="input" value={filters.window} onChange={(e) => setFilters((f) => ({ ...f, window: e.target.value }))}>
              {WINDOWS.map((w) => <option key={w.v} value={w.v}>{w.l}</option>)}
            </select></label>
          <label className="flex flex-1 flex-col gap-2"><span className="section-label">Agent</span>
            <select className="input" value={filters.agent} onChange={(e) => setFilters((f) => ({ ...f, agent: e.target.value }))}>
              <option value="">All agents</option>
              {(agents.data ?? []).map((a) => <option key={a.agent} value={a.agent}>{agentName(a.agent)}</option>)}
            </select></label>
          <button className="pill" onClick={() => load()}>Refresh</button>
        </div>
      </Card>
      {error && <ErrorNote>{error}</ErrorNote>}
      {loading && !rows && <Loading />}
      {rows && rows.length === 0 && <Empty>No decisions match these filters.</Empty>}
      {rows && rows.length > 0 && (
        <Card>
          <div className="overflow-x-auto">
            <table className="table min-w-[1080px]">
              <thead><tr><th>Time</th><th>Agent</th><th>Decision</th><th>Source</th><th>Rule · reason</th><th>Attack types</th><th>Session</th>{reviewer && <th>Review</th>}</tr></thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.event_id}>
                    <td className="subtle whitespace-nowrap text-xs">{new Date(r.created_at * 1000).toLocaleTimeString()}</td>
                    <td className="whitespace-nowrap text-xs"><span className="block">{agentName(r.agent_id)}</span>{agentName(r.agent_id) !== r.agent_id && <span className="mono subtle text-[11px]">{r.agent_id}</span>}</td>
                    <td><DecisionBadge decision={r.decision} />{r.enforced !== r.decision && <span className="subtle mt-1 block text-[11px]">monitor mode</span>}</td>
                    <td className="text-xs">{r.source.replace("tool:", "tool · ")}</td>
                    <td><span className="mono" style={{ color: "var(--accent-soft)" }}>{r.policy_rule}</span><span className="subtle mt-1 block text-xs">{r.reason}</span></td>
                    <td><div className="flex flex-wrap gap-1.5">{r.decision !== "ALLOW" && r.labels.map((l) => <Tag key={l}>{l}</Tag>)}</div></td>
                    <td><Link className="text-link mono" to={`/replay?session=${r.session_id}`}>{r.session_id.slice(0, 10)}</Link></td>
                    {reviewer && <td><Label row={r} /></td>}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="mt-5 flex flex-wrap items-center justify-between gap-3">
            <span className="subtle text-xs" aria-live="polite">{rows.length} decision{rows.length === 1 ? "" : "s"} shown</span>
            {more && <button className="pill pill-sm" disabled={loading} onClick={() => load(true)}>{loading ? "Loading…" : "Load older"}</button>}
          </div>
        </Card>
      )}
    </div>
  );
}
