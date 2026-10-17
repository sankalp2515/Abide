import { useState } from "react";
import type { EventRow } from "../api";
import { DecisionBadge, Empty, Tag } from "./ui";

const TIER = ["Tier 0 · structure and rules", "Tier 1 · local classifier", "Tier 2 · AI judge"];

function sourceLabel(e: EventRow): string {
  if (e.kind === "action") return `Tool call · ${e.action?.tool ?? e.source.replace("tool:", "")}`;
  if (e.source === "user") return "User message";
  return `Content in · ${e.source.replace("tool:", "")}`;
}

export function Timeline({ events }: { events: EventRow[] }) {
  const [open, setOpen] = useState<string | null>(null);
  if (!events.length) return <Empty>No events recorded for this session yet.</Empty>;
  return (
    <ol className="m-0 list-none border-t p-0 hairline" aria-label="Session timeline">
      {events.map((e) => {
        const isOpen = open === e.id;
        const notable = e.decision && e.decision !== "ALLOW";
        return (
          <li key={e.id} className="border-b hairline">
            <button
              className="flex min-h-[52px] w-full flex-wrap items-center gap-3 bg-transparent py-3 text-left"
              style={{ color: "var(--text)", border: 0, cursor: "pointer" }}
              aria-expanded={isOpen}
              onClick={() => setOpen(isOpen ? null : e.id)}
            >
              <span className="mono subtle w-7 shrink-0">{String(e.seq).padStart(2, "0")}</span>
              <span className="text-[13px]" style={notable ? { color: "var(--text)" } : { color: "var(--muted)" }}>{sourceLabel(e)}</span>
              <DecisionBadge decision={e.decision} />
              {e.decision !== e.enforced && e.enforced && <Tag>monitor mode: not enforced</Tag>}
              {e.degraded ? <Tag tone="var(--neutralise)">degraded</Tag> : null}
              <span className="subtle ml-auto text-[11px]">
                {e.policy_rule && e.policy_rule !== "allow" ? <span className="mono" style={{ color: "var(--accent-soft)" }}>rule: {e.policy_rule}</span> : null}
                {e.latency_ms !== null ? ` · ${Math.round(e.latency_ms)} ms` : ""}
              </span>
              <span aria-hidden className="subtle w-4 text-center">{isOpen ? "−" : "+"}</span>
            </button>
            {isOpen && (
              <div className="grid gap-5 pb-5 pl-0 text-[13px] sm:pl-10 md:grid-cols-2">
                <div className="space-y-3">
                  {e.reason && <p className="m-0"><span className="section-label block pb-1">Reason</span>{e.reason}</p>}
                  {e.labels?.length && e.decision !== "ALLOW" ? (
                    <div><span className="section-label block pb-2">Attack types</span><div className="flex flex-wrap gap-1.5">{e.labels.map((l) => <Tag key={l}>{l}</Tag>)}</div></div>
                  ) : null}
                  {e.tier !== null && <p className="m-0"><span className="section-label block pb-1">Deepest check</span>{TIER[e.tier] ?? e.tier}</p>}
                  {e.session_risk !== null && <p className="m-0"><span className="section-label block pb-1">Session risk after</span><span className="num">{e.session_risk.toFixed(2)}</span></p>}
                </div>
                <div className="space-y-3">
                  {e.action && (
                    <>
                      <div><span className="section-label block pb-2">Requested arguments (redacted)</span>
                        <pre className="mono well m-0 overflow-x-auto whitespace-pre-wrap break-words p-3">{e.action.args_redacted}</pre></div>
                      {e.action.pinned_overrides && (
                        <div><span className="section-label block pb-2">Pinned fields rewritten from trusted context</span>
                          <pre className="mono well m-0 overflow-x-auto whitespace-pre-wrap break-words p-3">{e.action.pinned_overrides}</pre></div>
                      )}
                    </>
                  )}
                  {e.findings?.length ? (
                    <div>
                      <span className="section-label block pb-2">Findings ({e.findings.length})</span>
                      <ul className="m-0 list-none space-y-2 p-0">
                        {e.findings.slice(0, 12).map((f, i) => (
                          <li key={i} className="well p-3" style={{ borderLeft: "2px solid var(--accent-soft)" }}>
                            <div className="subtle flex flex-wrap items-center gap-2 text-[11px]">
                              <span className="mono" style={{ color: "var(--accent-text)" }}>{f.signal}</span>
                              {f.origin && <span>{f.origin}</span>}
                              {f.span_start !== null && <span className="mono">span {f.span_start}–{f.span_end}</span>}
                              {f.score ? <span>score {f.score.toFixed(2)}</span> : null}
                            </div>
                            {f.span_preview && <p className="mono m-0 mt-2 break-words">{f.span_preview}</p>}
                          </li>
                        ))}
                      </ul>
                    </div>
                  ) : e.preview ? <p className="mono subtle m-0 break-words">{e.preview}</p> : null}
                </div>
              </div>
            )}
          </li>
        );
      })}
    </ol>
  );
}
