import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { tasks, type DApproval, type DState } from "../api";
import { useEmployee } from "../employee";
import { Empty, ErrorNote, PageHeader, Stamp, ago } from "../components/ui";
import { ApprovalCard, KeyGate } from "./Delegate";
import { stepsFromReceipts } from "./Agents";
import { Approvals } from "./Review";

/* Needs you: every question your agents are waiting on, from every task, in one place. Each answer covers one exact
   action, once, for 15 minutes. Below it, the security reviewers' queue (content the filter held for a person). */

interface Item { wf: string; request: string; business: string; a: DApproval; explain?: string }

export function Inbox() {
  const employee = useEmployee();
  const [items, setItems] = useState<Item[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [done, setDone] = useState<{ id: string; approved: boolean; what: string }[]>([]);

  async function load(key: string) {
    const rows = (await tasks.list(key)).filter((r) => r.waiting > 0);
    const states = await Promise.all(rows.map((r) => tasks.state(key, r.id).then((s) => [r, s] as const).catch(() => null)));
    const out: Item[] = [];
    for (const pair of states) {
      if (!pair) continue;
      const [r, s] = pair as readonly [typeof rows[number], DState];
      const steps = stepsFromReceipts(s.receipts, s.approvals);
      for (const a of s.approvals.filter((x) => x.status === "pending")) {
        out.push({ wf: r.id, request: r.request, business: r.business, a, explain: steps.find((e) => e.outcome === "waiting" && e.tool === a.tool)?.explain });
      }
    }
    return out.sort((x, y) => y.a.created - x.a.created);
  }
  useEffect(() => {
    if (!employee) return;
    let alive = true;
    const tick = () => load(employee.key).then((r) => { if (alive) { setItems(r); setError(null); } }).catch((e) => alive && setError((e as Error).message));
    tick();
    const timer = setInterval(() => { if (!document.hidden) tick(); }, 4000);
    return () => { alive = false; clearInterval(timer); };
  }, [employee?.key]);

  const header = <PageHeader label="Needs you" title="Questions waiting for you."
    lede="Your agents ask only when something falls outside the work order. Each answer covers one exact action, once." />;
  if (!employee) return <div className="space-y-8">{header}<KeyGate /></div>;

  return (
    <div className="space-y-8">
      {header}
      {error && <ErrorNote>{error}</ErrorNote>}
      <section className="space-y-4" aria-label="Your agents' questions">
        {items === null ? <p className="subtle text-sm" aria-live="polite">Checking your tasks…</p>
          : items.length === 0 ? (
            <div className="card settle flex flex-col items-start gap-3 p-6">
              <Stamp kind="ran">All clear</Stamp>
              <p className="m-0 text-[16px]">Nothing is waiting for you. Anything outside your work orders was handled without bothering you.</p>
              <Link className="text-link" to="/tasks">See your tasks</Link>
            </div>)
          : items.map((it) => (
            <div key={it.a.id} className="space-y-2">
              <p className="subtle m-0 flex flex-wrap items-center gap-2 text-[13px]"><span className="mono">{it.wf}</span>·<span>{it.business}</span>·<span>{ago(it.a.created)}</span>·
                <Link className="text-link" to={`/tasks?task=${encodeURIComponent(it.wf)}`}>Open task</Link></p>
              <p className="words m-0 text-[17px]">“{it.request}”</p>
              <ApprovalCard a={it.a} explain={it.explain} busy={busy === it.a.id} onDecide={async (ok) => {
                setBusy(it.a.id);
                try {
                  await tasks.decide(employee.key, it.wf, it.a.id, ok);
                  setDone((d) => [{ id: it.a.id, approved: ok, what: it.a.tool.replaceAll("_", " ") }, ...d].slice(0, 5));
                  setItems((xs) => (xs ?? []).filter((x) => x.a.id !== it.a.id));
                } catch (e) { setError((e as Error).message); } finally { setBusy(null); }
              }} />
            </div>))}
        {done.length > 0 && <ul className="m-0 list-none space-y-2 p-0" aria-live="polite">{done.map((d) => (
          <li key={d.id} className="settle flex items-center gap-3 text-[14px]">{d.approved ? <Stamp kind="ran" live>Approved</Stamp> : <Stamp kind="quiet" live>Declined</Stamp>}<span className="subtle">{d.what} · {d.id}</span></li>))}</ul>}
      </section>
      <details className="card p-5">
        <summary className="cursor-pointer text-[15px] font-semibold">Security reviewers' queue <span className="subtle font-normal">· content the filter held for a person to look at</span></summary>
        <div className="mt-4"><Approvals /></div>
      </details>
      {items !== null && items.length === 0 && <Empty>Tip: questions also appear on the task itself, and the sidebar shows how many are waiting.</Empty>}
    </div>
  );
}
