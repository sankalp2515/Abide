import { useState } from "react";
import { Link } from "react-router-dom";
import { admin, api, PRODUCT_NAME, type Approval } from "../api";
import { useReviewer } from "../reviewer";
import { ReviewerSignIn } from "../components/ReviewerSignIn";
import { ago, Card, CardTitle, Empty, ErrorNote, Loading, PageHeader, Stat, Tag, useAsync } from "../components/ui";

/** The Investigator's recommendation for a pending approval (sessions waiting for a human are triaged automatically). */
function Triage({ sessionId }: { sessionId: string }) {
  const { data } = useAsync(() => api.investigations(sessionId), [sessionId]);
  const r = data?.[0]?.report;
  if (!r) return <p className="subtle m-0 mt-3 text-xs">Investigator: no report yet for this session.</p>;
  return (
    <div className="well mt-3 p-3 text-[13px]">
      <span className="section-label block pb-1">Investigator · {r.verdict.replace("_", " ")} · severity {r.severity}</span>
      <p className="m-0">{r.approval_recommendation || r.summary}</p>
      <Link className="text-link text-xs" to={`/replay?session=${sessionId}`}>Full report</Link>
    </div>
  );
}

const STATUSES = ["pending", "approved", "denied"] as const;

function Decide({ a, onDone }: { a: Approval; onDone: () => void }) {
  const reviewer = useReviewer();
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  if (!reviewer || a.status !== "pending") return null;
  async function decide(approve: boolean) {
    setBusy(true);
    setErr(null);
    try {
      await admin.resolve(reviewer!.key, a.event_id, approve, note);
      onDone();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="mt-4 flex flex-col gap-3">
      <label className="flex flex-col gap-2 text-xs"><span className="section-label">Note (optional)</span>
        <input className="input" value={note} onChange={(e) => setNote(e.target.value)} placeholder="Why you approved or denied" /></label>
      <div className="flex flex-wrap gap-2">
        <button className="pill pill-primary pill-sm" disabled={busy} onClick={() => decide(true)}>Approve once</button>
        <button className="pill pill-sm" disabled={busy} onClick={() => decide(false)}>Deny</button>
      </div>
      {err && <ErrorNote>{err}</ErrorNote>}
    </div>
  );
}

function Approvals() {
  const [status, setStatus] = useState<(typeof STATUSES)[number]>("pending");
  const reviewer = useReviewer();
  const { data, error, loading, reload } = useAsync(() => api.approvals(status), [status]);
  return (
    <Card>
      <CardTitle meta="An approval lets the same call in the same session run once. It can never override a block.">Approvals inbox</CardTitle>
      <div className="mb-5 flex flex-wrap gap-2" role="group" aria-label="Approval status">
        {STATUSES.map((s) => (
          <button key={s} aria-pressed={status === s} className={`pill pill-sm ${status === s ? "pill-primary" : ""}`} onClick={() => setStatus(s)}>
            {s[0].toUpperCase() + s.slice(1)}
          </button>
        ))}
      </div>
      {loading && <Loading />}
      {error && <ErrorNote>{error}</ErrorNote>}
      {data && data.length === 0 && <Empty>No {status} approvals.</Empty>}
      {data && data.length > 0 && (
        <ul className="m-0 list-none border-t p-0 hairline">
          {data.map((a) => (
            <li key={a.event_id} className="grid gap-3 border-b py-5 hairline md:grid-cols-[1fr_1.2fr]">
              <div>
                <p className="m-0 text-[15px]">The agent wants to use <span className="mono" style={{ color: "var(--accent-text)" }}>{a.tool}</span></p>
                <p className="subtle m-0 mt-1 text-xs">{a.agent_id} · {ago(a.created_at)} · <Link className="text-link" to={`/replay?session=${a.session_id}`}>session</Link></p>
                <p className="m-0 mt-3 text-[13px]">{a.reason}</p>
                <div className="mt-3 flex flex-wrap gap-1.5">{a.labels.map((l) => <Tag key={l}>{l}</Tag>)}</div>
                {a.status === "pending" && <Triage sessionId={a.session_id} />}
              </div>
              <div>
                <span className="section-label block pb-2">Exact request (redacted)</span>
                <pre className="mono well m-0 whitespace-pre-wrap break-words p-3">{a.args_redacted ?? "—"}</pre>
                {a.status !== "pending" && (
                  <p className="subtle m-0 mt-2 text-xs">{a.status} by {a.reviewer ?? "—"}{a.note ? ` · “${a.note}”` : ""}{a.used_at ? " · used once" : ""}</p>
                )}
                <Decide a={a} onDone={reload} />
              </div>
            </li>
          ))}
        </ul>
      )}
      {!reviewer && (
        <div className="subtle mt-5 flex flex-wrap items-center gap-3 text-xs leading-6">
          <span>View-only: anyone can see this inbox. Approving or denying is for reviewers (local Docker key: <span className="mono">local-admin-key</span>).</span><ReviewerSignIn />
        </div>
      )}
    </Card>
  );
}

function Memory() {
  const reviewer = useReviewer();
  const { data, error, loading, reload } = useAsync(() => api.memory(), []);
  const [err, setErr] = useState<string | null>(null);
  async function forget(id: string) {
    if (!reviewer || !window.confirm("Forget this entry? Matching content will go through the normal checks again.")) return;
    setErr(null);
    try {
      await admin.forget(reviewer.key, id);
      reload();
    } catch (e) {
      setErr((e as Error).message);
    }
  }
  return (
    <Card>
      <CardTitle meta="Stored as fingerprints only, never as text.">Learned from review</CardTitle>
      {loading && <Loading />}
      {error && <ErrorNote>{error}</ErrorNote>}
      {data && (
        <>
          <div className="mb-6 flex flex-wrap gap-10">
            <Stat value={data.stats.attack ?? 0} label="Confirmed attacks: blocked by rules on sight, no AI call" />
            <Stat value={data.stats.benign ?? 0} label="Reviewed false alarms: exact content, per agent" />
          </div>
          {err && <div className="mb-4"><ErrorNote>{err}</ErrorNote></div>}
          {data.entries.length === 0 ? <Empty>Nothing confirmed yet. Signed-in reviewers can confirm attacks or false alarms from the Decisions page.</Empty> : (
            <div className="overflow-x-auto">
              <table className="table min-w-[560px]">
                <thead><tr><th>Kind</th><th>Labels</th><th>Agent</th><th>Origin</th><th>Added</th>{reviewer && <th><span className="sr-only">Actions</span></th>}</tr></thead>
                <tbody>
                  {data.entries.map((m) => (
                    <tr key={m.id}>
                      <td><Tag tone={m.kind === "attack" ? "var(--block)" : "var(--allow)"}>{m.kind === "attack" ? "confirmed attack" : "false alarm"}</Tag></td>
                      <td><div className="flex flex-wrap gap-1.5">{m.labels.map((l) => <Tag key={l}>{l}</Tag>)}</div></td>
                      <td className="text-xs">{m.agent_id === "*" ? "all agents" : m.agent_id}</td>
                      <td className="subtle text-xs">{m.origin}</td>
                      <td className="subtle text-xs">{ago(m.created_at)}</td>
                      {reviewer && <td><button className="pill pill-sm" onClick={() => forget(m.id)}>Forget</button></td>}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </Card>
  );
}

export function Review() {
  return (
    <div className="space-y-8">
      <PageHeader index="05" label="Human review" title={<>People decide<br />the hard cases.</>}
        lede={`${PRODUCT_NAME} asks humans rarely and with a clear reason. Confirmed attacks and false alarms make the next decision better.`} />
      <Approvals />
      <Memory />
    </div>
  );
}
