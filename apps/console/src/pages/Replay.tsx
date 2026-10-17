import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, type EventRow } from "../api";
import { Timeline } from "../components/Timeline";
import { InvestigationPanel } from "../components/Investigation";
import { ProvenancePanel } from "../components/Provenance";
import { Card, Empty, ErrorNote, PageHeader, Stat } from "../components/ui";

export function Replay() {
  const [params, setParams] = useSearchParams();
  const [sessionId, setSessionId] = useState(params.get("session") ?? "");
  const [events, setEvents] = useState<EventRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function load(id: string) {
    if (!id) return;
    setLoading(true);
    setError(null);
    try {
      setEvents(await api.events(id));
      setParams({ session: id });
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    const id = params.get("session");
    if (id) load(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const risk = events?.reduce((m, e) => Math.max(m, e.session_risk ?? 0), 0) ?? 0;
  const stopped = events?.filter((e) => e.decision && e.decision !== "ALLOW").length ?? 0;
  const actions = events?.filter((e) => e.kind === "action").length ?? 0;

  return (
    <div className="space-y-10">
      <PageHeader index="02" label="Prove" title={<>Leave nothing<br />unexplained.</>}
        lede="Replay a session end to end: what came in, what was found, which rule decided, and what the agent was allowed to do." />
      <Card>
        <form className="flex flex-col gap-3 sm:flex-row" onSubmit={(e) => { e.preventDefault(); load(sessionId.trim()); }}>
          <label className="sr-only" htmlFor="sid">Session ID</label>
          <input id="sid" className="input mono min-w-0 flex-1" placeholder="Session ID (from the Decisions feed)" value={sessionId} onChange={(e) => setSessionId(e.target.value)} />
          <button className="pill pill-primary" disabled={!sessionId.trim() || loading}>{loading ? "Loading…" : "Replay session"}</button>
        </form>
        {error && <div className="mt-4"><ErrorNote>{error}</ErrorNote></div>}
      </Card>
      {events === null ? (
        <Empty>Enter a session ID to replay it. Every decision in the Decisions feed links here.</Empty>
      ) : (
        <Card>
          <div className="mb-7 grid grid-cols-2 gap-6 sm:grid-cols-4">
            <Stat value={events.length} label="Events in order" />
            <Stat value={actions} label="Tool calls checked" />
            <Stat value={stopped} label="Decisions other than allow" tone={stopped ? "var(--block)" : undefined} />
            <Stat value={risk.toFixed(2)} label="Peak session risk" />
          </div>
          <Timeline events={events} />
        </Card>
      )}
      {events !== null && events.length > 0 && params.get("session") && <ProvenancePanel sessionId={params.get("session")!} />}
      {events !== null && events.length > 0 && params.get("session") && <InvestigationPanel sessionId={params.get("session")!} />}
    </div>
  );
}
