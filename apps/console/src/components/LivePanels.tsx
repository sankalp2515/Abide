import { useState } from "react";
import { api, type E2E } from "../api";
import { Card, CardTitle, DECISION_TEXT, Empty, ErrorNote, Loading, pct, Stat, Tag, useAsync } from "./ui";

const WINDOWS = [{ h: 1, l: "1 h" }, { h: 24, l: "24 h" }, { h: 168, l: "7 days" }];

/** Live operations from /v1/metrics (observability, NFR §12). */
export function LiveOperations() {
  const [hours, setHours] = useState(24);
  const { data: m, error, loading } = useAsync(() => api.metrics(hours), [hours]);
  const providers = (m?.providers ?? []).reduce<Record<string, Record<string, number>>>((acc, r) => {
    (acc[r.provider] ??= {})[r.status] = r.n;
    return acc;
  }, {});
  return (
    <Card>
      <CardTitle meta={
        <div className="flex gap-1.5" role="group" aria-label="Time window">
          {WINDOWS.map((w) => (
            <button key={w.h} className={`pill pill-sm ${hours === w.h ? "pill-primary" : ""}`} aria-pressed={hours === w.h} onClick={() => setHours(w.h)}>{w.l}</button>
          ))}
        </div>
      }>Live operations</CardTitle>
      {loading && !m && <Loading />}
      {error && <ErrorNote>{error}</ErrorNote>}
      {m && m.decisions === 0 && <Empty>No decisions in this window yet. Delegate a task to see live data.</Empty>}
      {m && m.decisions > 0 && (
        <>
          <div className="grid grid-cols-2 gap-8 md:grid-cols-4">
            <Stat value={m.decisions} label={`Decisions · ${m.inspections.n} content inspections`} />
            <Stat value={pct(m.inspections.escalation_rate)} label={`Sent to the AI judge (${m.inspections.escalated_to_judge})`} />
            <Stat value={m.latency_ms.fast_path.p95 !== null ? `${Math.round(m.latency_ms.fast_path.p95)} ms` : "—"}
              label={`Fast path p95 · judge path p95 ${m.latency_ms.judge_path.p95 !== null ? `${Math.round(m.latency_ms.judge_path.p95)} ms` : "—"}`} />
            <Stat value={m.inspections.degraded} label="Degraded inspections (held safely)" tone={m.inspections.degraded ? "var(--neutralise)" : undefined} />
          </div>
          <div className="mt-7 grid gap-8 border-t pt-6 hairline md:grid-cols-3">
            <div>
              <p className="section-label mb-3">Decisions</p>
              <div className="flex flex-wrap gap-1.5">
                {Object.entries(m.by_decision).map(([d, n]) => <Tag key={d}>{DECISION_TEXT[d as keyof typeof DECISION_TEXT] ?? d} · {n}</Tag>)}
              </div>
              <p className="section-label mb-3 mt-5">Review</p>
              <div className="flex flex-wrap gap-1.5">
                <Tag>Approvals pending · {m.approvals_pending}</Tag>
                {m.memory ? <>
                  <Tag>Confirmed attacks · {m.memory.attack ?? 0}</Tag>
                  <Tag>Reviewed false alarms · {m.memory.benign ?? 0}</Tag>
                </> : <span className="subtle text-xs">Memory counts are restricted in this public view.</span>}
              </div>
            </div>
            <div>
              <p className="section-label mb-3">Top rules</p>
              <ul className="m-0 list-none space-y-1.5 p-0 text-xs">
                {Object.entries(m.by_rule).sort((a, b) => b[1] - a[1]).slice(0, 6).map(([r, n]) => (
                  <li key={r} className="flex justify-between gap-3"><span className="mono" style={{ color: "var(--accent-soft)" }}>{r}</span><span className="num subtle">{n}</span></li>
                ))}
              </ul>
            </div>
            <div>
              <p className="section-label mb-3">Model providers</p>
              <ul className="m-0 list-none space-y-1.5 p-0 text-xs">
                {Object.entries(providers).map(([prov, st]) => {
                  const errs = Object.entries(st).filter(([k]) => k !== "ok" && k !== "cached").reduce((a, [, n]) => a + n, 0);
                  return (
                    <li key={prov} className="flex justify-between gap-3">
                      <span>{prov}</span>
                      <span className="num subtle">{st.ok ?? 0} ok · {st.cached ?? 0} cached{errs ? <span style={{ color: "var(--block)" }}> · {errs} errors</span> : null}</span>
                    </li>
                  );
                })}
              </ul>
            </div>
          </div>
        </>
      )}
    </Card>
  );
}

/** Spend (CON-6): security model use vs agent model use, measured per protected run where available. */
export function Spend({ e2e }: { e2e: E2E[] }) {
  const { data: c, error, loading } = useAsync(() => api.costs(7), []);
  const perRun = e2e.filter((e) => e.overhead);
  const usd = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `$${v.toFixed(v < 0.01 ? 4 : 2)}`);
  return (
    <Card>
      <CardTitle meta="Free tiers: nothing is paid. Costs are equivalents at list price where a model has one.">Spend</CardTitle>
      {perRun.length > 0 ? (
        <div className="mb-7 grid grid-cols-2 gap-8 md:grid-cols-4">
          {perRun.map((e) => (
            <Stat key={e.suite} value={e.overhead!.token_ratio ?? "—"}
              label={`Security tokens per agent token · ${e.suite} (${e.overhead!.runs} protected runs) · cost ratio ${e.overhead!.cost_ratio ?? "—"}`} />
          ))}
        </div>
      ) : (
        <p className="subtle mb-6 text-xs">Per-run overhead (G7b) appears here once a protected end-to-end run records usage.</p>
      )}
      {loading && <Loading />}
      {error && <ErrorNote>{error}</ErrorNote>}
      {c && (
        <div className="overflow-x-auto">
          <table className="table min-w-[640px]">
            <thead><tr><th>Role</th><th>Provider · model</th><th>Calls</th><th>Tokens in / out</th><th>Equivalent cost</th><th>Avg latency</th></tr></thead>
            <tbody>
              {c.by_model.slice(0, 12).map((r) => (
                <tr key={`${r.role}-${r.provider}-${r.model}`}>
                  <td className="mono">{r.role}</td>
                  <td className="text-xs">{r.provider} · {r.model}</td>
                  <td className="num">{r.calls}{r.cached ? <span className="subtle text-[11px]"> ({r.cached} cached)</span> : null}</td>
                  <td className="num text-xs">{(r.input_tokens ?? 0).toLocaleString()} / {(r.output_tokens ?? 0).toLocaleString()}</td>
                  <td className="num">{r.est_cost_usd ? usd(r.est_cost_usd) : <span className="subtle">no list price</span>}</td>
                  <td className="num subtle text-xs">{r.avg_latency_ms ? `${Math.round(r.avg_latency_ms)} ms` : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="subtle mt-4 text-xs leading-6">
            Last 7 days, all roles (including evaluation runs, which call the judge without an agent). Read the per-run
            figures above for the security-to-agent overhead.
          </p>
        </div>
      )}
    </Card>
  );
}
