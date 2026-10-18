import { useEffect, useState } from "react";
import { Link, useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { tasks, type DTaskRow } from "../api";
import { signOutEmployee, useEmployee } from "../employee";
import { Empty, ErrorNote, PageHeader } from "../components/ui";
import { TaskDetail, TaskRow } from "./Agents";
import { Delegate, KeyGate } from "./Delegate";

/* Tasks: one list for everything you handed off (sandbox runs and your own agents), and one view for a task:
   work order, steps, questions, receipt. "New task" opens the sandbox composer in the same place. */

type Show = "all" | "sandbox" | "api";
const SHOW: { id: Show; label: string }[] = [{ id: "all", label: "All" }, { id: "sandbox", label: "Sandbox" }, { id: "api", label: "Your agents" }];

export function Tasks() {
  const employee = useEmployee();
  const { pathname } = useLocation();
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const [rows, setRows] = useState<DTaskRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [missing, setMissing] = useState<Set<string>>(new Set());
  const [started, setStarted] = useState<Set<string>>(new Set());   // sandbox tasks started here, before the list knows them
  const show = (params.get("show") as Show) || (pathname === "/agents" ? "api" : "all");
  // `?task=<id>` opens a task; `/delegate` (or ?task=new) opens the composer
  const selected = params.get("task") ?? (pathname === "/delegate" ? "new" : null);

  useEffect(() => {
    if (!employee) return;
    let alive = true;
    const load = () => tasks.list(employee.key).then((r) => { if (alive) { setRows(r); setError(null); } })
      .catch((e) => alive && setError((e as Error).message));
    load();
    const timer = setInterval(() => { if (!document.hidden) load(); }, 4000);
    return () => { alive = false; clearInterval(timer); };
  }, [employee?.key]);

  const header = <PageHeader label="Tasks" title="Everything you handed off."
    lede="Sandbox runs and your own agents' tasks, in one list. Open one to see its work order, every step, any question waiting for you, and its receipt."
    actions={employee ? <span className="flex flex-wrap items-center gap-2 text-xs md:justify-end"><span className="tag">Signed in · {employee.name}</span><button className="pill pill-sm" onClick={signOutEmployee}>Sign out</button></span> : undefined} />;
  if (!employee) return <div className="space-y-8">{header}<KeyGate /></div>;

  const open = (task: string | null) => {
    const next = new URLSearchParams(params);
    if (task) next.set("task", task); else next.delete("task");
    if (pathname !== "/tasks") navigate({ pathname: "/tasks", search: next.toString() });
    else setParams(next, { replace: false });
  };
  const setShow = (s: Show) => { const next = new URLSearchParams(params); next.set("show", s); setParams(next); };
  const shown = (rows ?? []).filter((r) => show === "all" || r.source === show || (show === "sandbox" && r.source === "demo"));
  const current = selected && selected !== "new" ? rows?.find((r) => r.id === selected) : undefined;
  const isSandbox = selected === "new" || (selected !== null && started.has(selected) && !missing.has(selected))
    || (current ? current.source === "demo" && !missing.has(current.id) : false);
  const counts = { all: rows?.length ?? 0, sandbox: rows?.filter((r) => r.source === "demo").length ?? 0, api: rows?.filter((r) => r.source === "api").length ?? 0 };

  return (
    <div className="space-y-8">
      {header}
      <div className="grid items-start gap-6 lg:grid-cols-[300px_minmax(0,1fr)]">
        <section className={`card overflow-hidden lg:sticky lg:top-6 ${selected ? "order-2 lg:order-none" : ""}`} aria-labelledby="task-list">
          <div className="space-y-3 p-4">
            <div className="flex items-center justify-between gap-2">
              <h2 id="task-list" className="h3">Your tasks</h2>
              <button className={`pill pill-sm ${selected === "new" ? "" : "pill-primary"}`} onClick={() => open("new")}>New task</button>
            </div>
            <div className="flex flex-wrap gap-1" role="radiogroup" aria-label="Which tasks">
              {SHOW.map((s) => <button key={s.id} role="radio" aria-checked={show === s.id} className={`pill pill-sm ${show === s.id ? "pill-primary" : ""}`} style={{ minHeight: 32, padding: "0 10px" }}
                onClick={() => setShow(s.id)}>{s.label} <span className="mono" style={{ opacity: 0.75 }}>{counts[s.id]}</span></button>)}
            </div>
          </div>
          {error && <div className="px-4 pb-3"><ErrorNote>{error}</ErrorNote></div>}
          {rows === null ? <p className="subtle px-4 pb-4 text-sm" aria-live="polite">Loading your tasks…</p>
            : shown.length === 0 ? <div className="px-4 pb-4"><Empty>{show === "api" ? <>No tasks from your own agents yet. <Link className="text-link" to="/connect">Connect one</Link>.</> : "No tasks yet. Start one with New task."}</Empty></div>
            : <ul className="m-0 max-h-[68vh] list-none overflow-y-auto p-0">{shown.map((t, i) => (
                <div key={t.id} className="settle" style={{ animationDelay: `${Math.min(i, 8) * 35}ms` }}><TaskRow t={t} active={t.id === selected} onOpen={() => open(t.id)} /></div>
              ))}</ul>}
        </section>

        <div className="min-w-0">
          {!selected ? <Empty>Choose a task on the left, or start a <button className="text-link" onClick={() => open("new")}>new task</button>.</Empty>
            : !isSandbox && !current && rows === null ? <p className="subtle text-sm" aria-live="polite">Opening the task…</p>
            : isSandbox ? <Delegate embedded open={selected === "new" ? undefined : selected} onOpened={(wf) => { setStarted((m) => new Set(m).add(wf)); open(wf); }} onNew={() => open("new")}
                onMissing={(wf) => setMissing((m) => new Set(m).add(wf))} />
            : <div className="space-y-4">
                {current && <div><p className="section-label mb-1">{current.business} · {current.source === "api" ? "your agent" : "sandbox (live view ended; the record is below)"}</p>
                  <h2 className="h2" style={{ fontSize: "clamp(26px, 2.6vw, 36px)" }}>{current.status === "draft" ? "Check the work order" : current.waiting ? "Waits for you" : "Your agent's task"}</h2></div>}
                <TaskDetail apiKey={employee.key} wf={selected} />
              </div>}
        </div>
      </div>
    </div>
  );
}
