"""CORE-10 Decision logger: every decision, with evidence, in the event store.

Privacy: content is stored as a hash plus a short redacted preview (or hash only when
INTENTSEAL_STORE_PREVIEWS=0, e.g. public demo); user references are hashed; secrets, canaries and keys are
redacted before anything is written.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from typing import Any, Callable, Iterable

from . import siem
from .secrets import redact
from .store import Store
from .types import ActionResult, InspectResult

_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY, agent_id TEXT, user_ref TEXT, task_preview TEXT, started_at REAL, risk REAL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS events (
        id TEXT PRIMARY KEY, session_id TEXT, agent_id TEXT, seq INTEGER, kind TEXT, source TEXT,
        content_hash TEXT, preview TEXT, created_at REAL)""",
    "CREATE INDEX IF NOT EXISTS ix_events_session ON events(session_id, seq)",
    """CREATE TABLE IF NOT EXISTS findings (
        id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT, span_start INTEGER, span_end INTEGER,
        span_preview TEXT, origin TEXT, signal TEXT, labels TEXT, score REAL, tier INTEGER)""",
    """CREATE TABLE IF NOT EXISTS decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT, agent_id TEXT, session_id TEXT, decision TEXT,
        enforced TEXT, policy_rule TEXT, reason TEXT, labels TEXT, risk REAL, session_risk REAL, tier INTEGER,
        latency_ms REAL, degraded INTEGER, budget_degraded INTEGER, model TEXT, input_tokens INTEGER,
        est_cost REAL, created_at REAL, output_tokens INTEGER DEFAULT 0)""",
    "CREATE INDEX IF NOT EXISTS ix_decisions_time ON decisions(created_at)",
    """CREATE TABLE IF NOT EXISTS actions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT, tool TEXT, args_redacted TEXT,
        final_args_redacted TEXT, pinned_overrides TEXT)""",
    # CON-9: ESCALATE decisions wait here for a human. An approval is one-shot and only for the same
    # session + tool + arguments (args_hash); it can resolve an ESCALATE, never a BLOCK.
    """CREATE TABLE IF NOT EXISTS approvals (
        event_id TEXT PRIMARY KEY, session_id TEXT, agent_id TEXT, tool TEXT, args_hash TEXT, reason TEXT,
        status TEXT DEFAULT 'pending', reviewer TEXT, note TEXT, created_at REAL, resolved_at REAL, used_at REAL)""",
    "CREATE INDEX IF NOT EXISTS ix_approvals_key ON approvals(session_id, tool, args_hash, status)",
    # D-064: every version of a session's intent contract (immutable; each names its parent's hash)
    """CREATE TABLE IF NOT EXISTS contracts (
        session_id TEXT, version INTEGER, hash TEXT, parent TEXT, origin TEXT, body TEXT, created_at REAL,
        PRIMARY KEY (session_id, version))""",
    # Execution receipts: the application reports whether an allowed call actually ran (provenance evidence)
    """CREATE TABLE IF NOT EXISTS receipts (
        event_id TEXT PRIMARY KEY, executed INTEGER, detail TEXT, created_at REAL)""",
]


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()[:24]


def args_hash(tool: str, args: dict[str, Any]) -> str:
    return _hash(tool + "\n" + json.dumps(args or {}, sort_keys=True, default=str))


