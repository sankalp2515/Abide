// Typed client for the Abide Gateway API. Uses the public read-only key (never an admin key in the browser).

export const API_BASE: string = (import.meta.env.VITE_API_BASE as string | undefined) ?? "http://localhost:8000";
/** The service address an outside agent should call: the API base, or this page's origin when served together. */
export const serviceOrigin = (): string => (API_BASE && /^https?:/.test(API_BASE) ? API_BASE.replace(/\/$/, "") : typeof window !== "undefined" ? window.location.origin : "");
const API_KEY: string = (import.meta.env.VITE_API_KEY as string | undefined) ?? "";
export const PRODUCT_NAME: string = (import.meta.env.VITE_PRODUCT_NAME as string | undefined) ?? "Abide";
export const TAGLINE = "Hand off the task. Approve it once.";
export const AGENT = "delegate-workspace";

/** Display names for the agents this deployment ships with; any other agent shows its id. */
const AGENT_NAMES: Record<string, string> = {
  "delegate-workspace": "Delegate content filter",
};
export const agentName = (id: string) => AGENT_NAMES[id] ?? id;

export type Decision = "ALLOW" | "NEUTRALISE" | "BLOCK" | "ESCALATE";

export interface Scenario {
  id: string;
  title: string;
  attack_types?: string[];
  channel?: string;
  task?: string;
  expected?: Record<string, string>;
  surface?: "proxy";
  source?: string;
}

export interface Step {
  tool: string;
  args: Record<string, unknown>;
  allowed: boolean;
  notice: string | null;
}

export interface FindingRow {
  span_start: number | null;
  span_end: number | null;
  span_preview: string;
  origin: string;
  signal: string;
  labels: string[];
  score: number;
  tier: number;
}

export interface EventRow {
  id: string;
  seq: number;
  kind: string;
  source: string;
  preview: string;
  created_at: number;
  decision: Decision | null;
  enforced: Decision | null;
  policy_rule: string | null;
  reason: string | null;
  labels: string[];
  tier: number | null;
  latency_ms: number | null;
  degraded: number | null;
  session_risk: number | null;
  findings: FindingRow[];
  action: { tool: string; args_redacted: string; final_args_redacted: string | null; pinned_overrides: string | null;
            contract?: ContractRuling | null; receipt?: { executed: boolean; detail: string; at: number } | null } | null;
}

export interface ContractClause { id: string; effect: "permit" | "prohibit" | "require" | "policy"; capability: string; quote: string; origin: string }
export interface Contract {
  agent_id: string; session_id: string; version: number; hash: string; parent: string; origin: string; created_at: number;
  clauses: ContractClause[]; targets: string[];
}
export interface ContractRuling { version: number; hash: string; effect: string; capability: string | null; clause: ContractClause | null; rule: string }

export interface ChainSource {
  event_id: string; seq: number; source: string; decision: Decision | null; rule: string | null; reason: string | null;
  labels: string[]; preview: string; evidence: { signal: string; origin: string; span: string; score: number | null }[];
  link: string; proven: boolean; arg?: string;
}
export interface ProvenanceChain {
  kind: "action" | "content";
  sources: ChainSource[];
  action: { event_id: string; seq: number; tool: string; args: string; final_args: string | null; pinned: string | null } | null;
  control: { decision: Decision | null; rule: string | null; reason: string | null; labels: string[]; layer: string; contract: ContractRuling | null };
  outcome: { state: "executed" | "not-executed" | "allowed" | "quarantined" | "held" | "contradiction"; enforced: Decision | null; evidence: string[] };
}
export interface Provenance {
  chains: ProvenanceChain[]; contracts: Contract[];
  summary: { actions_stopped: number; content_stopped: number; executed_with_untrusted_link: number; contradictions: number };
}

export interface DemoResult {
  scenario_id: string;
  protection: "on" | "off";
  surface?: "proxy";
  agent_id?: string;
  answer: string;
  error: string | null;
  model?: string | null;
  steps?: Step[];
  outbox: Record<string, string>[];
  canary_leaked: boolean;
  external_sends: number;
  session_id: string | null;
  events: EventRow[];
}

export interface DecisionRow {
  event_id: string;
  agent_id: string;
  session_id: string;
  decision: Decision;
  enforced: Decision;
  policy_rule: string;
  reason: string;
  labels: string[];
  tier: number;
  latency_ms: number;
  degraded: number;
  created_at: number;
  source: string;
  kind: string;
  preview: string;
}

