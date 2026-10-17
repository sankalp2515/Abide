import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { admin, AGENT, api, type Policy as PolicyT } from "../api";
import { useReviewer } from "../reviewer";
import { ReviewerSignIn } from "../components/ReviewerSignIn";
import { Card, CardTitle, ErrorNote, Loading, PageHeader, Stat, Tag, useAsync } from "../components/ui";

const RISK_TONE: Record<string, string> = { low: "var(--allow)", medium: "var(--neutralise)", high: "var(--block)" };

function Editor({ policy, onSaved }: { policy: PolicyT; onSaved: () => void }) {
  const reviewer = useReviewer();
  const [text, setText] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  if (!reviewer) {
    return <div className="subtle flex flex-wrap items-center gap-3 text-xs"><span>Editing needs a reviewer key. Saves are validated, versioned and written atomically.</span><ReviewerSignIn /></div>;
  }
  async function save(body: unknown) {
    setBusy(true);
    setMsg(null);
    try {
      const r = await admin.putPolicy(reviewer!.key, policy.agent, body);
      setMsg({ ok: true, text: `Saved as version ${r.version} (${r.mode}).` });
      setText(null);
      onSaved();
    } catch (e) {
      setMsg({ ok: false, text: (e as Error).message });
    } finally {
      setBusy(false);
    }
  }
  const nextMode = policy.mode === "enforce" ? "monitor" : "enforce";
  return (
    <Card>
      <CardTitle meta={`Signed in as ${reviewer.name}`}>Edit policy</CardTitle>
      <div className="flex flex-wrap gap-3">
        <button className="pill pill-sm" disabled={busy}
          onClick={() => window.confirm(`Switch ${policy.agent} to ${nextMode} mode?`) && save({ ...policy, mode: nextMode })}>
          Switch to {nextMode}
        </button>
        <button className="pill pill-sm" disabled={busy} onClick={() => setText(text === null ? JSON.stringify(policy, null, 2) : null)}>
          {text === null ? "Edit as JSON" : "Close editor"}
        </button>
      </div>
      {text !== null && (
        <div className="mt-5 flex flex-col gap-3">
          <label className="sr-only" htmlFor="policy-json">Policy JSON</label>
          <textarea id="policy-json" className="input mono min-h-[360px] py-3" spellCheck={false} value={text} onChange={(e) => setText(e.target.value)} />
          <div className="flex gap-3">
            <button className="pill pill-primary pill-sm" disabled={busy} onClick={() => {
              try { save(JSON.parse(text)); } catch { setMsg({ ok: false, text: "That is not valid JSON." }); }
            }}>{busy ? "Saving…" : "Validate and save"}</button>
          </div>
        </div>
      )}
      {msg && <p role={msg.ok ? "status" : "alert"} className="mt-4 text-xs" style={{ color: msg.ok ? "var(--allow)" : "var(--block)" }}>{msg.text}</p>}
    </Card>
  );
}

