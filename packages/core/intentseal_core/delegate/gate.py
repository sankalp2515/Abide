"""⑦ Action gate: how a confirmed contract rules on one tool call, argument by argument. Pure and deterministic.

Rules (docs/DESIGN_NOTES.md):
  - The employee's latest word on an action wins: "don't send it" then "now send it to Priya" permits the send.
  - A prohibition applies to the whole action, or only to the values it names.
  - A permit that names recipients covers exactly those recipients. A governed action (send, share, ...) with no
    named recipient may reach only addresses inside the organisation.
  - Resources: anything named anywhere in the request may be used. A confidential or unknown resource that was never
    named is out of scope for every action, including reading it.
  - A governed action with no permit is out of scope. Drafting and reading need no permit (they stay inside).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .model import GOVERNED, Clause, DelegatePolicy, DelegationContract, ToolCall, call_values


@dataclass(frozen=True)
class Ruling:
    effect: str                                   # permit | prohibit | out-of-scope | unknown-tool
    action: str | None
    clause: Clause | None
    offending: dict[str, list[str]] = field(default_factory=dict)
    reason: str = ""
    values: dict[str, list[str]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"effect": self.effect, "action": self.action, "clause": self.clause.as_dict() if self.clause else None,
                "offending": self.offending, "reason": self.reason, "values": self.values}


def covers(clause_action: str, action: str) -> bool:
    return clause_action == action or {clause_action, action} <= {"draft", "write"}


def rule(contract: DelegationContract, call: ToolCall, policy: DelegatePolicy) -> Ruling:
    spec = policy.tools.get(call.tool)
    if spec is None:
        return Ruling("unknown-tool", None, None, {}, f"'{call.tool}' is not a tool this workspace allows")
    act = spec.action
    if act in GOVERNED:
        # A field the policy does not describe ("bcc" on send_email) could carry a recipient or data the gate cannot
        # see. For actions that leave the workspace, undescribed fields are refused (D-D8).
        extra = sorted(set(call.args) - spec.declared())
        if extra:
            return Ruling("unknown-field", act, None, {"field": extra},
                          f"The call has fields this workspace does not allow for '{call.tool}': {', '.join(extra)}")
    vals = call_values(spec, call.args, policy)

    # The deciding word: the latest applicable clause for this action.
    deciding: Clause | None = None
    since = 0
    for i, c in enumerate(contract.clauses):
        if not covers(c.action, act):
            continue
        if c.effect == "prohibit":
            if not c.values or any(set(c.values.get(f, ())) & set(vals.get(f, [])) for f in c.values):
                deciding, since = c, i + 1
        else:
            deciding = c
    if deciding is not None and deciding.effect == "prohibit":
        return Ruling("prohibit", act, deciding, {}, f'You said: "{deciding.quote}"', vals)

    permits = [c for c in contract.clauses[since:] if c.effect == "permit" and covers(c.action, act)]
    if not permits and act in GOVERNED:
        return Ruling("out-of-scope", act, None, {k: v for k, v in vals.items() if v},
                      f"You did not ask to {act} anything", vals)

    offending: dict[str, list[str]] = {}
    named_recipients = {r for c in permits for r in c.values.get("recipient", ())}
    for r in vals["recipient"]:
        if r in named_recipients:
            continue
        if named_recipients or (act in GOVERNED and not policy.is_internal(r)) or r.startswith("unknown:"):
            offending.setdefault("recipient", []).append(r)

    any_permit = [c for c in contract.clauses if c.effect == "permit"]
    named_resources = {x for c in any_permit for x in c.values.get("resource", ())}

    for res in vals["resource"]:
        if res in named_resources:
            continue
        # Anything that leaves or cannot be undone (send, pay, delete, ...) may use only what the employee named.
        if res.startswith("unknown:") or policy.is_confidential(res) or act in GOVERNED:
            offending.setdefault("resource", []).append(res)

    limits = [float(c.values["amount"][0]) for c in permits if c.values.get("amount")]
    for amt in vals.get("amount", []):
        if amt.startswith("unknown:") or (limits and float(amt) > min(limits)):
            offending.setdefault("amount", []).append(amt)

    clause = permits[-1] if permits else None
    if offending:
        parts = []
        if offending.get("recipient"):
            who = ", ".join(policy.label("recipient", r) for r in offending["recipient"])
            parts.append(f"{who} is not someone you named")
        if offending.get("resource"):
            what = ", ".join(policy.label("resource", r) for r in offending["resource"])
            parts.append(f"your request did not mention {what}")
        if offending.get("amount"):
            parts.append(f"{', '.join(policy.label('money', a) for a in offending['amount'])} is more than the "
                         f"{policy.label('money', str(min(limits)))} you allowed" if limits
                         else "the amount is not a number")
        reason = "; ".join(parts)
        return Ruling("out-of-scope", act, clause, offending, reason[0].upper() + reason[1:], vals)
    reason = f'Within the request: "{clause.quote}"' if clause else "Within the task (stays inside the workspace)"
    return Ruling("permit", act, clause, {}, reason, vals)