export interface Rate { n: number; detected?: number; recall: number | null }
export interface AsrRow { n: number; scored: number; compromised: number; asr: number | null }
export interface Fpr {
  n: number; held: number; false_alarms: number; fpr: number | null; disruptive: number; fpr_disruptive: number | null;
  escalation_rate: number | null; alarms: string[];
}
export interface Overhead {
  runs: number; agent_tokens: number; security_tokens: number; token_ratio: number | null;
  agent_usd: number; security_usd: number; cost_ratio: number | null;
}
export interface E2E {
  suite: string; models: string[]; off: AsrRow; on: AsrRow; relative_reduction: number | null; canary_leaks_on: number;
  caught: { n: number; caught: number; recall: number | null };
  overhead?: Overhead | null;
}
export interface CostRow {
  role: string; provider: string; model: string; calls: number; cached: number | null; input_tokens: number | null;
  output_tokens: number | null; est_cost_usd: number | null; avg_latency_ms: number | null; errors?: number | null;
}
export interface Costs {
  days: number; by_model: CostRow[];
  overhead: { security_tokens: number; agent_tokens: number; token_ratio: number | null; cost_ratio: number | null; unpriced_models: string[] };
}
export interface Summary {
  kind: "summary";
  file: string;
  date: string;
  git_sha: string | null;
  corpus_version: string;
  settings: { engine_versions: Record<string, Record<string, number>> };
  content_detection: {
    scored: number; held: number; recall: number | null; raw_recall_incl_not_delivered: number | null;
    not_delivered: Record<string, { n: number; detected: number; reason: string }>;
    by_type: Record<string, Rate>; by_channel: Record<string, Rate>;
    matrix: Record<string, Record<string, number | null>>; matrix_n: Record<string, Record<string, number>>;
  };
  fpr: { dev: Fpr | null; holdout: Fpr | null; all_channel_stress: { dev: Fpr | null; holdout: Fpr | null } };
  latency_isolated: { n: number; p50_ms: number; p95_ms: number; note: string } | null;
  e2e: E2E[];
  utility: { off: number; on: number; n: number; retention: number | null } | null;
  claim: {
    F: string; D: string; types_meeting: number; fpr_used: number | null; rule: string;
    types: { type: string; checkpoint: string; n: number; recall: number | null; status: string }[];
    text_channels: Record<string, boolean>; rich_channels: Record<string, boolean>;
  };
}

export interface Approval {
  event_id: string; session_id: string; agent_id: string; tool: string; reason: string; status: string;
  reviewer: string | null; note: string | null; created_at: number; resolved_at: number | null; used_at: number | null;
  args_redacted: string | null; policy_rule: string | null; labels: string[];
}

export interface MemoryEntry {
  id: string; kind: "attack" | "benign"; agent_id: string; labels: string[]; n: number; origin: string; note: string; created_at: number;
}

export interface ToolPolicy {
  risk: string; failure: string; pinned: string[]; allowed_recipients: string[]; allowed_domains: string[];
  escalate_if_uncertain: boolean;
}
export interface Policy {
  agent: string; mode: string; version: string; task_scope: string; unknown_tools: string; decision_model: string;
  tools: Record<string, ToolPolicy>;
  thresholds: { neutralise: number; block: number; escalate_band: [number, number]; judge_untrusted: string };
  session: { risk_decay_per_turn: number; block_at: number; escalate_at: number };
  sensitive_data: { patterns: string[]; drop_non_visible: boolean };
  budgets: { daily_tokens_tier2: number | null };
}

export interface Metrics {
  window_hours: number; decisions: number; by_decision: Record<string, number>; by_rule: Record<string, number>;
  by_label: Record<string, number>;
  inspections: { n: number; escalated_to_judge: number; escalation_rate: number | null; degraded: number };
  latency_ms: {
    fast_path: { n: number; p50: number | null; p95: number | null };
    judge_path: { n: number; p50: number | null; p95: number | null };
  };
  memory: Record<string, number> | null;
  approvals_pending: number;
  providers: { provider: string; status: string; n: number }[];
}

