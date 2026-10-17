import { api, type Insights as Data } from "../api";
import { Card, CardTitle, useAsync } from "./ui";

const TONE: Record<string, string> = { allow: "var(--allow)", block: "var(--block)", ask: "var(--escalate)" };
const WORD: Record<string, string> = { allow: "Allowed", block: "Blocked", ask: "Asked the employee" };

function Bars({ rows, label, value, tone }: { rows: Record<string, string | number>[]; label: string; value: string; tone?: (r: Record<string, string | number>) => string }) {
  const max = Math.max(1, ...rows.map((r) => Number(r[value]) || 0));
  if (!rows.length) return <p className="subtle text-xs">Nothing yet.</p>;
  return (
    <ul className="space-y-2">
      {rows.map((r, i) => (
        <li key={i} className="text-xs">
          <div className="flex justify-between gap-3"><span className="truncate">{String(r[label])}</span><span className="mono subtle">{r[value]}</span></div>
          <div className="mt-1 h-1.5 rounded-full" style={{ background: "var(--line)" }}>
            <div className="h-1.5 rounded-full" style={{ width: `${(Number(r[value]) / max) * 100}%`, background: tone ? tone(r) : "var(--accent)" }} />
          </div>
        </li>
      ))}
    </ul>
  );
}

/** BigQuery over every receipt the gateway has streamed: hidden when analytics are not configured. */
export function Insights() {
  const res = useAsync<Data>(() => api.insights(30), []);
  if (res.error?.includes("not configured")) return null;
  const d = res.data;
  const total = d ? d.decisions.reduce((n, r) => n + Number(r.n), 0) : 0;
  return (
    <Card>
      <CardTitle meta={d ? `BigQuery · ${d.table} · ${d.query_ms} ms` : "BigQuery"}>Last 30 days, across every task</CardTitle>
      {res.error && <p className="subtle text-xs">Insights are unavailable right now.</p>}
      {!d && !res.error && <p className="subtle text-xs">Asking BigQuery…</p>}
      {d && (
        <div className="grid gap-6 md:grid-cols-3">
          <div>
            <p className="section-label mb-2">{d.workflows} tasks · {total} agent actions checked</p>
            <Bars rows={d.decisions.map((r) => ({ ...r, decision: WORD[String(r.decision)] ?? r.decision, key: r.decision }))} label="decision" value="n" tone={(r) => TONE[String(r.key)] ?? "var(--accent)"} />
          </div>
          <div>
            <p className="section-label mb-2">What agents were stopped from doing</p>
            <Bars rows={d.stopped_tools} label="tool" value="n" tone={() => "var(--block)"} />
          </div>
          <div>
            <p className="section-label mb-2">Where blocked instructions came from</p>
            <Bars rows={d.sources} label="source" value="n" tone={() => "var(--escalate)"} />
          </div>
        </div>
      )}
    </Card>
  );
}
