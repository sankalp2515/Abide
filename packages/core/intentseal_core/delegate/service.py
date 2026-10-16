"""① Delegation intake and the decisions that tie the steps together (⑤ inspection, ⑥ handoffs, ⑦ gate, ⑧ approvals,
⑨ receipts). Everything is recorded in the evidence store, append-only.

Decisions for a tool call (docs/DESIGN_NOTES.md D-D2, D-D3):
  prohibit                      -> block. The employee said not to; the agent is told and continues.
  out of scope, traced to content an agent read
                                -> block, without asking anyone. The agent is told and continues.
  out of scope otherwise        -> ask the employee once, bound to this exact action.
  permit, but an "ask before" rule applies (e.g. email leaving the company)
                                -> ask once, bound to this exact action; an approval is used once.
  permit                        -> allow.
"""

from __future__ import annotations

import json
import logging
import os
import secrets as pysecrets
import time
import uuid
from typing import Any

from collections import OrderedDict

from ..extract import extract
from ..store import Store
from ..taint import TaintTracker
from . import handoff
from .compiler import Drafter, compile_request, split_material
from .gate import Ruling, rule
from .model import _NUMBER as _NUM
from .model import GOVERNED, DelegatePolicy, DelegationContract, ToolCall, norm, to_number
from .receipts import Writer, chain_hash, write_report

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS delegate_workflows (id TEXT PRIMARY KEY, owner TEXT, contract_id TEXT,
       session_id TEXT, workspace TEXT, created REAL)""",
    """CREATE TABLE IF NOT EXISTS delegate_contracts (id TEXT, version INTEGER, workflow_id TEXT, hash TEXT,
       parent TEXT, status TEXT, body TEXT, created REAL, PRIMARY KEY (id, version))""",
    """CREATE TABLE IF NOT EXISTS delegate_receipts (workflow_id TEXT, seq INTEGER, rid TEXT, kind TEXT, agent TEXT,
       tool TEXT, decision TEXT, reason TEXT, data TEXT, created REAL, prev TEXT, hash TEXT,
       PRIMARY KEY (workflow_id, seq))""",
    """CREATE TABLE IF NOT EXISTS delegate_approvals (id TEXT PRIMARY KEY, workflow_id TEXT, action_hash TEXT,
       contract_hash TEXT, tool TEXT, args TEXT, agent TEXT, reason TEXT, status TEXT, created REAL,
       resolved_by TEXT, resolved_at REAL, receipt TEXT)""",
    """CREATE TABLE IF NOT EXISTS delegate_content (workflow_id TEXT, receipt TEXT, source TEXT, trusted INTEGER,
       flagged INTEGER, labels TEXT, text TEXT)""",
    # Who delegated the task: only they may confirm, change or approve it (D-D9).
    """CREATE TABLE IF NOT EXISTS delegate_principals (workflow_id TEXT PRIMARY KEY, principal TEXT)""",
    # Text agents read from confidential files: it may not leave the company unless the employee said so (D-D10).
    """CREATE TABLE IF NOT EXISTS delegate_confidential (workflow_id TEXT, receipt TEXT, resource TEXT, text TEXT)""",
]

audit = logging.getLogger("intentseal.delegate.audit")

NOTICE_BLOCK = ("Not done: {reason}. This step is outside what the employee asked for, so it was stopped. Do not "
                "try it another way. Continue with the rest of the task.")
NOTICE_ASK = ("Waiting: the employee must confirm this exact action ({approval}). Continue with other work; do not "
              "change the action, or a new confirmation is needed.")


class DelegateError(ValueError):
    pass


class DelegateForbidden(DelegateError):
    """The caller is not the person who delegated the task (HTTP 403)."""


def _notes(r: Any) -> tuple[str, ...]:
    notes = []
    if r.material:
        notes.append("The text you quoted or pasted was treated as information for your assistants, not as "
                     "instructions: nothing in it can give them permission.")
    if not r.clauses:
        notes.append("I could not turn your request into permissions" + (
            " (the AI drafter is unavailable right now)" if not str(r.compiler).startswith("model") else "")
            + ". Try again, or rephrase it in plain English.")
    return tuple(notes)


def for_agent(reason: str) -> str:
    """Reasons are written to the employee ("you said ..."); the agent is told about the employee."""
    for a, b in (("You said", "The employee said"), ("You did not", "The employee did not"),
                 ("someone you named", "someone the employee named"), ("your request", "the employee's request"),
                 ("Your request", "The employee's request"), ("you allowed", "the employee allowed")):
        reason = reason.replace(a, b)
    return reason


class DelegateService:
    def __init__(self, policy: DelegatePolicy, engine: Any = None, store: Store | None = None,
                 drafter: Drafter | None = None, writer: Writer | None = None, secret: bytes | None = None,
                 inspect_content: bool = True) -> None:
        self.policy, self.engine = policy, engine
        self.store = store or (engine.store if engine is not None else Store(":memory:"))
        self.store.ensure_schema(SCHEMA)
        self.drafter, self.writer = drafter, writer
        env_secret = os.environ.get("INTENTSEAL_DELEGATE_SECRET", "").encode()
        if secret is None and not env_secret:
            audit.warning("INTENTSEAL_DELEGATE_SECRET is not set: handoff seals use a per-process key and stop "
                          "verifying after a restart or on another instance")
        self.secret = secret or env_secret or pysecrets.token_bytes(32)
        self.inspect_content = inspect_content and engine is not None
        # In-memory tracing state, rebuilt from the store on demand: bounded, oldest workflow first out (G4).
        self.cache_size = int(os.environ.get("INTENTSEAL_DELEGATE_CACHE") or 256)
        self._taint: OrderedDict[str, TaintTracker] = OrderedDict()
        self._conf: OrderedDict[str, tuple[TaintTracker, dict[str, str]]] = OrderedDict()
        self.approval_ttl_s = float(os.environ.get("INTENTSEAL_APPROVAL_TTL_S") or 900)
        days = os.environ.get("INTENTSEAL_DELEGATE_RETENTION_DAYS")
        if days:
            self.purge(float(days))

    # ---- store helpers --------------------------------------------------------------------------------------------
    def _workflow(self, workflow_id: str) -> dict[str, Any]:
        rows = self.store.query("SELECT * FROM delegate_workflows WHERE id = ?", (workflow_id,))
        if not rows:
            raise DelegateError("unknown workflow")
        return rows[0]

    def contract(self, workflow_id: str) -> DelegationContract:
        wf = self._workflow(workflow_id)
        rows = self.store.query("SELECT body FROM delegate_contracts WHERE id = ? ORDER BY version DESC LIMIT 1",
                                (wf["contract_id"],))
        return DelegationContract.from_dict(json.loads(rows[0]["body"]))

    def versions(self, workflow_id: str) -> list[dict[str, Any]]:
        wf = self._workflow(workflow_id)
        rows = self.store.query("SELECT body FROM delegate_contracts WHERE id = ? ORDER BY version",
                                (wf["contract_id"],))
        return [DelegationContract.from_dict(json.loads(r["body"])).as_dict(self.policy) for r in rows]

    def _save_contract(self, c: DelegationContract) -> None:
        self.store.execute("INSERT INTO delegate_contracts VALUES (?,?,?,?,?,?,?,?)",
                           (c.id, c.version, c.workflow_id, c.hash, c.parent, c.status,
                            json.dumps(c.as_dict(self.policy), ensure_ascii=False), c.created_at))

    def receipts(self, workflow_id: str) -> list[dict[str, Any]]:
        rows = self.store.query("SELECT * FROM delegate_receipts WHERE workflow_id = ? ORDER BY seq", (workflow_id,))
        for r in rows:
            r["data"] = json.loads(r["data"] or "{}")
        return rows

    def _receipt(self, workflow_id: str, kind: str, *, agent: str = "", tool: str = "", decision: str = "",
                 reason: str = "", data: dict[str, Any] | None = None) -> str:
        for attempt in range(5):      # G5: two writers on one workflow: the primary key decides, the loser retries
            last = self.store.query("SELECT seq, hash FROM delegate_receipts WHERE workflow_id = ? ORDER BY seq DESC "
                                    "LIMIT 1", (workflow_id,))
            seq = (last[0]["seq"] + 1) if last else 1
            prev = last[0]["hash"] if last else ""
            row = {"workflow_id": workflow_id, "seq": seq, "rid": f"R-{seq}", "kind": kind, "agent": agent,
                   "tool": tool, "decision": decision, "reason": reason, "data": data or {},
                   "created": round(time.time(), 3)}
            row["prev"], row["hash"] = prev, chain_hash(prev, row)
            try:
                self.store.execute("INSERT INTO delegate_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                   (workflow_id, seq, row["rid"], kind, agent, tool, decision, reason,
                                    json.dumps(row["data"], ensure_ascii=False, default=str), row["created"], prev,
                                    row["hash"]))
            except Exception as e:  # noqa: BLE001 - sqlite3 / psycopg integrity errors differ
                if "unique" in str(e).lower() or "duplicate" in str(e).lower() or "primary" in str(e).lower():
                    continue
                raise
            # G7: one structured audit line per receipt; ids and decisions only, never content or arguments
            audit.info(json.dumps({"event": f"delegate.{kind}", "workflow": workflow_id, "receipt": row["rid"],
                                   "agent": agent, "tool": tool, "decision": decision,
                                   "ruling": (data or {}).get("ruling"), "traced_to": bool((data or {}).get("traced_to")),
                                   "approval": (data or {}).get("approval"), "hash": row["hash"]}))
            return row["rid"]
        raise DelegateError("could not record the receipt; try again")

    def _tracker(self, workflow_id: str) -> TaintTracker:
        t = self._taint.get(workflow_id)
        if t is not None:
            self._taint.move_to_end(workflow_id)
        if t is None:
            t = self._taint[workflow_id] = TaintTracker()
            while len(self._taint) > self.cache_size:
                self._taint.popitem(last=False)
            t.observe(split_material(self.contract(workflow_id).request)[0], "employee", "employee", True, False)
            for r in self.store.query("SELECT * FROM delegate_content WHERE workflow_id = ?", (workflow_id,)):
                t.observe(r["text"], r["receipt"], r["source"], bool(r["trusted"]), bool(r["flagged"]))
        return t

    def _confidential(self, workflow_id: str) -> tuple[TaintTracker, dict[str, str]]:
        item = self._conf.get(workflow_id)
        if item is not None:
            self._conf.move_to_end(workflow_id)
        if item is None:
            t, origin = TaintTracker(), {}
            for r in self.store.query("SELECT * FROM delegate_confidential WHERE workflow_id = ?", (workflow_id,)):
                t.observe(r["text"], r["receipt"], r["resource"], False, False)
                origin[r["receipt"]] = r["resource"]
            item = self._conf[workflow_id] = (t, origin)
            while len(self._conf) > self.cache_size:
                self._conf.popitem(last=False)
        return item

    def purge(self, older_than_days: float) -> int:
        """G8 retention: delete the raw content kept for tracing (third-party text, confidential file text) of workflows
        older than `older_than_days`. Receipts stay: they are the evidence and hold only short summaries."""
        cutoff = time.time() - older_than_days * 86400
        old = [r["id"] for r in self.store.query("SELECT id FROM delegate_workflows WHERE created < ?", (cutoff,))]
        for wid in old:
            self.store.execute("DELETE FROM delegate_content WHERE workflow_id = ?", (wid,))
            self.store.execute("DELETE FROM delegate_confidential WHERE workflow_id = ?", (wid,))
            self._taint.pop(wid, None)
            self._conf.pop(wid, None)
        if old:
            audit.info(json.dumps({"event": "delegate.purge", "workflows": len(old), "older_than_days": older_than_days}))
        return len(old)

    def owner_of(self, workflow_id: str) -> str:
        rows = self.store.query("SELECT principal FROM delegate_principals WHERE workflow_id = ?", (workflow_id,))
        return rows[0]["principal"] if rows else self._workflow(workflow_id)["owner"]

    def _authorize(self, workflow_id: str, actor: str) -> None:
        """Only the person who delegated the task speaks for it. An agent runtime, even with a valid key, cannot
        confirm, widen or approve its own work."""
        if actor != self.owner_of(workflow_id):
            raise DelegateForbidden("only the person who delegated this task can confirm, change or approve it")

    async def _record_material(self, workflow_id: str, pieces: list[str]) -> None:
        """Text the employee pasted into their request: inspected and tracked like any third-party content."""
        for piece in pieces:
            await self.read(workflow_id, "employee", piece, "pasted by the employee")

    # ---- ① intake ---------------------------------------------------------------------------------------------------
    async def start(self, owner: str, request: str, principal: str | None = None) -> DelegationContract:
        if not request.strip():
            raise DelegateError("the request is empty")
        wid, cid = "wf-" + uuid.uuid4().hex[:10], "ct-" + uuid.uuid4().hex[:10]
        session_id = None
        if self.engine is not None:
            session_id = self.engine.start_session(self.policy.agent_id, user_ref=owner, task=request).session_id
        self.store.execute("INSERT INTO delegate_workflows VALUES (?,?,?,?,?,?)",
                           (wid, owner, cid, session_id, self.policy.workspace, time.time()))
        self.store.execute("INSERT INTO delegate_principals VALUES (?,?)", (wid, principal or owner))
        r = await compile_request(request, self.policy, self.drafter)
        c = DelegationContract(cid, wid, owner, request, 1, tuple(r.clauses), "", "draft", r.compiler,
                               tuple(r.questions), tuple(r.rejected), _notes(r))
        self._save_contract(c)
        self._receipt(wid, "contract", agent="employee", decision="draft", reason="contract drafted from the request",
                      data={"version": 1, "hash": c.hash, "status": "draft", "compiler": r.compiler,
                            "clauses": [x.as_dict() for x in c.clauses], "rejected": list(r.rejected),
                            "pasted_material": len(r.material)})
        await self._record_material(wid, r.material)
        return c

    def confirm(self, workflow_id: str, contract_hash: str | None = None, *, actor: str,
                accept: tuple[str, ...] | list[str] = ()) -> DelegationContract:
        """The employee confirms the card they saw. `contract_hash` must match it, so they confirm what they read.
        `accept`: ids of unverified clauses that need a tick (D-D12); unticked ones are dropped."""
        self._authorize(workflow_id, actor)
        cur = self.contract(workflow_id)
        if contract_hash and contract_hash != cur.hash:
            raise DelegateError("the contract changed since it was shown; review it again")
        if cur.status == "confirmed":
            return cur
        if not cur.clauses:
            raise DelegateError("there is nothing to confirm: your request did not give your assistants anything to "
                                "do. Rephrase it or try again")
        from dataclasses import replace as _replace
        clauses = []
        for cl in cur.clauses:
            if cl.verified or cl.effect == "prohibit" or cl.action not in GOVERNED:
                clauses.append(cl)
            elif cl.id in accept:
                clauses.append(_replace(cl, verified=True, note="you ticked this on the card"))
        c = DelegationContract(cur.id, cur.workflow_id, cur.owner, cur.request, cur.version + 1, tuple(clauses),
                               cur.hash, "confirmed", cur.compiler, cur.questions, cur.rejected)
        self._save_contract(c)
        self._receipt(workflow_id, "contract", agent="employee", decision="confirmed",
                      reason="the employee confirmed the contract",
                      data={"version": c.version, "hash": c.hash, "status": "confirmed",
                            "clauses": [x.as_dict() for x in c.clauses]})
        return c

    async def amend(self, workflow_id: str, text: str, *, actor: str) -> DelegationContract:
        """The employee's own next words ("looks good, send it"). Only the employee may call this (`actor`)."""
        self._authorize(workflow_id, actor)
        cur = self.contract(workflow_id)
        turn = sum(1 for v in self.versions(workflow_id) if v["status"] == "confirmed") + 1
        r = await compile_request(text, self.policy, self.drafter, cur, f"turn {turn}")
        c = DelegationContract(cur.id, cur.workflow_id, cur.owner, cur.request + "\n" + text, cur.version + 1,
                               cur.clauses + tuple(r.clauses), cur.hash, "confirmed", r.compiler, tuple(r.questions),
                               cur.rejected + tuple(r.rejected), cur.notes + _notes(r))
        self._save_contract(c)
        self._tracker(workflow_id).observe(split_material(text)[0], "employee", "employee", True, False)
        await self._record_material(workflow_id, r.material)
        self.store.execute("UPDATE delegate_approvals SET status = 'expired' WHERE workflow_id = ? AND status = "
                           "'pending'", (workflow_id,))
        self._receipt(workflow_id, "contract", agent="employee", decision="amended",
                      reason="the employee added to the request",
                      data={"version": c.version, "hash": c.hash, "status": "confirmed", "words": text,
                            "added": [x.as_dict() for x in r.clauses], "rejected": list(r.rejected)})
        return c

    # ---- ⑤ content firewall -----------------------------------------------------------------------------------------
    async def read(self, workflow_id: str, agent: str, content: str | bytes, source: str,
                   filename: str | None = None, inspect: bool | None = None,
                   resource: str | None = None, label: str | None = None) -> dict[str, Any]:
        """Inspect content an agent is about to read. Returns what the agent may see (cleaned) and records where it
        came from, so later tool calls can be traced back to it. `inspect=False` skips the content firewall for this
        read (demonstrations of the gate alone); provenance is recorded either way."""
        wf = self._workflow(workflow_id)
        raw = extract(content, filename=filename, source=source).text if isinstance(content, bytes) or filename \
            else str(content)
        cleaned, flagged, labels, decision = raw, False, [], "ALLOW"
        rule_, hidden_dropped = "", False
        if self.inspect_content and inspect is not False:
            res = await self.engine.inspect(content, source, self.policy.agent_id, session_id=wf["session_id"],
                                            filename=filename)
            cleaned, labels, decision, rule_ = res.cleaned_content, list(res.labels), res.decision.value, res.policy_rule
            flagged = res.decision.value != "ALLOW"
            if rule_ in ("judge-unavailable-hold", "budget-hold", "content-risk-block") and isinstance(content, bytes):
                # D-D5: a file held because of what is hidden in it (white or tiny text in a PDF or Word file, which
                # policy quarantines anyway): the visible text alone is inspected again. If that is clean, the agent
                # gets it and the hidden part stays out; if not, the whole file stays held. This keeps the legitimate
                # work going whether the hold came from the judge or from its absence.
                doc = extract(content, filename=filename, source=source)
                visible = "\n".join(s.text for s in doc.segments if s.visible and not s.origin.startswith(
                    (doc.format + ":metadata",)))
                if len(visible) < len(doc.text):
                    again = await self.engine.inspect(visible, source + ":visible", self.policy.agent_id,
                                                      session_id=wf["session_id"])
                    if again.decision.value == "ALLOW":
                        cleaned, decision, hidden_dropped = again.cleaned_content, "NEUTRALISE", True
                        rule_ = "drop-non-visible (visible text re-inspected)"
        label = label or (f"{source}: {filename}" if filename else source)
        rid = self._receipt(workflow_id, "inspect", agent=agent, decision=decision.lower(),
                            reason="content inspected before the agent read it",
                            data={"source": label, "filename": filename, "flagged": flagged, "labels": labels,
                                  "rule": rule_, "hidden_dropped": hidden_dropped, "chars": len(raw)})
        self.store.execute("INSERT INTO delegate_content VALUES (?,?,?,?,?,?,?)",
                           (workflow_id, rid, label, 0, int(flagged), json.dumps(labels), raw))
        self._tracker(workflow_id).observe(raw, rid, label, False, flagged)
        if resource and self.policy.is_confidential(resource):
            self.store.execute("INSERT INTO delegate_confidential VALUES (?,?,?,?)", (workflow_id, rid, resource, raw))
            t, origin = self._confidential(workflow_id)
            t.observe(raw, rid, resource, False, False)
            origin[rid] = resource
        return {"receipt": rid, "content": cleaned, "flagged": flagged, "labels": labels, "decision": decision}

    # ---- ⑥ handoff seal ---------------------------------------------------------------------------------------------
    def seal(self, workflow_id: str, from_agent: str, to_agent: str, content: str,
             sources: list[str]) -> dict[str, Any]:
        """Seal what `from_agent` passes on. `sources`: receipt ids of the content it was derived from."""
        c = self.contract(workflow_id)
        known = {r["receipt"]: r for r in self.store.query(
            "SELECT receipt, source, flagged, labels FROM delegate_content WHERE workflow_id = ?", (workflow_id,))}
        srcs = [{"receipt": s, "source": known[s]["source"], "trusted": False, "flagged": bool(known[s]["flagged"]),
                 "labels": json.loads(known[s]["labels"] or "[]")} for s in sources if s in known]
        env = handoff.seal(self.secret, workflow_id, c.hash, from_agent, to_agent, content, srcs)
        rid = self._receipt(workflow_id, "handoff", agent=from_agent, decision="sealed",
                            reason=f"handed to {to_agent}",
                            data={"to": to_agent, "untrusted": env["untrusted"], "sources": sources,
                                  "chars": len(content)})
        if env["untrusted"]:   # text derived from untrusted content stays traceable after the hop
            self.store.execute("INSERT INTO delegate_content VALUES (?,?,?,?,?,?,?)",
                               (workflow_id, rid, f"handoff from {from_agent}", 0,
                                int(any(s["flagged"] for s in srcs)), json.dumps(env["labels"]), content))
            self._tracker(workflow_id).observe(content, rid, f"handoff from {from_agent}", False,
                                               any(s["flagged"] for s in srcs))
        return env

    def open(self, env: dict[str, Any], workflow_id: str) -> dict[str, Any]:
        handoff.verify(self.secret, env)
        if env["workflow_id"] != workflow_id:
            raise handoff.SealError("the envelope belongs to another workflow")
        return env

    # ---- ⑦ gate + ⑧ approvals ---------------------------------------------------------------------------------------
    def _traced(self, workflow_id: str, call: ToolCall, ruling: Ruling) -> str | None:
        """Where an out-of-scope call came from, if it came from content an agent read: argument text copied from
        it, or an offending value (an address, a file) that appears in it but not in the employee's words."""
        hits = self._tracker(workflow_id).trace(call.args)
        if hits:
            return hits[0].source
        request = norm(split_material(self.contract(workflow_id).request)[0])
        rows = self.store.query("SELECT source, text FROM delegate_content WHERE workflow_id = ?", (workflow_id,))
        for amt in ruling.offending.get("amount", []):     # "refund 50,000": the number came from what they read
            for row in rows:
                numbers = {f"{n:g}" for n in (to_number(m.group()) for m in _NUM.finditer(row["text"])) if n is not None}
                if amt in numbers and amt not in request.replace(",", ""):
                    return row["source"]
        # By identity, not spelling: "order #9921" in a ticket and order_id "9921" in a call are the same record.
        asked = self.policy.mentions(split_material(self.contract(workflow_id).request)[0])
        for row in rows:
            seen = self.policy.mentions(row["text"])
            for fld in ("recipient", "resource"):
                named = {v for _, v in seen[fld]} - {v for _, v in asked[fld]}
                if any(v in named for v in ruling.offending.get(fld, [])):
                    return row["source"]
        for fld, values in ruling.offending.items():
            if fld == "amount":
                continue
            for v in values:
                needle = norm(v.removeprefix("unknown:"))
                candidates = {needle}
                r = self.policy.resource(needle)
                if r:
                    candidates |= {norm(r.name), *(norm(a) for a in r.aliases)}
                for row in rows:
                    text = norm(row["text"])
                    if any(c and c in text and c not in request for c in candidates):
                        return row["source"]
        return None

    def _confidential_leak(self, workflow_id: str, call: ToolCall, ruling: Ruling) -> str | None:
        """D-D10: text from a confidential file going to someone outside the company, when the employee did not name
        that file for this action."""
        external = [r for r in ruling.values.get("recipient", []) if not self.policy.is_internal(r)]
        if not external or ruling.action not in GOVERNED:
            return None
        spec = self.policy.tools[call.tool]
        body = {a: call.args.get(a) for a in spec.content_args if call.args.get(a)}
        t, origin = self._confidential(workflow_id)
        allowed = set(ruling.clause.values.get("resource", ())) if ruling.clause else set()
        for hit in t.trace(body, min_coverage=0.2):
            res = origin.get(hit.origin, hit.source)
            if res not in allowed:
                who = ", ".join(self.policy.label("recipient", r) for r in external)
                return (f"The message contains text from {self.policy.label('resource', res)}, a confidential file, "
                        f"and the employee did not ask to send it to {who}")
        return None

    def _ask_before(self, ruling: Ruling) -> bool:
        for rule_ in self.policy.ask_before:
            if rule_.action != ruling.action:
                continue
            if rule_.when == "always":
                return True
            if rule_.when == "external_recipient" and any(not self.policy.is_internal(r)
                                                         for r in ruling.values.get("recipient", [])):
                return True
        return False

    async def check(self, workflow_id: str, agent: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        c = self.contract(workflow_id)
        call = ToolCall(tool, args, agent)
        summary = {k: (v[:160] + "…" if isinstance(v, str) and len(v) > 160 else v) for k, v in args.items()}
        if c.status != "confirmed":
            rid = self._receipt(workflow_id, "check", agent=agent, tool=tool, decision="block",
                                reason="the employee has not confirmed the contract yet", data={"tool": tool,
                                                                                                "args": summary})
            return {"decision": "block", "receipt": rid, "reason": "not confirmed",
                    "notice": NOTICE_BLOCK.format(reason="the employee has not confirmed the task yet")}
        ruling = rule(c, call, self.policy)
        action_hash = call.action_hash(c.hash)
        data = {"tool": tool, "args": summary, "values": ruling.values, "offending": ruling.offending,
                "clause": ruling.clause.id if ruling.clause else None, "contract_version": c.version,
                "contract_hash": c.hash, "action_hash": action_hash, "ruling": ruling.effect}
        decision, reason, approval_id = "allow", ruling.reason, None
        if ruling.effect == "permit" and ruling.values.get("amount") and ruling.clause \
                and ruling.clause.values.get("amount"):
            # The limit is for the whole task: "refund up to 2,000" twice is 3,700 (D-D11).
            limit = float(ruling.clause.values["amount"][0])
            spent = sum(float(a) for r in self.receipts(workflow_id)
                        if r["kind"] == "check" and r["decision"] == "allow" and r["data"].get("ruling") == "permit"
                        and self.policy.tools.get(r["tool"]) and self.policy.tools[r["tool"]].action == ruling.action
                        for a in (r["data"].get("values") or {}).get("amount", []) if not str(a).startswith("unknown"))
            now = sum(float(a) for a in ruling.values["amount"] if not a.startswith("unknown"))
            if spent + now > limit:
                ruling = Ruling("out-of-scope", ruling.action, ruling.clause, {"amount": ruling.values["amount"]},
                                f"This would bring the total to {spent + now:g}, above the {limit:g} the employee "
                                f"allowed ({spent:g} already used)", ruling.values)
                data["ruling"], data["offending"] = ruling.effect, ruling.offending
                reason = ruling.reason
        leak = self._confidential_leak(workflow_id, call, ruling) if ruling.effect in ("permit", "out-of-scope") else None
        if ruling.effect in ("prohibit", "unknown-tool", "unknown-field"):
            decision = "block"
        elif leak:
            decision, reason, data["confidential"] = "block", leak, True
        elif ruling.effect == "out-of-scope":
            traced = self._traced(workflow_id, call, ruling)
            if traced:
                decision, data["traced_to"] = "block", traced
            else:
                decision = "ask"
        elif self._ask_before(ruling):
            decision = "ask"
            reason = "Company rule: confirm before this leaves the company"
        if decision == "ask":
            stale = time.time() - self.approval_ttl_s     # G3: a confirmation is for now, not for later
            self.store.execute("UPDATE delegate_approvals SET status = 'expired' WHERE workflow_id = ? AND status IN "
                               "('pending', 'approved') AND created < ?", (workflow_id, stale))
            ok = self.store.query("SELECT id FROM delegate_approvals WHERE workflow_id = ? AND action_hash = ? AND "
                                  "status = 'approved'", (workflow_id, action_hash))
            if ok:
                approval_id = ok[0]["id"]
                self.store.execute("UPDATE delegate_approvals SET status = 'used' WHERE id = ?", (approval_id,))
                decision, reason = "allow", f"Confirmed by the employee ({approval_id})"
            else:
                pending = self.store.query("SELECT id FROM delegate_approvals WHERE workflow_id = ? AND action_hash = ? "
                                           "AND status = 'pending'", (workflow_id, action_hash))
                if pending:
                    approval_id = pending[0]["id"]
                else:
                    approval_id = "A-" + uuid.uuid4().hex[:8]
                    self.store.execute("INSERT INTO delegate_approvals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                       (approval_id, workflow_id, action_hash, c.hash, tool,
                                        json.dumps(args, ensure_ascii=False, default=str), agent, reason, "pending",
                                        time.time(), None, None, None))
        data["approval"] = approval_id
        rid = self._receipt(workflow_id, "check", agent=agent, tool=tool, decision=decision, reason=reason, data=data)
        out = {"decision": decision, "receipt": rid, "reason": reason, "ruling": ruling.as_dict(),
               "action_hash": action_hash, "approval": approval_id, "traced_to": data.get("traced_to")}
        if decision == "block":
            out["notice"] = NOTICE_BLOCK.format(reason=for_agent(reason).rstrip("."))
        elif decision == "ask":
            out["notice"] = NOTICE_ASK.format(approval=approval_id)
        return out

    def approvals(self, workflow_id: str, status: str | None = None) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM delegate_approvals WHERE workflow_id = ?", [workflow_id]
        if status:
            sql, params = sql + " AND status = ?", params + [status]
        rows = self.store.query(sql + " ORDER BY created", params)
        for r in rows:
            r["args"] = json.loads(r["args"] or "{}")
        return rows

    def resolve(self, workflow_id: str, approval_id: str, approve: bool, by: str) -> dict[str, Any]:
        self._authorize(workflow_id, by)
        rows = self.store.query("SELECT * FROM delegate_approvals WHERE id = ? AND workflow_id = ?",
                                (approval_id, workflow_id))
        if not rows or rows[0]["status"] != "pending":
            raise DelegateError("no pending confirmation with this id")
        a = rows[0]
        if a["created"] < time.time() - self.approval_ttl_s:
            self.store.execute("UPDATE delegate_approvals SET status = 'expired' WHERE id = ?", (approval_id,))
            raise DelegateError("this question expired; the action must be proposed again")
        if a["contract_hash"] != self.contract(workflow_id).hash:
            self.store.execute("UPDATE delegate_approvals SET status = 'expired' WHERE id = ?", (approval_id,))
            raise DelegateError("the contract changed; the action must be proposed again")
        status = "approved" if approve else "denied"
        args = json.loads(a["args"] or "{}")
        spec = self.policy.tools.get(a["tool"])
        from .model import call_values
        values = call_values(spec, args, self.policy) if spec else {}
        rid = self._receipt(workflow_id, "approval", agent=by, tool=a["tool"], decision=status,
                            reason=f"the employee {status} this exact action",
                            data={"approval": approval_id, "status": status, "tool": a["tool"], "values": values,
                                  "action_hash": a["action_hash"]})
        self.store.execute("UPDATE delegate_approvals SET status = ?, resolved_by = ?, resolved_at = ?, receipt = ? "
                           "WHERE id = ?", (status, by, time.time(), rid, approval_id))
        return {"approval": approval_id, "status": status, "receipt": rid}

    def executed(self, workflow_id: str, check_receipt: str, executed: bool, detail: str = "") -> str:
        """The tool runner reports what actually ran (proof comes from the runner, not from the agent)."""
        rows = [r for r in self.receipts(workflow_id) if r["rid"] == check_receipt and r["kind"] == "check"]
        if not rows:
            raise DelegateError("unknown check receipt")
        chk = rows[0]
        if executed and chk["decision"] != "allow":
            detail = (detail + " " if detail else "") + "WARNING: ran although it was not allowed"
        return self._receipt(workflow_id, "executed", agent=chk["agent"], tool=chk["tool"],
                             decision="executed" if executed else "not-executed", reason=detail,
                             data={"check": check_receipt, "executed": executed, "tool": chk["tool"],
                                   "values": chk["data"].get("values"), "allowed": chk["decision"] == "allow"})

    # ---- ⑨ report ---------------------------------------------------------------------------------------------------
    async def report(self, workflow_id: str) -> dict[str, Any]:
        c = self.contract(workflow_id)
        rep = await write_report(c.request, self.receipts(workflow_id), self.writer,
                                 [r.id for r in self.policy.resources], self.policy.label,
                                 {n: t.description for n, t in self.policy.tools.items() if t.description})
        rep["workflow_id"], rep["contract_version"] = workflow_id, c.version
        return rep

    def state(self, workflow_id: str) -> dict[str, Any]:
        wf = self._workflow(workflow_id)
        return {"workflow": wf, "contract": self.contract(workflow_id).as_dict(self.policy), "versions": self.versions(workflow_id),
                "receipts": self.receipts(workflow_id), "approvals": self.approvals(workflow_id)}

    def workflows(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.store.query("SELECT * FROM delegate_workflows ORDER BY created DESC LIMIT ?", (limit,))