export interface AgentSummary {
  agent: string; org: string; mode: string; version: string; task_scope: string; tools: Record<string, string>;
  upstream: { provider: string | null; model: string | null; key: string };
}
export interface ProviderInfo { name: string; type: string; configured: boolean; base_url: string }
export interface PolicyTemplate { id: string; title: string; description: string; tools: Record<string, string>; mode: string }
export interface Integration { id: string; title: string; when: string; language: string; code: string }
export interface Connect {
  agent: string; gateway_url: string; mode: string; upstream: AgentSummary["upstream"]; integrations: Integration[]; next: string[];
}
export interface SelfTest {
  agent: string; mode: string; passed: boolean; note: string; ms: number;
  checks: { check: string; decision: Decision; rule: string; passed: boolean; session_id: string }[];
}
export interface NewAgent {
  agent: string; template: string; mode: "monitor" | "enforce"; task_scope?: string;
  upstream: { provider: string | null; model: string | null; key: "gateway" | "client" };
}

export interface InvestigationReport {
  verdict: "attack" | "likely_attack" | "false_alarm" | "inconclusive"; severity: "none" | "low" | "medium" | "high" | "critical";
  summary: string; attack_types?: string[]; entry_point?: string; objective: string; outcome: string;
  approval_recommendation?: string; next_steps?: string[]; confidence?: number;
  facts: { events: number; flagged: number; inbound_flagged: number; actions_attempted: number; actions_stopped: number;
           actions_run: number; attack_types: string[]; max_session_risk: number };
}
export interface InvestigationStep { step: "thought" | "tool" | "failover" | "error"; tool?: string; args?: Record<string, string>; ok?: boolean; detail?: string }
export interface Suggestion { id: string; kind: "memory" | "policy"; status: "pending" | "applied" | "dismissed"; payload: Record<string, string>; resolved_by?: string | null }
export interface Investigation {
  id: string; session_id: string; agent_id: string; trigger: string; status: "running" | "done" | "fallback"; model: string;
  created_at: number; finished_at: number | null; report: InvestigationReport | null; steps: InvestigationStep[]; suggestions: Suggestion[];
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function call<T>(path: string, init?: RequestInit, key: string = API_KEY): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", ...(key ? { Authorization: `Bearer ${key}` } : {}), ...(init?.headers ?? {}) },
    });
  } catch {
    throw new ApiError(0, "Backend unreachable");
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, String(detail));
  }
  return (await res.json()) as T;
}

export const api = {
  health: () => call<{ ok: boolean; uptime_s: number }>("/v1/health"),
  scenarios: () => call<Scenario[]>("/demo/scenarios"),
  run: (scenario_id: string, protection: "on" | "off") =>
    call<DemoResult>("/demo/run", { method: "POST", body: JSON.stringify({ scenario_id, protection }) }),
  events: (sessionId: string) => call<EventRow[]>(`/v1/sessions/${encodeURIComponent(sessionId)}/events`),
  decisions: (params: Record<string, string>) => {
    const q = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== ""));
    return call<DecisionRow[]>(`/v1/decisions?${q.toString()}`);
  },
  scorecard: (agent: string) =>
    call<{ agent: string; summary: Summary | null; reports: Record<string, unknown> }>(`/v1/scorecard/${agent}`),
  approvals: (status = "pending") => call<Approval[]>(`/v1/approvals?status=${status}`),
  memory: () => call<{ stats: Record<string, number>; entries: MemoryEntry[] }>("/v1/memory"),
  policy: (agent: string) => call<Policy>(`/v1/policies/${encodeURIComponent(agent)}`),
  metrics: (hours = 24) => call<Metrics>(`/v1/metrics?hours=${hours}`),
  insights: (days = 30) => call<Insights>(`/v1/insights?days=${days}`),
  costs: (days = 7) => call<Costs>(`/v1/costs?days=${days}`),
  whoami: (key: string) => call<{ name: string; scope: string }>("/v1/whoami", undefined, key),
  /** A private 7-day key for this visitor, when the deployment allows it (no sign-up). */
  trialKey: () => call<{ key: string; name: string; expires: number }>("/v1/trial-keys", { method: "POST" }, ""),
  agents: () => call<AgentSummary[]>("/v1/agents"),
  providers: () => call<ProviderInfo[]>("/v1/providers"),
  templates: () => call<PolicyTemplate[]>("/v1/setup/templates"),
  connect: (agent: string) => call<Connect>(`/v1/agents/${encodeURIComponent(agent)}/connect`),
  investigations: (sessionId: string) => call<Investigation[]>(`/v1/sessions/${encodeURIComponent(sessionId)}/investigations`),
  recentInvestigations: (limit = 50) => call<Investigation[]>(`/v1/investigations?limit=${limit}`),
  provenance: (sessionId: string) => call<Provenance>(`/v1/sessions/${encodeURIComponent(sessionId)}/provenance`),
  /** Read-only triage of one session; any proposal it makes still waits for a reviewer. */
  investigate: (sessionId: string) =>
    call<Investigation>(`/v1/sessions/${encodeURIComponent(sessionId)}/investigate`, { method: "POST" }),
};