class DecisionLogger:
    def __init__(self, store: Store, secrets_to_redact: Iterable[str] = ()) -> None:
        self.store = store
        self.store.ensure_schema(_SCHEMA)
        for table, column in (("decisions", "output_tokens INTEGER DEFAULT 0"),   # G7b
                              ("actions", "taint TEXT"),                           # CORE-17
                              ("actions", "contract TEXT")):                       # D-064
            try:   # databases created before the column existed
                self.store.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
            except Exception:
                pass   # column already present
        self.extra_secrets = [s for s in secrets_to_redact if s]
        self.previews = os.environ.get("INTENTSEAL_STORE_PREVIEWS", "1") != "0"
        self._seq: dict[str, int] = {}
        self.org_of: Callable[[str], str | None] | None = None   # set by the engine: agent -> organisation (SIEM)

    def add_secrets(self, values: Iterable[str]) -> None:
        self.extra_secrets += [v for v in values if v]

    def _redact(self, text: str) -> str:
        t = redact(text or "")
        for s in sorted(self.extra_secrets, key=len, reverse=True):
            t = re.sub(re.escape(s), "[REDACTED]", t, flags=re.I)
        return t

    def scrub(self, text: str, limit: int = 160) -> str:
        if not self.previews:
            return ""
        t = self._redact(text)
        t = re.sub(r"\s+", " ", t).strip()
        return t[:limit] + ("…" if len(t) > limit else "")

    def scrub_obj(self, obj: Any) -> str:
        return self.scrub(json.dumps(obj, ensure_ascii=False, default=str), limit=600)

    # ---- writers ---------------------------------------------------------------------------
    def session(self, session_id: str, agent_id: str, user_ref: str | None, task: str) -> None:
        self.store.execute("INSERT INTO sessions (id, agent_id, user_ref, task_preview, started_at) VALUES (?,?,?,?,?) "
                           "ON CONFLICT (id) DO NOTHING",
                           (session_id, self._redact(agent_id), user_ref, self.scrub(task), time.time()))

    def event(self, session_id: str, agent_id: str, kind: str, source: str, content: str = "") -> str:
        eid = uuid.uuid4().hex[:20]
        if session_id not in self._seq:   # continue a session logged before a restart (or evicted below)
            row = self.store.query("SELECT COALESCE(MAX(seq), 0) AS n FROM events WHERE session_id = ?", (session_id,))
            self._seq[session_id] = int(row[0]["n"])
        seq = self._seq.pop(session_id) + 1
        self._seq[session_id] = seq       # most recent last
        while len(self._seq) > 20000:
            del self._seq[next(iter(self._seq))]
        self.store.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)",
                           (eid, session_id, self._redact(agent_id), seq, kind, self._redact(source), _hash(content) if content else None,
                            self.scrub(content), time.time()))
        return eid

    def inspection(self, session_id: str, agent_id: str, source: str, content: str, r: InspectResult,
                   model: str | None = None, input_tokens: int = 0, est_cost: float = 0.0, output_tokens: int = 0) -> str:
        eid = self.event(session_id, agent_id, "content_in", source, content)
        for f in r.findings:
            self.store.execute(
                "INSERT INTO findings (event_id, span_start, span_end, span_preview, origin, signal, labels, score, tier) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (eid, f.span[0] if f.span else None, f.span[1] if f.span else None, self.scrub(f.preview, 120), self._redact(f.origin),
                 self._redact(f.signal), json.dumps(f.labels), f.score, f.tier))
        self._decision(eid, agent_id, session_id, r.decision.value, r.enforced.value, r.policy_rule, r.reason, r.labels,
                       r.risk, r.session_risk, r.tier, r.latency_ms, r.degraded, r.budget_degraded, model, input_tokens, est_cost,
                       output_tokens, kind="content_in", source=source)
        self.store.execute("UPDATE sessions SET risk = ? WHERE id = ?", (r.session_risk, session_id))
        return eid

    def action(self, session_id: str, agent_id: str, tool: str, args: dict[str, Any], r: ActionResult,
               session_risk: float) -> str:
        eid = self.event(session_id, agent_id, "action", f"tool:{tool}")
        self.store.execute("INSERT INTO actions (event_id, tool, args_redacted, final_args_redacted, pinned_overrides, taint, "
                           "contract) VALUES (?,?,?,?,?,?,?)",
                           (eid, self._redact(tool), self.scrub_obj(args), self.scrub_obj(r.final_args) if r.final_args else None,
                            self.scrub_obj(r.pinned_overrides) if r.pinned_overrides else None,
                            json.dumps(r.taint) if r.taint else None,
                            self._redact(json.dumps(r.contract, ensure_ascii=False)) if r.contract else None))
        self._decision(eid, agent_id, session_id, r.decision.value, r.enforced.value, r.policy_rule, r.reason, r.labels,
                       0.0, session_risk, 0, r.latency_ms, r.degraded, False, None, 0, 0.0, kind="action", source=f"tool:{tool}")
        if r.decision.value == "ESCALATE":
            self.store.execute("INSERT INTO approvals (event_id, session_id, agent_id, tool, args_hash, reason, created_at) "
                               "VALUES (?,?,?,?,?,?,?)", (eid, session_id, self._redact(agent_id), self._redact(tool),
                                                          args_hash(tool, args), self.scrub(r.reason, 400), time.time()))
        return eid

    # ---- approvals (CON-9) -------------------------------------------------------------------
    def approvals(self, status: str | None = "pending", agent: str | None = None, limit: int = 100,
                  agents: Iterable[str] | None = None) -> list[dict[str, Any]]:
        q = ("SELECT a.event_id, a.session_id, a.agent_id, a.tool, a.reason, a.status, a.reviewer, a.note, a.created_at, "
             "a.resolved_at, a.used_at, x.args_redacted, d.policy_rule, d.labels FROM approvals a "
             "LEFT JOIN actions x ON x.event_id = a.event_id LEFT JOIN decisions d ON d.event_id = a.event_id WHERE 1=1")
        p: list[Any] = []
        if status:
            q += " AND a.status = ?"; p.append(status)
        if agent:
            q += " AND a.agent_id = ?"; p.append(agent)
        if agents is not None:   # tenancy: only these agents (API-12)
            allowed = sorted(agents) or [None]
            q += f" AND a.agent_id IN ({', '.join('?' for _ in allowed)})"; p.extend(allowed)
        q += " ORDER BY a.created_at DESC LIMIT ?"; p.append(limit)
        rows = self.store.query(q, tuple(p))
        for r in rows:
            r["labels"] = json.loads(r["labels"] or "[]")
        return rows

    def resolve_approval(self, event_id: str, approve: bool, reviewer: str, note: str = "") -> bool:
        cur = self.store.execute("UPDATE approvals SET status = ?, reviewer = ?, note = ?, resolved_at = ? "
                                 "WHERE event_id = ? AND status = 'pending'",
                                 ("approved" if approve else "denied", self._redact(reviewer), self.scrub(note, 400),
                                  time.time(), event_id))
        return bool(cur.rowcount)

    def take_approval(self, session_id: str, tool: str, args: dict[str, Any]) -> str | None:
        """Consume one approved, unused approval for exactly this call; returns its event id."""
        rows = self.store.query("SELECT event_id FROM approvals WHERE session_id = ? AND tool = ? AND args_hash = ? "
                                "AND status = 'approved' AND used_at IS NULL ORDER BY resolved_at LIMIT 1",
                                (session_id, tool, args_hash(tool, args)))
        if not rows:
            return None
        self.store.execute("UPDATE approvals SET used_at = ? WHERE event_id = ?", (time.time(), rows[0]["event_id"]))
        return rows[0]["event_id"]

    def _decision(self, eid, agent_id, session_id, decision, enforced, rule, reason, labels, risk, session_risk, tier,
                  latency_ms, degraded, budget_degraded, model, input_tokens, est_cost, output_tokens=0,
                  kind: str = "", source: str = "") -> None:
        self.store.execute(
            "INSERT INTO decisions (event_id, agent_id, session_id, decision, enforced, policy_rule, reason, labels, risk, "
            "session_risk, tier, latency_ms, degraded, budget_degraded, model, input_tokens, est_cost, created_at, output_tokens) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (eid, self._redact(agent_id), session_id, decision, enforced, self._redact(rule), self.scrub(reason, 400), json.dumps(labels), risk,
             session_risk, tier, latency_ms, int(degraded), int(budget_degraded), self._redact(model) if model else None,
             input_tokens, est_cost, time.time(), output_tokens))
        fw = siem.forwarder()
        if fw is not None:   # API-12 push export (asynchronous, bounded; never blocks or fails a decision)
            try:
                org = self.org_of(agent_id) if self.org_of else None
                fw.submit(siem.record({
                    "event_id": eid, "agent_id": agent_id, "session_id": session_id, "decision": decision,
                    "enforced": enforced, "policy_rule": rule, "reason": self.scrub(reason, 400), "labels": labels,
                    "risk": risk, "session_risk": session_risk, "tier": tier, "latency_ms": latency_ms,
                    "degraded": degraded, "budget_degraded": budget_degraded, "model": model, "input_tokens": input_tokens,
                    "output_tokens": output_tokens, "kind": kind, "source": self._redact(source), "created_at": time.time()}, org))
            except Exception:
                pass

    def contract(self, c: Any) -> None:
        """Store one contract version (D-064). The user's words in clause quotes are redacted like any preview."""
        body = self._redact(json.dumps(c.as_dict(), ensure_ascii=False))
        self.store.execute("INSERT INTO contracts (session_id, version, hash, parent, origin, body, created_at) "
                           "VALUES (?,?,?,?,?,?,?) ON CONFLICT (session_id, version) DO NOTHING",
                           (c.session_id, c.version, c.hash, c.parent, self._redact(c.origin), body, c.created_at))

    def receipt(self, event_id: str, executed: bool, detail: str = "") -> bool:
        """The application's record that an action did or did not run. Only for action events; first report wins."""
        if not self.store.query("SELECT 1 AS x FROM actions WHERE event_id = ?", (event_id,)):
            return False
        self.store.execute("INSERT INTO receipts (event_id, executed, detail, created_at) VALUES (?,?,?,?) "
                           "ON CONFLICT (event_id) DO NOTHING", (event_id, int(bool(executed)), self.scrub(detail, 200), time.time()))
        return True

    def contracts(self, session_id: str) -> list[dict[str, Any]]:
        return [json.loads(r["body"]) for r in
                self.store.query("SELECT body FROM contracts WHERE session_id = ? ORDER BY version", (session_id,))]

    # ---- readers (API / Console) ------------------------------------------------------------
    def session_events(self, session_id: str) -> list[dict[str, Any]]:
        rows = self.store.query(
            "SELECT e.*, d.decision, d.enforced, d.policy_rule, d.reason, d.labels, d.tier, d.latency_ms, d.degraded, "
            "d.session_risk FROM events e LEFT JOIN decisions d ON d.event_id = e.id WHERE e.session_id = ? ORDER BY e.seq",
            (session_id,))
        for r in rows:
            r["labels"] = json.loads(r["labels"]) if r.get("labels") else []
            r["findings"] = [dict(f, labels=json.loads(f["labels"] or "[]"))
                             for f in self.store.query("SELECT * FROM findings WHERE event_id = ?", (r["id"],))]
            act = self.store.query("SELECT tool, args_redacted, final_args_redacted, pinned_overrides, taint, contract "
                                   "FROM actions WHERE event_id = ?", (r["id"],))
            if act:
                act[0]["taint"] = json.loads(act[0]["taint"]) if act[0]["taint"] else []
                act[0]["contract"] = json.loads(act[0]["contract"]) if act[0].get("contract") else None
                rec = self.store.query("SELECT executed, detail, created_at FROM receipts WHERE event_id = ?", (r["id"],))
                act[0]["receipt"] = ({"executed": bool(rec[0]["executed"]), "detail": rec[0]["detail"],
                                      "at": rec[0]["created_at"]} if rec else None)
            r["action"] = act[0] if act else None
        return rows

    def decisions(self, decision: str | None = None, label: str | None = None, agent: str | None = None,
                  since: float | None = None, limit: int = 200, before: float | None = None,
                  agents: Iterable[str] | None = None, until: float | None = None,
                  ascending: bool = False) -> list[dict[str, Any]]:
        q, p = "SELECT d.*, e.source, e.kind, e.preview FROM decisions d JOIN events e ON e.id = d.event_id WHERE 1=1", []
        if decision:
            q += " AND d.decision = ?"; p.append(decision)
        if agent:
            q += " AND d.agent_id = ?"; p.append(agent)
        if since:
            q += " AND d.created_at >= ?"; p.append(since)
        if before:   # keyset pagination: pass the oldest created_at of the previous page
            q += " AND d.created_at < ?"; p.append(before)
        if until:
            q += " AND d.created_at <= ?"; p.append(until)
        if label:
            q += " AND d.labels LIKE ?"; p.append(f'%"{label}"%')
        if agents is not None:   # tenancy: only these agents (API-12)
            allowed = sorted(agents) or [None]
            q += f" AND d.agent_id IN ({', '.join('?' for _ in allowed)})"; p.extend(allowed)
        q += f" ORDER BY d.created_at {'ASC' if ascending else 'DESC'} LIMIT ?"; p.append(limit)
        rows = self.store.query(q, tuple(p))
        for r in rows:
            r["labels"] = json.loads(r["labels"] or "[]")
        return rows