export function Policy() {
  const [params] = useSearchParams();
  const [agent, setAgent] = useState(params.get("agent") || AGENT);
  const [input, setInput] = useState(params.get("agent") || AGENT);
  const { data: p, error, loading, reload } = useAsync(() => api.policy(agent), [agent]);
  return (
    <div className="space-y-8">
      <PageHeader index="06" label="Policy" title={<>The rules that<br />decide.</>}
        lede="Deterministic, per agent, versioned. AI signals feed these rules; they never override them."
        actions={
          <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); setAgent(input.trim() || AGENT); }}>
            <label className="sr-only" htmlFor="agent">Agent</label>
            <input id="agent" className="input mono" value={input} onChange={(e) => setInput(e.target.value)} />
            <button className="pill">Load</button>
          </form>
        } />
      {loading && <Loading />}
      {error && <ErrorNote>{error}</ErrorNote>}
      {p && (
        <>
          <Card>
            <div className="grid grid-cols-2 gap-8 md:grid-cols-4">
              <Stat value={p.mode === "enforce" ? "Enforce" : "Monitor"} label={p.mode === "enforce" ? "Decisions are applied" : "Decisions are logged, not applied"} tone={p.mode === "enforce" ? "var(--allow)" : "var(--neutralise)"} />
              <Stat value={`v${p.version}`} label="Policy version" />
              <Stat value={p.unknown_tools === "block" ? "Blocked" : "Allowed"} label="Tools not listed below" />
              <Stat value={p.thresholds.judge_untrusted === "always" ? "Always" : "On signal"} label="AI judge on third-party documents" />
            </div>
            <p className="lede mt-7 text-[13px]"><span className="section-label block pb-2">Task scope</span>{p.task_scope || "—"}</p>
          </Card>
          <Card>
            <CardTitle meta="Every call is checked before it runs.">Tools</CardTitle>
            <div className="overflow-x-auto">
              <table className="table min-w-[720px]">
                <thead><tr><th>Tool</th><th>Risk</th><th>If the check fails</th><th>Boundaries</th></tr></thead>
                <tbody>
                  {Object.entries(p.tools).map(([name, t]) => (
                    <tr key={name}>
                      <td className="mono">{name}</td>
                      <td><Tag tone={RISK_TONE[t.risk]}>{t.risk}</Tag></td>
                      <td className="text-xs">{t.failure === "closed" ? "Blocked (fail closed)" : "Allowed and logged (fail open)"}</td>
                      <td className="text-xs leading-6">
                        {t.pinned.length > 0 && <span className="block">Pinned from the signed-in user: <span className="mono">{t.pinned.join(", ")}</span></span>}
                        {t.allowed_recipients.length > 0 && <span className="block">Recipients only: <span className="mono">{t.allowed_recipients.join(", ")}</span></span>}
                        {t.allowed_domains.length > 0 && <span className="block">Domains only: <span className="mono">{t.allowed_domains.join(", ")}</span></span>}
                        {t.escalate_if_uncertain && <span className="block">Asks a human when the target came from untrusted content</span>}
                        {!t.pinned.length && !t.allowed_recipients.length && !t.allowed_domains.length && !t.escalate_if_uncertain && <span className="subtle">Allow-listed tool</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
          <div className="grid gap-8 md:grid-cols-2">
            <Card>
              <CardTitle>Content thresholds</CardTitle>
              <ul className="m-0 list-none space-y-3 p-0 text-[13px]">
                <li>Neutralise offending spans at risk <span className="num" style={{ color: "var(--accent-soft)" }}>≥ {p.thresholds.neutralise}</span></li>
                <li>Block the content at risk <span className="num" style={{ color: "var(--accent-soft)" }}>≥ {p.thresholds.block}</span></li>
                <li>Hidden (non-visible) text: {p.sensitive_data.drop_non_visible ? "always quarantined" : "kept"}</li>
                <li>Protected data patterns: {p.sensitive_data.patterns.join(", ")}</li>
              </ul>
            </Card>
            <Card>
              <CardTitle>Session and budget</CardTitle>
              <ul className="m-0 list-none space-y-3 p-0 text-[13px]">
                <li>Ask a human at session risk <span className="num" style={{ color: "var(--accent-soft)" }}>≥ {p.session.escalate_at}</span>, block at <span className="num" style={{ color: "var(--accent-soft)" }}>≥ {p.session.block_at}</span></li>
                <li>Risk fades by {p.session.risk_decay_per_turn} per turn</li>
                <li>AI judge budget: {p.budgets.daily_tokens_tier2 ? `${p.budgets.daily_tokens_tier2.toLocaleString()} tokens a day; at the cap rules continue and flagged content is held` : "no daily cap"}</li>
                <li>Judge: <span className="mono">{p.decision_model}</span></li>
              </ul>
            </Card>
          </div>
          <Editor policy={p} onSaved={reload} />
        </>
      )}
    </div>
  );
}