/* ---- Abide -------------------------------------------------------------------- */
export interface DClause {
  id: string; effect: "permit" | "prohibit"; action: string; values: Record<string, string[]>; quote: string;
  origin: string; note: string; plain: string; verified?: boolean; superseded_by?: string | null;
}
export interface DContract {
  id: string; workflow_id: string; owner: string; request: string; version: number; hash: string; parent: string;
  status: "draft" | "confirmed"; compiler: string; clauses: DClause[]; questions: string[]; notes?: string[];
  rejected: { reason: string; quote?: string; value?: string; action?: string; effect?: string }[]; created_at: number;
}
export interface DReceipt {
  seq: number; rid: string; kind: string; agent: string; tool: string; decision: string; reason: string;
  data: Record<string, unknown>; created: number; prev: string; hash: string;
}
export interface DApproval {
  id: string; action_hash: string; contract_hash: string; tool: string; args: Record<string, unknown>; agent: string;
  reason: string; status: "pending" | "approved" | "denied" | "used" | "expired"; created: number;
}
type Row = Record<string, string | number>;
export interface Insights {
  configured?: boolean; table: string; days: number; engine: string; query_ms: number; workflows: number;
  decisions: Row[]; stopped_tools: Row[]; sources: Row[]; daily: Row[];
}
export interface DEvent { agent: string; tool: string; args: Record<string, unknown>; outcome: string; note: string; explain?: string }
export interface DReport { writer: string; sentences: { text: string; cites: string[] }[]; dropped: { text: string; reason: string }[]; chain_ok: boolean }
export interface DState {
  workflow: { id: string; owner: string; created: number; session_id?: string | null; workspace?: string };
  contract: DContract; versions: DContract[]; receipts: DReceipt[]; approvals: DApproval[];
  demo?: { domain?: string; attack: string; phase: string; firewall: boolean; events: DEvent[]; waiting: string[];
           agents?: "gemini" | "scripted"; agents_note?: string | null; error?: string | null; compromised?: boolean; custom_attack?: boolean;
           workspace: { drafts: Record<string, unknown>[]; sent: Record<string, unknown>[]; refunds?: Record<string, unknown>[]; executed: { tool: string; args: Record<string, unknown> }[] } };
  report?: DReport;
}

export interface DArm {
  attack_scenarios: number; attack_success_rate: number | null; unauthorized_actions_executed: number;
  benign_scenarios: number; benign_task_completed: number | null; questions_per_benign_task: number | null;
}
export interface DEval { created: string; method: string; arms: Record<string, DArm> }

export const delegate = {
  evaluation: () => call<DEval>("/v1/delegate-eval"),
  start: (key: string, request: string, attack: string, firewall: boolean, domain: "office" | "desk" = "office",
          compromised = false, attack_text = "") =>
    call<DState>("/v1/delegate-demo", { method: "POST", body: JSON.stringify({ request, attack, firewall, domain, compromised, attack_text }) }, key),
  get: (key: string, wf: string) => call<DState>(`/v1/delegate-demo/${encodeURIComponent(wf)}`, undefined, key),
  confirm: (key: string, wf: string, hash: string, accept: string[] = []) =>
    call<DState>(`/v1/delegate-demo/${encodeURIComponent(wf)}/confirm`, { method: "POST", body: JSON.stringify({ hash, accept }) }, key),
  say: (key: string, wf: string, text: string) =>
    call<DState>(`/v1/delegate-demo/${encodeURIComponent(wf)}/say`, { method: "POST", body: JSON.stringify({ text }) }, key),
  decide: (key: string, wf: string, approval: string, approve: boolean) =>
    call<{ status: string }>(`/v1/delegations/${encodeURIComponent(wf)}/approvals/${encodeURIComponent(approval)}`,
      { method: "POST", body: JSON.stringify({ approve }) }, key),
  list: (key: string) => call<DTaskRow[]>("/v1/delegations", undefined, key),
};

/** Tasks your own agents run through the API (the "Your agents" page): the same work order, approvals and receipt. */
export interface DTaskRow {
  id: string; owner: string; created: number; workspace: string; business: string; request: string;
  status: "draft" | "confirmed"; source: "demo" | "api"; allowed: number; stopped: number; asked: number; waiting: number;
}
const wfPath = (wf: string) => `/v1/delegations/${encodeURIComponent(wf)}`;
export const tasks = {
  list: (key: string) => call<DTaskRow[]>("/v1/delegations", undefined, key),
  state: (key: string, wf: string) => call<DState>(wfPath(wf), undefined, key),
  confirm: (key: string, wf: string, hash: string, accept: string[] = []) =>
    call<DContract>(`${wfPath(wf)}/confirm`, { method: "POST", body: JSON.stringify({ hash, accept }) }, key),
  amend: (key: string, wf: string, text: string) =>
    call<DContract>(`${wfPath(wf)}/amend`, { method: "POST", body: JSON.stringify({ text }) }, key),
  decide: (key: string, wf: string, approval: string, approve: boolean) =>
    call<{ status: string }>(`${wfPath(wf)}/approvals/${encodeURIComponent(approval)}`, { method: "POST", body: JSON.stringify({ approve }) }, key),
  report: (key: string, wf: string) => call<DReport>(`${wfPath(wf)}/report`, undefined, key),
  agents: (key: string) => call<DAgent[]>("/v1/my-agents", undefined, key),
  businesses: (key: string) => call<{ id: string; builtin: boolean; business?: string; description?: string }[]>("/v1/delegate-workspaces", undefined, key),
  business: (key: string, id: string) => call<{ id: string; builtin: boolean; policy: DBusiness }>(`/v1/delegate-workspaces/${encodeURIComponent(id)}`, undefined, key),
};
export interface DAgent {
  agent: string; checks: number; tasks: number; last: number; allowed: number; stopped: number; asked: number;
  sandbox: boolean; kind: string; business: string;
}
export interface DBusiness {
  workspace: string; org_domains: string[]; currency?: string;
  tools: Record<string, { action: string; description?: string }>;
  contacts: { id: string; name: string; address: string; aliases?: string[] }[];
  resources: { id: string; name: string; data_class?: string }[];
  records: { type: string; data_class?: string }[];
  ask_before: { action: string; when: string }[];
}

/** Reviewer (admin-key) actions. The key is the reviewer's own, typed at sign-in; it is never part of the build. */
export const admin = {
  resolve: (key: string, eventId: string, approve: boolean, note = "") =>
    call<{ status: string }>(`/v1/approvals/${encodeURIComponent(eventId)}`, { method: "POST", body: JSON.stringify({ approve, note }) }, key),
  feedback: (key: string, verdict: "attack" | "false_positive", eventId: string, labels: string[] = [], note = "") =>
    call<{ memory_id: string }>("/v1/feedback", { method: "POST", body: JSON.stringify({ verdict, event_id: eventId, labels, note }) }, key),
  forget: (key: string, memoryId: string) =>
    call<{ removed: boolean }>(`/v1/memory/${encodeURIComponent(memoryId)}`, { method: "DELETE" }, key),
  createAgent: (key: string, body: NewAgent) =>
    call<{ agent: string; mode: string; version: string; tools: string[] }>("/v1/agents", { method: "POST", body: JSON.stringify(body) }, key),
  selfTest: (key: string, agent: string) =>
    call<SelfTest>(`/v1/agents/${encodeURIComponent(agent)}/self-test`, { method: "POST" }, key),
  resolveSuggestion: (key: string, id: string, apply: boolean) =>
    call<{ status: string; memory_id?: string }>(`/v1/suggestions/${encodeURIComponent(id)}`, { method: "POST", body: JSON.stringify({ apply }) }, key),
  putPolicy: (key: string, agent: string, body: unknown) =>
    call<{ version: string; mode: string }>(`/v1/policies/${encodeURIComponent(agent)}`, { method: "PUT", body: JSON.stringify(body) }, key),
};
